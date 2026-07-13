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

"""Tests for failure-domain topology observability validations (STG05-01)."""

from __future__ import annotations

from typing import Any

from isvtest.validations.topology import FailureDomainObservabilityCheck


def _host(host_id: str, failure_domain: str) -> dict[str, Any]:
    """Build one per-host topology record."""
    return {"host_id": host_id, "failure_domain": failure_domain}


def _topology_output(
    *,
    success: bool = True,
    hosts: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a minimal topology step output."""
    if hosts is None:
        hosts = [_host("m-1", "rack-A"), _host("m-2", "rack-B")]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "hosts_checked": len(hosts),
        "hosts": hosts,
        "error": error,
    }


class TestFailureDomainObservabilityCheck:
    """Tests for FailureDomainObservabilityCheck (STG05-01)."""

    def test_all_hosts_mapped_passes(self) -> None:
        """Every host reporting a failure domain passes."""
        check = FailureDomainObservabilityCheck(config={"step_output": _topology_output()})
        check.run()
        assert check._passed is True

    def test_single_domain_still_observable(self) -> None:
        """A single failure domain is observable and passes the default floor of 1."""
        hosts = [_host("m-1", "rack-A"), _host("m-2", "rack-A")]
        check = FailureDomainObservabilityCheck(config={"step_output": _topology_output(hosts=hosts)})
        check.run()
        assert check._passed is True

    def test_unmapped_host_fails(self) -> None:
        """A host with no failure domain fails when all hosts must be mapped."""
        hosts = [_host("m-1", "rack-A"), _host("m-2", "")]
        check = FailureDomainObservabilityCheck(config={"step_output": _topology_output(hosts=hosts)})
        check.run()
        assert check._passed is False
        assert "m-2" in check._error

    def test_unmapped_allowed_when_not_required(self) -> None:
        """An unmapped host passes when require_all_hosts_mapped is disabled."""
        hosts = [_host("m-1", "rack-A"), _host("m-2", "")]
        check = FailureDomainObservabilityCheck(
            config={"step_output": _topology_output(hosts=hosts), "require_all_hosts_mapped": False}
        )
        check.run()
        assert check._passed is True

    def test_min_failure_domains_enforced(self) -> None:
        """A site with fewer distinct domains than required fails."""
        hosts = [_host("m-1", "rack-A"), _host("m-2", "rack-A")]
        check = FailureDomainObservabilityCheck(
            config={"step_output": _topology_output(hosts=hosts), "min_failure_domains": 2}
        )
        check.run()
        assert check._passed is False
        assert "physical diversity" in check._error

    def test_min_failure_domains_met(self) -> None:
        """Two distinct domains satisfy a diversity floor of 2."""
        check = FailureDomainObservabilityCheck(config={"step_output": _topology_output(), "min_failure_domains": 2})
        check.run()
        assert check._passed is True

    def test_step_failure_fails(self) -> None:
        """A failed step output fails the check."""
        check = FailureDomainObservabilityCheck(config={"step_output": _topology_output(success=False, error="boom")})
        check.run()
        assert check._passed is False
        assert "boom" in check._error

    def test_missing_hosts_list_fails(self) -> None:
        """Step output without a hosts list fails."""
        check = FailureDomainObservabilityCheck(config={"step_output": {"success": True}})
        check.run()
        assert check._passed is False

    def test_min_hosts_enforced(self) -> None:
        """Fewer hosts than min_hosts fails."""
        check = FailureDomainObservabilityCheck(config={"step_output": _topology_output(hosts=[]), "min_hosts": 1})
        check.run()
        assert check._passed is False
