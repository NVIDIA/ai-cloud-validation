#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thin AI Cloud Validation transport for the Kubernetes Launch Kit CLI.

The provider deliberately exposes the real Launch Kit operations. It forwards
user-supplied arguments verbatim and adds ``--output json`` so stdout can be
preserved as structured evidence. When a complete user config is supplied, the
discover operation also stages it transiently in the working directory, binds
Launch Kit's native ``--user-config`` and ``--save-cluster-config`` flags, and
removes the staged input after discovery. Launch Kit remains the owner of
command flags, configuration schema, and defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from json import JSONDecoder
from pathlib import Path
from typing import Any

_WORKFLOW_COMMANDS = ("discover", "generate", "deploy", "validate", "clean")
_INSTALLER_URL = "https://raw.githubusercontent.com/NVIDIA/k8s-launch-kit/{ref}/scripts/install.sh"
_STAGED_USER_CONFIG = "user-config.yaml"
_DISCOVERED_CLUSTER_CONFIG = "cluster-config.yaml"


def _parse_json_value(raw: str, source: str, expected_type: type[Any]) -> Any:
    """Parse a JSON CLI value and enforce its root type."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(value, expected_type):
        raise ValueError(f"{source} must contain a {expected_type.__name__}")
    return value


def _parse_json_stream(raw: str, source: str) -> list[dict[str, Any]]:
    """Parse zero or more concatenated JSON objects from ``raw``."""
    decoder = JSONDecoder()
    documents: list[dict[str, Any]] = []
    offset = 0
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset >= len(raw):
            break
        try:
            value, offset = decoder.raw_decode(raw, offset)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source} contains invalid JSON at byte {exc.pos}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{source} document #{len(documents) + 1} is not an object")
        documents.append(value)
    return documents


def _write_json(path: Path, value: Any) -> None:
    """Write deterministic structured evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _resolve_executable(value: str) -> Path:
    """Resolve an explicit path or a command available on ``PATH``."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Launch Kit executable not found: {resolved}")
        return resolved
    found = shutil.which(value)
    if found is None:
        raise FileNotFoundError(f"Launch Kit executable not found on PATH: {value}")
    return Path(found).resolve()


def _with_json_output(arguments: list[str]) -> list[str]:
    """Return workflow arguments that request Launch Kit's automation output."""
    result = list(arguments)
    for index, token in enumerate(result):
        if token == "--output":
            if index + 1 >= len(result):
                raise ValueError("--output requires a value")
            if result[index + 1] != "json":
                raise ValueError("the Launch Kit provider requires --output json")
            return result
        if token.startswith("--output="):
            if token.partition("=")[2] != "json":
                raise ValueError("the Launch Kit provider requires --output json")
            return result
    result.extend(["--output", "json"])
    return result


def _structured_error(documents: list[dict[str, Any]]) -> str | None:
    """Extract the most actionable Launch Kit structured error."""
    for document in reversed(documents):
        error = document.get("error")
        if not isinstance(error, dict):
            continue
        message = error.get("message")
        if not isinstance(message, str) or not message:
            continue
        suggestion = error.get("suggestion")
        if isinstance(suggestion, str) and suggestion:
            return f"{message}; {suggestion}"
        return message
    return None


def _stderr_excerpt(stderr: str) -> str | None:
    """Return the last non-empty stderr line without flooding the envelope."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _run_process(argv: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    """Execute a child process and retain both output streams."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "duration_seconds": time.monotonic() - started,
        }
    except OSError as exc:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
            "duration_seconds": time.monotonic() - started,
        }


def _record_process(directory: Path, argv: list[str], result: dict[str, Any]) -> dict[str, str]:
    """Persist one command, stdout, and stderr as evidence."""
    directory.mkdir(parents=True, exist_ok=True)
    stdout_path = directory / "stdout.txt"
    stderr_path = directory / "stderr.log"
    command_path = directory / "command.json"
    stdout_path.write_text(str(result["stdout"]), encoding="utf-8")
    stderr_path.write_text(str(result["stderr"]), encoding="utf-8")
    _write_json(
        command_path,
        {
            "argv": argv,
            "exit_code": result["exit_code"],
            "duration_seconds": result["duration_seconds"],
        },
    )
    return {
        "stdout": str(stdout_path.resolve()),
        "stderr": str(stderr_path.resolve()),
        "command": str(command_path.resolve()),
    }


def _environment(raw: str) -> dict[str, str]:
    """Merge user-supplied string environment entries with the process environment."""
    supplied = _parse_json_value(raw, "--environment-json", dict)
    invalid = [str(key) for key, value in supplied.items() if not isinstance(key, str) or not isinstance(value, str)]
    if invalid:
        raise ValueError("--environment-json keys and values must be strings")
    env = os.environ.copy()
    env.update(supplied)
    return env


def _stage_user_config(
    source_value: str,
    working_dir: Path,
    arguments: list[str],
) -> tuple[list[str], Path | None, dict[str, Any] | None]:
    """Stage a complete user config for discovery and return safe provenance."""
    if not source_value:
        return arguments, None, None

    conflicting_flags = [
        flag
        for flag in ("--user-config", "--save-cluster-config")
        if any(token == flag or token.startswith(f"{flag}=") for token in arguments)
    ]
    if conflicting_flags:
        raise ValueError(
            "context.k8s_launch_kit.user_config cannot be combined with raw discovery flag(s): "
            + ", ".join(conflicting_flags)
        )

    source = Path(source_value).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Launch Kit user config not found: {source}")
    try:
        source.relative_to(working_dir)
    except ValueError:
        pass
    else:
        raise ValueError("Launch Kit user config must be outside the retained provider working directory")

    staged = working_dir / _STAGED_USER_CONFIG
    content = source.read_bytes()
    staged.unlink(missing_ok=True)
    try:
        staged.write_bytes(content)
        staged.chmod(0o600)
    except OSError:
        staged.unlink(missing_ok=True)
        raise

    discovered = working_dir / _DISCOVERED_CLUSTER_CONFIG
    return (
        [
            *arguments,
            "--user-config",
            str(staged),
            "--save-cluster-config",
            str(discovered),
        ],
        staged,
        {
            "source_path": str(source),
            "staged_path": str(staged),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "retained": False,
        },
    )


def _run_workflow(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Invoke exactly one real Launch Kit workflow command."""
    executable = _resolve_executable(args.executable)
    arguments = _parse_json_value(args.arguments_json, "--arguments-json", list)
    if not all(isinstance(value, str) for value in arguments):
        raise ValueError("--arguments-json must contain only strings")
    environment = _environment(args.environment_json)
    working_dir = Path(args.working_dir).expanduser().resolve()
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path(args.artifact_dir).expanduser().resolve()
    staged_user_config: Path | None = None
    user_config_metadata_path: Path | None = None
    try:
        if args.user_config:
            if args.command != "discover":
                raise ValueError("--user-config is supported only with the discover workflow command")
            arguments, staged_user_config, user_config_metadata = _stage_user_config(
                args.user_config,
                working_dir,
                arguments,
            )
            user_config_metadata_path = artifact_dir / "inputs" / "user-config.json"
            _write_json(user_config_metadata_path, user_config_metadata)
        arguments = _with_json_output(arguments)
        argv = [str(executable), args.command, *arguments]
        result = _run_process(argv, cwd=working_dir, env=environment)
    finally:
        if staged_user_config is not None:
            staged_user_config.unlink(missing_ok=True)
    artifacts = _record_process(artifact_dir / "commands" / args.command, argv, result)
    if user_config_metadata_path is not None:
        artifacts["user_config"] = str(user_config_metadata_path)

    parse_error: str | None = None
    try:
        documents = _parse_json_stream(str(result["stdout"]), f"l8k {args.command} stdout")
    except ValueError as exc:
        documents = []
        parse_error = str(exc)

    success = result["exit_code"] == 0 and parse_error is None
    error = parse_error or _structured_error(documents)
    if not success and error is None:
        error = f"l8k {args.command} exited with code {result['exit_code']}"
        if excerpt := _stderr_excerpt(str(result["stderr"])):
            error = f"{error}: {excerpt}"
    envelope: dict[str, Any] = {
        "success": success,
        "platform": "kubernetes",
        "operation": args.command,
        "executable": str(executable),
        "argv": argv,
        "working_directory": str(working_dir),
        "exit_code": result["exit_code"],
        "duration_seconds": result["duration_seconds"],
        "documents": documents,
        "artifacts": artifacts,
    }
    if error:
        envelope["error"] = error
    excerpt = _stderr_excerpt(str(result["stderr"]))
    if excerpt:
        envelope["stderr_excerpt"] = excerpt
    exit_code = int(result["exit_code"])
    return envelope, exit_code if exit_code > 0 else (0 if success else 1)


def _verify_executable(
    executable: Path,
    artifact_dir: Path,
    expected_version: str = "",
    environment: dict[str, str] | None = None,
) -> tuple[dict[str, Any], bool, str | None]:
    """Run Launch Kit version and schema commands and preserve both responses."""
    env = environment.copy() if environment is not None else os.environ.copy()
    checks: dict[str, Any] = {}
    artifacts: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for name, command_args in (("version", ["version", "--output", "json"]), ("schema", ["schema"])):
        argv = [str(executable), *command_args]
        result = _run_process(argv, cwd=Path.cwd(), env=env)
        artifacts[name] = _record_process(artifact_dir / name, argv, result)
        try:
            documents = _parse_json_stream(str(result["stdout"]), f"l8k {name} stdout")
        except ValueError as exc:
            documents = []
            errors.append(str(exc))
        if result["exit_code"] != 0:
            errors.append(
                _stderr_excerpt(str(result["stderr"])) or f"l8k {name} exited with code {result['exit_code']}"
            )
        elif len(documents) != 1:
            errors.append(f"l8k {name} must emit exactly one JSON object, got {len(documents)}")
        passed = result["exit_code"] == 0 and len(documents) == 1
        if name == "schema" and passed:
            commands = documents[0].get("commands")
            advertised = set(commands) if isinstance(commands, dict) else set()
            missing_commands = set(_WORKFLOW_COMMANDS) - advertised
            if missing_commands:
                passed = False
                errors.append(
                    "l8k schema does not advertise required command(s): " + ", ".join(sorted(missing_commands))
                )
        if name == "version" and passed and expected_version:
            actual_version = documents[0].get("version")
            if actual_version != expected_version:
                passed = False
                errors.append(f"l8k version mismatch: expected {expected_version!r}, got {actual_version!r}")
        checks[name] = {
            "passed": passed,
            "documents": documents,
            "exit_code": result["exit_code"],
            "artifacts": artifacts[name],
        }
    return {"checks": checks, "artifacts": artifacts}, not errors, "; ".join(errors) or None


def _download_installer(installer_ref: str, expected_sha256: str, artifact_dir: Path) -> tuple[Path, str]:
    """Download an immutable installer and verify its trusted SHA-256 digest."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", installer_ref):
        raise ValueError("Launch Kit installer_ref must be a full 40-character Git commit SHA")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("Launch Kit installer_sha256 must be a 64-character SHA-256 digest")
    expected_sha256 = expected_sha256.lower()
    url = _INSTALLER_URL.format(ref=installer_ref.lower())
    installer = artifact_dir / "installer.sh"
    installer.parent.mkdir(parents=True, exist_ok=True)
    installer.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "ai-cloud-validation"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
    digest = hashlib.sha256(content).hexdigest()
    verified = digest == expected_sha256
    _write_json(
        artifact_dir / "installer-download.json",
        {
            "url": url,
            "ref": installer_ref.lower(),
            "expected_sha256": expected_sha256,
            "sha256": digest,
            "verified": verified,
        },
    )
    if not verified:
        raise ValueError(f"Launch Kit installer SHA-256 mismatch: expected {expected_sha256}, got {digest}")
    installer.write_bytes(content)
    return installer, url


def _installed_executable(prefix: str) -> Path:
    """Resolve the executable installed by the official Launch Kit installer."""
    install_prefix = Path(prefix).expanduser() if prefix else Path("/usr/local")
    return _resolve_executable(str(install_prefix / "bin" / "l8k"))


def _prepare(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Optionally install Launch Kit, then verify version and schema."""
    artifact_dir = Path(args.artifact_dir).expanduser().resolve() / "prepare"
    environment = _environment(args.environment_json)
    installed = False
    install_details: dict[str, Any] | None = None
    if args.mode == "install":
        installer, url = _download_installer(args.installer_ref, args.installer_sha256, artifact_dir)
        installer_argv = ["/bin/sh", str(installer)]
        if args.prefix:
            installer_argv.extend(["-d", args.prefix])
        env = environment.copy()
        if args.version:
            env["L8K_VERSION"] = args.version
        result = _run_process(installer_argv, cwd=Path.cwd(), env=env)
        install_artifacts = _record_process(artifact_dir / "install", installer_argv, result)
        install_details = {
            "url": url,
            "exit_code": result["exit_code"],
            "installer": str(installer.resolve()),
            "download_metadata": str((artifact_dir / "installer-download.json").resolve()),
            "artifacts": install_artifacts,
        }
        if result["exit_code"] != 0:
            error = _stderr_excerpt(str(result["stderr"])) or "Launch Kit installer failed"
            return {
                "success": False,
                "platform": "kubernetes",
                "operation": "prepare",
                "installed": False,
                "install": install_details,
                "error": error,
            }, 1
        executable = _installed_executable(args.prefix)
        installed = True
    else:
        executable = _resolve_executable(args.executable)

    verification, success, error = _verify_executable(
        executable,
        artifact_dir / "verify",
        args.version,
        environment,
    )
    envelope: dict[str, Any] = {
        "success": success,
        "platform": "kubernetes",
        "operation": "prepare",
        "installed": installed,
        "executable": str(executable),
        **verification,
    }
    if install_details is not None:
        envelope["install"] = install_details
        envelope["artifacts"]["installer"] = install_details["installer"]
        envelope["artifacts"]["installer_download"] = install_details["download_metadata"]
        envelope["artifacts"]["install"] = install_details["artifacts"]
    if error:
        envelope["error"] = error
    return envelope, 0 if success else 1


def _verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Verify an existing Launch Kit executable without installing it."""
    executable = _resolve_executable(args.executable)
    environment = _environment(args.environment_json)
    verification, success, error = _verify_executable(
        executable,
        Path(args.artifact_dir).expanduser().resolve() / "verify-test",
        args.expected_version,
        environment,
    )
    envelope: dict[str, Any] = {
        "success": success,
        "platform": "kubernetes",
        "operation": "verify",
        "executable": str(executable),
        **verification,
    }
    if error:
        envelope["error"] = error
    return envelope, 0 if success else 1


def _kubeconfig_from_workflow(raw: str) -> str | None:
    """Extract one consistent explicit kubeconfig from the real workflow arguments."""
    workflow = _parse_json_value(raw, "--workflow-arguments-json", dict)
    if set(workflow) != set(_WORKFLOW_COMMANDS):
        raise ValueError("--workflow-arguments-json must contain exactly: " + ", ".join(_WORKFLOW_COMMANDS))
    found: set[str] = set()
    for command, values in workflow.items():
        if command not in _WORKFLOW_COMMANDS or not isinstance(values, list):
            raise ValueError("--workflow-arguments-json must map Launch Kit workflow commands to argument lists")
        if not all(isinstance(value, str) for value in values):
            raise ValueError(f"workflow arguments for {command} must contain only strings")
        index = 0
        while index < len(values):
            token = values[index]
            if token == "--kubeconfig":
                if index + 1 >= len(values):
                    raise ValueError(f"{command} --kubeconfig requires a value")
                value = values[index + 1]
                if not value:
                    raise ValueError(f"{command} --kubeconfig requires a non-empty value")
                found.add(value)
                index += 2
                continue
            if token.startswith("--kubeconfig="):
                value = token.partition("=")[2]
                if not value:
                    raise ValueError(f"{command} --kubeconfig requires a non-empty value")
                found.add(value)
            index += 1
    if len(found) > 1:
        raise ValueError(f"Launch Kit workflow commands select different kubeconfigs: {sorted(found)}")
    return next(iter(found), None)


def _kubectl_prefix(raw: str, environment: dict[str, str]) -> list[str]:
    """Resolve the configured kubectl-compatible invocation."""
    supplied = _parse_json_value(raw, "--kubectl-command-json", list)
    if supplied:
        if not all(isinstance(value, str) and value for value in supplied):
            raise ValueError("--kubectl-command-json must contain non-empty strings")
        invocation_dir = Path.cwd()
        return [
            str((invocation_dir / value).resolve())
            if not Path(value).is_absolute() and "/" in value and (invocation_dir / value).exists()
            else value
            for value in supplied
        ]
    override = environment.get("KUBECTL", "").strip()
    return shlex.split(override) if override else ["kubectl"]


def _preflight_check(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    artifact_dir: Path,
    environment: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run and record one Kubernetes prerequisite command."""
    result = _run_process(argv, cwd=cwd, env=environment)
    artifacts = _record_process(artifact_dir / name, argv, result)
    passed = result["exit_code"] == 0
    message = "command succeeded" if passed else (_stderr_excerpt(str(result["stderr"])) or "command failed")
    return {
        "name": name,
        "passed": passed,
        "message": message,
        "exit_code": result["exit_code"],
        "artifacts": artifacts,
    }, result


def _preflight(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Prove that the Kubernetes API and at least one Ready node are available."""
    working_dir = Path(args.working_dir).expanduser().resolve()
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path(args.artifact_dir).expanduser().resolve() / "kubernetes-preflight"
    kubeconfig = _kubeconfig_from_workflow(args.workflow_arguments_json)
    environment = _environment(args.environment_json)
    prefix = _kubectl_prefix(args.kubectl_command_json, environment)
    kubeconfig_args = ["--kubeconfig", kubeconfig] if kubeconfig else []

    version_check, version_result = _preflight_check(
        "api-version",
        [*prefix, *kubeconfig_args, "version", "-o", "json"],
        cwd=working_dir,
        artifact_dir=artifact_dir,
        environment=environment,
    )
    nodes_check, nodes_result = _preflight_check(
        "nodes",
        [*prefix, *kubeconfig_args, "get", "nodes", "-o", "json"],
        cwd=working_dir,
        artifact_dir=artifact_dir,
        environment=environment,
    )

    server_version: str | None = None
    if version_check["passed"]:
        try:
            version_payload = json.loads(str(version_result["stdout"]))
            server = version_payload.get("serverVersion") if isinstance(version_payload, dict) else None
            if isinstance(server, dict) and isinstance(server.get("gitVersion"), str):
                server_version = server["gitVersion"]
            else:
                version_check["passed"] = False
                version_check["message"] = "kubectl output has no serverVersion.gitVersion"
        except json.JSONDecodeError as exc:
            version_check["passed"] = False
            version_check["message"] = f"kubectl version output is invalid JSON: {exc}"

    total_nodes = 0
    ready_nodes = 0
    if nodes_check["passed"]:
        try:
            nodes_payload = json.loads(str(nodes_result["stdout"]))
            items = nodes_payload.get("items") if isinstance(nodes_payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("kubectl node output has no items list")
            total_nodes = len(items)
            ready_nodes = sum(
                any(
                    isinstance(condition, dict)
                    and condition.get("type") == "Ready"
                    and condition.get("status") == "True"
                    for condition in (node.get("status", {}).get("conditions", []) if isinstance(node, dict) else [])
                )
                for node in items
            )
        except (json.JSONDecodeError, ValueError) as exc:
            nodes_check["passed"] = False
            nodes_check["message"] = str(exc)

    inventory_check = {
        "name": "non-empty-cluster",
        "passed": total_nodes > 0,
        "message": f"found {total_nodes} node(s)" if total_nodes else "cluster contains no nodes",
    }
    readiness_check = {
        "name": "ready-node",
        "passed": ready_nodes > 0,
        "message": f"found {ready_nodes}/{total_nodes} Ready node(s)" if ready_nodes else "cluster has no Ready nodes",
    }
    checks = [version_check, nodes_check, inventory_check, readiness_check]
    success = all(check["passed"] is True for check in checks)
    envelope: dict[str, Any] = {
        "success": success,
        "platform": "kubernetes",
        "operation": "kubernetes-preflight",
        "kubeconfig_source": "workflow arguments" if kubeconfig else "kubectl environment/default resolution",
        "server_version": server_version,
        "node_count": total_nodes,
        "ready_node_count": ready_nodes,
        "checks": checks,
        "artifacts": {
            "api_version": version_check["artifacts"],
            "nodes": nodes_check["artifacts"],
        },
    }
    if not success:
        failures = [f"{check['name']}: {check['message']}" for check in checks if check["passed"] is not True]
        envelope["error"] = "Kubernetes prerequisite failed: " + "; ".join(failures)
        envelope["remediation"] = "Select a reachable cluster and verify Kubernetes API and Ready-node access"
    return envelope, 0 if success else 1


def _parser() -> argparse.ArgumentParser:
    """Build the provider command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    prepare = subparsers.add_parser("prepare", help="Install when requested, then verify l8k")
    prepare.add_argument("--mode", choices=("verify", "install"), required=True)
    prepare.add_argument("--executable", required=True)
    prepare.add_argument("--version", default="")
    prepare.add_argument("--installer-ref", default="")
    prepare.add_argument("--installer-sha256", default="")
    prepare.add_argument("--prefix", default="")
    prepare.add_argument("--environment-json", default="{}")
    prepare.add_argument("--artifact-dir", required=True)

    verify = subparsers.add_parser("verify", help="Verify l8k version and schema")
    verify.add_argument("--executable", required=True)
    verify.add_argument("--expected-version", default="")
    verify.add_argument("--environment-json", default="{}")
    verify.add_argument("--artifact-dir", required=True)

    preflight = subparsers.add_parser("preflight", help="Verify Kubernetes API and Ready-node access")
    preflight.add_argument("--kubectl-command-json", default="[]")
    preflight.add_argument("--workflow-arguments-json", required=True)
    preflight.add_argument("--environment-json", default="{}")
    preflight.add_argument("--working-dir", required=True)
    preflight.add_argument("--artifact-dir", required=True)

    run = subparsers.add_parser("run", help="Run one real Launch Kit workflow command")
    run.add_argument("--executable", required=True)
    run.add_argument("--command", choices=_WORKFLOW_COMMANDS, required=True)
    run.add_argument("--arguments-json", required=True)
    run.add_argument("--user-config", default="")
    run.add_argument("--environment-json", default="{}")
    run.add_argument("--working-dir", required=True)
    run.add_argument("--artifact-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute one provider operation and emit a single JSON envelope."""
    args = _parser().parse_args(argv)
    try:
        if args.action == "prepare":
            envelope, exit_code = _prepare(args)
        elif args.action == "verify":
            envelope, exit_code = _verify(args)
        elif args.action == "preflight":
            envelope, exit_code = _preflight(args)
        else:
            envelope, exit_code = _run_workflow(args)
    except (FileNotFoundError, OSError, TypeError, ValueError, urllib.error.URLError) as exc:
        envelope = {
            "success": False,
            "platform": "kubernetes",
            "operation": args.action,
            "error": str(exc),
        }
        exit_code = 1
    print(json.dumps(envelope))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
