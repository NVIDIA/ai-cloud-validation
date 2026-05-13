# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for the AWS VM virtual-device hardening reference script."""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from .conftest import load_vm_script


def _guest_probe_with_services(module: ModuleType, monkeypatch: pytest.MonkeyPatch, services: str) -> dict:
    """Run guest-probe parsing with a fake merged-probe SSH response."""
    captured_scripts: list[str] = []

    def fake_run_combined_probe(
        host: str,
        user: str,
        key_file: str,
        timeout: int,
    ) -> tuple[dict[str, str] | None, str | None]:
        assert host == "203.0.113.10"
        assert user == "ubuntu"
        assert key_file == "/tmp/key.pem"
        assert timeout == 60
        captured_scripts.append(module._combined_probe_script())
        return (
            {
                "usb_count": "0",
                "pci_devices": "",
                "processes": "",
                "services": services,
                "device_paths": "",
            },
            None,
        )

    monkeypatch.setattr(module, "_run_combined_probe", fake_run_combined_probe)
    result = module._collect_guest_probe("203.0.113.10", "ubuntu", "/tmp/key.pem", 60)

    assert captured_scripts
    assert "--state=running" in captured_scripts[0]
    assert "--output=json" in captured_scripts[0]
    return result


def test_inactive_vmware_unit_descriptions_are_not_virtual_device_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed-but-inactive VMware units are not active virtual-device evidence."""
    module = load_vm_script("virtual_device_hardening.py")
    services = json.dumps(
        [
            {
                "unit": "open-vm-tools.service",
                "load": "loaded",
                "active": "inactive",
                "sub": "dead",
                "description": "Service for virtual machines hosted on VMware",
            },
            {
                "unit": "vgauth.service",
                "load": "loaded",
                "active": "inactive",
                "sub": "dead",
                "description": "Authentication service for virtual machines hosted on VMware",
            },
        ]
    )

    result = _guest_probe_with_services(module, monkeypatch, services)

    assert result["status"] == "completed"
    assert result["unnecessary_device_signals"] == []


def test_running_vmware_guest_agent_unit_is_virtual_device_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active VMware guest-agent service remains hardening evidence."""
    module = load_vm_script("virtual_device_hardening.py")
    services = json.dumps(
        [
            {
                "unit": "open-vm-tools.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "Service for virtual machines hosted on VMware",
            }
        ]
    )

    result = _guest_probe_with_services(module, monkeypatch, services)

    assert result["status"] == "completed"
    assert result["unnecessary_device_signals"] == ["open-vm-tools.service"]


def test_systemctl_json_output_ignores_descriptions_for_signal_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured systemctl output is parsed without matching descriptions."""
    module = load_vm_script("virtual_device_hardening.py")
    services = json.dumps(
        [
            {
                "unit": "open-vm-tools.service",
                "load": "loaded",
                "active": "inactive",
                "sub": "dead",
                "description": "Service for virtual machines hosted on VMware",
            },
            {
                "unit": "vgauth.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "Authentication service for virtual machines hosted on VMware",
            },
        ]
    )

    result = _guest_probe_with_services(module, monkeypatch, services)

    assert result["status"] == "completed"
    assert result["unnecessary_device_signals"] == ["vgauth.service"]


def test_combined_probe_output_round_trips_via_sentinels() -> None:
    """The shell script's sentinel-separated output parses back to a per-probe dict."""
    module = load_vm_script("virtual_device_hardening.py")
    sentinel = module.PROBE_SENTINEL
    combined = "\n".join(
        [
            f"{sentinel} usb_count",
            "3",
            f"{sentinel} pci_devices",
            "00:00.0 USB controller: Foo",
            f"{sentinel} processes",
            "init",
            "spice-vdagent",
            f"{sentinel} services",
            "[]",
            f"{sentinel} device_paths",
            "/dev/sr0",
        ]
    )

    outputs = module._split_probe_outputs(combined)

    assert outputs["usb_count"] == "3"
    assert outputs["pci_devices"] == "00:00.0 USB controller: Foo"
    assert outputs["processes"] == "init\nspice-vdagent"
    assert outputs["services"] == "[]"
    assert outputs["device_paths"] == "/dev/sr0"


def test_main_emits_minimal_contract_for_guest_probe_signals(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guest signals fail subtests without leaking raw provider diagnostics."""
    module = load_vm_script("virtual_device_hardening.py")

    def fake_collect_guest_probe(
        host: str,
        user: str,
        key_file: str,
        timeout: int,
    ) -> dict[str, object]:
        assert host == "203.0.113.10"
        assert user == "ubuntu"
        assert key_file == "/tmp/key.pem"
        assert timeout == 60
        return {
            "status": "completed",
            "usb_device_count": 2,
            "usb_signals": ["usb device entries present: 2"],
            "clipboard_signals": [],
            "unnecessary_device_signals": [],
        }

    monkeypatch.setattr(module, "_collect_guest_probe", fake_collect_guest_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "virtual_device_hardening.py",
            "--instance-id",
            "i-123",
            "--public-ip",
            "203.0.113.10",
            "--key-file",
            "/tmp/key.pem",
        ],
    )

    exit_code = module.main()
    result = json.loads(capsys.readouterr().out)
    usb_result = result["tests"]["usb_devices_disabled"]

    assert exit_code == 1
    assert set(result) == {"success", "platform", "test_name", "tests"}
    assert result["success"] is False
    assert usb_result["passed"] is False
    assert usb_result["error"] == "USB device/controller signals detected: usb device entries present: 2"
    assert "usb_device_count" not in usb_result
    assert "signals" not in usb_result


def test_main_returns_structured_failure_for_malformed_guest_probe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed guest-probe output is reported as JSON instead of crashing."""
    module = load_vm_script("virtual_device_hardening.py")

    def fake_collect_guest_probe(
        host: str,
        user: str,
        key_file: str,
        timeout: int,
    ) -> dict[str, object]:
        assert host == "203.0.113.10"
        assert user == "ubuntu"
        assert key_file == "/tmp/key.pem"
        assert timeout == 60
        msg = "Expected integer output, got 'bad usb count'"
        raise ValueError(msg)

    monkeypatch.setattr(module, "_collect_guest_probe", fake_collect_guest_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "virtual_device_hardening.py",
            "--instance-id",
            "i-123",
            "--public-ip",
            "203.0.113.10",
            "--key-file",
            "/tmp/key.pem",
        ],
    )

    exit_code = module.main()
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["success"] is False
    assert "guest_probe" not in result
    assert "instance_id" not in result
    assert result["error"] == "Expected integer output, got 'bad usb count'"


def test_main_returns_failure_for_unavailable_guest_probe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unavailable guest-probe status is a hard step failure."""
    module = load_vm_script("virtual_device_hardening.py")

    def fake_collect_guest_probe(
        host: str,
        user: str,
        key_file: str,
        timeout: int,
    ) -> dict[str, object]:
        assert host == "203.0.113.10"
        assert user == "ubuntu"
        assert key_file == "/tmp/key.pem"
        assert timeout == 60
        return {"status": "unavailable", "error": "SSH command exited 255"}

    monkeypatch.setattr(module, "_collect_guest_probe", fake_collect_guest_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "virtual_device_hardening.py",
            "--instance-id",
            "i-123",
            "--public-ip",
            "203.0.113.10",
            "--key-file",
            "/tmp/key.pem",
        ],
    )

    exit_code = module.main()
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["success"] is False
    assert result["error"] == "SSH command exited 255"
