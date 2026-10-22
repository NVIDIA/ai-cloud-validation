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

"""Regression coverage for SEC30-03 attribution and approval evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from isvtest.validations import AllowRuleApprovalCheck


def evidence() -> dict[str, Any]:
    """Build independently identifiable rule and approval records."""
    rule = {
        "rule_id": "rule-1",
        "policy_id": "policy-1",
        "owner": "network-team",
        "effective": True,
        "action": "allow",
        "scope": {
            "direction": "ingress",
            "source": "cidr:192.0.2.0/24",
            "destination": "policy-1",
            "protocol": "tcp",
            "port_range": [443, 443],
        },
    }
    return {
        "success": True,
        "observed_at": "2026-10-21T12:00:00Z",
        "effective_rule": rule,
        "approval": {
            "record_id": "CHANGE-123",
            "source": "change-management",
            "approver": "reviewer",
            "status": "approved",
            "approved_at": "2026-10-20T12:00:00Z",
            **{key: deepcopy(rule[key]) for key in ("rule_id", "policy_id", "owner", "scope")},
        },
    }


def run(output: Any) -> dict[str, Any]:
    """Execute the pure validator without cloud access."""
    return AllowRuleApprovalCheck(config={"step_output": output}).execute()


def test_matching_approval_passes() -> None:
    """An effective rule has an explicit approval covering its complete scope."""
    assert run(evidence())["passed"] is True


@pytest.mark.parametrize("output", [None, [], {}, {"success": False}, {"success": "true"}, {"success": True}])
def test_incomplete_collection_fails(output: Any) -> None:
    """Missing evidence cannot pass based on a truthy success flag."""
    assert run(output)["passed"] is False


@pytest.mark.parametrize(
    "section,field",
    [("effective_rule", field) for field in ("rule_id", "policy_id", "owner", "scope", "effective", "action")]
    + [
        ("approval", field)
        for field in (
            "rule_id",
            "policy_id",
            "owner",
            "scope",
            "record_id",
            "source",
            "approver",
            "status",
            "approved_at",
        )
    ],
)
def test_missing_evidence_fails(section: str, field: str) -> None:
    """Each identity, audit reference and traffic-scope dimension is required."""
    output = evidence()
    del output[section][field]
    assert run(output)["passed"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("rule_id", "another-rule"),
        ("policy_id", "another-tenant"),
        ("owner", "another-team"),
        ("owner", ""),
        ("record_id", " "),
        ("source", []),
        ("approver", True),
        ("status", "pending"),
        ("status", "rejected"),
        ("status", "revoked"),
        ("approved_at", "2026-10-22T00:00:00Z"),
        ("approved_at", "2026-10-20T00:00:00"),
        ("approved_at", "not-a-time"),
        ("expires_at", "2026-10-21T12:00:00Z"),
        ("expires_at", None),
    ],
)
def test_invalid_approval_fails(field: str, value: Any) -> None:
    """Audit/owner metadata and a non-approved or stale record cannot substitute for approval."""
    output = evidence()
    output["approval"][field] = value
    assert run(output)["passed"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "cidr:0.0.0.0/0"),
        ("destination", "another-policy"),
        ("direction", "egress"),
        ("port_range", [0, 65535]),
        ("protocol", "udp"),
    ],
)
def test_changed_live_scope_fails(field: str, value: Any) -> None:
    """A previously approved rule cannot broaden or change traffic scope unnoticed."""
    output = evidence()
    output["effective_rule"]["scope"][field] = value
    assert run(output)["passed"] is False


@pytest.mark.parametrize("field,value", [("effective", False), ("effective", 1), ("action", "deny"), ("owner", " ")])
def test_non_effective_or_unowned_rule_fails(field: str, value: Any) -> None:
    """A configured but inactive rule is not an effective-rule sample."""
    output = evidence()
    output["effective_rule"][field] = value
    assert run(output)["passed"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("port_range", [True, 443]),
        ("port_range", [443, 80]),
        ("port_range", [0, 65536]),
        ("port_range", "443"),
        ("direction", "both"),
        ("source", ""),
        ("destination", []),
        ("protocol", "6"),
        ("protocol", "unknown"),
        ("extra_constraint", "ignored?"),
    ],
)
def test_matching_malformed_scopes_fail(field: str, value: Any) -> None:
    """Identical malformed scopes must not produce a false positive."""
    output = evidence()
    for section in ("effective_rule", "approval"):
        output[section]["scope"][field] = value
    assert run(output)["passed"] is False


@pytest.mark.parametrize(
    "protocol,dimensions",
    [
        ("udp", {"port_range": [53, 53]}),
        ("all", {}),
        ("47", {}),
        ("icmp", {"icmp_type": 8, "icmp_code": -1}),
        ("icmpv6", {"icmp_type": -1, "icmp_code": -1}),
    ],
)
def test_other_protocols_require_exact_scope(protocol: str, dimensions: dict[str, Any]) -> None:
    """Non-TCP rules retain their protocol-specific match dimensions."""
    output = evidence()
    for section in ("effective_rule", "approval"):
        scope = output[section]["scope"]
        del scope["port_range"]
        scope.update(protocol=protocol, **dimensions)
    assert run(output)["passed"] is True


def test_expiry_after_observation_passes() -> None:
    """A time-limited approval remains usable before expiration."""
    output = evidence()
    output["approval"]["expires_at"] = "2026-10-22T00:00:00Z"
    assert run(output)["passed"] is True


def test_audit_event_alone_fails() -> None:
    """An audit event proves a mutation occurred, not that anyone approved it."""
    output = evidence()
    output["approval"] = {"event": "AuthorizeSecurityGroupIngress", "actor": "network-team"}
    assert run(output)["passed"] is False
