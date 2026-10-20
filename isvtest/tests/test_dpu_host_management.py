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

"""Evidence contract tests for privileged host DPU management denial."""

from typing import Any

import pytest

from isvtest.validations.hardware import DpuHostManagementBlockedCheck


def _probe() -> dict[str, Any]:
    """Return one observed host-root denial record."""
    return {
        "executed": True,
        "execution_context": "host",
        "effective_uid": 0,
        "outcome": "denied",
        "denial_evidence": "DPU management policy: host access denied",
    }


def _check(output: Any, required: Any = None) -> DpuHostManagementBlockedCheck:
    """Run the validator with an independently configured probe inventory."""
    check = DpuHostManagementBlockedCheck(
        config={
            "required_probes": ["host0/dpu0/config", "host0/dpu0/firmware"] if required is None else required,
            "step_output": output,
        }
    )
    check.run()
    return check


def _output() -> dict[str, Any]:
    """Return complete evidence for two management paths."""
    return {"success": True, "probes": {"host0/dpu0/config": _probe(), "host0/dpu0/firmware": _probe()}}


def test_every_required_probe_denied() -> None:
    """Both independently configured paths must pass and be reported."""
    check = _check(_output())
    assert check.passed
    assert len(check._subtest_results) == 2


@pytest.mark.parametrize("required", [[], "config", {}, [""], [" "], [" config"], [1], [{}], ["config", "config"]])
def test_invalid_inventory_cannot_pass(required: Any) -> None:
    """An empty, ambiguous, or malformed expected inventory is a configuration failure."""
    check = _check(_output(), required)
    assert not check.passed
    assert "required_probes" in check._error


def test_missing_inventory_cannot_pass() -> None:
    """Observed successes cannot define their own coverage."""
    check = DpuHostManagementBlockedCheck(config={"step_output": _output()})
    check.run()
    assert not check.passed


@pytest.mark.parametrize(
    "output",
    [
        None,
        [],
        {},
        {"success": True, "security_mode": "restricted"},
        {"success": True, "probes": []},
        {"success": True, "probes": {}},
    ],
)
def test_missing_or_mode_only_evidence_fails(output: Any) -> None:
    """Security-mode reporting and absent probes do not prove blocked management."""
    assert not _check(output).passed


@pytest.mark.parametrize("success", [False, "true", 1, None])
def test_step_success_must_be_boolean(success: Any) -> None:
    """Truthy values cannot stand in for a successfully completed probe step."""
    output = _output()
    output["success"] = success
    assert not _check(output).passed


@pytest.mark.parametrize("field,value", [("skipped", True), ("error", "probe collection failed")])
def test_step_error_cannot_pass(field: str, value: Any) -> None:
    """Contradictory top-level success does not mask a collection failure."""
    output = _output()
    output[field] = value
    assert not _check(output).passed


def test_missing_required_path_fails() -> None:
    """One successful path cannot mask a second path that was never probed."""
    output = _output()
    del output["probes"]["host0/dpu0/firmware"]
    check = _check(output)
    assert not check.passed
    assert any(not result["passed"] for result in check._subtest_results)


@pytest.mark.parametrize("field", list(_probe()))
def test_missing_probe_field_fails(field: str) -> None:
    """Every execution and denial evidence field is required."""
    output = _output()
    del output["probes"]["host0/dpu0/firmware"][field]
    assert not _check(output).passed


@pytest.mark.parametrize(
    "field,value",
    [
        ("executed", False),
        ("executed", "true"),
        ("executed", 1),
        ("execution_context", "container"),
        ("execution_context", "dpu"),
        ("effective_uid", 1000),
        ("effective_uid", "0"),
        ("effective_uid", False),
        ("effective_uid", 0.0),
        ("outcome", "allowed"),
        ("outcome", "timeout"),
        ("outcome", "error"),
        ("outcome", "command_not_found"),
        ("outcome", True),
        ("denial_evidence", ""),
        ("denial_evidence", "  "),
        ("denial_evidence", True),
        ("skipped", True),
        ("error", "device unreachable"),
    ],
)
def test_unproven_or_contradictory_denial_fails(field: str, value: Any) -> None:
    """Root in a container, tool failures, and weak evidence cannot count as policy denial."""
    output = _output()
    output["probes"]["host0/dpu0/firmware"][field] = value
    assert not _check(output).passed


@pytest.mark.parametrize("record", [None, [], True, "denied"])
def test_malformed_probe_record_fails(record: Any) -> None:
    """Malformed records produce validation failures, not crashes."""
    output = _output()
    output["probes"]["host0/dpu0/config"] = record
    assert not _check(output).passed


@pytest.mark.parametrize("name", ["", " ", " extra", 1])
def test_malformed_probe_name_fails(name: Any) -> None:
    """Reject ambiguous evidence identifiers."""
    output = _output()
    output["probes"][name] = _probe()
    assert not _check(output).passed


def test_additional_allowed_probe_fails() -> None:
    """An observed management bypass must not be hidden by a smaller required list."""
    output = _output()
    output["probes"]["host1/dpu0/config"] = {**_probe(), "outcome": "allowed"}
    assert not _check(output).passed


def test_multiple_targets_are_checked() -> None:
    """Distinct IDs preserve coverage for the same path on multiple hosts and DPUs."""
    output = _output()
    output["probes"]["host1/dpu1/config"] = _probe()
    check = _check(output, list(output["probes"]))
    assert check.passed
    assert len(check._subtest_results) == 3
