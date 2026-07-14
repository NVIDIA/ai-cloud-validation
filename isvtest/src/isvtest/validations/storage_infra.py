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

"""Storage-infrastructure observability validations (STG03 / STG04).

Provider-agnostic checks for NICo bare-metal storage controller requirements
that are observable through the machine REST API without a live SDS backend.
"""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation


def _host_label(host: dict[str, Any]) -> str:
    """Human-facing identifier for a host record."""
    return host.get("host_id") or "unknown"


class StableStorageNodeIpCheck(BaseValidation):
    """Validate stable admin IP assignment is queryable per host (STG03-01).

    Asserts every host reports at least one non-empty IP on its primary admin
    interface (``machineInterfaces[].ipAddresses`` on the primary interface, or
    the first interface when no primary is flagged). This is the provider-neutral
    signal that static IP assignments are visible and stable across lifecycle
    operations.

    Config:
        step_output: Step output containing per-host stable IP records.
        min_hosts: Minimum number of hosts expected (default: 1).
        storage_only: When true, only hosts whose ``hw_sku_device_type`` is
            ``storage`` are in scope (default: false -- all ingested hosts).

    Step output (from query_stable_ips.py):
        success: bool
        platform: str
        site_id: str
        hosts_checked: int
        hosts: list[dict]:
            host_id: str
            hw_sku_device_type: str
            primary_ip_addresses: list[str]
    """

    description: ClassVar[str] = "Check stable admin IP assignment is queryable for storage nodes"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate each in-scope host exposes at least one stable admin IP."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"Stable IP query step failed: {step_output.get('error', 'Unknown error')}")
            return

        hosts = step_output.get("hosts")
        if not isinstance(hosts, list):
            self.set_failed("Stable IP step output is missing the 'hosts' list")
            return

        min_hosts = self._parse_positive_int("min_hosts", default=1)
        if min_hosts is None:
            return

        storage_only = self.config.get("storage_only", False)
        scoped = [h for h in hosts if not storage_only or (h.get("hw_sku_device_type") or "").lower() == "storage"]
        if len(scoped) < min_hosts:
            scope = "storage " if storage_only else ""
            self.set_failed(f"Expected at least {min_hosts} {scope}host(s) with stable IP data, got {len(scoped)}")
            return

        missing: list[str] = []
        for host in scoped:
            label = _host_label(host)
            ips = [ip for ip in (host.get("primary_ip_addresses") or []) if ip]
            if ips:
                self.report_subtest(
                    f"stable_ip_{label}",
                    passed=True,
                    message=f"{label}: admin IP(s) {', '.join(ips)}",
                )
            else:
                missing.append(label)
                self.report_subtest(
                    f"stable_ip_{label}",
                    passed=False,
                    message=f"{label}: no stable admin IP reported on machineInterfaces",
                )

        if missing:
            sample = ", ".join(missing[:3])
            more = len(missing) - min(len(missing), 3)
            summary = f"{sample} (+{more} more)" if more else sample
            self.set_failed(f"{len(missing)}/{len(scoped)} host(s) missing stable admin IPs: {summary}")
            return

        self.set_passed(f"Stable admin IPs queryable for {len(scoped)} host(s)")


class OobFailureDetectionCheck(BaseValidation):
    """Validate out-of-band failure detection is observable per host (STG04-01).

    Asserts the per-host health API exposes BMC/out-of-band probes and that the
    STG04 failure classes (device, network, memory, drive) are observable through
    those probes. By default the check requires ``BmcSensor`` (the baseline BMC
    path) and the ``device`` category to be observable.

    Config:
        step_output: Step output containing per-host OOB health records.
        min_hosts: Minimum number of hosts expected (default: 1).
        require_oob_report: Whether each host must return an OOB health report
            (default: true).
        require_bmc_probes: Probe IDs that must be present (default:
            ``["BmcSensor"]``).
        require_failure_categories: Categories that must be observable per host
            (default: ``["device"]`` -- the minimum BMC sensor surface).

    Step output (from query_oob_health.py):
        success: bool
        platform: str
        site_id: str
        hosts_checked: int
        hosts: list[dict]:
            host_id: str
            oob_health_present: bool
            bmc_probe_ids: list[str]
            failure_categories: dict[str, dict]:
                <category>: {observable: bool, probe_ids: list[str]}
    """

    description: ClassVar[str] = "Check out-of-band failure detection is observable via BMC health probes"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate each host exposes OOB probes covering the required failure classes."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"OOB health query step failed: {step_output.get('error', 'Unknown error')}")
            return

        hosts = step_output.get("hosts")
        if not isinstance(hosts, list):
            self.set_failed("OOB health step output is missing the 'hosts' list")
            return

        min_hosts = self._parse_positive_int("min_hosts", default=1)
        if min_hosts is None:
            return

        if len(hosts) < min_hosts:
            self.set_failed(f"Expected at least {min_hosts} host(s) with OOB health data, got {len(hosts)}")
            return

        require_report = self.config.get("require_oob_report", True)
        require_bmc_probes = self.config.get("require_bmc_probes", ["BmcSensor"])
        required_categories = self.config.get("require_failure_categories", ["device"])

        failed: dict[str, str] = {}

        for host in hosts:
            label = _host_label(host)

            if require_report and not host.get("oob_health_present"):
                self.report_subtest(
                    f"oob_report_{label}",
                    passed=False,
                    message=f"{label}: OOB health API returned no BMC report",
                )
                failed.setdefault(label, "no OOB report")
                continue
            self.report_subtest(
                f"oob_report_{label}",
                passed=True,
                message=f"{label}: OOB health report present",
            )

            present = set(host.get("bmc_probe_ids") or [])
            missing_probes = [probe for probe in require_bmc_probes if probe not in present]
            if missing_probes:
                self.report_subtest(
                    f"oob_probes_{label}",
                    passed=False,
                    message=f"{label}: missing required BMC probe(s): {', '.join(missing_probes)}",
                )
                failed.setdefault(label, f"missing probes {', '.join(missing_probes)}")
            else:
                self.report_subtest(
                    f"oob_probes_{label}",
                    passed=True,
                    message=f"{label}: required BMC probe(s) present: {', '.join(require_bmc_probes)}",
                )

            categories = host.get("failure_categories") or {}
            observable = [
                name for name, data in categories.items() if isinstance(data, dict) and data.get("observable")
            ]
            missing_categories = [name for name in required_categories if name not in observable]
            if missing_categories:
                self.report_subtest(
                    f"oob_categories_{label}",
                    passed=False,
                    message=f"{label}: missing observable failure categories: {', '.join(missing_categories)}",
                )
                failed.setdefault(label, f"missing categories {', '.join(missing_categories)}")
            else:
                self.report_subtest(
                    f"oob_categories_{label}",
                    passed=True,
                    message=f"{label}: {len(observable)} failure categories observable ({', '.join(observable)})",
                )

        total = len(hosts)
        if failed:
            failed_desc = ", ".join(f"{lbl} ({reason})" for lbl, reason in failed.items())
            self.set_failed(f"OOB failure-detection gaps on {len(failed)}/{total} host(s): {failed_desc}")
            return

        self.set_passed(f"Out-of-band failure detection observable for all {total} host(s)")
