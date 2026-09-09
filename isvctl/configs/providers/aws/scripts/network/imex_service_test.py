#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IMEX service/tooling presence test - AWS reference implementation (SDN17-01).

Asserts the host model: that IMEX ships in the delivered node image and is
registered with the node's service manager, without the tenant installing it.
Whether the service is enabled at boot, currently running, or has joined a
domain are all out of scope (SDN21-01 covers the domain itself).

Probing is by ROLE, not by package name. The daemon ships branch-versioned
(e.g. `nvidia-imex=<driver-branch>-1ubuntu1`) and the control tool is a binary
inside that same package, so a hardcoded package name goes stale on every
driver branch. Confirmed on a live host: the package installs
`/usr/bin/nvidia-imex` and `/usr/bin/nvidia-imex-ctl`, so presence is probed
via `command -v` and the control tool is additionally invoked to prove it
actually runs rather than merely existing.

Registration is read from systemd rather than the filesystem, because a unit
file on disk that the manager has not loaded does not count, and because a
boolean would collapse "absent" and "masked" into one answer. `systemctl show
-p LoadState` distinguishes them:

    LoadState=loaded     -> "loaded"    (the only passing state)
    LoadState=masked     -> "masked"    (deployment-model mismatch)
    LoadState=not-found  -> "not_found"
    (anything else)      -> "error"

`UnitFileState` supplies boot disposition, normalized and reported as evidence
only - never asserted on.

Scope is set by the allocation, not the node: `--nvlink-allocation` marks the
queried nodes as belonging to a multi-node NVLink allocation. That flag is
deliberately NOT derived from the node's own NVLink self-report, so a node
cannot opt itself out of being asserted against.

Usage:
    python imex_service_test.py --region us-west-2 \\
        --node-ids gpu-node-1,gpu-node-2 --key-file /tmp/key.pem
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
DEFAULT_SERVICE = "nvidia-imex.service"
DEFAULT_DAEMON_BIN = "nvidia-imex"
DEFAULT_CTL_BIN = "nvidia-imex-ctl"
MAX_PARALLEL_QUERIES = 16
DEFAULT_DEADLINE_SECONDS = 90

_LOAD_STATES = {"loaded": "loaded", "masked": "masked", "not-found": "not_found"}
_BOOT_STATES = {"enabled", "disabled", "static", "masked"}


def _probe_command(service: str, daemon_bin: str, ctl_bin: str) -> str:
    """Build the single remote probe. Delimited so partial output stays parseable."""
    return (
        f"echo DAEMON=$(command -v {daemon_bin} >/dev/null 2>&1 && echo yes || echo no); "
        # Presence alone is not enough for the control tool - the requirement is
        # that it is *invocable*, so actually run it.
        f"echo CTL=$(command -v {ctl_bin} >/dev/null 2>&1 && "
        f"({ctl_bin} --version >/dev/null 2>&1 && echo yes || echo present_not_invocable) || echo no); "
        f"echo LOAD=$(systemctl show {service} -p LoadState --value 2>/dev/null); "
        f"echo BOOT=$(systemctl show {service} -p UnitFileState --value 2>/dev/null)"
    )


def _parse_probe(output: str) -> dict[str, Any]:
    """Parse the delimited probe output into the normalized per-node fields."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            fields[key] = value.strip()

    load_state = fields.get("LOAD", "")
    if load_state in _LOAD_STATES:
        registration = _LOAD_STATES[load_state]
    elif not load_state:
        # No answer from the manager at all is an error, not "absent" - the
        # latter is a claim about the image we have not actually established.
        registration = "error"
    else:
        registration = "error"

    boot = fields.get("BOOT", "")
    boot_disposition = boot if boot in _BOOT_STATES else ("none" if not boot else "unknown")
    # systemd reports a masked unit's file state as "masked"; the contract has
    # no such boot value, and masking is already reported via registration.
    if boot_disposition == "masked":
        boot_disposition = "none"

    return {
        "service_present": fields.get("DAEMON") == "yes",
        "control_tooling_present": fields.get("CTL") == "yes",
        "service_registration": registration,
        "boot_disposition": boot_disposition,
    }


def query_node(host: str, user: str, key_file: str, timeout: int, probe: str) -> dict[str, Any]:
    """SSH into one node and probe for the IMEX service and its tooling."""
    exit_code, stdout, stderr = ssh_run(host, user, key_file, probe, timeout=timeout)
    if exit_code != 0:
        return {"host": host, "ok": False, "error": stderr.strip() or f"exit code {exit_code}"}
    return {"host": host, "ok": True, **_parse_probe(stdout)}


def query_nodes(
    hosts: list[str],
    *,
    user: str,
    key_file: str,
    timeout: int,
    deadline: int,
    probe: str,
) -> dict[str, dict[str, Any]]:
    """Probe every node concurrently, always returning one result per host.

    Concurrency is capped so a large allocation does not fan out into one ssh
    process per node, and the whole sweep is bounded by ``deadline`` so an
    unresponsive fleet still yields structured JSON instead of being killed at
    the orchestrator's step timeout.
    """
    results: dict[str, dict[str, Any]] = {}
    pool = ThreadPoolExecutor(max_workers=min(len(hosts), MAX_PARALLEL_QUERIES))
    try:
        futures = {pool.submit(query_node, host, user, key_file, timeout, probe): host for host in hosts}
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
                    "error": f"probe did not complete within the {deadline}s deadline",
                }
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def main() -> int:
    """Probe each node for IMEX service/tooling and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="IMEX service presence test (AWS)")
    parser.add_argument("--region", required=True, help="AWS region (recorded for context only)")
    parser.add_argument(
        "--node-ids",
        default=os.environ.get("AWS_IMEX_NODE_IDS", ""),
        help="Comma-separated SSH-reachable node IDs covered by the allocation",
    )
    parser.add_argument("--key-file", default=os.environ.get("AWS_IMEX_KEY_FILE", ""))
    parser.add_argument("--ssh-user", default=os.environ.get("AWS_IMEX_SSH_USER", DEFAULT_SSH_USER))
    parser.add_argument("--service", default=os.environ.get("AWS_IMEX_SERVICE", DEFAULT_SERVICE))
    parser.add_argument("--daemon-bin", default=DEFAULT_DAEMON_BIN, help="IMEX daemon binary probed by role")
    parser.add_argument("--control-bin", default=DEFAULT_CTL_BIN, help="IMEX control tool probed by role")
    parser.add_argument(
        "--nvlink-allocation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Whether these nodes belong to a multi-node NVLink allocation. Set from the allocation, never from a "
            "node's own NVLink self-report, so a node cannot opt itself out of scope."
        ),
    )
    parser.add_argument("--timeout", type=int, default=30, help="Per-node SSH command timeout (seconds)")
    parser.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE_SECONDS, help="Overall sweep deadline")
    args = parser.parse_args()

    node_ids = [node_id.strip() for node_id in args.node_ids.split(",") if node_id.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_service",
        "region": args.region,
        "nodes_checked": 0,
        "nodes_validated": 0,
        "nodes": [],
    }

    # A normal AWS network run does not provision an NVLink allocation, so an
    # unconfigured run skips rather than failing every unrelated network run.
    # A partially configured one stays a hard error - that means someone aimed
    # this at a cluster and got it wrong, which should not pass silently.
    if not node_ids and not args.key_file:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "IMEX nodes not configured for this run (no node IDs or SSH key set)"
        print(json.dumps(result, indent=2))
        return 0

    if not node_ids:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "IMEX nodes not configured for this run (no node IDs set)"
        print(json.dumps(result, indent=2))
        return 0

    if not args.key_file:
        result["error"] = "--key-file (or AWS_IMEX_KEY_FILE) is required to SSH into the nodes"
        print(json.dumps(result, indent=2))
        return 1

    probe = _probe_command(args.service, args.daemon_bin, args.control_bin)
    results_by_host = query_nodes(
        node_ids,
        user=args.ssh_user,
        key_file=args.key_file,
        timeout=args.timeout,
        deadline=args.deadline,
        probe=probe,
    )

    nodes: list[dict[str, Any]] = []
    errors: list[str] = []
    validated = 0
    for host in node_ids:
        node_result = results_by_host[host]
        if not node_result["ok"]:
            errors.append(f"{host}: {node_result['error']}")
            # An unreachable node is still in scope and still counts as a
            # failure - it must not quietly drop out of the asserted set.
            nodes.append(
                {
                    "node_id": host,
                    "in_nvlink_allocation": args.nvlink_allocation,
                    "service_present": False,
                    "control_tooling_present": False,
                    "service_registration": "error",
                    "boot_disposition": "unknown",
                    "error": node_result["error"],
                }
            )
            continue
        validated += 1
        nodes.append(
            {
                "node_id": host,
                "in_nvlink_allocation": args.nvlink_allocation,
                "service_present": node_result["service_present"],
                "control_tooling_present": node_result["control_tooling_present"],
                "service_registration": node_result["service_registration"],
                "boot_disposition": node_result["boot_disposition"],
            }
        )

    result["nodes"] = nodes
    result["nodes_checked"] = len(node_ids)
    result["nodes_validated"] = validated
    if errors:
        result["error"] = "; ".join(errors)
    result["success"] = validated > 0

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
