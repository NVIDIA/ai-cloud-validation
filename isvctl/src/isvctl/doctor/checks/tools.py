# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""External-tool presence and version checks."""

import importlib.util
import json
import logging
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import yaml
from isvtest.core.k8s import get_kubectl_command

from isvctl.doctor.result import CategoryReport, CheckResult, Status

# Providers that, when selected, upgrade their tool from "recommended" to "required".
# Keys must be real provider directories under `isvctl/configs/providers/` so the
# tools and config categories agree on what a valid `--provider` is.
_PROVIDER_TOOLS: dict[str, frozenset[str]] = {
    "aws": frozenset({"terraform", "aws"}),
}


@dataclass(frozen=True)
class _Tool:
    """Describes how to look up and version-probe a single binary."""

    name: str
    version_args: tuple[str, ...] | None  # None → skip version probe
    required: bool  # True → missing is FAIL, False → missing is WARN


# Always-required toolchain. `pytest` is intentionally absent: isvtest runs it
# in-process via `pytest.main()`, so it must be importable, not on PATH (see
# `_check_pytest`).
_BASE_TOOLS: tuple[_Tool, ...] = (
    _Tool("python3", ("--version",), required=True),
    _Tool("uv", ("--version",), required=True),
)

# Recommended / provider-conditional toolchain — missing is WARN unless a
# selected provider escalates it. `ssh`/`scp` are only used by `deploy run`
# (remote transfer), so a local-only `test run` does not need them.
_OPTIONAL_TOOLS: tuple[_Tool, ...] = (
    _Tool("ssh", ("-V",), required=False),
    _Tool("scp", None, required=False),
    _Tool("terraform", ("-version",), required=False),
    _Tool("aws", ("--version",), required=False),
    _Tool("kubectl", ("version", "--client=true", "--output=yaml"), required=False),
)


def _probe_output(executable: str, args: tuple[str, ...]) -> str | None:
    """Run `<executable> <args>` with a short timeout and return combined output.

    Returns None on any failure — version probes are best-effort and must never
    raise out of this module.
    """
    try:
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Some tools (ssh, terraform) print to stderr.
    blob = (proc.stdout or "") + (proc.stderr or "")
    blob = blob.strip()
    return blob or None


def _probe_version(executable: str, args: tuple[str, ...]) -> str | None:
    """Run `<executable> <args>` with a short timeout and return the first line."""
    blob = _probe_output(executable, args)
    if not blob:
        return None
    return blob.splitlines()[0]


def _kubectl_client_version(output: str) -> str | None:
    """Extract clientVersion.gitVersion from kubectl JSON/YAML output."""
    for loader in (json.loads, yaml.safe_load):
        try:
            data = loader(output)
        except (json.JSONDecodeError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        client_version = data.get("clientVersion")
        if not isinstance(client_version, dict):
            continue
        git_version = client_version.get("gitVersion")
        if isinstance(git_version, str) and git_version.strip():
            return git_version.strip()
    return None


def _probe_kubectl_version(executable: str, args: tuple[str, ...]) -> str | None:
    """Run kubectl's version probe and return the actual client gitVersion."""
    blob = _probe_output(executable, args)
    if not blob:
        return None
    return _kubectl_client_version(blob) or blob.splitlines()[0]


def _kubectl_command() -> list[str]:
    """Resolve the kubectl invocation the suite would actually use.

    Reuses isvtest's resolver so doctor honors $KUBECTL *and* K8S_PROVIDER
    (microk8s/k3s/minikube) exactly like a real run — e.g. ["microk8s",
    "kubectl"]. Falls back to the configured tokens (or bare ["kubectl"]) when
    resolution raises, so the caller's `shutil.which` still reports it missing.

    The resolver logs its decision at INFO; we silence it here so the report
    (which may be machine-read JSON on stdout) is never polluted.
    """
    resolver_log = logging.getLogger("isvtest.core.k8s")
    previous_level = resolver_log.level
    resolver_log.setLevel(logging.WARNING)
    try:
        return get_kubectl_command()
    except (FileNotFoundError, ValueError):
        override = (os.environ.get("KUBECTL") or "").strip()
        if override:
            try:
                tokens = shlex.split(override, posix=True)
            except ValueError:
                tokens = []
            if tokens:
                return tokens
        return ["kubectl"]
    finally:
        resolver_log.setLevel(previous_level)


def _check_pytest() -> CheckResult:
    """Verify pytest is importable.

    isvtest runs pytest in-process via ``pytest.main()`` (see isvtest.main), so
    it must be an importable module — not necessarily a binary on PATH.
    """
    if importlib.util.find_spec("pytest") is None:
        return CheckResult(
            name="pytest",
            status=Status.FAIL,
            message="not importable",
            remediation="install pytest (a workspace dependency); run `uv sync`",
        )
    try:
        version: str | None = _pkg_version("pytest")
    except PackageNotFoundError:
        version = None
    return CheckResult(
        name="pytest",
        status=Status.OK,
        message=version or "importable",
        detail=f"version: {version}" if version else None,
    )


def _check_one(tool: _Tool, *, escalate_to_required: bool) -> CheckResult:
    """Look up one binary and produce a CheckResult.

    `escalate_to_required` overrides `tool.required` for provider-conditional
    tools when the user selected a provider that needs them.
    """
    if tool.name == "kubectl":
        command = _kubectl_command()
        executable = command[0]
        prefix: tuple[str, ...] = tuple(command[1:])
    else:
        executable = tool.name
        prefix = ()
    resolved = shutil.which(executable)

    if resolved is None:
        status = Status.FAIL if (tool.required or escalate_to_required) else Status.WARN
        msg = f"not found in PATH (looked for '{executable}')"
        hint = (
            f"install {executable}"
            if tool.required or escalate_to_required
            else f"install {executable} if you need it for this workflow"
        )
        return CheckResult(name=tool.name, status=status, message=msg, remediation=hint)

    version: str | None = None
    if tool.version_args is not None:
        probe_args = (*prefix, *tool.version_args)
        if tool.name == "kubectl":
            version = _probe_kubectl_version(executable, probe_args)
        else:
            version = _probe_version(executable, probe_args)

    detail = f"path: {resolved}" + (f"\nversion: {version}" if version else "")
    summary = version or "found"
    return CheckResult(name=tool.name, status=Status.OK, message=summary, detail=detail)


def check_tools(providers: list[str] | None = None) -> CategoryReport:
    """Run the tools category.

    Args:
        providers: Provider names (e.g. ["aws"]) that escalate optional tools
            to required. None / empty → all optional tools stay recommended.

    Returns:
        CategoryReport with one CheckResult per probed tool.
    """
    selected = set(providers or [])
    escalations: set[str] = set()
    for prov in selected:
        escalations |= _PROVIDER_TOOLS.get(prov, frozenset())

    results: list[CheckResult] = []
    for tool in _BASE_TOOLS:
        results.append(_check_one(tool, escalate_to_required=False))
    results.append(_check_pytest())
    for tool in _OPTIONAL_TOOLS:
        results.append(_check_one(tool, escalate_to_required=tool.name in escalations))

    return CategoryReport(name="tools", results=results)
