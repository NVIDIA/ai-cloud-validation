# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run Kubernetes Launch Kit against the current cluster, once per session.

Every ``network_operator`` check reports a native JUnit suite of the same
``l8k validate`` run, so the first check to ask runs Launch Kit and the rest
reuse its result. ``l8k sosreport`` runs right after validate, whatever the
outcome, so every attempted validation leaves diagnostics behind.

Launch Kit owns its flags, configuration schema, defaults, and validation
deadline. This module binds the two required inputs, keeps the evidence, and
reports what Launch Kit emitted.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from json import JSONDecoder
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACT_DIR = Path("_output") / "k8s-launch-kit"
_VALIDATION_REPORT_NAME = "k8s-launch-kit-validation-report.html"
_SOSREPORT_TIMEOUT_SECONDS = 1800

_logger = logging.getLogger(__name__)


def run_launch_kit(
    session_state: dict[str, Any],
    *,
    user_config: str,
    deployment_files: str,
    executable: str = "l8k",
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> dict[str, Any]:
    """Return the validate result for these inputs, running Launch Kit once per session.

    The run is cached in ``session_state``, which every check of one validation
    session shares, so a later session always checks the cluster again.

    The result carries ``success``, ``artifacts`` (``validation_junit`` when
    Launch Kit wrote one), an ``error`` when anything went wrong, and the
    ``sosreport`` result in the same shape. A command that could not start,
    because an input or the executable is missing, carries ``skip_reason``
    instead of ``error``.
    """
    artifacts = Path(artifact_dir).expanduser().resolve()
    runs = session_state.setdefault(__name__, {})
    key = (executable, user_config, deployment_files, str(artifacts))
    if key not in runs:
        result = _guarded(lambda: _validate(executable, user_config, deployment_files, artifacts))
        if "skip_reason" in result:
            # Nothing was attempted, so there is nothing to diagnose.
            result["sosreport"] = {"success": False, "artifacts": {}, "skip_reason": result["skip_reason"]}
        else:
            result["sosreport"] = _guarded(lambda: _sosreport(executable, artifacts))
        runs[key] = result
    return runs[key]


class LaunchKitUnavailable(Exception):
    """A prerequisite is missing, so Launch Kit cannot start."""


def _guarded(operation: Any) -> dict[str, Any]:
    """Turn a missing prerequisite into a skip and any other error into a failure."""
    try:
        return operation()
    except LaunchKitUnavailable as exc:
        return {"success": False, "artifacts": {}, "skip_reason": str(exc)}
    except (OSError, ValueError) as exc:
        return {"success": False, "artifacts": {}, "error": str(exc)}


def _validate(executable: str, user_config: str, deployment_files: str, artifact_dir: Path) -> dict[str, Any]:
    """Run ``l8k validate`` and retain its JUnit, HTML report, and process evidence."""
    junit = artifact_dir / "launch-kit-junit.xml"
    retained_report = artifact_dir / _VALIDATION_REPORT_NAME
    # A stale report from an earlier run must never stand in for this one.
    junit.unlink(missing_ok=True)
    retained_report.unlink(missing_ok=True)
    inputs = _validate_inputs(user_config, deployment_files)
    binary = _resolve_executable(executable)
    working_dir = _working_dir(artifact_dir)

    argv = [str(binary), "validate", "--junit-path", str(junit), *inputs, "--output", "json"]
    process = _run_process(argv, cwd=working_dir)
    artifacts = _record_process(artifact_dir / "commands" / "validate", argv, process)

    parse_error: str | None = None
    try:
        documents = _parse_json_stream(str(process["stdout"]), "l8k validate stdout")
    except ValueError as exc:
        documents = []
        parse_error = str(exc)

    report_error: str | None = None
    if parse_error is None:
        try:
            report = _retain_validation_report(documents, working_dir=working_dir, artifact_dir=artifact_dir)
        except OSError as exc:
            report_error = f"failed to retain Launch Kit HTML validation report: {exc}"
        else:
            if report is not None:
                artifacts["validation_report"] = str(report)

    junit_error: str | None = None
    if junit.is_file():
        artifacts["validation_junit"] = str(junit)
    try:
        ET.parse(junit)
    except (OSError, ET.ParseError) as exc:
        junit_error = f"failed to read Launch Kit JUnit report (l8k must support --junit-path): {exc}"

    exit_code = int(process["exit_code"])
    error = parse_error or _structured_error(documents)
    if exit_code != 0 and error is None:
        error = _exit_error("validate", process)
    for extra in (report_error, junit_error):
        if extra:
            error = f"{error}; {extra}" if error else extra
    result: dict[str, Any] = {
        "success": exit_code == 0 and not any((parse_error, report_error, junit_error)),
        "artifacts": artifacts,
    }
    if error:
        result["error"] = error
    return result


def _sosreport(executable: str, artifact_dir: Path) -> dict[str, Any]:
    """Run ``l8k sosreport`` into the artifact directory and retain its text output."""
    binary = _resolve_executable(executable)
    output_dir = artifact_dir / "sosreport"
    # The Network Operator helper tars the output directory next to itself and
    # removes the directory; it is only left behind uncompressed or on failure.
    archive = output_dir.with_name(f"{output_dir.name}.tar.gz")
    archive.unlink(missing_ok=True)
    argv = [str(binary), "sosreport", "--output-dir", str(output_dir)]
    process = _run_process(argv, cwd=_working_dir(artifact_dir), timeout=_SOSREPORT_TIMEOUT_SECONDS)
    artifacts = _record_process(artifact_dir / "commands" / "sosreport", argv, process)
    for collected in (archive, output_dir):
        if collected.exists():
            artifacts["sosreport"] = str(collected)
            break
    result: dict[str, Any] = {"success": process["exit_code"] == 0, "artifacts": artifacts}
    if process["exit_code"] != 0:
        result["error"] = _exit_error("sosreport", process)
    return result


def _validate_inputs(user_config_value: str, deployment_files_value: str) -> list[str]:
    """Return the ``--user-config``/``--deployment-files`` arguments for existing inputs."""
    if not user_config_value:
        raise LaunchKitUnavailable(
            "tests.settings.k8s_launch_kit.user_config is required for Network Operator validation"
        )
    if not deployment_files_value:
        raise LaunchKitUnavailable(
            "tests.settings.k8s_launch_kit.deployment_files is required for Network Operator validation"
        )
    user_config = Path(user_config_value).expanduser().resolve()
    if not user_config.is_file():
        raise LaunchKitUnavailable(f"Launch Kit user config not found: {user_config}")
    deployment_files = Path(deployment_files_value).expanduser().resolve()
    if not deployment_files.is_dir():
        raise LaunchKitUnavailable(f"Launch Kit deployment directory not found: {deployment_files}")
    return ["--user-config", str(user_config), "--deployment-files", str(deployment_files)]


def _resolve_executable(value: str) -> Path:
    """Resolve an explicit path or a command available on ``PATH``."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise LaunchKitUnavailable(f"Launch Kit executable not found: {resolved}")
        return resolved
    found = shutil.which(value)
    if found is None:
        raise LaunchKitUnavailable(f"Launch Kit executable not found on PATH: {value}")
    return Path(found).resolve()


def _working_dir(artifact_dir: Path) -> Path:
    """Return the directory Launch Kit runs in, kept apart from retained evidence."""
    working_dir = artifact_dir / "work"
    working_dir.mkdir(parents=True, exist_ok=True)
    return working_dir


def _run_process(argv: list[str], *, cwd: Path, timeout: float | None = None) -> dict[str, Any]:
    """Execute a child process and retain both output streams."""
    _logger.info("Running: %s", " ".join(argv))
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Keep what the command wrote before the deadline; it shows where it hung.
        stderr = f"{_text(exc.stderr).rstrip()}\ntimed out after {timeout} seconds".lstrip()
        result = {"exit_code": -1, "stdout": _text(exc.stdout), "stderr": stderr}
    except OSError as exc:
        result = {"exit_code": -1, "stdout": "", "stderr": str(exc)}
    else:
        result = {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    result["duration_seconds"] = time.monotonic() - started
    _logger.info(
        "%s %s exited with code %s after %.0fs",
        Path(argv[0]).name,
        argv[1],
        result["exit_code"],
        result["duration_seconds"],
    )
    return result


def _text(output: str | bytes | None) -> str:
    """Return captured process output as text."""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _record_process(directory: Path, argv: list[str], result: dict[str, Any]) -> dict[str, str]:
    """Persist one command, stdout, and stderr as evidence."""
    directory.mkdir(parents=True, exist_ok=True)
    stdout_path = directory / "stdout.txt"
    stderr_path = directory / "stderr.log"
    command_path = directory / "command.json"
    stdout_path.write_text(str(result["stdout"]), encoding="utf-8")
    stderr_path.write_text(str(result["stderr"]), encoding="utf-8")
    command = {"argv": argv, "exit_code": result["exit_code"], "duration_seconds": result["duration_seconds"]}
    command_path.write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
    return {
        "stdout": str(stdout_path.resolve()),
        "stderr": str(stderr_path.resolve()),
        "command": str(command_path.resolve()),
    }


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


def _exit_error(command: str, process: dict[str, Any]) -> str:
    """Describe a non-zero exit with the last stderr line."""
    error = f"l8k {command} exited with code {process['exit_code']}"
    lines = [line.strip() for line in str(process["stderr"]).splitlines() if line.strip()]
    return f"{error}: {lines[-1]}" if lines else error
