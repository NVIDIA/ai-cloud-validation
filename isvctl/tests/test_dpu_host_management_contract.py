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

"""Verify the SEC47-02 suite, provider template, and validator agree."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from isvtest.validations.hardware import DpuHostManagementBlockedCheck

from isvctl.config.merger import merge_yaml_files

ROOT = Path(__file__).resolve().parents[2]
PROVIDER = ROOT / "isvctl/configs/providers/my-isv"


@pytest.mark.parametrize("demo", [True, False])
def test_provider_template_contract(demo: bool) -> None:
    """Demo emits complete evidence; normal execution cannot manufacture a pass."""
    config = merge_yaml_files([str(PROVIDER / "config/security.yaml")])
    wiring = config["tests"]["validations"]["dpu_host_management"]["checks"]["DpuHostManagementBlockedCheck"]
    assert wiring["test_id"] == "SEC47-02"
    assert wiring["requires"] == ["bare_metal"]
    steps = config["commands"]["security"]["steps"]
    step = next(item for item in steps if item["name"] == wiring["step"])
    assert step["command"] == "python ../scripts/security/dpu_host_management_test.py"
    env = {**os.environ, "ISVCTL_DEMO_MODE": "1" if demo else "0"}
    result = subprocess.run(
        [sys.executable, str(PROVIDER / "scripts/security/dpu_host_management_test.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output = json.loads(result.stdout)
    check = DpuHostManagementBlockedCheck(config={**wiring, "step_output": output})
    check.run()
    assert check.passed is demo
    assert result.returncode == (0 if demo else 1)
    if demo:
        assert set(output["probes"]) == set(wiring["required_probes"])
    else:
        assert output["probes"] == {}
        assert "Not implemented" in output["error"]
