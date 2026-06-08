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

"""Unified Health API validations (requirement CAP05).

Two provider-agnostic checks that assert a cloud's health API surfaces the
signals NVIDIA requires:

- ``HostHealthCheck`` (CAP05-01): the per-host health API returns real-time
  GPU state, thermal status, and memory health for every host.
- ``HealthAggregationCheck`` (CAP05-02): health can be rolled up to a
  primitive level (cluster, nodegroup, or reservation) with internally
  consistent counts.

Both validations only inspect provider-neutral JSON produced by a step script,
so any provider that emits the documented fields can reuse them.
"""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation

# Health categories a per-host health API must surface for CAP05-01. The step
# script is responsible for mapping provider-specific probes into these
# categories; the validation only checks the resulting per-category summary.
DEFAULT_REQUIRED_CATEGORIES: tuple[str, ...] = ("gpu", "thermal", "memory")


def _host_label(host: dict[str, Any]) -> str:
    """Human-facing identifier for a host record."""
    return host.get("host_id") or host.get("machine_id") or host.get("chassis_serial") or "unknown"


class HostHealthCheck(BaseValidation):
    """Validate the per-host health API returns a fresh, healthy report.

    NICo (and similar providers) expose host health as an alert-driven report:
    a healthy subsystem is simply the absence of an alert, so a passing probe
    per category is not guaranteed. This check therefore asserts, for every
    host, that the health API returns a report and that any health categories
    it does surface (GPU state, thermal status, memory health) are not
    alerting. Category *coverage* can additionally be required via
    ``require_present`` for providers whose API enumerates every category.

    Config:
        step_output: Step output containing per-host health data.
        required_categories: Categories to evaluate (default: ["gpu", "thermal",
            "memory"]). A category that is present must be healthy; a missing
            one only fails when ``require_present`` is true.
        require_report: Whether each host must return a health report at all
            (default: true) -- the baseline "the per-host health API works"
            assertion.
        require_present: Whether each required category must expose at least one
            probe (default: false). Enable for providers whose health API
            enumerates GPU/thermal/memory coverage explicitly.
        max_observation_age_seconds: When set, each host's health observation
            must be no older than this many seconds (default: None = freshness
            not enforced).

    Step output (from query_host_health.py):
        success: bool
        platform: str
        site_id: str
        hosts_checked: int
        hosts: list[dict]:
            host_id: str
            chassis_serial: str -- debug aid only, may be empty
            status: str
            health_present: bool -- the API returned any health data for the host
            observed_age_seconds: int | None -- age of the health observation
            categories: dict[str, dict]:
                <category>:
                    present: bool -- the API surfaced at least one probe
                    healthy: bool -- no alerts in this category
                    probes: list[str] -- probe IDs contributing to this category
                    alerts: list[dict] -- alerting probes ({id, message})
    """

    description: ClassVar[str] = "Check per-host health API returns a fresh, healthy report"
    timeout: ClassVar[int] = 120
    labels: ClassVar[tuple[str, ...]] = ("bare_metal", "health")

    def run(self) -> None:
        """Validate per-host report presence, category health, coverage, and freshness."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"Host health step failed: {step_output.get('error', 'Unknown error')}")
            return

        hosts = step_output.get("hosts", [])
        if not hosts:
            self.set_failed("No hosts found in step output")
            return

        required_categories = self.config.get("required_categories", list(DEFAULT_REQUIRED_CATEGORIES))
        require_report = self.config.get("require_report", True)
        require_present = self.config.get("require_present", False)
        max_age = self.config.get("max_observation_age_seconds")

        # Maps each failing host label to a short reason for the summary line.
        failed: dict[str, str] = {}

        for host in hosts:
            label = _host_label(host)
            categories = host.get("categories") or {}

            # Baseline: the per-host health API must return a report at all.
            if require_report:
                if not host.get("health_present"):
                    self.report_subtest(
                        f"host_{label}_report",
                        passed=False,
                        message=f"Host {label}: health API returned no report",
                    )
                    failed.setdefault(label, "no health report")
                    # No report means there is nothing further to evaluate.
                    continue
                self.report_subtest(
                    f"host_{label}_report",
                    passed=True,
                    message=f"Host {label}: health report returned",
                )

            for category in required_categories:
                summary = categories.get(category) or {}
                present = bool(summary.get("present"))
                healthy = bool(summary.get("healthy"))
                probes = summary.get("probes") or []
                alerts = summary.get("alerts") or []

                if not present:
                    if require_present:
                        self.report_subtest(
                            f"host_{label}_{category}",
                            passed=False,
                            message=f"Host {label}: no {category} health signal returned by the API",
                        )
                        failed.setdefault(label, f"missing {category}")
                    else:
                        self.report_subtest(
                            f"host_{label}_{category}",
                            passed=True,
                            skipped=True,
                            message=f"Host {label}: no {category} signal returned (coverage not enforced)",
                        )
                    continue

                if not healthy:
                    alert_msgs = (
                        "; ".join(f"{a.get('id', '?')}: {a.get('message', '?')}" for a in alerts)
                        if alerts
                        else "alerting (no detail)"
                    )
                    self.report_subtest(
                        f"host_{label}_{category}",
                        passed=False,
                        message=f"Host {label}: {category} unhealthy -- {alert_msgs}",
                    )
                    failed.setdefault(label, f"{category} unhealthy")
                else:
                    self.report_subtest(
                        f"host_{label}_{category}",
                        passed=True,
                        message=f"Host {label}: {category} healthy ({len(probes)} probe(s))",
                    )

            if max_age is not None:
                age = host.get("observed_age_seconds")
                if age is None:
                    self.report_subtest(
                        f"host_{label}_freshness",
                        passed=False,
                        message=f"Host {label}: health API returned no observation timestamp",
                    )
                    failed.setdefault(label, "no observation timestamp")
                elif age > max_age:
                    self.report_subtest(
                        f"host_{label}_freshness",
                        passed=False,
                        message=f"Host {label}: health observed {age}s ago, exceeds max {max_age}s",
                    )
                    failed.setdefault(label, f"stale ({age}s)")
                else:
                    self.report_subtest(
                        f"host_{label}_freshness",
                        passed=True,
                        message=f"Host {label}: health observed {age}s ago",
                    )

        total = len(hosts)
        if failed:
            failed_desc = ", ".join(f"{lbl} ({reason})" for lbl, reason in failed.items())
            self.set_failed(f"Host health issues on {len(failed)}/{total} host(s): {failed_desc}")
        else:
            self.set_passed(f"All {total} host(s) return a healthy report via the per-host health API")


class HealthAggregationCheck(BaseValidation):
    """Validate primitive-level health aggregation (cluster/nodegroup/reservation).

    Asserts that the health API can roll host health up to a higher-level
    primitive and that each group's counts are internally consistent
    (``healthy + unhealthy == total``, non-negative) and that the reported
    aggregate status agrees with the counts. Optionally requires every group
    to be fully healthy.

    Config:
        step_output: Step output containing aggregated health groups.
        min_groups: Minimum number of groups expected (default: 1).
        require_all_healthy: Whether every group must be fully healthy
            (default: false -- a degraded group is reported but not fatal).

    Step output (from query_health_aggregation.py):
        success: bool
        platform: str
        site_id: str
        aggregation_level: str -- e.g. "nodegroup" | "reservation" | "cluster"
        groups: list[dict]:
            group_id: str
            group_type: str
            name: str
            total: int
            healthy: int
            unhealthy: int
            status: str -- "Healthy" when unhealthy == 0, else "Degraded"
            unhealthy_hosts: list[str]
    """

    description: ClassVar[str] = "Check primitive-level health aggregation is exposed and consistent"
    timeout: ClassVar[int] = 120
    labels: ClassVar[tuple[str, ...]] = ("bare_metal", "health")

    def run(self) -> None:
        """Validate aggregation presence, per-group count consistency, and status."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"Health aggregation step failed: {step_output.get('error', 'Unknown error')}")
            return

        level = step_output.get("aggregation_level")
        if not level:
            self.set_failed("Health aggregation step output is missing 'aggregation_level'")
            return

        groups = step_output.get("groups")
        if not isinstance(groups, list):
            self.set_failed("Health aggregation step output is missing the 'groups' list")
            return

        min_groups = self._parse_positive_int("min_groups", default=1)
        if min_groups is None:
            return

        if len(groups) < min_groups:
            self.set_failed(f"Expected at least {min_groups} aggregation group(s), got {len(groups)}")
            return

        require_all_healthy = self.config.get("require_all_healthy", False)

        inconsistent: list[str] = []
        degraded: list[str] = []

        for group in groups:
            name = group.get("name") or group.get("group_id") or "unknown"
            total = group.get("total")
            healthy = group.get("healthy")
            unhealthy = group.get("unhealthy")
            status = group.get("status")

            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (total, healthy, unhealthy)):
                self.report_subtest(
                    f"group_{name}_counts",
                    passed=False,
                    message=f"Group {name}: non-integer counts (total={total}, healthy={healthy}, unhealthy={unhealthy})",
                )
                inconsistent.append(name)
                continue

            counts_ok = healthy >= 0 and unhealthy >= 0 and total >= 0 and (healthy + unhealthy == total)
            expected_status = "Healthy" if unhealthy == 0 else "Degraded"
            status_ok = status == expected_status

            if not counts_ok or not status_ok:
                problems = []
                if not counts_ok:
                    problems.append(
                        f"counts don't reconcile (healthy {healthy} + unhealthy {unhealthy} != total {total})"
                    )
                if not status_ok:
                    problems.append(f"status {status!r} != expected {expected_status!r}")
                self.report_subtest(
                    f"group_{name}_counts",
                    passed=False,
                    message=f"Group {name}: {'; '.join(problems)}",
                )
                inconsistent.append(name)
                continue

            self.report_subtest(
                f"group_{name}_counts",
                passed=True,
                message=f"Group {name}: {healthy}/{total} healthy ({status})",
            )

            if unhealthy > 0:
                degraded.append(f"{name} ({unhealthy}/{total} unhealthy)")

        if inconsistent:
            self.set_failed(
                f"Health aggregation is inconsistent for {len(inconsistent)}/{len(groups)} group(s): "
                f"{', '.join(inconsistent)}"
            )
            return

        if require_all_healthy and degraded:
            self.set_failed(f"{len(degraded)} aggregation group(s) degraded: {', '.join(degraded)}")
            return

        summary = f"{len(groups)} {level}-level group(s) aggregated consistently"
        if degraded:
            summary += f"; {len(degraded)} degraded: {', '.join(degraded)}"
        self.set_passed(summary)
