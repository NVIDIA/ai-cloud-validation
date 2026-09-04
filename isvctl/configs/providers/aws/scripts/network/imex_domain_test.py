#!/usr/bin/env python3
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

"""IMEX domain connectivity test - AWS reference implementation (SDN21-01).

Bringing an IMEX domain up (writing nodes_config.cfg, starting the
`nvidia-imex` service) is a cluster-lifecycle concern owned elsewhere (see
NVIDIA Mission Control autonomous-hardware-recovery's `imex_bring_up.sh`
action) - out of scope per the requirement ("service lifecycle management").
This script only observes an ALREADY-RUNNING domain: it SSHes into each
expected member node and asks `nvidia-imex-ctl -N` what that node currently
sees, then reports each node's raw view. It never aggregates a single
"fully connected" verdict itself - ImexDomainConnectivityCheck computes
pairwise connectivity independently from the raw `reachability` map so a
one-way fault is never masked by an aggregate flag.

Nodes are GPU hosts (multi-node NVLink domain, e.g. P5/P4d instance family)
that must already be provisioned and IMEX-enabled; this script does not
launch or provision them (unlike dhcp_ip_test.py's throwaway t3.micro),
since real GPU capacity is not something a per-test script should launch on
demand. Point it at an existing cluster via --node-ids (SSH-reachable
hostnames or IPs) and --key-file, or the AWS_IMEX_* env var equivalents,
mirroring the "reuse an existing instance" dev workflow documented in
bare_metal.yaml.

`nvidia-imex-ctl -N -j` (JSON output; confirmed against a live single-node
UP domain and a live 2-node DOWN domain on 2026-09-04) returns one entry per
CONFIGURED member (not just the queried node), each with its own
"connections" map, e.g.:
    {
      "nodes": {
        "0": {
          "status": "READY",
          "host": "10.0.0.1",
          "hostName": "gpu-node-1",
          "connections": {
            "0": {"host": "10.0.0.1", "status": "CONNECTED", "changed": true},
            "1": {"host": "10.0.0.2", "status": "CONNECTED", "changed": true}
          },
          "changed": true,
          "version": "580.95.05"
        },
        "1": { "status": "UNAVAILABLE", "host": "10.0.0.2", "connections": {...}, ... }
      },
      "timestamp": "9/4/2026 00:37:39.905",
      "status": "UP"
    }
Peer entries (status "UNAVAILABLE", "connections" all "INVALID") appear to be
reconstructed from nodes_config.cfg rather than a live report from that peer,
so `_parse_imex_ctl_json` only reads the entry matching the node we actually
SSHed into - that is what keeps `reachability[node]` an independent
per-node observation rather than one node's possibly-stale view of everyone
else. `nvidia-imex-ctl` exits 255 (confirmed live) when it cannot read its
node config, so a non-zero exit reliably means "this node has no usable
view" - it is excluded from `members` entirely rather than reported with an
empty/misleading reachability list. Confirmed live too: the daemon can also
fail to *start* on a version-mismatched host ("NvGpu Library version ... is
not matching with current GPU driver version") - `-N` then reports domain
status "DOWN" with every configured member "UNAVAILABLE", which flows
through the same non-READY/no-CONNECTED-peers path as any other outage.

Usage:
    python imex_domain_test.py --region us-west-2 \\
        --node-ids gpu-node-1.cluster.internal,gpu-node-2.cluster.internal \\
        --key-file /tmp/isv-imex-test-key.pem

Output JSON: see isvctl/src/isvctl/config/output_schemas.py "imex_domain".
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from common.ssh_utils import ssh_run

DEFAULT_SSH_USER = "ubuntu"
IMEX_CTL_COMMAND = "sudo nvidia-imex-ctl -N -j -H"


def _parse_imex_ctl_json(output: str, queried_host: str) -> tuple[str, list[str]]:
    """Parse `nvidia-imex-ctl -N -j -H` JSON output into (domain_state, peer_hosts).

    Confirmed live (2026-09-04, 2-node cluster): `nodes` contains an entry for
    EVERY configured member, not just the queried one - including entries for
    peers whose own daemon is down (e.g. "status": "UNAVAILABLE", "connections"
    all "INVALID"), which appear to be reconstructed from nodes_config.cfg
    rather than a live report from that peer. So we deliberately use only the
    entry matching `queried_host`'s own connections map as this node's
    observation, rather than any other entry in the payload - that is what
    keeps `reachability[node]` an independent, per-node observation instead of
    the daemon's possibly-stale view of everyone else.

    `queried_host` is whatever identity form --node-ids used (IP or hostname).
    `nvidia-imex-ctl` always reports the IP in "host" and (with -H) the
    hostname in "hostName", but each `connections` entry only carries "host"
    (IP) - so a hostname-configured domain would otherwise (a) never match
    `queried_host` against "host", finding no "own" entry at all, and (b)
    even if it did, report peers as IPs that don't match the caller's
    hostname-based `expected_members`, silently producing zero peers. Both
    are fixed by matching "own" against either field, then translating each
    peer's IP back to the same identity form `queried_host` used - built from
    every node's host/hostName pair, since a peer's own top-level entry
    carries both even when its `connections` sub-entry only has the IP.
    """
    data = json.loads(output)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object from nvidia-imex-ctl, got {type(data).__name__}")
    domain_state = data.get("status", "")

    nodes_raw = data.get("nodes", {})
    if not isinstance(nodes_raw, dict):
        raise ValueError(f"`nodes` must be an object, got {type(nodes_raw).__name__}")
    # Skip individually malformed entries rather than fail the whole node's
    # query over one bad record - a partial view is still a real observation.
    nodes = [node for node in nodes_raw.values() if isinstance(node, dict)]

    own_connections: dict[str, Any] = {}
    own_host = None
    identity_field = "host"
    for node in nodes:
        if node.get("host") == queried_host:
            own_host = node.get("host")
            own_connections = node.get("connections") or {}
            identity_field = "host"
            break
        if node.get("hostName") and node.get("hostName") == queried_host:
            own_host = node.get("host")
            own_connections = node.get("connections") or {}
            identity_field = "hostName"
            break
    if not isinstance(own_connections, dict):
        own_connections = {}

    # host (IP) -> hostName, so peer connection entries (IP-only) can be
    # reported in whichever identity form the caller's --node-ids used.
    host_to_name = {node.get("host"): node.get("hostName") for node in nodes if node.get("host")}

    def _peer_identity(peer_host: str) -> str:
        if identity_field == "hostName":
            resolved = host_to_name.get(peer_host)
            if resolved:
                return resolved
        return peer_host

    peers = [
        _peer_identity(peer["host"])
        for peer in own_connections.values()
        if isinstance(peer, dict)
        and peer.get("status") == "CONNECTED"
        and peer.get("host")
        and peer.get("host") != own_host
    ]
    return domain_state, peers


def query_node(host: str, user: str, key_file: str, timeout: int) -> dict[str, Any]:
    """SSH into a single node and query its IMEX domain view."""
    exit_code, stdout, stderr = ssh_run(host, user, key_file, IMEX_CTL_COMMAND, timeout=timeout)
    if exit_code != 0:
        return {"host": host, "ok": False, "error": stderr.strip() or f"exit code {exit_code}"}

    try:
        domain_state, peers = _parse_imex_ctl_json(stdout, host)
    except (json.JSONDecodeError, ValueError) as e:
        return {"host": host, "ok": False, "error": f"could not parse nvidia-imex-ctl JSON output: {e}"}

    return {"host": host, "ok": True, "domain_state": domain_state, "peers": peers}


def main() -> int:
    """Query each expected IMEX domain member and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="IMEX domain connectivity test (AWS)")
    parser.add_argument("--region", required=True, help="AWS region (recorded for context only)")
    parser.add_argument(
        "--node-ids",
        default=os.environ.get("AWS_IMEX_NODE_IDS", ""),
        help="Comma-separated SSH-reachable hostnames/IPs of expected IMEX domain members (min 2)",
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("AWS_IMEX_KEY_FILE", ""),
        help="SSH private key file for the node(s)",
    )
    parser.add_argument("--ssh-user", default=os.environ.get("AWS_IMEX_SSH_USER", DEFAULT_SSH_USER))
    parser.add_argument("--domain-id", default=os.environ.get("AWS_IMEX_DOMAIN_ID", ""))
    parser.add_argument("--timeout", type=int, default=30, help="Per-node SSH command timeout (seconds)")
    args = parser.parse_args()

    expected_members = [node_id.strip() for node_id in args.node_ids.split(",") if node_id.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_domain",
        "region": args.region,
        "domain_id": args.domain_id or "-".join(sorted(expected_members)),
        "domain_state": "",
        "expected_members": expected_members,
        "members": [],
        "reachability": {},
        "tests": {"domain_queried": {"passed": False}},
    }

    if len(expected_members) < 2:
        result["error"] = "--node-ids must list at least two expected IMEX domain members"
        print(json.dumps(result, indent=2))
        return 1

    if not args.key_file:
        result["error"] = "--key-file (or AWS_IMEX_KEY_FILE) is required to SSH into domain members"
        print(json.dumps(result, indent=2))
        return 1

    # Query every member concurrently so wall-clock time stays bounded by
    # --timeout regardless of member count - sequential SSH calls could
    # otherwise add up past the orchestrator's step timeout for large domains.
    with ThreadPoolExecutor(max_workers=len(expected_members)) as pool:
        node_results = list(
            pool.map(lambda host: query_node(host, args.ssh_user, args.key_file, args.timeout), expected_members)
        )

    members: list[str] = []
    reachability: dict[str, list[str]] = {}
    domain_states: set[str] = set()
    query_errors: list[str] = []

    # Iterate in expected_members order (not completion order) for
    # deterministic output regardless of which SSH call finishes first.
    for host, node_result in zip(expected_members, node_results, strict=True):
        if not node_result["ok"]:
            query_errors.append(f"{host}: {node_result['error']}")
            continue
        members.append(host)
        reachability[host] = node_result["peers"]
        if node_result["domain_state"]:
            domain_states.add(node_result["domain_state"])

    result["members"] = members
    result["reachability"] = reachability
    # Members disagreeing on domain state is itself a fault; report empty/mixed
    # rather than picking one node's view and hiding the disagreement.
    if len(domain_states) == 1:
        result["domain_state"] = next(iter(domain_states))
    elif len(domain_states) > 1:
        result["domain_state"] = f"MIXED({','.join(sorted(domain_states))})"

    if query_errors:
        result["error"] = "; ".join(query_errors)

    result["tests"]["domain_queried"] = {"passed": bool(members)}
    result["success"] = bool(members)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
