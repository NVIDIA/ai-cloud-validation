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

"""Tests for serial-console retention output contracts."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_COMMON_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "common"
MY_ISV_BM_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "my-isv" / "scripts" / "bare_metal"


def _load_aws_common_script(script_name: str) -> ModuleType:
    """Load an AWS common script as a module for direct helper testing."""
    return _load_script(AWS_COMMON_SCRIPTS / script_name)


def _load_script(script_path: Path) -> ModuleType:
    """Load a provider script as a module for direct constant/helper testing."""
    spec = importlib.util.spec_from_file_location(f"test_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSerialConsoleEc2:
    """Fake EC2 client for serial-console helper tests."""

    def get_serial_console_access_status(self) -> dict[str, bool]:
        """Return enabled serial-console account status."""
        return {"SerialConsoleAccessEnabled": True}

    def get_console_output(self, InstanceId: str, Latest: bool) -> dict[str, str]:
        """Return a current console output sample."""
        return {
            "Output": f"boot output for {InstanceId}",
            "Timestamp": "2026-04-01T00:00:00Z",
        }


def test_aws_serial_console_helper_emits_retention_fields() -> None:
    """AWS helper must include the provider-agnostic retention contract fields."""
    module = _load_aws_common_script("serial_console.py")
    required_days = getattr(module, "SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED")

    result, exit_code = module.run_serial_console_check(FakeSerialConsoleEc2(), "i-abc123", platform="bm")

    assert exit_code == 0
    assert result["success"] is True
    assert result["console_available"] is True
    assert result["serial_access_enabled"] is True
    assert result["console_log_queryable"] is False
    assert result["retention_days_required"] == required_days
    assert result["retention_days_configured"] == 0
    assert result["oldest_queryable_log_age_days"] == 0
    assert result["query_result_count"] == 0
    assert "does not prove one-month log retention" in result["retention_evidence"]


def test_aws_serial_console_helper_does_not_claim_retention_proof() -> None:
    """Current EC2 console output availability is not historical retention proof."""
    module = _load_aws_common_script("serial_console.py")

    result, _ = module.run_serial_console_check(FakeSerialConsoleEc2(), "i-abc123", platform="bm")

    assert result["console_log_queryable"] is False
    assert result["retention_days_configured"] < result["retention_days_required"]
    assert result["oldest_queryable_log_age_days"] < result["retention_days_required"]
    assert result["query_result_count"] == 0


def test_my_isv_bare_metal_demo_emits_passing_retention_evidence() -> None:
    """my-isv demo mode should demonstrate the complete retention contract."""
    script = MY_ISV_BM_SCRIPTS / "serial_console.py"
    module = _load_script(script)
    required_days = getattr(module, "SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED")
    env = os.environ | {"ISVCTL_DEMO_MODE": "1"}

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--instance-id",
            "bm-demo-1",
            "--region",
            "demo-region",
        ],
        capture_output=True,
        env=env,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["success"] is True
    assert result["instance_id"] == "bm-demo-1"
    assert result["console_log_queryable"] is True
    assert result["retention_days_required"] == required_days
    assert result["retention_days_configured"] == required_days
    assert result["oldest_queryable_log_age_days"] == required_days
    assert result["query_result_count"] == 1
    assert result["retention_evidence"] == "demo serial console log archive"
