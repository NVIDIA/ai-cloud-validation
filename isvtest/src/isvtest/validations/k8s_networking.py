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

"""Validate observed LoadBalancer, DNS forwarding and CIDR allocation evidence."""

import ipaddress
from typing import Any, ClassVar

import pytest

from isvtest.core.validation import BaseValidation


def evidence(check: BaseValidation, name: str) -> dict[str, Any] | None:
    """Handle prerequisite skips without accepting incomplete or failed probes."""
    output = check.config.get("step_output")
    if output is None:
        pytest.skip(f"{name} requires a configured networking probe")
    if not isinstance(output, dict) or output.get("test_name") != name:
        check.set_failed(f"Invalid {name} evidence")
    elif output.get("cleanup_errors"):
        check.set_failed(f"Networking cleanup failed: {output['cleanup_errors']}")
    elif output.get("skipped") is True:
        reason = output.get("skip_reason")
        if output.get("success") is False and isinstance(reason, str) and reason.strip():
            pytest.skip(reason)
        check.set_failed("Invalid prerequisite skip report")
    elif output.get("success") is not True:
        check.set_failed(str(output.get("error") or "Networking probe failed"))
    else:
        return output
    return None


def addresses(values: Any) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Require nonempty lists of literal IP addresses."""
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise ValueError("Expected a nonempty list of IP addresses")
    return {ipaddress.ip_address(value) for value in values}


def networks(values: Any) -> set[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Require canonical, nonempty configured CIDR ranges."""
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise ValueError("Expected a nonempty list of CIDR ranges")
    return {ipaddress.ip_network(value) for value in values}


def private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Recognize RFC1918 and IPv6 unique-local addresses, excluding loopback/link-local."""
    return any(
        address in ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    )


class K8sLoadBalancerCheck(BaseValidation):
    """Require actual backend responses through public, private and static-IP services."""

    description: ClassVar[str] = "Verify public/private LoadBalancer reachability and static IP assignment."

    def run(self) -> None:
        """Grade every observed endpoint, not just Service status or annotations."""
        output = evidence(self, "load_balancer")
        if output is None:
            return
        try:
            expected = output["expected_response"]
            if not isinstance(expected, str) or not expected:
                raise ValueError("Missing unique backend response")
            services = output["services"]
            if not isinstance(services, list) or len(services) != 3:
                raise ValueError("Public, private and static Service evidence is required")
            if {s["kind"] for s in services} != {"external", "internal", "static"}:
                raise ValueError("Missing or duplicated Service modes")
            for service in services:
                mode = service["kind"]
                if service["service_type"] != "LoadBalancer":
                    raise ValueError(f"{mode}: not a LoadBalancer Service")
                location = "pod" if mode == "internal" else "controller"
                if service["probe_location"] != location:
                    raise ValueError(f"{mode}: wrong probe location")
                probes = service["probes"]
                ips = addresses([p["address"] for p in probes])
                if any(p["body"] != expected for p in probes):
                    raise ValueError(f"{mode}: backend response mismatch")
                if mode == "internal":
                    if not all(private(ip) for ip in ips):
                        raise ValueError("Internal endpoints must have private addresses")
                elif not all(ip.is_global for ip in ips):
                    raise ValueError(f"{mode}: endpoints are not publicly routable")
                if mode == "static" and ips != addresses(output["requested_static_ips"]):
                    raise ValueError("Assigned static addresses differ from requested addresses")
        except (KeyError, TypeError, ValueError) as error:
            self.set_failed(str(error))
            return
        self.set_passed("Public/private backend requests and exact static IP assignment verified")


class K8sDnsForwardingCheck(BaseValidation):
    """Require a zone-specific answer and evidence from the designated upstream resolver."""

    description: ClassVar[str] = "Verify conditional CoreDNS forwarding and preservation of cluster DNS."

    def run(self) -> None:
        """Compare real answers and require cleanup of the temporary forwarding rule."""
        output = evidence(self, "dns_forwarding")
        if output is None:
            return
        try:
            if not isinstance(output["query"], str) or not output["query"]:
                raise ValueError("Missing forwarded query")
            if addresses(output["answers"]) != addresses(output["expected_addresses"]):
                raise ValueError("Forwarded DNS answer mismatch")
            if output["forwarded_query_seen"] is not True:
                raise ValueError("Designated upstream did not observe the test query")
            if addresses(output["control_answers"]) != addresses(output["control_expected"]):
                raise ValueError("Cluster DNS control lookup failed")
            if output["rule_restored"] is not True:
                raise ValueError("Temporary forwarding rule was not removed")
        except (KeyError, TypeError, ValueError) as error:
            self.set_failed(str(error))
            return
        self.set_passed("Conditional forwarding, upstream observation and cluster DNS verified")


class K8sCidrRangesCheck(BaseValidation):
    """Corroborate configured ranges with control-plane ranges and fresh allocations."""

    description: ClassVar[str] = "Verify configured service, node and pod ranges against live allocations."

    def run(self) -> None:
        """Reject missing allocations and CIDRs inconsistent with the requested configuration."""
        output = evidence(self, "cidr_ranges")
        if output is None:
            return
        try:
            expected = {kind: networks(output["requested_ranges"][kind]) for kind in ("service", "node", "pod")}
            if networks(output["service_cidrs"]) != expected["service"]:
                raise ValueError("ServiceCIDR configuration differs from requested ranges")
            for subnet in networks(output["node_pod_cidrs"]):
                if not any(subnet.version == net.version and subnet.subnet_of(net) for net in expected["pod"]):
                    raise ValueError("Node pod CIDR is outside the requested pod ranges")
            for kind in ("service", "node", "pod"):
                for ip in addresses(output[f"{kind}_ips"]):
                    if not any(ip in net for net in expected[kind]):
                        raise ValueError(f"{kind} address {ip} is outside the requested ranges")
        except (KeyError, TypeError, ValueError) as error:
            self.set_failed(str(error))
            return
        self.set_passed("Configured service/pod CIDRs and service/node/pod allocations verified")
