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

"""Shared harness for the Firebird provider tests.

HTTP is faked at the client boundary (``FirebirdClient._send``), so the real
request, pagination, and Operation-wait logic of each script runs against canned
responses. Validation classes run on script output with the parameters the suite
wires, and config steps are rendered with the real orchestrator templating.
"""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs

import pytest
import yaml
from isvtest.core.discovery import discover_all_tests
from isvtest.core.validation import BaseValidation

from isvctl.config.merger import merge_yaml_files
from isvctl.config.output_schemas import get_schema_for_step, validate_output
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.context import Context
from isvctl.orchestrator.step_executor import StepExecutor

ISVCTL_ROOT = Path(__file__).resolve().parents[3]
FIREBIRD = ISVCTL_ROOT / "configs" / "providers" / "firebird"
SCRIPTS = FIREBIRD / "scripts"
SUITES = ISVCTL_ROOT / "configs" / "suites"

PROJECT = "project.P"
_module_ids = itertools.count()


@contextlib.contextmanager
def isolated_imports() -> Iterator[None]:
    """Let a Firebird script's ``from common...`` resolve to the Firebird package.

    Other providers ship a sibling top-level ``common`` package and scripts
    insert their scripts directory into ``sys.path``; both are restored after
    the load so no other provider's tests see Firebird's modules.
    """
    saved_modules = {n: m for n, m in sys.modules.items() if n == "common" or n.startswith("common.")}
    saved_path = list(sys.path)
    for name in saved_modules:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if n == "common" or n.startswith("common.")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def load(relative: str) -> ModuleType:
    """Load a Firebird script as a module; ``module._fb`` is its firebird_client (None if it uses none)."""
    path = SCRIPTS / relative
    spec = importlib.util.spec_from_file_location(f"test_firebird_{path.stem}_{next(_module_ids)}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with isolated_imports():
        spec.loader.exec_module(module)
        module._fb = sys.modules.get("common.firebird_client")  # type: ignore[attr-defined]
        # Every common helper the script loaded, e.g. module._common["probes"].
        module._common = {  # type: ignore[attr-defined]
            name.removeprefix("common."): mod for name, mod in sys.modules.items() if name.startswith("common.")
        }
    return module


class HttpError(Exception):
    """Raised by a route callable to answer with an HTTP error status."""

    def __init__(self, status: int) -> None:
        """Remember the status to answer with."""
        super().__init__(f"HTTP {status}")
        self.status = status


class WithToken:
    """A route whose callable also receives the request's bearer token: ``(body, query, token)``."""

    def __init__(self, fn: Callable[[Any, dict[str, list[str]], str | None], dict[str, Any]]) -> None:
        """Wrap ``fn``."""
        self.fn = fn


Route = dict[str, Any] | Callable[[Any, dict[str, list[str]]], dict[str, Any]] | WithToken | int


class FakeApi:
    """Canned Firebird API responses keyed by ``"METHOD /path"`` (path under /api/v1).

    A route value is a JSON payload, a callable ``(body, query) -> payload``, a
    ``WithToken`` callable, or an int HTTP status to fail with. A callable may
    raise ``HttpError``. An unrouted request fails the test.
    """

    def __init__(self, module: ModuleType, routes: dict[str, Route]) -> None:
        """Remember the routes and the client module whose error type to raise."""
        self.module = module
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any] | bytes | None, dict[str, list[str]]]] = []
        self.raw_paths: list[str] = []  # as sent, including the /api/v1 prefix when present
        self.tokens: list[str | None] = []  # bearer token of each call, aligned with ``calls``
        self.stderr = ""  # the script's progress log, set after the run

    def send(self, method: str, path: str, body: dict[str, Any] | bytes | None, *, token: str | None) -> dict[str, Any]:
        """Stand in for ``FirebirdClient._send`` (patched on the class as a bound method)."""
        self.raw_paths.append(path)
        self.tokens.append(token)
        route_path, _, query_string = path.removeprefix("/api/v1").partition("?")
        query = parse_qs(query_string)
        self.calls.append((method, route_path, body, query))
        key = f"{method} {route_path}"
        if key not in self.routes:
            raise AssertionError(f"unexpected request {key}")
        route = self.routes[key]
        if isinstance(route, int):
            raise self.module._fb.FirebirdApiError(f"{key}: HTTP {route}", status=route)
        try:
            if isinstance(route, WithToken):
                return route.fn(body, query, token)
            return route(body, query) if callable(route) else route
        except HttpError as e:
            raise self.module._fb.FirebirdApiError(f"{key}: HTTP {e.status}", status=e.status) from None

    def paths(self, method: str | None = None) -> list[str]:
        """Return the requested paths, optionally only for one method."""
        return [f"{m} {p}" for m, p, _, _ in self.calls if method in (None, m)]


def _no_ssh(*args: Any, **_kwargs: Any) -> tuple[int, str, str]:
    """Stand in for ``ssh_run`` so an unstubbed probe fails instead of reaching a host."""
    raise AssertionError(f"unexpected SSH to {args[0] if args else '?'} in a unit test")


def _no_socket(_sock: socket.socket, address: Any) -> None:
    """Stand in for ``socket.connect`` so an unstubbed network call fails instead of leaving the host."""
    raise AssertionError(f"unexpected network connection to {address} in a unit test")


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    relative: str,
    routes: dict[str, Route],
    argv: list[str] | None = None,
    *,
    prepare: Callable[[ModuleType], None] | None = None,
    wrap: Callable[[dict[str, Route], ModuleType], dict[str, Route]] | None = None,
) -> tuple[int, dict[str, Any], FakeApi]:
    """Run a script's main() against the fake API; return (exit code, JSON output, api).

    ``wrap`` rewrites the routes given the script's client module (for error
    translation); ``prepare`` patches the loaded script module before it runs.
    """
    module = load(relative)
    api = FakeApi(module, wrap(routes, module._fb) if wrap else routes)
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FIREBIRD_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("BM_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BM_KEY_FILE", raising=False)
    if module._fb:
        monkeypatch.setattr(module._fb.FirebirdClient, "_send", api.send)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(sys, "argv", [relative, *(argv or [])])
    # No test may reach a real host: any SSH or socket the test did not stub fails loudly.
    for holder in (module, *module._common.values()):
        if hasattr(holder, "ssh_run"):
            monkeypatch.setattr(holder, "ssh_run", _no_ssh)
    monkeypatch.setattr(socket.socket, "connect", _no_socket)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_socket)
    if prepare:
        prepare(module)
    code = module.main()
    captured = capsys.readouterr()
    api.stderr = captured.err
    return code, json.loads(captured.out), api


class FakeClock:
    """A controllable ``time`` stand-in: ``sleep`` advances ``now``; ``monotonic`` and ``time`` read it."""

    def __init__(self, now: float = 1000.0) -> None:
        """Start the clock at ``now``."""
        self.now = now

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now

    def time(self) -> float:
        """Return the current fake time (wall clock)."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock instead of sleeping."""
        self.now += seconds

    def install(self, monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> None:
        """Replace ``time`` in the script and every common helper it loaded."""
        for holder in (module, *module._common.values()):
            if hasattr(holder, "time"):
                monkeypatch.setattr(holder, "time", self)


def fake_keygen(monkeypatch: pytest.MonkeyPatch, module: ModuleType, temp_root: Path) -> list[Path]:
    """Keep key generation local: ``mkdtemp`` under ``temp_root`` and a stand-in for ssh-keygen.

    Returns the list of private-key paths generated, in order. Each public key
    names its file, so a test can tell which key was injected.
    """
    ssh_utils = module._common["ssh_utils"]
    generated: list[Path] = []

    def keygen(path: Path) -> None:
        path.write_text("PRIVATE KEY\n")
        Path(f"{path}.pub").write_text(f"ssh-ed25519 AAAA generated:{path}\n")
        generated.append(path)

    monkeypatch.setattr(ssh_utils.tempfile, "tempdir", str(temp_root))
    monkeypatch.setattr(ssh_utils, "_ssh_keygen", keygen)
    return generated


def suite_params(suite: str, check_name: str) -> dict[str, Any]:
    """Return the parameters ``suites/<suite>.yaml`` wires for ``check_name``."""
    validations = yaml.safe_load((SUITES / f"{suite}.yaml").read_text())["tests"]["validations"]
    for group in validations.values():
        if check_name in (group.get("checks") or {}):
            params = dict(group["checks"][check_name])
            for key in ("test_id", "labels", "description", "requires", "step"):
                params.pop(key, None)
            return params
    raise AssertionError(f"{check_name} is not wired in the {suite} suite")


def validate(suite: str, check_cls: type[BaseValidation], output: dict[str, Any]) -> BaseValidation:
    """Run a validation class on step output with the parameters its suite wires."""
    check = check_cls(config={"step_output": output, **suite_params(suite, check_cls.__name__)})
    check.run()
    return check


def composite(suite: str, check_name: str, output: dict[str, Any]) -> list[str]:
    """Run a suite's composite check member by member on step output; return the failures."""
    for group in yaml.safe_load((SUITES / f"{suite}.yaml").read_text())["tests"]["validations"].values():
        if check_name in (group.get("checks") or {}):
            compose = group["checks"][check_name]["compose"]
            break
    else:
        raise AssertionError(f"{check_name} is not a composite in the {suite} suite")
    classes = {cls.__name__: cls for cls in discover_all_tests()}
    failures = []
    for entry in compose:
        member, params = (entry, {}) if isinstance(entry, str) else next(iter(entry.items()))
        check = classes[member](config={"step_output": output, **(params or {})})
        check.run()
        if not check._passed:
            failures.append(f"{member}: {check._error}")
    return failures


def schema_errors(step_name: str, output: dict[str, Any]) -> list[str]:
    """Return the orchestrator's output-schema errors for ``output`` of ``step_name``."""
    schema = get_schema_for_step(step_name)
    return validate_output(output, schema)[1] if schema else []


def operation(resource_id: str, status: str = "COMPLETED", op_id: str = "operation.1") -> dict[str, Any]:
    """Return an OperationResponse payload."""
    return {"operation": {"id": op_id, "status": status, "resourceId": resource_id}}


def _config(config: str) -> RunConfig:
    """Return the merged Firebird ``config/<config>.yaml``."""
    return RunConfig.model_validate(merge_yaml_files([FIREBIRD / "config" / f"{config}.yaml"]))


def config_steps(config: str, platform: str) -> dict[str, Any]:
    """Return a Firebird config's steps by name, in order."""
    return {step.name: step for step in _config(config).commands[platform].steps}


def render(config: str, platform: str, step_name: str, outputs: dict[str, dict[str, Any]]) -> list[str]:
    """Render one step's args with the given upstream step outputs."""
    run_config = _config(config)
    step = next(s for s in run_config.commands[platform].steps if s.name == step_name)
    context = Context(run_config)
    for name, output in outputs.items():
        context.set_step_output(name, output)
    return StepExecutor()._render_args(step.args, context)
