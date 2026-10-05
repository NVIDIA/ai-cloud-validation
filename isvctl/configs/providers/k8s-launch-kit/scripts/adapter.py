#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thin AI Cloud Validation transport for the Kubernetes Launch Kit CLI.

The provider runs the Launch Kit validate and sosreport commands. It forwards
user-supplied arguments verbatim and requests structured output from validate.
Validation binds an existing complete user config and rendered deployment
directory directly. Its emitted HTML report and sosreport output are retained
below the provider artifact directory. Launch Kit remains the owner of command
flags, configuration schema, and defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from json import JSONDecoder
from pathlib import Path
from typing import Any

_RUN_COMMANDS = ("validate", "sosreport")
_VALIDATION_REPORT_NAME = "k8s-launch-kit-validation-report.html"


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


def _bind_sosreport_output(arguments: list[str], *, working_dir: Path, artifact_dir: Path) -> tuple[list[str], Path]:
    """Resolve the sosreport output directory and default it to retained evidence."""
    result = list(arguments)
    output_dir: Path | None = None
    index = 0
    while index < len(result):
        token = result[index]
        if token == "--output-dir":
            if index + 1 >= len(result) or not result[index + 1]:
                raise ValueError("--output-dir requires a non-empty value")
            output_dir = Path(result[index + 1]).expanduser()
            index += 2
            continue
        if token.startswith("--output-dir="):
            value = token.partition("=")[2]
            if not value:
                raise ValueError("--output-dir requires a non-empty value")
            output_dir = Path(value).expanduser()
        index += 1

    if output_dir is None:
        output_dir = artifact_dir / "sosreport"
        result.extend(["--output-dir", str(output_dir)])
    elif not output_dir.is_absolute():
        output_dir = (working_dir / output_dir).resolve()

    return result, output_dir.resolve()


def _retain_validation_report(
    documents: list[dict[str, Any]],
    *,
    working_dir: Path,
    artifact_dir: Path,
) -> Path | None:
    """Copy the HTML report advertised by Launch Kit into retained evidence."""
    report_value = next(
        (
            document["reportPath"]
            for document in reversed(documents)
            if isinstance(document.get("reportPath"), str) and document["reportPath"]
        ),
        None,
    )
    if report_value is None:
        return None

    source = Path(report_value).expanduser()
    if not source.is_absolute():
        source = working_dir / source
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Launch Kit HTML validation report not found: {source}")

    destination = (artifact_dir / _VALIDATION_REPORT_NAME).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination:
        shutil.copy2(source, destination)
    return destination


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


def _bind_validate_inputs(
    user_config_value: str,
    deployment_files_value: str,
    arguments: list[str],
) -> list[str]:
    """Bind required, pre-existing Launch Kit validation inputs."""
    if not user_config_value:
        raise ValueError("context.k8s_launch_kit.user_config is required for Network Operator validation")
    if not deployment_files_value:
        raise ValueError("context.k8s_launch_kit.deployment_files is required for Network Operator validation")

    conflicting_flags = [
        flag
        for flag in ("--user-config", "--deployment-files")
        if any(token == flag or token.startswith(f"{flag}=") for token in arguments)
    ]
    if conflicting_flags:
        raise ValueError(
            "dedicated Launch Kit validation inputs cannot be combined with raw flag(s): "
            + ", ".join(conflicting_flags)
        )

    user_config = Path(user_config_value).expanduser().resolve()
    if not user_config.is_file():
        raise FileNotFoundError(f"Launch Kit user config not found: {user_config}")
    deployment_files = Path(deployment_files_value).expanduser().resolve()
    if not deployment_files.is_dir():
        raise FileNotFoundError(f"Launch Kit deployment directory not found: {deployment_files}")
    return [
        *arguments,
        "--user-config",
        str(user_config),
        "--deployment-files",
        str(deployment_files),
    ]


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
    junit = artifact_dir / "launch-kit-junit.xml"
    retained_validation_report = artifact_dir / _VALIDATION_REPORT_NAME
    if args.command == "validate":
        retained_validation_report.unlink(missing_ok=True)
        junit.unlink(missing_ok=True)
        if any(token == "--junit-path" or token.startswith("--junit-path=") for token in arguments):
            raise ValueError("--junit-path is managed by the Launch Kit provider")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        arguments.extend(["--junit-path", str(junit)])
    sosreport_output_dir: Path | None = None
    if args.command == "validate" and args.deployment_files is not None:
        arguments = _bind_validate_inputs(args.user_config, args.deployment_files, arguments)
    elif args.user_config:
        raise ValueError("--user-config requires --deployment-files for the validate workflow command")
    if args.command == "sosreport":
        arguments, sosreport_output_dir = _bind_sosreport_output(
            arguments,
            working_dir=working_dir,
            artifact_dir=artifact_dir,
        )
    else:
        arguments = _with_json_output(arguments)
    argv = [str(executable), args.command, *arguments]
    result = _run_process(argv, cwd=working_dir, env=environment)
    artifacts = _record_process(artifact_dir / "commands" / args.command, argv, result)
    if sosreport_output_dir is not None and sosreport_output_dir.exists():
        artifacts["sosreport"] = str(sosreport_output_dir)

    parse_error: str | None = None
    if args.command == "sosreport":
        # The current sosreport command accepts the global --output flag but
        # streams human-readable helper output in both modes. Preserve it as a
        # process artifact and let this adapter provide the structured envelope.
        documents = []
    else:
        try:
            documents = _parse_json_stream(str(result["stdout"]), f"l8k {args.command} stdout")
        except ValueError as exc:
            documents = []
            parse_error = str(exc)

    report_retention_error: str | None = None
    if args.command == "validate" and parse_error is None:
        try:
            retained_report = _retain_validation_report(
                documents,
                working_dir=working_dir,
                artifact_dir=artifact_dir,
            )
        except (FileNotFoundError, OSError) as exc:
            retained_report = None
            report_retention_error = f"failed to retain Launch Kit HTML validation report: {exc}"
        if retained_report is not None:
            artifacts["validation_report"] = str(retained_report)

    junit_error: str | None = None
    if args.command == "validate":
        if junit.is_file():
            artifacts["validation_junit"] = str(junit)
        try:
            ET.parse(junit)
        except (OSError, ET.ParseError) as exc:
            junit_error = f"failed to read Launch Kit JUnit report (l8k must support --junit-path): {exc}"
    success = result["exit_code"] == 0 and not any((parse_error, report_retention_error, junit_error))
    error = parse_error or _structured_error(documents)
    if result["exit_code"] != 0 and error is None:
        error = f"l8k {args.command} exited with code {result['exit_code']}"
        if excerpt := _stderr_excerpt(str(result["stderr"])):
            error = f"{error}: {excerpt}"
    if report_retention_error is not None:
        error = f"{error}; {report_retention_error}" if error else report_retention_error
    if junit_error:
        error = f"{error}; {junit_error}" if error else junit_error
    envelope: dict[str, Any] = {
        "success": success,
        "platform": "kubernetes",
        "operation": args.command,
        "artifacts": artifacts,
    }
    if error:
        envelope["error"] = error
    exit_code = int(result["exit_code"])
    return envelope, exit_code if exit_code > 0 else (0 if success else 1)


def _parser() -> argparse.ArgumentParser:
    """Build the provider command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    run = subparsers.add_parser("run", help="Run one real Launch Kit workflow command")
    run.add_argument("--executable", required=True)
    run.add_argument("--command", choices=_RUN_COMMANDS, required=True)
    run.add_argument("--arguments-json", required=True)
    run.add_argument("--user-config", default="")
    run.add_argument("--deployment-files", default=None)
    run.add_argument("--environment-json", default="{}")
    run.add_argument("--working-dir", required=True)
    run.add_argument("--artifact-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute one provider operation and emit a single JSON envelope."""
    args = _parser().parse_args(argv)
    try:
        envelope, exit_code = _run_workflow(args)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        envelope = {
            "success": False,
            "platform": "kubernetes",
            "operation": args.command,
            "error": str(exc),
        }
        exit_code = 1
    print(json.dumps(envelope))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
