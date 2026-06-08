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

"""Tests for the unified health API validations (CAP05-01, CAP05-02)."""

from __future__ import annotations

from typing import Any

from isvtest.validations.health import HealthAggregationCheck, HostHealthCheck

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _category(
    *,
    present: bool = True,
    healthy: bool = True,
    probes: list[str] | None = None,
    alerts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a single per-category health summary."""
    return {
        "present": present,
        "healthy": healthy,
        "probes": probes if probes is not None else (["probe"] if present else []),
        "alerts": alerts or [],
    }


def _host(
    *,
    host_id: str = "m-001",
    status: str = "Ready",
    observed_age_seconds: int | None = 10,
    categories: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a per-host health record."""
    if categories is None:
        categories = {
            "gpu": _category(),
            "thermal": _category(),
            "memory": _category(),
        }
    return {
        "host_id": host_id,
        "chassis_serial": f"SER-{host_id}",
        "status": status,
        "observed_age_seconds": observed_age_seconds,
        "categories": categories,
    }


def _host_health_output(
    *,
    success: bool = True,
    hosts: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a per-host health step output."""
    if hosts is None:
        hosts = [_host()]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "hosts_checked": len(hosts),
        "hosts": hosts,
        "error": error,
    }


def _group(
    *,
    name: str = "it-1",
    total: int = 4,
    healthy: int = 4,
    unhealthy: int = 0,
    status: str | None = None,
    unhealthy_hosts: list[str] | None = None,
) -> dict[str, Any]:
    """Build an aggregation group record."""
    return {
        "group_id": name,
        "group_type": "instance_type",
        "name": name,
        "total": total,
        "healthy": healthy,
        "unhealthy": unhealthy,
        "status": status if status is not None else ("Healthy" if unhealthy == 0 else "Degraded"),
        "unhealthy_hosts": unhealthy_hosts or [],
    }


def _aggregation_output(
    *,
    success: bool = True,
    aggregation_level: str = "nodegroup",
    groups: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a health aggregation step output."""
    if groups is None:
        groups = [_group()]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "aggregation_level": aggregation_level,
        "groups": groups,
        "error": error,
    }


# ===========================================================================
# HostHealthCheck tests (CAP05-01)
# ===========================================================================


class TestHostHealthCheck:
    """Tests for HostHealthCheck validation."""

    def test_all_categories_present_and_healthy(self) -> None:
        """A host that surfaces healthy GPU/thermal/memory health passes."""
        check = HostHealthCheck(config={"step_output": _host_health_output()})
        check.run()
        assert check._passed is True, check._error
        subtests = [r for r in check._subtest_results if r["name"].startswith("host_m-001_")]
        assert len(subtests) == 3
        assert all(r["passed"] for r in subtests)

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = HostHealthCheck(config={"step_output": _host_health_output(success=False, error="API timeout")})
        check.run()
        assert check._passed is False
        assert "API timeout" in check._error

    def test_no_hosts(self) -> None:
        """An empty host list fails -- nothing was validated."""
        check = HostHealthCheck(config={"step_output": _host_health_output(hosts=[])})
        check.run()
        assert check._passed is False
        assert "No hosts" in check._error

    def test_missing_required_category_fails(self) -> None:
        """A host missing the memory signal fails coverage."""
        categories = {"gpu": _category(), "thermal": _category(), "memory": _category(present=False)}
        check = HostHealthCheck(config={"step_output": _host_health_output(hosts=[_host(categories=categories)])})
        check.run()
        assert check._passed is False
        assert "missing memory" in check._error
        mem = next(r for r in check._subtest_results if r["name"] == "host_m-001_memory")
        assert mem["passed"] is False

    def test_missing_category_allowed_when_require_present_false(self) -> None:
        """With require_present disabled, a missing category is skipped, not failed."""
        categories = {"gpu": _category(), "thermal": _category(), "memory": _category(present=False)}
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[_host(categories=categories)]),
                "require_present": False,
            }
        )
        check.run()
        assert check._passed is True, check._error
        mem = next(r for r in check._subtest_results if r["name"] == "host_m-001_memory")
        assert mem["skipped"] is True

    def test_alerting_category_fails(self) -> None:
        """A present-but-alerting category fails and names the alert."""
        categories = {
            "gpu": _category(),
            "thermal": _category(healthy=False, probes=[], alerts=[{"id": "Temperature", "message": "overtemp"}]),
            "memory": _category(),
        }
        check = HostHealthCheck(config={"step_output": _host_health_output(hosts=[_host(categories=categories)])})
        check.run()
        assert check._passed is False
        assert "thermal unhealthy" in check._error
        thermal = next(r for r in check._subtest_results if r["name"] == "host_m-001_thermal")
        assert thermal["passed"] is False
        assert "overtemp" in thermal["message"]

    def test_required_categories_override(self) -> None:
        """Only the configured categories are required."""
        categories = {"gpu": _category(), "thermal": _category(present=False), "memory": _category(present=False)}
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[_host(categories=categories)]),
                "required_categories": ["gpu"],
            }
        )
        check.run()
        assert check._passed is True, check._error
        names = {r["name"] for r in check._subtest_results}
        assert names == {"host_m-001_gpu"}

    def test_freshness_stale_fails(self) -> None:
        """An observation older than max_observation_age_seconds fails."""
        host = _host(observed_age_seconds=600)
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[host]),
                "max_observation_age_seconds": 300,
            }
        )
        check.run()
        assert check._passed is False
        assert "stale" in check._error
        freshness = next(r for r in check._subtest_results if r["name"] == "host_m-001_freshness")
        assert freshness["passed"] is False

    def test_freshness_fresh_passes(self) -> None:
        """A recent observation passes the freshness subtest."""
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[_host(observed_age_seconds=30)]),
                "max_observation_age_seconds": 300,
            }
        )
        check.run()
        assert check._passed is True, check._error
        freshness = next(r for r in check._subtest_results if r["name"] == "host_m-001_freshness")
        assert freshness["passed"] is True

    def test_freshness_missing_timestamp_fails(self) -> None:
        """When freshness is enforced, a missing timestamp is a failure."""
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[_host(observed_age_seconds=None)]),
                "max_observation_age_seconds": 300,
            }
        )
        check.run()
        assert check._passed is False
        assert "no observation timestamp" in check._error

    def test_freshness_not_enforced_by_default(self) -> None:
        """Without max_observation_age_seconds, a null timestamp does not fail."""
        check = HostHealthCheck(config={"step_output": _host_health_output(hosts=[_host(observed_age_seconds=None)])})
        check.run()
        assert check._passed is True, check._error
        assert not any(r["name"].endswith("_freshness") for r in check._subtest_results)


# ===========================================================================
# HealthAggregationCheck tests (CAP05-02)
# ===========================================================================


class TestHealthAggregationCheck:
    """Tests for HealthAggregationCheck validation."""

    def test_consistent_groups_pass(self) -> None:
        """Internally consistent, fully healthy groups pass."""
        check = HealthAggregationCheck(config={"step_output": _aggregation_output()})
        check.run()
        assert check._passed is True, check._error
        assert "nodegroup-level group" in check._output

    def test_degraded_group_passes_but_is_reported(self) -> None:
        """A degraded group is reported in the summary but is not fatal by default."""
        groups = [_group(total=4, healthy=3, unhealthy=1, unhealthy_hosts=["m-x"])]
        check = HealthAggregationCheck(config={"step_output": _aggregation_output(groups=groups)})
        check.run()
        assert check._passed is True, check._error
        assert "degraded" in check._output

    def test_require_all_healthy_fails_on_degraded(self) -> None:
        """With require_all_healthy, any degraded group fails the check."""
        groups = [_group(total=4, healthy=3, unhealthy=1, unhealthy_hosts=["m-x"])]
        check = HealthAggregationCheck(
            config={"step_output": _aggregation_output(groups=groups), "require_all_healthy": True}
        )
        check.run()
        assert check._passed is False
        assert "degraded" in check._error

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = HealthAggregationCheck(config={"step_output": _aggregation_output(success=False, error="API down")})
        check.run()
        assert check._passed is False
        assert "API down" in check._error

    def test_missing_aggregation_level(self) -> None:
        """A missing aggregation_level field fails."""
        output = _aggregation_output()
        output["aggregation_level"] = ""
        check = HealthAggregationCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "aggregation_level" in check._error

    def test_missing_groups_list(self) -> None:
        """A non-list groups field fails."""
        output = _aggregation_output()
        output["groups"] = None
        check = HealthAggregationCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "groups" in check._error

    def test_min_groups_not_met(self) -> None:
        """Fewer groups than min_groups fails."""
        check = HealthAggregationCheck(config={"step_output": _aggregation_output(groups=[]), "min_groups": 1})
        check.run()
        assert check._passed is False
        assert "at least 1" in check._error

    def test_inconsistent_counts_fail(self) -> None:
        """Counts that do not reconcile fail the group subtest."""
        groups = [_group(total=4, healthy=2, unhealthy=1, status="Degraded")]
        check = HealthAggregationCheck(config={"step_output": _aggregation_output(groups=groups)})
        check.run()
        assert check._passed is False
        assert "inconsistent" in check._error

    def test_status_mismatch_fails(self) -> None:
        """A status that disagrees with the counts fails."""
        groups = [_group(total=4, healthy=4, unhealthy=0, status="Degraded")]
        check = HealthAggregationCheck(config={"step_output": _aggregation_output(groups=groups)})
        check.run()
        assert check._passed is False
        assert "inconsistent" in check._error

    def test_non_integer_counts_fail(self) -> None:
        """Non-integer counts (e.g. null) are reported as inconsistent."""
        groups = [_group()]
        groups[0]["unhealthy"] = None
        check = HealthAggregationCheck(config={"step_output": _aggregation_output(groups=groups)})
        check.run()
        assert check._passed is False
        assert "inconsistent" in check._error
