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

"""Tests for governance metrics validations."""

from __future__ import annotations

import copy
from typing import Any

from isvtest.validations.governance import GovernanceMetricsCheck


def _metrics_output(
    *,
    success: bool = True,
    delivered: dict[str, int] | None = None,
    healthy: dict[str, int] | None = None,
    reserved: dict[str, int] | None = None,
    active: dict[str, int] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a governance metrics step output with valid defaults.

    Defaults satisfy the inter-metric invariants (Healthy/Reserved ⊆ Delivered,
    Active ⊆ Reserved) so individual fields can be overridden per test.
    """
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "machine_count": 20,
        "metrics": {
            "delivered": delivered or {"nodes": 20, "gpus": 160},
            "healthy": healthy or {"nodes": 19, "gpus": 152},
            "reserved": reserved or {"nodes": 15, "gpus": 120},
            "active": active or {"nodes": 10, "gpus": 80},
        },
        "error": error,
    }


class TestGovernanceMetricsCheck:
    """Tests for GovernanceMetricsCheck."""

    def test_well_formed_metrics_pass(self) -> None:
        """All four buckets present with consistent counts -- should pass."""
        check = GovernanceMetricsCheck(config={"step_output": _metrics_output()})
        check.run()
        assert check._passed is True
        # One passing subtest per bucket, so callers see the counts.
        bucket_subtests = [r for r in check._subtest_results if r["name"].startswith("metric_")]
        assert {r["name"] for r in bucket_subtests} == {
            "metric_delivered",
            "metric_healthy",
            "metric_reserved",
            "metric_active",
        }
        assert all(r["passed"] for r in bucket_subtests)
        assert "delivered" in check._output

    def test_step_failure_propagates(self) -> None:
        """When the underlying step reports failure the check should fail."""
        check = GovernanceMetricsCheck(
            config={"step_output": _metrics_output(success=False, error="API down")}
        )
        check.run()
        assert check._passed is False
        assert "API down" in check._error

    def test_missing_metrics_object_fails(self) -> None:
        """A step output without a 'metrics' object should fail with a clear message."""
        output = _metrics_output()
        del output["metrics"]
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "missing the 'metrics' object" in check._error

    def test_missing_required_bucket_fails(self) -> None:
        """All four canonical buckets are required."""
        output = _metrics_output()
        del output["metrics"]["active"]
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "missing required buckets" in check._error
        assert "active" in check._error

    def test_missing_resource_field_fails(self) -> None:
        """Each bucket must expose both ``nodes`` and ``gpus``."""
        output = _metrics_output()
        del output["metrics"]["delivered"]["gpus"]
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "delivered" in check._error and "gpus" in check._error

    def test_negative_count_fails(self) -> None:
        """Counts must be non-negative integers."""
        output = _metrics_output(delivered={"nodes": -1, "gpus": 0})
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "delivered.nodes" in check._error

    def test_bool_rejected_as_count(self) -> None:
        """A boolean masquerading as an int should be rejected."""
        output = _metrics_output()
        output["metrics"]["healthy"]["nodes"] = True  # type: ignore[assignment]
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "healthy.nodes" in check._error

    def test_string_value_fails(self) -> None:
        """Non-integer count types should be rejected."""
        output = _metrics_output()
        output["metrics"]["reserved"]["gpus"] = "120"  # type: ignore[assignment]
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "reserved.gpus" in check._error

    def test_healthy_exceeds_delivered_fails(self) -> None:
        """Healthy must be a subset of Delivered."""
        output = _metrics_output(
            delivered={"nodes": 5, "gpus": 40},
            healthy={"nodes": 6, "gpus": 40},
        )
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "healthy nodes" in check._error
        assert "delivered nodes" in check._error

    def test_reserved_exceeds_delivered_fails(self) -> None:
        """Reserved must be a subset of Delivered."""
        output = _metrics_output(
            delivered={"nodes": 5, "gpus": 40},
            reserved={"nodes": 5, "gpus": 48},
        )
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "reserved gpus" in check._error

    def test_active_exceeds_reserved_fails(self) -> None:
        """Active must be a subset of Reserved."""
        output = _metrics_output(
            reserved={"nodes": 3, "gpus": 24},
            active={"nodes": 4, "gpus": 24},
        )
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "active nodes" in check._error
        assert "reserved nodes" in check._error

    def test_min_delivered_thresholds_enforced(self) -> None:
        """Configurable minimum thresholds enforce a delivered fleet floor."""
        output = _metrics_output(delivered={"nodes": 0, "gpus": 0})
        check = GovernanceMetricsCheck(
            config={"step_output": output, "min_delivered_nodes": 1}
        )
        check.run()
        assert check._passed is False
        assert "Delivered nodes 0" in check._error

    def test_min_delivered_thresholds_default_zero(self) -> None:
        """Without overrides, a zero-machine site is still well-formed."""
        zero = {"nodes": 0, "gpus": 0}
        output = _metrics_output(
            delivered=zero, healthy=zero, reserved=zero, active=zero
        )
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is True

    def test_invalid_min_threshold_type_fails(self) -> None:
        """A non-int min threshold should produce an actionable error."""
        check = GovernanceMetricsCheck(
            config={
                "step_output": _metrics_output(),
                "min_delivered_nodes": "many",
            }
        )
        check.run()
        assert check._passed is False
        assert "min_delivered_nodes" in check._error

    def test_bucket_not_an_object_fails(self) -> None:
        """A metric bucket that is not a dict should be rejected up front."""
        output = _metrics_output()
        output["metrics"]["healthy"] = [1, 2, 3]  # type: ignore[assignment]
        check = GovernanceMetricsCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "healthy" in check._error

    def test_empty_step_output_fails(self) -> None:
        """Empty step_output should fail (no success flag)."""
        check = GovernanceMetricsCheck(config={"step_output": {}})
        check.run()
        assert check._passed is False
        assert "step failed" in check._error

    def test_default_step_output_unchanged(self) -> None:
        """The helper should hand back independent dicts so tests don't bleed into each other."""
        # If the default mutates across calls a later test could silently see
        # the previous test's overrides; pin the contract here.
        first = _metrics_output()
        second = _metrics_output()
        assert first is not second
        assert first["metrics"] == second["metrics"]
        # Mutating one must not change the other.
        snapshot = copy.deepcopy(second["metrics"])
        first["metrics"]["delivered"]["nodes"] = 42
        assert second["metrics"] == snapshot
