# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for direct GB300 BMC log inspection (BFX03-03)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from isvtest.validations.breakfix import BmcKernelLogCheck

ISVCTL_ROOT = Path(__file__).resolve().parents[3]
GB300_ROOT = ISVCTL_ROOT / "configs" / "providers" / "gb300"
SCRIPT_PATH = GB300_ROOT / "scripts" / "breakfix" / "query_bmc_kernel_logs.py"
CONFIG_PATH = GB300_ROOT / "config" / "bmc_kernel_logs.yaml"


def _load_script() -> ModuleType:
    """Load the provider script as an isolated module."""
    spec = importlib.util.spec_from_file_location("test_gb300_bmc_kernel_logs_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a subprocess result for privileged-helper mocks."""
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _journal_evidence(*, message_count: int = 1000) -> str:
    """Build normalized evidence matching the live GB300 Journal response."""
    return json.dumps(
        {
            "log_source": "/redfish/v1/Managers/BMC_0/LogServices/Journal/Entries",
            "message_count": message_count,
        }
    )


def test_config_wires_only_the_read_only_bmc_query() -> None:
    """The focused GB300 config binds only BFX03-03's query step."""
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    steps = config["commands"]["bare_metal"]["steps"]

    assert steps == [
        {
            "name": "query_bmc_kernel_logs",
            "phase": "test",
            "continue_on_failure": True,
            "command": "python ../scripts/breakfix/query_bmc_kernel_logs.py",
            "timeout": 120,
        }
    ]


def test_privileged_helper_contains_only_read_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    """The GB300 helper performs BCM reads and Redfish GETs only."""
    module = _load_script()
    observed: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.update(command=command, kwargs=kwargs)
        return _completed(_journal_evidence())

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module._run_privileged("a05-p01-dgx-03-c01", "/etc/ssl/certs/bmc-ca.pem")

    script = observed["kwargs"]["input"]
    assert observed["command"] == [
        "sudo",
        "-n",
        "bash",
        "-s",
        "--",
        "a05-p01-dgx-03-c01",
        "/etc/ssl/certs/bmc-ca.pem",
    ]
    assert "--insecure" not in script
    assert '--cacert "$bmc_ca_cert"' in script
    assert '[ ! -f "$bmc_ca_cert" ] || [ ! -r "$bmc_ca_cert" ]' in script
    assert 'get_redfish "/redfish/v1/Managers"' in script
    assert 'service_id" != "Journal"' in script
    assert "*bmc*journal*" in script
    assert "LogServices/EventLog" not in script
    assert "/redfish/v1/Systems" not in script
    assert "--request GET" not in script
    assert all(
        token not in script for token in ("-X POST", "-X PATCH", "-X DELETE", "clearlog", "CollectDiagnosticData")
    )


def test_live_shaped_log_produces_passing_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observed GB300 Manager Journal messages satisfy BFX03-03."""
    module = _load_script()
    monkeypatch.setattr(module, "_run_privileged", lambda host, ca_cert: _completed(_journal_evidence()))

    host = module._query_host("a05-p01-dgx-03-c01", "/trusted/bmc-ca.pem")

    assert host == {
        "host_id": "a05-p01-dgx-03-c01",
        "kernel_log_available": True,
        "message_count": 1000,
        "log_source": "/redfish/v1/Managers/BMC_0/LogServices/Journal/Entries",
    }
    check = BmcKernelLogCheck(config={"step_output": {"success": True, "hosts": [host]}})
    check.run()
    assert check.passed


def test_empty_log_cannot_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable BMC without actual messages is not positive evidence."""
    module = _load_script()
    monkeypatch.setattr(
        module,
        "_run_privileged",
        lambda host, ca_cert: _completed(_journal_evidence(message_count=0)),
    )

    with pytest.raises(module.InspectionError, match="no log messages"):
        module._query_host("a05-p01-dgx-03-c01")


def test_main_fails_when_target_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An applicable GB300 run fails rather than skipping without a target."""
    module = _load_script()
    monkeypatch.delenv("GB300_NODE_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", [SCRIPT_PATH.name])

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["hosts"] == []
    assert payload["error"] == "GB300_NODE_HOST is required"
    assert "skipped" not in payload


def test_query_failure_does_not_leak_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Credentials and raw command diagnostics never enter provider JSON."""
    module = _load_script()
    monkeypatch.setenv("GB300_NODE_HOST", "a05-p01-dgx-03-c01")
    monkeypatch.setattr(
        module,
        "_run_privileged",
        lambda host, ca_cert: _completed("", returncode=23, stderr="password=do-not-print"),
    )
    monkeypatch.setattr(sys, "argv", [SCRIPT_PATH.name])

    assert module.main() == 1
    output = capsys.readouterr().out
    assert "do-not-print" not in output
    assert "password=" not in output
    assert json.loads(output)["error"] == "unable to retrieve a non-empty GB300 BMC Journal log"


def test_invalid_hostname_is_rejected_before_privileged_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shell metacharacters cannot enter the fixed privileged helper."""
    module = _load_script()
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run"))

    with pytest.raises(module.InspectionError, match="invalid GB300 node hostname"):
        module._run_privileged("node;reboot")
