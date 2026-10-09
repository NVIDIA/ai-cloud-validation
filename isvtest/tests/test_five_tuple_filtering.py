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

"""Tests for the five-tuple traffic filtering validation (SEC15-01)."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.network import FiveTupleFilteringCheck


def _output(**overrides: Any) -> dict[str, Any]:
    """Return a passing step output with ``overrides`` applied."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "network",
        "baseline": {
            "protocol": "tcp",
            "source_ip": "10.85.1.10",
            "destination_ip": "10.85.2.10",
            "source_port": 40000,
            "destination_port": 8443,
            "result": "connected",
        },
        "variants": [
            {"dimension": "protocol", "value": "udp", "result": "timeout"},
            {"dimension": "source_ip", "value": "10.85.1.11", "result": "timeout"},
            {"dimension": "destination_ip", "value": "10.85.2.11", "result": "timeout"},
            {"dimension": "destination_port", "value": 8444, "result": "timeout"},
        ],
    }
    output.update(overrides)
    return output


def _replace_variant(output: dict[str, Any], dimension: str, **fields: Any) -> dict[str, Any]:
    """Return ``output`` with the variant for ``dimension`` updated by ``fields``."""
    for variant in output["variants"]:
        if variant["dimension"] == dimension:
            variant.update(fields)
    return output


def _run(step_output: dict[str, Any], **config: Any) -> dict[str, Any]:
    """Execute the check against ``step_output``."""
    return FiveTupleFilteringCheck(config={"step_output": step_output, **config}).execute()


def test_baseline_connects_and_every_variant_dropped_passes() -> None:
    """The baseline connects and each default dimension has a dropped variant."""
    result = _run(_output())
    assert result["passed"] is True
    for dimension in ("protocol", "source_ip", "destination_ip", "destination_port"):
        assert dimension in result["output"]
    assert "source_port" not in result["output"]


def test_step_failure_fails_with_step_error() -> None:
    """A failed step surfaces its own error."""
    result = _run({"success": False, "error": "VPC quota exceeded"})
    assert result["passed"] is False
    assert "VPC quota exceeded" in result["error"]


@pytest.mark.parametrize("baseline_result", ["timeout", "refused", "error", None])
def test_baseline_must_connect(baseline_result: str | None) -> None:
    """Without a working baseline, dropped variants prove nothing, so the check fails."""
    output = _output()
    output["baseline"]["result"] = baseline_result
    result = _run(output)
    assert result["passed"] is False
    assert "Baseline flow did not connect" in result["error"]


def test_missing_baseline_fails() -> None:
    """A run that never probed an allowed flow fails."""
    output = _output()
    del output["baseline"]
    result = _run(output)
    assert result["passed"] is False
    assert "baseline" in result["error"]


def test_baseline_missing_required_field_fails() -> None:
    """A baseline that omits a required dimension cannot anchor that variant."""
    output = _output()
    del output["baseline"]["destination_ip"]
    result = _run(output)
    assert result["passed"] is False
    assert "missing destination_ip" in result["error"]


@pytest.mark.parametrize("variants", [None, "timeout"])
def test_malformed_variants_fails(variants: Any) -> None:
    """Variants must be a list."""
    result = _run(_output(variants=variants))
    assert result["passed"] is False
    assert "`variants` must be a list" in result["error"]


def test_missing_dimension_variant_fails() -> None:
    """Every required dimension needs its own probe, not a vacuous pass."""
    output = _output()
    output["variants"] = [v for v in output["variants"] if v["dimension"] != "source_ip"]
    result = _run(output)
    assert result["passed"] is False
    assert "no source_ip variant probed" in result["error"]


@pytest.mark.parametrize("variant_result", ["connected", "refused"])
def test_variant_reaching_target_fails(variant_result: str) -> None:
    """A refused variant still reached the host, so that dimension is not filtered."""
    output = _replace_variant(_output(), "destination_port", result=variant_result)
    result = _run(output)
    assert result["passed"] is False
    assert f"destination_port=8444 reached the target ({variant_result})" in result["error"]
    assert "protocol" not in result["error"]


@pytest.mark.parametrize("variant_result", ["error", None, "blocked"])
def test_incomplete_variant_probe_fails(variant_result: str | None) -> None:
    """A probe that did not complete cannot count as filtered."""
    output = _replace_variant(_output(), "protocol", result=variant_result)
    result = _run(output)
    assert result["passed"] is False
    assert "protocol='udp' probe did not complete" in result["error"]


@pytest.mark.parametrize("value", ["10.85.1.10", None, ""])
def test_variant_not_differing_from_baseline_fails(value: str | None) -> None:
    """A variant that does not change its dimension tests nothing about it."""
    output = _replace_variant(_output(), "source_ip", value=value)
    result = _run(output)
    assert result["passed"] is False
    assert "does not differ from the baseline" in result["error"]


def test_every_probe_for_a_dimension_must_be_dropped() -> None:
    """A second variant on the same dimension that gets through fails the check."""
    output = _output()
    output["variants"].append({"dimension": "destination_port", "value": 22, "result": "connected"})
    result = _run(output)
    assert result["passed"] is False
    assert "destination_port=22 reached the target" in result["error"]


def test_source_port_not_required_by_default() -> None:
    """A source port variant that gets through is ignored unless it is required."""
    output = _output()
    output["variants"].append({"dimension": "source_port", "value": 40001, "result": "connected"})
    result = _run(output)
    assert result["passed"] is True


def test_required_source_port_without_variant_fails() -> None:
    """Requiring source port makes its variant mandatory."""
    result = _run(_output(), required_dimensions=["source_port"])
    assert result["passed"] is False
    assert "no source_port variant probed" in result["error"]


def test_required_source_port_dropped_passes() -> None:
    """A dropped source port variant satisfies a source port requirement."""
    output = _output()
    output["variants"].append({"dimension": "source_port", "value": 40001, "result": "timeout"})
    dimensions = ["protocol", "source_ip", "destination_ip", "source_port", "destination_port"]
    result = _run(output, required_dimensions=dimensions)
    assert result["passed"] is True
    assert "source_port" in result["output"]


def test_unknown_required_dimension_fails() -> None:
    """A misspelled dimension in config is an error, not a silently skipped requirement."""
    result = _run(_output(), required_dimensions=["dst_port"])
    assert result["passed"] is False
    assert "Unknown five-tuple dimension(s) in required_dimensions: dst_port" in result["error"]
