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

"""VPC, subnet, and firewall-rule helpers for the Firebird network scripts.

Every mutation returns an asynchronous Operation whose ``resourceId`` names the
created/changed resource. ``submit`` returns that Operation *before* waiting, so
callers record the ID first and clean up exactly what they created even when
the wait fails.

Firewall rules are VPC-scoped allow/deny rules (``/network/vpcs/{v}/firewall-rules``);
the API has no rule priority. Two constraints shape every probe rule (where the
API does not check them at request time, it accepts the rule and its Operation
then fails):

- some deployments require a source port range on a TCP/UDP rule; sending
  1-65535 is equivalent, so ``tcp_rule`` always sends it;
- at least one of the rule's prefixes must fall inside an existing subnet of the
  rule's VPC (the VPC CIDR alone is not enough).

Probe rules that need no real node address therefore pair one ``/32`` inside a
subnet (``probe_host``) with a TEST-NET-1 address (RFC 5737) on the other side:
no real traffic comes from or goes to TEST-NET-1, so they cannot affect it.
"""

import ipaddress
import secrets
from typing import Any
from urllib.parse import quote

from common.firebird_client import FirebirdApiError, FirebirdClient, remaining

# RFC 5737 documentation address: a valid prefix no real traffic uses.
TEST_NET_SRC = "192.0.2.1/32"

# The full source port range; some deployments reject a TCP/UDP rule without one.
SRC_PORT_FROM = 1
SRC_PORT_TO = 65535

# Cleanup gets at least this long even when the step's own deadline is spent:
# the step failing on time must not leave the rule it created behind.
CLEANUP_SECONDS = 120


def cleanup_timeout(deadline: float) -> int:
    """Return the timeout for deleting what a step created, given its deadline."""
    return max(remaining(deadline), CLEANUP_SECONDS)


def unique_name(prefix: str) -> str:
    """Return ``prefix`` plus a short random suffix (names are unique per VPC)."""
    return f"{prefix}-{secrets.token_hex(3)}"


def vpcs_path(client: FirebirdClient, vpc_id: str = "", suffix: str = "") -> str:
    """Return the project VPC collection path, or one VPC's path plus ``suffix``."""
    base = client.project_path("/network/vpcs")
    return f"{base}/{quote(vpc_id)}{suffix}" if vpc_id else base


def rules_path(client: FirebirdClient, vpc_id: str, rule_id: str = "") -> str:
    """Return a VPC's firewall-rule collection path, or one rule's path."""
    path = vpcs_path(client, vpc_id, "/firewall-rules")
    return f"{path}/{quote(rule_id)}" if rule_id else path


def submit(client: FirebirdClient, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a mutation and return its (unawaited) Operation; raise if it names no resource."""
    operation = client.request(method, path, body).get("operation") or {}
    if not operation.get("resourceId"):
        raise RuntimeError(f"{method} {path} returned no operation resource ID")
    return operation


def delete_and_wait(client: FirebirdClient, path: str, timeout: int, interval: float | None = None) -> bool:
    """DELETE ``path`` and wait for its Operation; return False if it was already gone.

    Only a 404 counts as gone: a 400 (for example a subnet still in use) is a failure.
    """
    try:
        operation = client.request("DELETE", path).get("operation") or {}
    except FirebirdApiError as e:
        if e.status == 404:
            return False
        raise
    client.wait_operation(operation, timeout, interval)
    return True


def exists(client: FirebirdClient, path: str) -> bool:
    """Return whether GET ``path`` finds the resource (404 means it does not)."""
    try:
        client.request("GET", path)
    except FirebirdApiError as e:
        if e.status == 404:
            return False
        raise
    return True


def gateway_ip(cidr: str) -> str:
    """Return the first usable host of ``cidr`` (the subnet gateway)."""
    network = ipaddress.ip_network(cidr, strict=True)
    if network.version != 4 or network.num_addresses < 4:
        raise ValueError(f"subnet CIDR {cidr} must be IPv4 and /30 or larger")
    return str(network.network_address + 1)


def probe_host(subnet_cidr: str) -> str:
    """Return the ``/32`` of the last usable host of ``subnet_cidr``.

    It is the in-subnet side of a probe rule: a probe rule only reaches READY
    when at least one prefix is inside an existing subnet of the VPC (depending
    on the deployment an invalid rule is rejected immediately or through its
    Operation). The other side is TEST-NET-1, so no real traffic matches even
    if a host holds this address.
    """
    network = ipaddress.ip_network(subnet_cidr, strict=True)
    if network.version != 4 or network.num_addresses < 4:
        raise ValueError(f"subnet CIDR {subnet_cidr} must be IPv4 and /30 or larger")
    return f"{network.broadcast_address - 1}/32"


def tcp_rule(name: str, action: str, src_prefix: str, dst_prefix: str, dst_port: int) -> dict[str, Any]:
    """Return a rule body matching TCP to one destination port, from any source port.

    The explicit full source range (1-65535) is sent because some deployments
    reject a TCP rule without one.
    """
    return {
        "name": name,
        "action": action,
        "protocol": "TCP",
        "srcPrefix": src_prefix,
        "dstPrefix": dst_prefix,
        "srcPortFrom": SRC_PORT_FROM,
        "srcPortTo": SRC_PORT_TO,
        "dstPortFrom": dst_port,
        "dstPortTo": dst_port,
    }


def subnet_probe_host(client: FirebirdClient, vpc_id: str, subnet_id: str) -> str:
    """Return ``probe_host`` of ``subnet_id``, which must be a subnet of ``vpc_id``.

    The CIDR is read from the API, so this works for a supplied subnet too.
    """
    vpc, subnet = locate_subnet(client, subnet_id)
    if vpc.get("id") != vpc_id:
        raise RuntimeError(f"subnet {subnet_id} is in VPC {vpc.get('id')}, not {vpc_id}")
    if not subnet.get("cidr"):
        raise RuntimeError(f"subnet {subnet_id} reports no CIDR")
    return probe_host(subnet["cidr"])


def echo_request_rule(name: str, action: str, src_prefix: str, dst_prefix: str) -> dict[str, Any]:
    """Return a stateless rule body matching ICMP echo requests (type 8) only.

    Matching requests but not replies lets a DENY block pings in one direction
    while the other direction's replies still pass.
    """
    return {
        "name": name,
        "action": action,
        "protocol": "ICMP",
        "icmpType": 8,
        "stateful": False,
        "srcPrefix": src_prefix,
        "dstPrefix": dst_prefix,
    }


def locate_subnet(client: FirebirdClient, subnet_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(vpc, subnet)`` for a project subnet (GET .../vpcs-with-subnets)."""
    for item in client.paginate(client.project_path("/network/vpcs-with-subnets"), "items"):
        for subnet in item.get("subnets") or []:
            if subnet.get("id") == subnet_id:
                return item.get("vpc") or {}, subnet
    raise RuntimeError(f"subnet {subnet_id} not found in project {client.project_id}")


class RuleGuard:
    """Firewall rules this process created in one VPC, so every path can delete them."""

    def __init__(self, client: FirebirdClient, vpc_id: str) -> None:
        """Track rules created in ``vpc_id``."""
        self.client = client
        self.vpc_id = vpc_id
        self.rule_ids: list[str] = []

    def create(self, body: dict[str, Any], timeout: int) -> str:
        """Create a rule, recording it before waiting on its Operation; return its ID."""
        operation = submit(self.client, "POST", rules_path(self.client, self.vpc_id), body)
        self.rule_ids.append(operation["resourceId"])
        self.client.wait_operation(operation, timeout)
        return operation["resourceId"]

    def delete(self, rule_id: str, timeout: int) -> None:
        """Delete a tracked rule and wait for its Operation."""
        delete_and_wait(self.client, rules_path(self.client, self.vpc_id, rule_id), timeout)
        self.rule_ids.remove(rule_id)

    def cleanup(self, timeout: int) -> list[str]:
        """Delete every rule still tracked; return one error per rule that could not be."""
        errors = []
        for rule_id in list(self.rule_ids):
            try:
                self.delete(rule_id, timeout)
            except Exception as e:
                errors.append(f"firewall_rule:{rule_id}: {e}")
        return errors
