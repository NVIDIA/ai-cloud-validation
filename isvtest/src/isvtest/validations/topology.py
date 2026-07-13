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

"""Physical topology / failure-domain observability validations (STG05).

Provider-agnostic checks that assert the platform exposes the physical failure
domain each host belongs to, so a tenant can spread compute or storage across
independent failure domains for physical diversity.
"""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation


def _host_label(host: dict[str, Any]) -> str:
    """Human-facing identifier for a host record."""
    return host.get("host_id") or "unknown"


class FailureDomainObservabilityCheck(BaseValidation):
    """Validate failure-domain topology is observable per host (STG05-01).

    Asserts that every host reports the failure domain (rack / physical
    enclosure) it belongs to, and that the site exposes at least
    ``min_failure_domains`` distinct domains. Topology observability is the
    prerequisite for physical-diversity placement; raise ``min_failure_domains``
    above 1 in a provider config to additionally enforce that a site actually
    spans multiple failure domains.

    Config:
        step_output: Step output containing per-host failure-domain records.
        min_hosts: Minimum number of hosts expected (default: 1).
        min_failure_domains: Minimum distinct failure domains required
            (default: 1 -- observability only).
        require_all_hosts_mapped: Fail if any host lacks a failure domain
            (default: true).

    Step output (from query_topology.py):
        success: bool
        platform: str
        site_id: str
        hosts_checked: int
        hosts: list[dict]:
            host_id: str
            failure_domain: str -- rack/enclosure identifier ("" if unknown)
    """

    description: ClassVar[str] = "Check failure-domain topology is observable for physical diversity"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate each host reports a failure domain and the site meets the diversity floor."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"Topology query step failed: {step_output.get('error', 'Unknown error')}")
            return

        hosts = step_output.get("hosts")
        if not isinstance(hosts, list):
            self.set_failed("Topology step output is missing the 'hosts' list")
            return

        min_hosts = self._parse_positive_int("min_hosts", default=1)
        min_failure_domains = self._parse_positive_int("min_failure_domains", default=1)
        if min_hosts is None or min_failure_domains is None:
            return

        if len(hosts) < min_hosts:
            self.set_failed(f"Expected at least {min_hosts} host(s) with topology data, got {len(hosts)}")
            return

        require_all_mapped = self.config.get("require_all_hosts_mapped", True)

        unmapped: list[str] = []
        domains: set[str] = set()
        for host in hosts:
            label = _host_label(host)
            domain = host.get("failure_domain") or ""
            if domain:
                domains.add(domain)
                self.report_subtest(f"failure_domain_{label}", passed=True, message=f"{label}: failure domain {domain}")
            else:
                unmapped.append(label)
                self.report_subtest(
                    f"failure_domain_{label}",
                    passed=not require_all_mapped,
                    message=f"{label}: no failure domain reported",
                )

        if require_all_mapped and unmapped:
            sample = ", ".join(unmapped[:3])
            more = len(unmapped) - min(len(unmapped), 3)
            summary = f"{sample} (+{more} more)" if more else sample
            self.set_failed(f"{len(unmapped)}/{len(hosts)} host(s) report no failure domain: {summary}")
            return

        if len(domains) < min_failure_domains:
            self.set_failed(
                f"Site exposes {len(domains)} failure domain(s), requires at least {min_failure_domains} "
                f"for physical diversity"
            )
            return

        self.set_passed(
            f"Failure-domain topology observable for {len(hosts)} host(s) across {len(domains)} failure domain(s)"
        )
