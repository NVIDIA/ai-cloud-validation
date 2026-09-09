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

The expected member set comes from configuration membership (--node-ids /
AWS_IMEX_NODE_IDS), i.e. the operator states which nodes were allocated to the
domain. Those IDs must match the identity form written into nodes_config.cfg.

Usage:
    python imex_domain_test.py --region us-west-2 \\
        --node-ids gpu-node-1.cluster.internal,gpu-node-2.cluster.internal \\
        --key-file /tmp/isv-imex-test-key.pem

Emits the SDN21-01 step output contract (see output_schemas.py "imex_domain"):
    {
      "success": true,
      "platform": "network",
      "domain": {
        "domain_id": "...", "state": "up",
        "expected_members": ["node-01", "node-02"],
        "fully_connected": true
      },
      "nodes_checked": 2,
      "nodes_validated": 2,
      "nodes": [
        {"node_id": "node-01", "service_state": "active",
         "domain_member": true, "peers_reachable": ["node-02"]}
      ]
    }
``domain.fully_connected`` is the vendor's own aggregate claim, reported for
context only - ImexDomainConnectivityCheck ignores it and recomputes pairwise
connectivity from each node's ``peers_reachable``.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from common.ssh_utils import ssh_run

DEFAULT_SSH_USER = "ubuntu"
IMEX_CTL_COMMAND = "sudo nvidia-imex-ctl -N -j -H"
# Cap simultaneous ssh processes so a large IMEX domain does not fan out into
# hundreds of them at once.
MAX_PARALLEL_QUERIES = 16
# Overall sweep deadline. Kept well under the wired step timeout so this script
# always wins the race and prints its JSON contract instead of being killed.
DEFAULT_DEADLINE_SECONDS = 90


def _parse_imex_ctl_json(output: str, queried_host: str) -> tuple[str, str, list[str]]:
    """Parse `nvidia-imex-ctl -N -j -H` output into (domain_state, own_status, peer_hosts).

    ``own_status`` is the queried node's own entry status ("READY" when its IMEX
    service is up and participating, "UNAVAILABLE" when it is not), which the
    caller maps onto the contract's ``service_state`` / ``domain_member``.

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
    own_status = ""
    identity_field = "host"
    for node in nodes:
        if node.get("host") == queried_host:
            own_host = node.get("host")
            own_connections = node.get("connections") or {}
            own_status = node.get("status") or ""
            identity_field = "host"
            break
        if node.get("hostName") and node.get("hostName") == queried_host:
            own_host = node.get("host")
            own_connections = node.get("connections") or {}
            own_status = node.get("status") or ""
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
    return domain_state, own_status, peers


def _service_state(own_status: str) -> str:
    """Map a node's nvidia-imex-ctl entry status onto the contract's service_state."""
    return {"READY": "active", "UNAVAILABLE": "inactive"}.get(own_status, (own_status or "unknown").lower())


def query_node(host: str, user: str, key_file: str, timeout: int) -> dict[str, Any]:
    """SSH into a single node and query its IMEX domain view."""
    exit_code, stdout, stderr = ssh_run(host, user, key_file, IMEX_CTL_COMMAND, timeout=timeout)
    if exit_code != 0:
        return {"host": host, "ok": False, "error": stderr.strip() or f"exit code {exit_code}"}

    try:
        domain_state, own_status, peers = _parse_imex_ctl_json(stdout, host)
    except (json.JSONDecodeError, ValueError) as e:
        return {"host": host, "ok": False, "error": f"could not parse nvidia-imex-ctl JSON output: {e}"}

    return {
        "host": host,
        "ok": True,
        "domain_state": domain_state,
        "own_status": own_status,
        "peers": peers,
    }


def query_members(
    hosts: list[str],
    *,
    user: str,
    key_file: str,
    timeout: int,
    deadline: int,
) -> dict[str, dict[str, Any]]:
    """Query every member concurrently, always returning one result per host.

    Concurrency is capped (rather than one thread per member) so a large domain
    does not fan out into hundreds of simultaneous ssh processes. That cap means
    wall-clock time is roughly ``ceil(len(hosts) / MAX_PARALLEL_QUERIES) *
    timeout``, which for a big domain of unresponsive nodes could otherwise run
    past the orchestrator's step timeout and get this process killed before it
    prints anything. So the whole sweep is also bounded by ``deadline``: members
    that have not answered by then are reported as timed-out query errors and
    the caller still emits the full structured JSON contract.
    """
    results: dict[str, dict[str, Any]] = {}
    pool = ThreadPoolExecutor(max_workers=min(len(hosts), MAX_PARALLEL_QUERIES))
    try:
        futures = {pool.submit(query_node, host, user, key_file, timeout): host for host in hosts}
        try:
            for future in as_completed(futures, timeout=deadline):
                results[futures[future]] = future.result()
        except FuturesTimeoutError:
            pass
        for future, host in futures.items():
            if host not in results:
                future.cancel()
                results[host] = {
                    "host": host,
                    "ok": False,
                    "error": f"query did not complete within the {deadline}s deadline",
                }
    finally:
        # Do not block on stragglers - each in-flight ssh_run is already bounded
        # by its own per-node timeout, and queued work is dropped outright.
        pool.shutdown(wait=False, cancel_futures=True)
    return results


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
    parser.add_argument(
        "--deadline",
        type=int,
        default=DEFAULT_DEADLINE_SECONDS,
        help=(
            "Overall deadline for querying every member (seconds). Members that have not answered by then are "
            "reported as timed out so structured JSON is still emitted, rather than the orchestrator killing "
            "this process at its step timeout with no output."
        ),
    )
    args = parser.parse_args()

    expected_members = [node_id.strip() for node_id in args.node_ids.split(",") if node_id.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_domain",
        "region": args.region,
        "domain": {
            "domain_id": args.domain_id or "-".join(sorted(expected_members)),
            "state": "",
            "expected_members": expected_members,
            "fully_connected": False,
        },
        "nodes_checked": 0,
        "nodes_validated": 0,
        "nodes": [],
    }

    # SDN21-01 needs a pre-existing multi-node IMEX cluster, which a normal AWS
    # network run does not provision. When the run simply has not been pointed
    # at one, skip rather than fail - otherwise wiring this step would break
    # every network run that isn't specifically testing IMEX. A partially
    # configured run is still a hard error: it means someone tried to point this
    # at a cluster and got it wrong, which should not pass silently.
    if not expected_members and not args.key_file:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "IMEX domain not configured for this run (no node IDs or SSH key set)"
        print(json.dumps(result, indent=2))
        return 0

    if not expected_members:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "IMEX domain not configured for this run (no node IDs set)"
        print(json.dumps(result, indent=2))
        return 0

    if len(expected_members) < 2:
        result["error"] = "--node-ids must list at least two expected IMEX domain members"
        print(json.dumps(result, indent=2))
        return 1

    if not args.key_file:
        result["error"] = "--key-file (or AWS_IMEX_KEY_FILE) is required to SSH into domain members"
        print(json.dumps(result, indent=2))
        return 1

    results_by_host = query_members(
        expected_members,
        user=args.ssh_user,
        key_file=args.key_file,
        timeout=args.timeout,
        deadline=args.deadline,
    )

    nodes: list[dict[str, Any]] = []
    domain_states: set[str] = set()
    query_errors: list[str] = []
    validated = 0

    # Iterate in expected_members order (not completion order) for
    # deterministic output regardless of which SSH call finishes first.
    for host in expected_members:
        node_result = results_by_host[host]
        if not node_result["ok"]:
            query_errors.append(f"{host}: {node_result['error']}")
            # A node we could not query reports no membership, so the
            # validation's set-equality check flags it as missing.
            nodes.append(
                {
                    "node_id": host,
                    "service_state": "unreachable",
                    "domain_member": False,
                    "peers_reachable": [],
                    "error": node_result["error"],
                }
            )
            continue
        validated += 1
        own_status = node_result["own_status"]
        nodes.append(
            {
                "node_id": host,
                "service_state": _service_state(own_status),
                # Only a READY node is actually participating in the domain; a
                # configured-but-down peer is reported, not counted as a member.
                "domain_member": own_status == "READY",
                "peers_reachable": node_result["peers"],
            }
        )
        if node_result["domain_state"]:
            domain_states.add(node_result["domain_state"])

    result["nodes"] = nodes
    result["nodes_checked"] = len(expected_members)
    result["nodes_validated"] = validated

    # Members disagreeing on domain state is itself a fault; report it as mixed
    # rather than picking one node's view and hiding the disagreement.
    if len(domain_states) == 1:
        result["domain"]["state"] = next(iter(domain_states)).lower()
    elif len(domain_states) > 1:
        result["domain"]["state"] = "mixed(" + ",".join(sorted(s.lower() for s in domain_states)) + ")"

    # The vendor's own aggregate claim, carried for reporting only. The
    # validation ignores it and recomputes connectivity from peers_reachable.
    result["domain"]["fully_connected"] = result["domain"]["state"] == "up"

    if query_errors:
        result["error"] = "; ".join(query_errors)

    result["success"] = validated > 0

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
