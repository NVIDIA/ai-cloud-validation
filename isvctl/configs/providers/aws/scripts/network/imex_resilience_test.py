#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IMEX unaided-run / persistence / self-heal test - AWS reference (SDN18-01).

DESTRUCTIVE. This kills a running IMEX daemon, so it must only be pointed at an
idle or drained domain. The teardown restores the service and reports what it
restored under ``operations.restore``.

Three properties are observed, and the provider script keeps them separable so
the validation can indict the right thing:

  * the service is *already* running and the node is *already* an operational
    domain member. This script never starts the service to make that true -
    ``started_by_test`` is reported false so the observation is auditable, and
    a node that arrives not running fails rather than being repaired.
  * boot persistence is read back from the service manager, not assumed.
  * the daemon is killed outright and the node must return to operational
    domain membership on its own.

The stimulus is a kill, never `systemctl stop`. A correct supervisor
deliberately does not restart a deliberate stop, so a stop-based version of
this check would fail on exactly the nodes that are behaving properly.

Recovery bound (--recovery-timeout), measured rather than guessed, per the
requirement to derive it from an observed restart baseline:

    Observed on the reference platform (g4dn.xlarge, driver 595.91.07,
    nvidia-imex 595.91.07, single-node domain), time from service start to the
    domain reporting UP, three consecutive runs:
        1550 ms, 1543 ms, 1547 ms
    systemd's own restart delay (RestartUSec) is 100 ms, so a correctly
    supervised node should rejoin in roughly 1.7 s.

    The default below is far above that. It is not tuned to be tight, because
    the failure it has to separate is not "slightly slow" but "never comes
    back": on stock packaging the shipped unit carries no Restart= directive
    (systemd therefore defaults to Restart=no), and a killed daemon was
    observed to stay down indefinitely - NRestarts=0, Result=signal, unit
    failed, nothing recovered it in 120 s. So the bound only needs to clear a
    healthy rejoin by a wide margin while still failing decisively on a node
    with no supervision at all.

Usage:
    python imex_resilience_test.py --region us-west-2 \\
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

DEFAULT_SERVICE = "nvidia-imex.service"
DEFAULT_DAEMON_BIN = "nvidia-imex"
# See the module docstring: observed healthy rejoin is ~1.7s, and an
# unsupervised node never returns, so a wide bound separates them cleanly.
DEFAULT_RECOVERY_TIMEOUT = 60
RECOVERY_POLL_SECONDS = 1


def _domain_member_command() -> str:
    """Return the command that answers 'is this node an operational domain member?'.

    Uses the same `nvidia-imex-ctl -N -j` view SDN21-01 reads, reduced here to
    the single yes/no this check needs.
    """
    # Kept to a grep rather than piping through a remote interpreter: this runs
    # inside a larger `echo FOO=$(...)` shell substitution over SSH, where extra
    # quoting layers are a reliable source of breakage.
    return 'sudo nvidia-imex-ctl -N -j 2>/dev/null | grep -q \'"status":"UP"\' && echo yes || echo no'


def _arrival_command(service: str, daemon_bin: str) -> str:
    """Probe the node's state on arrival, before anything is touched.

    ``service`` and ``daemon_bin`` are operator-configurable, so they are quoted
    before being interpolated into the remote shell command.
    """
    service = shlex.quote(service)
    daemon_bin = shlex.quote(daemon_bin)
    return (
        f"echo ACTIVE=$(systemctl is-active {service} 2>/dev/null); "
        f"echo PID=$(pgrep -x {daemon_bin} | head -1); "
        f"echo BOOT=$(systemctl show {service} -p UnitFileState --value 2>/dev/null); "
        f"echo RESTART=$(systemctl show {service} -p Restart --value 2>/dev/null); "
        f"echo MEMBER=$({_domain_member_command()})"
    )


def _parse_arrival(output: str) -> dict[str, str]:
    """Parse the delimited arrival probe into its fields."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            fields[key] = value.strip()
    return fields


def _boot_persistence_configured(unit_file_state: str) -> bool:
    """Whether the manager reports the service will return after a node restart.

    Only ``enabled`` counts, because only it proves a boot target will pull the
    unit in.

    ``static`` does not: a static unit has no [Install] section, so it starts
    only when something else pulls it in - which may be a manual start or
    on-demand (socket/path/dbus) activation rather than the boot target. A
    running static unit can therefore fail to come back after a reboot, and
    ``UnitFileState`` alone cannot tell the two apart. The real IMEX unit ships
    with [Install] WantedBy=multi-user.target, so it is enabled or disabled and
    never static.

    ``enabled-runtime`` does not either: those symlinks live under /run, which
    is tmpfs, so the enablement is erased by the very reboot this property is
    about.
    """
    return unit_file_state == "enabled"


def main() -> int:
    """Observe unaided running, persistence and self-heal; emit structured JSON."""
    parser = argparse.ArgumentParser(description="IMEX resilience test (AWS)")
    parser.add_argument("--region", required=True, help="AWS region (recorded for context only)")
    parser.add_argument(
        "--node-ids",
        default=os.environ.get("AWS_IMEX_NODE_IDS", ""),
        help="The single SSH-reachable node to test; this check is per-node and destructive",
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("AWS_IMEX_KEY_FILE", ""),
        help="SSH private key file for the node",
    )
    parser.add_argument("--ssh-user", default=os.environ.get("AWS_IMEX_SSH_USER", DEFAULT_SSH_USER))
    parser.add_argument("--service", default=os.environ.get("AWS_IMEX_SERVICE", DEFAULT_SERVICE))
    parser.add_argument("--daemon-bin", default=DEFAULT_DAEMON_BIN, help="IMEX daemon binary, probed by role")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-command SSH timeout (seconds)"
    )
    parser.add_argument(
        "--recovery-timeout",
        type=int,
        default=DEFAULT_RECOVERY_TIMEOUT,
        help="Bound on automatic recovery; see the module docstring for how it was derived",
    )
    args = parser.parse_args()

    node_ids = parse_node_ids(args.node_ids)
    node_id = node_ids[0] if node_ids else ""

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_resilience",
        "region": args.region,
        "node_id": node_id,
        "operations": {},
    }

    # This check kills a daemon, so it must never silently choose which of
    # several nodes to do that to. One node per run, named explicitly.
    if len(node_ids) > 1:
        result["error"] = (
            f"--node-ids must name exactly one node for this check, got {len(node_ids)}. It kills a running "
            "daemon, so the node is named deliberately rather than picked from a domain list"
        )
        print(json.dumps(result, indent=2))
        return 1

    # An unconfigured run skips; a partially configured one is a hard error.
    gate = config_gate(node_ids, args.key_file, subject="IMEX node")
    if gate is not None:
        result.update(gate)
        if "skip_reason" in gate:
            result["success"] = True
            result["skipped"] = True
            print(json.dumps(result, indent=2))
            return 0
        print(json.dumps(result, indent=2))
        return 1

    def remote(command: str) -> dict[str, Any]:
        """Run one command on the node under test with this run's SSH settings."""
        return run_remote(node_id, args.ssh_user, args.key_file, command, args.timeout)

    service = shlex.quote(args.service)
    daemon_bin = shlex.quote(args.daemon_bin)

    def restore_node() -> dict[str, Any]:
        """Put the service back and report the state the node was left in.

        Called on every path that may have killed the daemon - including the
        unconfirmed-termination path, because run_remote can fail after the
        remote shell has already executed pkill (an SSH disconnect or command
        timeout), and returning early there would leave the node down.
        """
        restored = remote(
            f"sudo systemctl restart {service} >/dev/null 2>&1; sleep 2; "
            f"echo ACTIVE=$(systemctl is-active {service}); echo MEMBER=$({_domain_member_command()})"
        )
        fields = _parse_arrival(restored["stdout"]) if restored["ok"] else {}
        return {
            "restored_to": fields.get("ACTIVE", "unknown"),
            "domain_member": fields.get("MEMBER") == "yes",
        }

    # 1. Observe arrival state. Nothing is started here, by design.
    arrival = remote(_arrival_command(args.service, args.daemon_bin))
    if not arrival["ok"]:
        result["error"] = f"{node_id}: {arrival['error']}"
        print(json.dumps(result, indent=2))
        return 1

    fields = _parse_arrival(arrival["stdout"])
    original_pid = fields.get("PID", "")
    presence = {
        "running_on_arrival": fields.get("ACTIVE") == "active" and bool(original_pid),
        "domain_member": fields.get("MEMBER") == "yes",
        "started_by_test": False,
        "boot_persistence_configured": _boot_persistence_configured(fields.get("BOOT", "")),
        "restart_policy": fields.get("RESTART", ""),
    }
    result["operations"]["unaided_presence"] = presence

    # Arrival failures are reported without touching the node: starting the
    # service here would destroy the very property under test.
    if not presence["running_on_arrival"] or not presence["domain_member"]:
        result["error"] = (
            f"{node_id}: IMEX was not running and/or not an operational domain member on arrival; "
            "not starting it, since that would invalidate the unaided-run observation"
        )
        print(json.dumps(result, indent=2))
        return 1

    # 2. Kill the daemon outright. Never `systemctl stop` - a correct
    #    supervisor will not restart a deliberate stop.
    #    Timing starts here, before the request: recovery is measured from the
    #    kill, so the round trip and settle below must be inside the window.
    kill_started = time.monotonic()
    killed = remote(
        f"sudo pkill -9 -x {daemon_bin}; sleep 1; "
        f"echo PID=$(pgrep -x {daemon_bin} | head -1); echo MEMBER=$({_domain_member_command()})"
    )
    post_kill = _parse_arrival(killed["stdout"]) if killed["ok"] else {}
    post_kill_pid = post_kill.get("PID", "")
    # The original daemon is gone if nothing is running OR if what is running is
    # a different process. Treating any live PID as "kill failed" would reject
    # precisely the well-supervised node this check exists to pass, because a
    # fast supervisor can replace the process inside the settle window.
    confirmed = killed["ok"] and post_kill_pid != original_pid
    result["operations"]["terminate"] = {"method": "kill", "confirmed": confirmed}

    if not confirmed:
        # The kill may have landed before the failure (the remote shell runs
        # pkill first), so put the node back rather than abandoning it down.
        result["operations"]["restore"] = restore_node()
        result["error"] = f"{node_id}: could not confirm the daemon was killed, so recovery cannot be attributed"
        print(json.dumps(result, indent=2))
        return 1

    # 3. Poll for automatic recovery, with no intervention of any kind. A
    #    supervisor fast enough to have already replaced the process counts as
    #    recovered without waiting for another poll.
    recovered = bool(post_kill_pid) and post_kill.get("MEMBER") == "yes"
    while not recovered and time.monotonic() - kill_started < args.recovery_timeout:
        time.sleep(RECOVERY_POLL_SECONDS)
        probe = remote(f"echo PID=$(pgrep -x {daemon_bin} | head -1); echo MEMBER=$({_domain_member_command()})")
        # A probe can itself take up to the per-command SSH timeout, so the
        # deadline is re-checked on the way out. Accepting a result that landed
        # after the bound would report success past the deadline it exists to
        # enforce.
        if time.monotonic() - kill_started >= args.recovery_timeout:
            break
        if not probe["ok"]:
            continue
        current = _parse_arrival(probe["stdout"])
        if current.get("PID") and current.get("PID") != original_pid and current.get("MEMBER") == "yes":
            recovered = True

    elapsed = round(time.monotonic() - kill_started, 1)
    result["operations"]["recovery"] = {
        "domain_member": recovered,
        "elapsed_seconds": elapsed,
        "operator_intervention": False,
    }

    # 4. Restore prior state regardless of outcome - this check is destructive
    #    and must not leave the node down for the next one.
    result["operations"]["restore"] = restore_node()

    if not recovered:
        result["error"] = (
            f"{node_id}: the node did not return to operational domain membership within "
            f"{args.recovery_timeout}s of the daemon being killed (restart policy: "
            f"{presence['restart_policy'] or 'unreported'})"
        )

    result["success"] = recovered
    print(json.dumps(result, indent=2))
    return 0 if recovered else 1


if __name__ == "__main__":
    sys.exit(main())
