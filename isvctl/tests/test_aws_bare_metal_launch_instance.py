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

"""Tests for AWS bare-metal launch script safety behavior."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_BM_LAUNCH_SCRIPT = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "bare_metal" / "launch_instance.py"


def _load_launch_script() -> ModuleType:
    """Load the AWS bare-metal launch script as a module."""
    spec = importlib.util.spec_from_file_location("test_aws_bm_launch_instance", AWS_BM_LAUNCH_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeReuseEc2:
    """Fake EC2 client for the existing-instance reuse path."""

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """Return an existing running instance."""
        assert InstanceIds == ["i-reuse"]
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-reuse",
                            "InstanceType": "g4dn.metal",
                            "PublicIpAddress": "203.0.113.20",
                            "PrivateIpAddress": "10.0.0.20",
                            "VpcId": "vpc-old",
                            "SubnetId": "subnet-old",
                            "State": {"Name": "running"},
                            "KeyName": "reuse-key",
                            "Placement": {"AvailabilityZone": "us-west-2a"},
                        }
                    ]
                }
            ]
        }


def test_bare_metal_launch_refuses_reuse_when_explicit_network_requested(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AWS_BM reuse must not override an explicit VPC/subnet launch request."""
    module = _load_launch_script()
    monkeypatch.setenv("AWS_BM_INSTANCE_ID", "i-reuse")
    monkeypatch.setenv("AWS_BM_KEY_FILE", "/tmp/reuse-key.pem")
    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: _FakeReuseEc2())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launch_instance.py",
            "--region",
            "us-west-2",
            "--vpc-id",
            "vpc-new",
            "--subnet-id",
            "subnet-new",
        ],
    )

    exit_code = module.main()

    assert exit_code == 1
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert result["success"] is False
    assert result["instance_id"] == "i-reuse"
    assert "explicit --vpc-id/--subnet-id" in result["error"]
