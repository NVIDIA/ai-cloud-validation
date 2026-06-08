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


def _alert(
    *,
    probe_id: str = "BmcSensor",
    target: str = "",
    message: str = "",
    classifications: list[str] | None = None,
) -> dict[str, Any]:
    """Build a single host health alert record."""
    return {
        "id": probe_id,
        "target": target,
        "message": message,
        "classifications": classifications or [],
    }


def _host(
    *,
    host_id: str = "m-001",
    status: str = "Ready",
    health_present: bool = True,
    observed_age_seconds: int | None = 10,
    probe_ids: list[str] | None = None,
    alerts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a per-host health record."""
    return {
        "host_id": host_id,
        "chassis_serial": f"SER-{host_id}",
        "status": status,
        "health_present": health_present,
        "healthy": not alerts,
        "observed_age_seconds": observed_age_seconds,
        "probe_ids": probe_ids if probe_ids is not None else ["BmcSensor", "BgpDaemonEnabled"],
        "alerts": alerts or [],
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

    def test_healthy_host_passes(self) -> None:
        """A host with a fresh report and no alerts passes."""
        check = HostHealthCheck(config={"step_output": _host_health_output()})
        check.run()
        assert check._passed is True, check._error
        # report + alerts subtests, both passing.
        subtests = [r for r in check._subtest_results if r["name"].startswith("host_m-001_")]
        assert {r["name"] for r in subtests} == {"host_m-001_report", "host_m-001_alerts"}
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

    def test_missing_report_fails(self) -> None:
        """A host the health API returns nothing for fails the baseline check."""
        host = _host(health_present=False, probe_ids=[])
        check = HostHealthCheck(config={"step_output": _host_health_output(hosts=[host])})
        check.run()
        assert check._passed is False
        assert "no health report" in check._error
        report = next(r for r in check._subtest_results if r["name"] == "host_m-001_report")
        assert report["passed"] is False

    def test_any_alert_fails_by_default(self) -> None:
        """By default any alert (regardless of classification) fails the host."""
        host = _host(alerts=[_alert(probe_id="HeartbeatTimeout", message="dpu agent silent")])
        check = HostHealthCheck(config={"step_output": _host_health_output(hosts=[host])})
        check.run()
        assert check._passed is False
        assert "HeartbeatTimeout" in check._error
        alerts_sub = next(r for r in check._subtest_results if r["name"] == "host_m-001_alerts")
        assert alerts_sub["passed"] is False
        assert "HeartbeatTimeout" in alerts_sub["message"]

    def test_leak_detection_alert_fails(self) -> None:
        """A liquid-cooling leak (BmcLeakDetection/Leak) fails the host."""
        host = _host(
            probe_ids=["BmcSensor", "BmcLeakDetection"],
            alerts=[
                _alert(
                    probe_id="BmcLeakDetection",
                    target="RackLeakDetector_1",
                    message="Leak detector reports leak",
                    classifications=["Leak"],
                )
            ],
        )
        check = HostHealthCheck(config={"step_output": _host_health_output(hosts=[host])})
        check.run()
        assert check._passed is False
        alerts_sub = next(r for r in check._subtest_results if r["name"] == "host_m-001_alerts")
        assert alerts_sub["passed"] is False
        assert "BmcLeakDetection" in alerts_sub["message"]
        assert "Leak" in alerts_sub["message"]

    def test_fail_on_classifications_scopes_failures(self) -> None:
        """With fail_on_classifications set, only matching alerts are blocking."""
        host = _host(
            alerts=[
                _alert(probe_id="BmcSensor", message="warn", classifications=["SensorWarning"]),
            ],
        )
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[host]),
                "fail_on_classifications": ["SensorCritical", "SensorFailure", "Leak"],
            }
        )
        check.run()
        assert check._passed is True, check._error
        alerts_sub = next(r for r in check._subtest_results if r["name"] == "host_m-001_alerts")
        assert alerts_sub["passed"] is True

    def test_fail_on_classifications_catches_matching_alert(self) -> None:
        """A matching classification still fails when fail_on_classifications is set."""
        host = _host(
            alerts=[_alert(probe_id="BmcSensor", message="crit", classifications=["SensorCritical"])],
        )
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[host]),
                "fail_on_classifications": ["SensorCritical"],
            }
        )
        check.run()
        assert check._passed is False
        assert "BmcSensor" in check._error

    def test_require_probes_coverage(self) -> None:
        """require_probes enforces that specific probe IDs are present."""
        host = _host(probe_ids=["BgpDaemonEnabled"])
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[host]),
                "require_probes": ["BmcSensor"],
            }
        )
        check.run()
        assert check._passed is False
        assert "missing probes BmcSensor" in check._error
        probes_sub = next(r for r in check._subtest_results if r["name"] == "host_m-001_probes")
        assert probes_sub["passed"] is False

    def test_require_probes_present_passes(self) -> None:
        """require_probes passes when the required probe IDs are present."""
        host = _host(probe_ids=["BmcSensor", "BgpDaemonEnabled"])
        check = HostHealthCheck(
            config={
                "step_output": _host_health_output(hosts=[host]),
                "require_probes": ["BmcSensor"],
            }
        )
        check.run()
        assert check._passed is True, check._error

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
