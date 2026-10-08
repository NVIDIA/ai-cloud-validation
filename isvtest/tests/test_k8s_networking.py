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

"""Reject inventory-only, incomplete and contradictory networking evidence."""

from copy import deepcopy
from typing import Any

import pytest

from isvtest.validations.k8s_networking import K8sCidrRangesCheck, K8sDnsForwardingCheck, K8sLoadBalancerCheck

CHECKS = {
    "load_balancer": K8sLoadBalancerCheck,
    "dns_forwarding": K8sDnsForwardingCheck,
    "cidr_ranges": K8sCidrRangesCheck,
}


def good(name: str) -> dict[str, Any]:
    """Return observed outcomes for each independent probe."""
    details = {
        "load_balancer": {
            "expected_response": "nonce",
            "requested_static_ips": ["1.1.1.1"],
            "services": [
                {
                    "kind": kind,
                    "service_type": "LoadBalancer",
                    "probe_location": "pod" if kind == "internal" else "controller",
                    "probes": [{"address": ip, "body": "nonce"}],
                }
                for kind, ip in (("external", "8.8.8.8"), ("internal", "10.1.2.3"), ("static", "1.1.1.1"))
            ],
        },
        "dns_forwarding": {
            "query": "canary.unique.invalid",
            "expected_addresses": ["198.18.0.42"],
            "answers": ["198.18.0.42"],
            "forwarded_query_seen": True,
            "control_expected": ["10.96.0.1"],
            "control_answers": ["10.96.0.1"],
            "rule_restored": True,
        },
        "cidr_ranges": {
            "requested_ranges": {"service": ["10.96.0.0/16"], "pod": ["10.244.0.0/16"], "node": ["192.168.49.0/24"]},
            "service_cidrs": ["10.96.0.0/16"],
            "node_pod_cidrs": ["10.244.0.0/24"],
            "node_ips": ["192.168.49.2"],
            "pod_ips": ["10.244.0.7"],
            "service_ips": ["10.96.1.7"],
        },
    }
    return {"success": True, "platform": "kubernetes", "test_name": name, **deepcopy(details[name])}


@pytest.mark.parametrize("name", CHECKS)
def test_complete_evidence_passes(name: str) -> None:
    """Require successful runtime proof for each check."""
    check = CHECKS[name](config={"step_output": good(name)})
    check.run()
    assert check.passed, check.message


@pytest.mark.parametrize("name", CHECKS)
def test_missing_probe_skips(name: str) -> None:
    """Standalone runs do not claim success without a configured probe."""
    with pytest.raises(pytest.skip.Exception):
        CHECKS[name](config={}).run()


@pytest.mark.parametrize("name", CHECKS)
def test_missing_component_skips_but_cleanup_failure_does_not(name: str) -> None:
    """A skip cannot hide failure to restore mutated resources."""
    output = {"test_name": name, "success": False, "skipped": True, "skip_reason": "component absent"}
    with pytest.raises(pytest.skip.Exception):
        CHECKS[name](config={"step_output": output}).run()
    output["cleanup_errors"] = ["namespace deletion failed"]
    check = CHECKS[name](config={"step_output": output})
    check.run()
    assert not check.passed


@pytest.mark.parametrize("name", CHECKS)
@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_success_must_be_true(name: str, value: Any) -> None:
    """Truthy strings and numbers are not successful execution evidence."""
    output = good(name)
    output["success"] = value
    check = CHECKS[name](config={"step_output": output})
    check.run()
    assert not check.passed


@pytest.mark.parametrize("name", CHECKS)
def test_each_required_observation_is_necessary(name: str) -> None:
    """A missing individual evidence field cannot pass."""
    for key in good(name):
        if key == "platform":
            continue
        output = good(name)
        output.pop(key)
        check = CHECKS[name](config={"step_output": output})
        check.run()
        assert not check.passed, key


@pytest.mark.parametrize(
    "fault",
    [
        "type",
        "private-public",
        "public-private",
        "loopback",
        "link-local",
        "reserved",
        "response",
        "static",
        "location",
        "empty",
        "duplicate",
    ],
)
def test_load_balancer_false_positives(fault: str) -> None:
    """Service status alone, private 'public' addresses and wrong backend responses fail."""
    output = good("load_balancer")
    external, internal, static = output["services"]
    if fault == "type":
        external["service_type"] = "ClusterIP"
    elif fault == "private-public":
        external["probes"][0]["address"] = "10.1.1.1"
    elif fault == "public-private":
        internal["probes"][0]["address"] = "8.8.8.8"
    elif fault in ("loopback", "link-local", "reserved"):
        internal["probes"][0]["address"] = {
            "loopback": "127.0.0.1",
            "link-local": "169.254.1.1",
            "reserved": "192.0.2.1",
        }[fault]
    elif fault == "response":
        external["probes"][0]["body"] = "another service"
    elif fault == "static":
        static["probes"][0]["address"] = "1.0.0.1"
    elif fault == "location":
        internal["probe_location"] = "controller"
    elif fault == "empty":
        external["probes"] = []
    elif fault == "duplicate":
        static["kind"] = "external"
    check = K8sLoadBalancerCheck(config={"step_output": output})
    check.run()
    assert not check.passed


@pytest.mark.parametrize(
    "field,value",
    [
        ("answers", ["198.18.0.43"]),
        ("control_answers", ["10.96.0.2"]),
        ("forwarded_query_seen", False),
        ("forwarded_query_seen", 1),
        ("rule_restored", False),
        ("rule_restored", "true"),
    ],
)
def test_dns_requires_upstream_and_control_evidence(field: str, value: Any) -> None:
    """A matching record without forwarding or restoration evidence is insufficient."""
    output = good("dns_forwarding")
    output[field] = value
    check = K8sDnsForwardingCheck(config={"step_output": output})
    check.run()
    assert not check.passed


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_cidrs", ["10.97.0.0/16"]),
        ("node_pod_cidrs", ["10.245.0.0/24"]),
        ("pod_ips", ["10.245.0.1"]),
        ("node_ips", ["192.168.50.2"]),
        ("service_ips", ["10.97.0.1"]),
        ("pod_ips", []),
        ("node_pod_cidrs", []),
        ("node_ips", ["not-an-ip"]),
    ],
)
def test_cidr_mismatch_or_empty_allocations_fail(field: str, value: Any) -> None:
    """No passing result from requested ranges alone or from the wrong live allocations."""
    output = good("cidr_ranges")
    output[field] = value
    check = K8sCidrRangesCheck(config={"step_output": output})
    check.run()
    assert not check.passed


def test_dual_stack_ranges_pass() -> None:
    """IPv6 allocations are compared to their matching-family configured ranges."""
    output = good("cidr_ranges")
    for kind, net, ip in (
        ("service", "fd01::/112", "fd01::7"),
        ("pod", "fd02::/64", "fd02::7"),
        ("node", "fd03::/64", "fd03::7"),
    ):
        output["requested_ranges"][kind].append(net)
        output[kind + "_ips"].append(ip)
    output["service_cidrs"].append("fd01::/112")
    output["node_pod_cidrs"].append("fd02::/80")
    check = K8sCidrRangesCheck(config={"step_output": output})
    check.run()
    assert check.passed, check.message
