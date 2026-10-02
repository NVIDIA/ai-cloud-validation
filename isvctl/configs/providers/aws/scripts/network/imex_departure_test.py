#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IMEX deliberate node departure test - AWS reference (SDN19-01).

DESTRUCTIVE. This removes a node from a live domain, so it must only be pointed
at an idle or drained domain. Restoration is mandatory rather than best-effort,
and runs on every path that may have stopped the service.

The complement of SDN18-01: there an unexpected kill must be recovered from,
here a deliberate `systemctl stop` must be *noticed* by the survivors. The
stimuli have opposite correct behaviours, which is why they are separate
checks - a supervisor that restarted this stop would be wrong.

Normalizing the peer view (the part worth reading before changing anything)
--------------------------------------------------------------------------
`nvidia-imex-ctl -N -j` reports three different state vocabularies, and only one
of them actually answers "does this survivor still see the target?":

  * domain level   - "status": "UP" | "DEGRADED" | "DOWN"
  * per-node level - "status": "READY" | "UNAVAILABLE"
  * per-connection - "status": "CONNECTED" | "INVALID"

The per-node status is NOT a usable availability signal. Observed on a live
2-node AWS domain: each node reported *itself* READY and its peer UNAVAILABLE
even while both daemons were up and their gRPC channels connected, with the
domain reporting DEGRADED. Matching on the peer's node-level status would
therefore report the target "unavailable" before it was ever stopped - a
false pass for exactly the assertion this check exists to make.

What does move with reality is the observing node's own `connections` map. In a
live one-way fault injection the observer's entry for the blocked peer flipped
CONNECTED -> INVALID while everything else held. So availability is read from
the surviving node's own connection to the target, and mapped here onto the
contract's enum:

    CONNECTED                  -> "available"
    any other reported status  -> "unavailable"
    no entry for the target    -> "unknown"

The validation only ever sees that enum; it never substring-matches vendor
output.

Usage:
    python imex_departure_test.py --region us-west-2 \\
        --target-node gpu-node-1 --observer-node gpu-node-2 --key-file /tmp/key.pem
"""

import argparse
import json
import os
import shlex
import sys
import time
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from common.imex import (
    DEFAULT_SSH_USER,
    DEFAULT_TIMEOUT_SECONDS,
    parse_node_ids,
    run_remote,
)

IMEX_CTL_COMMAND = "sudo nvidia-imex-ctl -N -j -H"
DEFAULT_SERVICE = "nvidia-imex.service"
# Peer convergence is a control-plane state change, not a restart, so the bound
# only has to clear gRPC noticing a closed channel. Kept generous anyway: the
# failure being separated is "never noticed", not "noticed slowly".
DEFAULT_CONVERGENCE_TIMEOUT = 60
CONVERGENCE_POLL_SECONDS = 1
# Restoration is polled rather than slept on: rejoining the domain takes a
# moment, and a single early sample would misreport a success as a failure.
DEFAULT_RESTORE_TIMEOUT = 60
RESTORE_POLL_SECONDS = 2


def _load_nodes(output: str) -> dict[str, Any] | None:
    """Return the payload's `nodes` mapping, or None when the output is unusable."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    nodes = data.get("nodes")
    return nodes if isinstance(nodes, dict) else None


def _own_entry(output: str, host: str) -> dict[str, Any] | None:
    """Return the node's own entry in the payload, matched by IP or hostname."""
    nodes = _load_nodes(output)
    if nodes is None:
        return None
    for node in nodes.values():
        if isinstance(node, dict) and host in {node.get("host"), node.get("hostName")}:
            return node
    return None


def _identities_for(output: str, host: str) -> set[str]:
    """Return every identity form the payload uses for a node (IP and hostname)."""
    identities = {host}
    entry = _own_entry(output, host)
    if entry is not None:
        identities.update(h for h in (entry.get("host"), entry.get("hostName")) if h)
    return identities


def _is_ready(output: str, host: str) -> bool:
    """Whether the node reports itself as an operational domain member."""
    entry = _own_entry(output, host)
    return entry is not None and entry.get("status") == "READY"


def _peer_view(output: str, observer_host: str, target_host: str) -> str:
    """Return the observer's normalized view of the target: available/unavailable/unknown.

    Read from the observer's OWN connections map rather than the target's
    node-level status - see the module docstring for why the latter is not a
    usable signal.
    """
    own = _own_entry(output, observer_host)
    if own is None:
        return "unknown"
    connections = own.get("connections")
    if not isinstance(connections, dict):
        return "unknown"

    target_hosts = _identities_for(output, target_host)
    for peer in connections.values():
        if isinstance(peer, dict) and peer.get("host") in target_hosts:
            # Anything other than CONNECTED means the link is gone. Observed
            # live after a deliberate stop: CONNECTED -> RECOVERING, which
            # persists rather than settling to INVALID.
            return "available" if peer.get("status") == "CONNECTED" else "unavailable"
    return "unknown"


def _survivors_operational(output: str, observer_host: str, departed_host: str) -> bool:
    """Whether the surviving members are still operating together.

    Deliberately NOT read from the domain-level "status". Observed live on a
    2-node AWS domain: it reports DEGRADED while both members are healthy and
    still DEGRADED after one departs, so it cannot tell "a node left" apart from
    "the domain fell over" - which is exactly the distinction this assertion has
    to make.

    What does carry that signal is the observer's own entry: it stays READY with
    its connections to the remaining members CONNECTED when the domain merely
    degrades, and stops being READY if the domain actually collapses.
    """
    observer = _own_entry(output, observer_host)
    if observer is None or observer.get("status") != "READY":
        return False

    connections = observer.get("connections")
    if not isinstance(connections, dict):
        return False

    departed = _identities_for(output, departed_host)
    own = _identities_for(output, observer_host)
    for peer in connections.values():
        if not isinstance(peer, dict):
            continue
        host = peer.get("host")
        # The departed node is expected to be gone; every other member the
        # observer knows about must still be connected.
        if host in departed or host in own:
            continue
        if peer.get("status") != "CONNECTED":
            return False
    return True


def main() -> int:
    """Stop one node deliberately, observe peer convergence, restore; emit JSON."""
    parser = argparse.ArgumentParser(description="IMEX node departure test (AWS)")
    parser.add_argument("--region", required=True, help="AWS region (recorded for context only)")
    parser.add_argument(
        "--target-node",
        default=os.environ.get("AWS_IMEX_DEPARTURE_TARGET", ""),
        help="The single node whose IMEX service is stopped; this check is destructive",
    )
    parser.add_argument(
        "--observer-node",
        default=os.environ.get("AWS_IMEX_DEPARTURE_OBSERVER", ""),
        help="A surviving domain member the departure is observed from",
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("AWS_IMEX_KEY_FILE", ""),
        help="SSH private key file for the nodes",
    )
    parser.add_argument("--ssh-user", default=os.environ.get("AWS_IMEX_SSH_USER", DEFAULT_SSH_USER))
    parser.add_argument("--service", default=os.environ.get("AWS_IMEX_SERVICE", DEFAULT_SERVICE))
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-command SSH timeout (seconds)"
    )
    parser.add_argument(
        "--restore-timeout",
        type=int,
        default=DEFAULT_RESTORE_TIMEOUT,
        help="Bound on the stopped node coming back and rejoining the domain",
    )
    parser.add_argument(
        "--convergence-timeout",
        type=int,
        default=DEFAULT_CONVERGENCE_TIMEOUT,
        help="Bound on a surviving member observing the departure",
    )
    args = parser.parse_args()

    target_ids = parse_node_ids(args.target_node)
    observer_ids = parse_node_ids(args.observer_node)
    target = target_ids[0] if target_ids else ""
    observer = observer_ids[0] if observer_ids else ""

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_departure",
        "region": args.region,
        "target_node": target,
        "operations": {},
    }

    # A normal network run provisions no IMEX domain, so an unconfigured run
    # skips rather than failing every unrelated check. A partially configured
    # one is a hard error: this stops a service, so both ends are named
    # deliberately.
    if not target and not observer and not args.key_file:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "IMEX departure not configured for this run (no target, observer or SSH key set)"
        print(json.dumps(result, indent=2))
        return 0

    if not target or not observer:
        result["error"] = (
            "--target-node and --observer-node must both name a node: this check stops a service on one member "
            "and observes the departure from another, so neither end is inferred"
        )
        print(json.dumps(result, indent=2))
        return 1

    # Silently taking the first of several would aim a destructive stop at an
    # ambiguous node, so both ends must name exactly one.
    if len(target_ids) > 1 or len(observer_ids) > 1:
        result["error"] = (
            f"--target-node and --observer-node must each name exactly one node, got {len(target_ids)} and "
            f"{len(observer_ids)}. This check stops a service, so the node is named deliberately rather than "
            "picked from a domain list"
        )
        print(json.dumps(result, indent=2))
        return 1

    if target == observer:
        result["error"] = "--observer-node must be a surviving member, not the node being stopped"
        print(json.dumps(result, indent=2))
        return 1

    if not args.key_file:
        result["error"] = "--key-file (or AWS_IMEX_KEY_FILE) is required to SSH into the nodes"
        print(json.dumps(result, indent=2))
        return 1

    service = shlex.quote(args.service)

    def on(host: str, command: str) -> dict[str, Any]:
        """Run one command on the given node with this run's SSH settings."""
        return run_remote(host, args.ssh_user, args.key_file, command, args.timeout)

    def _sample_target() -> tuple[str, str]:
        """Return the target's (service state, nvidia-imex-ctl payload)."""
        sampled = on(
            target,
            f"echo ACTIVE=$(systemctl is-active {service}); echo OUT=$({IMEX_CTL_COMMAND} 2>/dev/null | head -c 4000)",
        )
        active = ""
        payload = ""
        if sampled["ok"]:
            for line in sampled["stdout"].splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "ACTIVE":
                    active = value.strip()
                elif sep and key.strip() == "OUT":
                    payload = value.strip()
        return active, payload

    def restore_target() -> dict[str, Any]:
        """Put the stopped node back and report the state it was left in.

        Mandatory, and called on every path that may have stopped the service:
        this check deliberately leaves the domain a member short, so abandoning
        it there would hand the next check a degraded domain.

        The result is polled to a bounded deadline rather than sampled once
        after a fixed sleep - rejoining the domain is not instantaneous, and a
        single early sample would report a restoration that did in fact succeed
        as having failed.
        """
        on(target, f"sudo systemctl start {service} >/dev/null 2>&1")
        active = ""
        payload = ""
        deadline = time.monotonic() + args.restore_timeout
        while time.monotonic() < deadline:
            active, payload = _sample_target()
            if active == "active" and _is_ready(payload, target):
                break
            time.sleep(RESTORE_POLL_SECONDS)
        return {
            "restored_to": active or "unknown",
            "domain_member": _is_ready(payload, target),
        }

    # 1. Record what we are about to disturb. An already-stopped target would
    #    satisfy "inactive" after the stop and report a departure this check
    #    never caused - and restoring it would start a service that was not
    #    running to begin with, which is not the prior state either.
    prior_active, prior_payload = _sample_target()
    prior_member = _is_ready(prior_payload, target)
    result["operations"]["prior_state"] = {"service_state": prior_active or "unknown", "domain_member": prior_member}

    if prior_active != "active" or not prior_member:
        result["error"] = (
            f"{target}: was not an active, operational domain member before the stop (service "
            f"{prior_active or 'unknown'!r}, domain member {prior_member}) - there is no departure to cause, and "
            "stopping it here would not be restoring its prior state either"
        )
        print(json.dumps(result, indent=2))
        return 1

    # 2. Stop the target deliberately, through the service manager. This is a
    #    graceful stop, never a kill - SDN18-01 covers the kill.
    stop = on(target, f"sudo systemctl stop {service}; sleep 2; echo ACTIVE=$(systemctl is-active {service})")
    stopped_clean = stop["ok"] and "ACTIVE=inactive" in stop["stdout"]
    result["operations"]["stop"] = {"requested": True, "clean_exit": stopped_clean}

    if not stopped_clean:
        result["operations"]["restore"] = restore_target()
        result["error"] = f"{target}: the service did not stop cleanly through the node service manager"
        print(json.dumps(result, indent=2))
        return 1

    # 3. Poll a surviving member until it reports the target gone.
    started = time.monotonic()
    reported = "unknown"
    operational = False
    while time.monotonic() - started < args.convergence_timeout:
        probe = on(observer, IMEX_CTL_COMMAND)
        # A probe can take up to the SSH timeout, so re-check the bound before
        # accepting its result rather than reporting convergence past it.
        if time.monotonic() - started >= args.convergence_timeout:
            break
        if probe["ok"]:
            reported = _peer_view(probe["stdout"], observer, target)
            operational = _survivors_operational(probe["stdout"], observer, target)
            if reported == "unavailable":
                break
        time.sleep(CONVERGENCE_POLL_SECONDS)

    result["operations"]["peer_convergence"] = {
        "observed_from": observer,
        "target_reported": reported,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "surviving_members_operational": operational,
    }

    # 4. Restore, always.
    result["operations"]["restore"] = restore_target()

    converged = reported == "unavailable" and operational
    restore = result["operations"]["restore"]
    restored = restore["restored_to"] == "active" and restore["domain_member"] is True

    if not converged:
        result["error"] = (
            f"{target}: surviving member {observer} reported it {reported!r} with the domain "
            f"{'operational' if operational else 'not operational'} among survivors"
        )
    elif not restored:
        # Restoration is part of the contract for a check that deliberately
        # degrades the domain, so it gates reported success too.
        result["error"] = (
            f"{target}: peers converged, but the node was left {restore['restored_to']!r} and "
            f"{'in' if restore['domain_member'] else 'out of'} the domain after restoration"
        )
    result["success"] = converged and restored
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
