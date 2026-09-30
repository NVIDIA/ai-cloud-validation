#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IMEX reboot rejoin test - AWS reference implementation (SDN20-01).

DESTRUCTIVE AND SLOW. This reboots a node, so it must only be pointed at an
idle or drained domain. Only one member is rebooted: re-forming the whole
domain from cold is explicitly not a substitute, because it does not exercise a
single node rejoining peers that stayed up.

Where SDN17-01 reads back that boot persistence is *configured*, this proves it
by actually rebooting. The pass condition is deliberately strict - a node
shipped with IMEX not enabled at boot fails here, since shipping it enabled is
the provider obligation being checked.

Confirming the reboot actually happened
---------------------------------------
The one thing this check cannot get wrong is believing a reboot it did not
cause. Reachability proves nothing: a node that never went down answers SSH
just as happily as one that came back. So the reboot is confirmed by comparing
the kernel's own uptime across it:

  * before: read /proc/uptime and the boot id
  * after:  read them again once SSH returns

The reboot is confirmed only when uptime has gone BACKWARDS (the node is
younger than it was) or the boot id changed. Either is positive evidence the
kernel restarted; neither can be satisfied by a node that stayed up.

The boot id is checked as well as uptime because uptime alone is a race on a
very fast reboot: a node that rebooted and has been up for 90s still reads
lower than one up for an hour, but two samples taken close together around a
slow SSH reconnect could in principle read forwards. A changed
/proc/sys/kernel/random/boot_id is unambiguous.

--node-ids must use the same identity form the domain lists in
nodes_config.cfg. Pointing this at a public address while the domain knows the
node by a private one makes it look like the node never rejoined, so that case
is detected and reported as an identity mismatch rather than a failed rejoin.

Usage:
    python imex_reboot_test.py --region us-west-2 \\
        --node-ids gpu-node-1 --key-file /tmp/key.pem
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
    config_gate,
    parse_node_ids,
    run_remote,
)

IMEX_CTL_COMMAND = "sudo nvidia-imex-ctl -N -j -H"
DEFAULT_SERVICE = "nvidia-imex.service"
# Generous by design: the requirement calls for a bounded but generous bound,
# since bare-metal GB200-class reboots take minutes. The failure being
# separated is "never came back", not "came back slowly" - and elapsed time is
# reported either way so a slow-but-working platform stays visible.
DEFAULT_REJOIN_TIMEOUT = 900
DEFAULT_SSH_RETURN_TIMEOUT = 600
POLL_SECONDS = 10


def _state_command(service: str) -> str:
    """Probe boot identity, uptime, service state and domain membership."""
    return (
        "echo UPTIME=$(cut -d' ' -f1 /proc/uptime); "
        "echo BOOTID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); "
        f"echo ACTIVE=$(systemctl is-active {service} 2>/dev/null); "
        f"echo ENABLED=$(systemctl show {service} -p UnitFileState --value 2>/dev/null); "
        # Deliberately untruncated: this output is parsed, and cutting a
        # large domain's payload mid-structure would make it invalid JSON,
        # which reads as "node absent" and reports a false identity mismatch.
        f"echo OUT=$({IMEX_CTL_COMMAND} 2>/dev/null)"
    )


def _parse_state(output: str) -> dict[str, str]:
    """Parse the delimited state probe into its fields."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def _uptime(fields: dict[str, str]) -> float | None:
    """Return uptime in seconds, or None when it could not be read."""
    try:
        return float(fields.get("UPTIME", ""))
    except ValueError:
        return None


def membership(output: str, host: str) -> str:
    """Classify the node's domain membership: member, not_ready, or absent.

    ``absent`` is kept distinct from ``not_ready`` because the two have very
    different causes. A node missing from the payload entirely usually means the
    identity does not match the one the domain knows it by - nodes_config.cfg
    might list a private address while the check was pointed at a public one -
    and reporting that as "did not rejoin" sends the reader hunting a product
    failure that is not there.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return "absent"
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, dict):
        return "absent"
    for node in nodes.values():
        if isinstance(node, dict) and host in {node.get("host"), node.get("hostName")}:
            return "member" if node.get("status") == "READY" else "not_ready"
    return "absent"


def reboot_confirmed(before: dict[str, str], after: dict[str, str]) -> bool:
    """Whether the node demonstrably restarted, rather than merely answering SSH.

    Positive evidence only: uptime must have gone backwards, or the boot id must
    have changed. A node that never went down satisfies neither, which is the
    whole point - reachability is not proof of a reboot.
    """
    before_id = before.get("BOOTID", "")
    after_id = after.get("BOOTID", "")
    if before_id and after_id and before_id != after_id:
        return True

    before_up = _uptime(before)
    after_up = _uptime(after)
    if before_up is None or after_up is None:
        return False
    return after_up < before_up


def main() -> int:
    """Reboot one node and confirm IMEX returns and rejoins; emit structured JSON."""
    parser = argparse.ArgumentParser(description="IMEX reboot rejoin test (AWS)")
    parser.add_argument("--region", required=True, help="AWS region (recorded for context only)")
    parser.add_argument(
        "--node-ids",
        default=os.environ.get("AWS_IMEX_REBOOT_NODE", ""),
        help="The single node to reboot; this check is per-node and destructive",
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("AWS_IMEX_KEY_FILE", ""),
        help="SSH private key file for the node",
    )
    parser.add_argument("--ssh-user", default=os.environ.get("AWS_IMEX_SSH_USER", DEFAULT_SSH_USER))
    parser.add_argument("--service", default=os.environ.get("AWS_IMEX_SERVICE", DEFAULT_SERVICE))
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-command SSH timeout (seconds)"
    )
    parser.add_argument(
        "--ssh-return-timeout",
        type=int,
        default=DEFAULT_SSH_RETURN_TIMEOUT,
        help="Bound on the node answering SSH again after the reboot",
    )
    parser.add_argument(
        "--rejoin-timeout",
        type=int,
        default=DEFAULT_REJOIN_TIMEOUT,
        help="Bound on IMEX returning and rejoining the domain after boot",
    )
    args = parser.parse_args()

    node_ids = parse_node_ids(args.node_ids)
    node_id = node_ids[0] if node_ids else ""

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_reboot",
        "region": args.region,
        "node_id": node_id,
        "persistence_configured": False,
        "reboot_confirmed": False,
        "uptime_seconds": 0,
        "post_reboot": {},
    }

    # This reboots a node, so it must never silently choose which one.
    if len(node_ids) > 1:
        result["error"] = (
            f"--node-ids must name exactly one node for this check, got {len(node_ids)}. It reboots the node, "
            "so it is named deliberately rather than picked from a domain list"
        )
        print(json.dumps(result, indent=2))
        return 1

    gate = config_gate(node_ids, args.key_file, subject="IMEX reboot node")
    if gate is not None:
        result.update(gate)
        if "skip_reason" in gate:
            result["success"] = True
            result["skipped"] = True
            print(json.dumps(result, indent=2))
            return 0
        print(json.dumps(result, indent=2))
        return 1

    service = shlex.quote(args.service)

    def probe() -> dict[str, str]:
        """Sample the node's boot identity, service state and domain membership."""
        sampled = run_remote(node_id, args.ssh_user, args.key_file, _state_command(service), args.timeout)
        return _parse_state(sampled["stdout"]) if sampled["ok"] else {}

    # 1. Record the pre-reboot identity. Without this there is nothing to
    #    compare against, and the reboot could never be affirmatively confirmed.
    before = probe()
    if not before or _uptime(before) is None:
        result["error"] = f"{node_id}: could not read pre-reboot uptime, so a reboot could not be confirmed later"
        print(json.dumps(result, indent=2))
        return 1

    result["persistence_configured"] = before.get("ENABLED") == "enabled"
    prior_membership = membership(before.get("OUT", ""), node_id)
    result["prior_state"] = {
        "service_state": before.get("ACTIVE", "unknown"),
        "domain_member": prior_membership == "member",
    }

    # SDN20-01 is about rebooting a *member* and watching it rejoin. A node that
    # was inactive or not in the domain beforehand would be joining rather than
    # rejoining, and would report success for a property never demonstrated - so
    # refuse before doing anything destructive.
    if before.get("ACTIVE") != "active" or prior_membership != "member":
        result["error"] = (
            f"{node_id}: was not an active domain member before the reboot (service "
            f"{before.get('ACTIVE', 'unknown')!r}, membership {prior_membership}). This check reboots a member "
            "and watches it rejoin; a node that was not one beforehand would be joining, not rejoining"
        )
        print(json.dumps(result, indent=2))
        return 1

    # 2. Reboot. The command is expected to drop the connection, so its exit
    #    status says nothing useful and is deliberately not treated as failure.
    run_remote(node_id, args.ssh_user, args.key_file, "sudo systemctl reboot || sudo reboot", args.timeout)

    # 3. Wait for evidence of a restart, not merely for a readable sample.
    #    `systemctl reboot` returns once the reboot is enqueued, so the node can
    #    still be up and answering for some time afterwards - on bare metal,
    #    longer than a poll interval. Breaking on the first readable sample
    #    would therefore capture pre-reboot uptime and boot id and report "not
    #    confirmed", which looks exactly like the defect this check exists to
    #    catch. So keep polling until the sample actually shows a restart.
    started = time.monotonic()
    after: dict[str, str] = {}
    confirmed = False
    while time.monotonic() - started < args.ssh_return_timeout:
        time.sleep(POLL_SECONDS)
        sample = probe()
        if not sample or _uptime(sample) is None:
            continue
        # Keep the most recent readable sample either way, so a run that never
        # sees restart evidence can still report what it did observe.
        after = sample
        if reboot_confirmed(before, after):
            confirmed = True
            break

    if not after or _uptime(after) is None:
        result["post_reboot"] = {
            "service_ready": False,
            "domain_member": False,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "intervention_required": False,
        }
        result["error"] = f"{node_id}: the node did not answer again within {args.ssh_return_timeout}s of the reboot"
        print(json.dumps(result, indent=2))
        return 1

    # elapsed_seconds is defined as boot-to-domain-membership, so timing starts
    # here, once the node is answering again. Counting the downtime would
    # inflate the reported metric and eat into the rejoin bound.
    back_at = time.monotonic()
    result["reboot_confirmed"] = confirmed
    result["uptime_seconds"] = _uptime(after) or 0

    if not result["reboot_confirmed"]:
        result["post_reboot"] = {
            "service_ready": after.get("ACTIVE") == "active",
            "domain_member": membership(after.get("OUT", ""), node_id) == "member",
            "elapsed_seconds": round(time.monotonic() - back_at, 1),
            "intervention_required": False,
        }
        result["error"] = (
            f"{node_id}: the node kept answering for {args.ssh_return_timeout}s without uptime going backwards or "
            "the boot id changing, so the reboot was not confirmed. Reachability alone is not evidence that a "
            "node restarted"
        )
        print(json.dumps(result, indent=2))
        return 1

    # 4. Wait for IMEX to come back on its own. Nothing is started here: the
    #    property under test is that it returns unassisted.
    # The confirming sample came from the same probe and already carries ACTIVE
    # and OUT, so seed from it rather than discarding it. Requiring a fresh
    # probe would let a node that had already recovered report a false rejoin
    # failure - and wait out the whole timeout doing it - if later probes fail.
    status = membership(after.get("OUT", ""), node_id)
    ready = after.get("ACTIVE") == "active"
    member = status == "member"
    while not (ready and member) and time.monotonic() - back_at < args.rejoin_timeout:
        state = probe()
        if time.monotonic() - back_at >= args.rejoin_timeout:
            break
        if state:
            ready = state.get("ACTIVE") == "active"
            status = membership(state.get("OUT", ""), node_id)
            member = status == "member"
            result["uptime_seconds"] = _uptime(state) or result["uptime_seconds"]
            if ready and member:
                break
        time.sleep(POLL_SECONDS)

    result["post_reboot"] = {
        "service_ready": ready,
        "domain_member": member,
        "elapsed_seconds": round(time.monotonic() - back_at, 1),
        # Nothing is ever started by this check, so any return observed here was
        # by definition unassisted.
        "intervention_required": False,
    }

    persisted = result["persistence_configured"]
    if not (persisted and ready and member):
        if ready and member and not persisted:
            # The strict pass condition from the requirement: shipping IMEX
            # enabled at boot is the provider obligation being checked, so a
            # node that came back only because something else started it fails.
            detail = "IMEX returned and rejoined, but it was not enabled at boot"
        elif ready and status == "absent":
            # Far more often a misconfigured run than a product failure, so say
            # so rather than reporting a rejoin that never had a chance.
            detail = (
                "IMEX was running but the domain does not know this node by that identity. --node-ids must use "
                "the same identity form the domain lists in nodes_config.cfg"
            )
        else:
            detail = (
                f"IMEX was {'running' if ready else 'not running'} and "
                f"{'a domain member' if member else 'not a domain member'}"
            )
        # The suffix explains a failure the detail has not already attributed,
        # so it is omitted when the detail is itself about boot persistence -
        # otherwise the phrase appears twice with the elapsed time between them.
        suffix = "" if persisted or "enabled at boot" in detail else " (it was not enabled at boot)"
        result["error"] = (
            f"{node_id}: after a confirmed reboot {detail} after {result['post_reboot']['elapsed_seconds']}s{suffix}"
        )

    result["success"] = persisted and ready and member
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
