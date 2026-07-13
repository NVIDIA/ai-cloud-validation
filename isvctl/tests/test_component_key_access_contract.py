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

"""Contract tests for AUTH03 component key access scripts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from paramiko import RSAKey

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_VM_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "vm"
MY_ISV_VM_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "my-isv" / "scripts" / "vm"


def _load_aws_script(script_name: str) -> ModuleType:
    """Load an AWS VM script as a module for direct helper testing."""
    script_path = AWS_VM_SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(f"test_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_temp_rsa_key() -> Path:
    """Create a temporary RSA private key PEM for SOL authorization tests."""
    key = RSAKey.generate(2048)
    path = Path(tempfile.mkstemp(prefix="isv-auth03-", suffix=".pem")[1])
    key.write_private_key_file(str(path))
    return path


def test_load_openssh_public_key_from_rsa_pem() -> None:
    """Private key PEM derives an OpenSSH public key line."""
    module = _load_aws_script("component_key_access.py")
    key_path = _write_temp_rsa_key()
    try:
        public_key = module._load_openssh_public_key(str(key_path))
    finally:
        key_path.unlink(missing_ok=True)

    assert public_key.startswith("ssh-rsa ")


def test_probe_sol_access_skips_when_serial_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled serial console yields a skipped SOL probe."""
    module = _load_aws_script("component_key_access.py")
    monkeypatch.setattr(module, "check_serial_access", lambda _ec2: {"enabled": False})

    result = module._probe_sol_access(MagicMock(), MagicMock(), "i-abc", "ssh-rsa AAAAB3")

    assert result["passed"] is False
    assert result["skipped"] is True


def test_probe_sol_access_authorizes_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled serial console + Instance Connect Success proves SOL key access."""
    module = _load_aws_script("component_key_access.py")
    monkeypatch.setattr(module, "check_serial_access", lambda _ec2: {"enabled": True})
    eic = MagicMock()
    eic.send_serial_console_ssh_public_key.return_value = {"Success": True, "RequestId": "req-1"}

    result = module._probe_sol_access(MagicMock(), eic, "i-abc", "ssh-rsa AAAAB3")

    assert result["passed"] is True
    eic.send_serial_console_ssh_public_key.assert_called_once()


def test_probe_sol_access_fails_on_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instance Connect API errors fail the SOL probe."""
    module = _load_aws_script("component_key_access.py")
    monkeypatch.setattr(module, "check_serial_access", lambda _ec2: {"enabled": True})
    eic = MagicMock()
    eic.send_serial_console_ssh_public_key.side_effect = ClientError(
        {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "missing"}},
        "SendSerialConsoleSSHPublicKey",
    )

    result = module._probe_sol_access(MagicMock(), eic, "i-missing", "ssh-rsa AAAAB3")

    assert result["passed"] is False
    assert "missing" in result["error"]


def test_network_device_access_is_provider_hidden() -> None:
    """AWS marks network-device SSH as provider-hidden rather than failing."""
    module = _load_aws_script("component_key_access.py")

    result = module._probe_network_device_access()

    assert result["passed"] is True
    assert result["provider_hidden"] is True


def test_my_isv_demo_component_key_access_emits_contract() -> None:
    """Demo mode emits the AUTH03 JSON contract for make demo-test."""
    import os

    script = MY_ISV_VM_SCRIPTS / "component_key_access.py"
    env = os.environ.copy()
    env["ISVCTL_DEMO_MODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--instance-id",
            "dummy-vm-0001",
            "--key-file",
            "/tmp/dummy-key.pem",
            "--key-name",
            "isv-test-gpu",
            "--region",
            "my-isv-region-1",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["success"] is True
    assert payload["key_name"] == "isv-test-gpu"
    assert payload["tests"]["sol_access"]["passed"] is True
    assert payload["tests"]["network_device_access"]["passed"] is True
