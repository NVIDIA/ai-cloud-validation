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

"""Tests for the tenant-to-provider default-deny validation (SEC30-01)."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.network import SegmentBoundaryDefaultDenyCheck


def _output(**overrides: Any) -> dict[str, Any]:
    """Return a passing step output with ``overrides`` applied."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "network",
        "positive_control": {"protocol": "tcp", "port": 22, "result": "connected"},
        "prohibited_flows": [
            {"protocol": "icmp", "result": "timeout"},
            {"protocol": "tcp", "port": 443, "result": "timeout"},
        ],
    }
    output.update(overrides)
    return output


def _run(step_output: dict[str, Any]) -> dict[str, Any]:
    """Execute the check against ``step_output``."""
    return SegmentBoundaryDefaultDenyCheck(config={"step_output": step_output}).execute()


def test_control_connects_and_prohibited_dropped_passes() -> None:
    """The allowed flow connects and every prohibited flow times out."""
    result = _run(_output())
    assert result["passed"] is True
    assert "tcp/22" in result["output"]
    assert "icmp" in result["output"]
    assert "tcp/443" in result["output"]


def test_step_failure_fails_with_step_error() -> None:
    """A failed step surfaces its own error."""
    result = _run({"success": False, "error": "VPC quota exceeded"})
    assert result["passed"] is False
    assert "VPC quota exceeded" in result["error"]


@pytest.mark.parametrize("control_result", ["timeout", "refused", "error", None])
def test_positive_control_must_connect(control_result: str | None) -> None:
    """Without a working allowed flow, blocked probes prove nothing, so the check fails."""
    control = {"protocol": "tcp", "port": 22, "result": control_result}
    result = _run(_output(positive_control=control))
    assert result["passed"] is False
    assert "Positive control" in result["error"]


def test_missing_positive_control_fails() -> None:
    """A run that never probed an allowed flow fails."""
    output = _output()
    del output["positive_control"]
    result = _run(output)
    assert result["passed"] is False
    assert "positive_control" in result["error"]


@pytest.mark.parametrize("flows", [[], None])
def test_no_prohibited_flows_fails(flows: Any) -> None:
    """Zero prohibited probes is a failure, not a vacuous pass."""
    result = _run(_output(prohibited_flows=flows))
    assert result["passed"] is False
    assert "prohibited_flows" in result["error"]


@pytest.mark.parametrize("flow_result", ["connected", "refused"])
def test_prohibited_flow_reaching_target_fails(flow_result: str) -> None:
    """A refused probe still reached the host, so it is not default-deny."""
    flows = [{"protocol": "icmp", "result": "timeout"}, {"protocol": "tcp", "port": 8080, "result": flow_result}]
    result = _run(_output(prohibited_flows=flows))
    assert result["passed"] is False
    assert "tcp/8080 reached the target" in result["error"]
    assert "icmp" not in result["error"]


@pytest.mark.parametrize("flow_result", ["error", None, "blocked"])
def test_incomplete_probe_fails(flow_result: str | None) -> None:
    """A probe that did not complete cannot count as denied."""
    result = _run(_output(prohibited_flows=[{"protocol": "tcp", "port": 443, "result": flow_result}]))
    assert result["passed"] is False
    assert "did not complete" in result["error"]
