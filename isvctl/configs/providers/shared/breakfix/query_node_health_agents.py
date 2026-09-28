#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read-only reference implementation of the BFX04-01 node-health-agent query.

Reports, per configured GPU node, whether a GPU health monitoring process is
running, and names the one it found. Evidence comes from ``systemctl is-active``
over SSH, so it works on any bare-metal fleet whose control-plane API does not
expose agent state.

BFX04-01 itself names no product -- the requirement is that *something* is
watching GPU health -- so the unit list below is this reference's answer to
"which processes count here", not the requirement's. A fleet running a different
agent names its own units through ``--units`` rather than editing this shared
script or failing the check.

Nodes are named through ``--nodes``. With none configured the step emits a
structured skip: an unconfigured site is indistinguishable from one with no
agents, and a pass there would assert nothing.

SSH uses the caller's own configuration by default. ``--ssh-user`` and
``--key-file`` name the login and private key instead, for providers that
provision a host with a key generated for the run. Host keys are still checked
against the caller's known hosts unless ``--no-host-key-check`` is also given:
a freshly provisioned host's key cannot be known in advance, so a provider may
opt out, as its own SSH helpers do for the hosts they provision.

``--expected-nodes`` is how a short list stops being a quiet pass. The probe
judges the nodes it is given, so naming three of a site's sixty-four would
otherwise carry BFX04-01 for the whole site. The fleet size is therefore
declared separately -- ideally from the provider's own inventory rather than by
hand -- and the check fails when the records do not cover it. Deriving it in
YAML has one trap worth knowing: an aggregate like ``| length`` over a step that
never ran renders ``0`` rather than empty, so the orchestrator's missing-step
guard does not fire. A zero is rejected below instead, which turns that into a
loud failure rather than a fleet of nobody that everything covers.

Probes fan out across nodes because they are dominated by SSH round-trip
latency. A serial probe of a full rack would outlive the step timeout and the
executor would kill the process before it could emit its JSON contract.

Fanning out bounds the wall time but does not cap it: an unreachable fleet costs
``ceil(nodes / MAX_CONCURRENT_PROBES) * PROBE_TIMEOUT_SECONDS``, which on a large
enough fleet still outruns the step timeout. So the sweep also has its own
deadline, set below that timeout, and stops when it expires. Whatever remains
unprobed fails the step by name -- a diagnosis the orchestrator cannot give,
because killing the process discards stdout and with it the JSON contract.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

TEST_NAME = "query_node_health_agents"

# systemd units this reference probes by default: NVIDIA Fleet Intelligence
# Agent (GPUd) and the NVSentinel GPU Health Monitor, as of Aug 2026. Generic
# GPU telemetry (a DCGM exporter, say) is deliberately absent: it reports
# metrics but performs none of the health detection the requirement is about.
AGENT_UNITS = ("fleetintd", "gpud", "nvsentinel", "gpu-health-monitor")

# SSH targets must be bare host names or addresses. Anything else could be read
# by ssh(1) as an option (`-oProxyCommand=...`) or by the remote shell.
NODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Unit names reach the remote shell inside the systemctl argument list, so they
# are held to systemd's own alphabet rather than interpolated as given.
UNIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._-]*$")

# Login names reach ssh(1) as the value of ``-l``; held to a portable user-name
# alphabet so a value can never smuggle in another option.
USER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")

CONNECT_TIMEOUT_SECONDS = 10
PROBE_TIMEOUT_SECONDS = 20
# Sized so a full rack still fits inside the budget below: the sweep costs
# ceil(nodes / MAX_CONCURRENT_PROBES) * PROBE_TIMEOUT_SECONDS at worst, and each
# probe is a blocked thread waiting on SSH rather than any local work.
MAX_CONCURRENT_PROBES = 32

# Wall-clock ceiling for reporting, chosen to sit below the step timeout the
# providers configure (300s). The process itself can outlive this by one probe
# timeout -- the pool's threads are non-daemon, so the interpreter joins the
# in-flight probes on the way out even though the JSON is already written --
# so budget for PROBE_BUDGET_SECONDS + PROBE_TIMEOUT_SECONDS against that
# timeout, and raise the two together, never this alone.
PROBE_BUDGET_SECONDS = 240.0


class NodeHealthQueryError(RuntimeError):
    """Raised when a node's health-agent state cannot be determined."""


class ProviderArgumentParser(argparse.ArgumentParser):
    """Raise provider errors instead of exiting without a JSON result."""

    def error(self, message: str) -> None:
        """Convert invalid arguments into the provider failure path."""
        raise NodeHealthQueryError(f"Invalid arguments: {message}")


def _split(value: str) -> list[str]:
    """Return the de-duplicated, non-empty items of a comma- or space-separated list."""
    return list(dict.fromkeys(part for part in re.split(r"[\s,]+", value) if part))


def _parse_nodes(value: str) -> list[str]:
    """Return the de-duplicated node list, rejecting unsafe SSH targets."""
    nodes = _split(value)
    if any(not NODE_PATTERN.fullmatch(node) for node in nodes):
        raise NodeHealthQueryError("Invalid bare-metal node name")
    return nodes


def _parse_units(value: str) -> tuple[str, ...]:
    """Return the unit list to probe, falling back to this reference's default."""
    units = _split(value)
    if not units:
        return AGENT_UNITS
    if any(not UNIT_PATTERN.fullmatch(unit) for unit in units):
        raise NodeHealthQueryError("Invalid health agent unit name")
    return tuple(units)


def _ssh_options(user: str, key_file: str, no_host_key_check: bool = False) -> tuple[str, ...]:
    """Return the ssh(1) options selecting the login, key, and host-key policy; empty for the caller's defaults."""
    options: list[str] = []
    if user:
        if not USER_PATTERN.fullmatch(user):
            raise NodeHealthQueryError("Invalid SSH user name")
        options += ["-l", user]
    if key_file:
        options += ["-i", key_file, "-o", "IdentitiesOnly=yes"]
    if no_host_key_check:
        options += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    return tuple(options)


def _parse_expected(value: str, nodes: list[str]) -> int:
    """Return the declared GPU fleet size, rejecting a count that asserts nothing."""
    if not nodes:
        return 0
    if not value.strip():
        raise NodeHealthQueryError(
            "--expected-nodes is required alongside --nodes: without the platform's GPU node count "
            "a probe of a subset would pass BFX04-01 for the whole fleet"
        )
    try:
        # A count below 1 is rejected by _query, which owns that invariant for
        # every caller rather than only for this one.
        return int(value)
    except ValueError:
        raise NodeHealthQueryError(f"Invalid expected node count: {value!r}") from None


def _active_unit(node: str, units: tuple[str, ...] = AGENT_UNITS, ssh_options: tuple[str, ...] = ()) -> str:
    """Return the supported agent unit active on ``node``, or an empty string.

    ``systemctl is-active`` prints one state per unit argument, in order, and
    exits non-zero unless every unit is active - so the exit status is not a
    useful signal here and the states are read positionally instead. ``|| true``
    keeps the remote shell's status clean so a non-zero status can only mean the
    SSH transport itself failed.
    """
    remote_command = f"systemctl is-active {' '.join(units)} 2>/dev/null || true"
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        *ssh_options,
        # Before the host, not after: ssh only stops option parsing at a `--`
        # that precedes its first non-option argument.
        "--",
        node,
        remote_command,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NodeHealthQueryError(f"Health agent query failed for {node}") from exc
    if completed.returncode != 0:
        raise NodeHealthQueryError(f"Health agent query failed for {node}")

    states = completed.stdout.split()
    if len(states) != len(units):
        raise NodeHealthQueryError(f"Health agent query returned an unreadable status for {node}")
    return next((unit for unit, state in zip(units, states, strict=True) if state == "active"), "")


def _probe(node: str, units: tuple[str, ...] = AGENT_UNITS, ssh_options: tuple[str, ...] = ()) -> str | None:
    """Return the active agent unit, or None when the node yielded no evidence.

    A node we could not read is not a node without an agent, so it must not
    become a ``running: False`` record, which would report a missing agent on a
    host the probe never reached. It fails the step instead. A node that *was*
    read and has nothing running does belong in the output, as the check expects
    every GPU node accounted for rather than the covered ones listed.
    """
    try:
        return _active_unit(node, units, ssh_options)
    except NodeHealthQueryError:
        return None


def _sweep(
    nodes: list[str],
    budget_seconds: float,
    units: tuple[str, ...] = AGENT_UNITS,
    ssh_options: tuple[str, ...] = (),
) -> dict[str, str | None]:
    """Probe every node concurrently, abandoning the sweep when the budget expires.

    Returns the units keyed by node; a node missing from the mapping was never
    probed within the budget. Shutdown does not wait, so the result is reported
    while any in-flight probe drains -- it is bounded by its own subprocess
    timeout, and stdout has already been written by the time it finishes.
    """
    probed: dict[str, str | None] = {}
    pool = ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_PROBES, len(nodes)))
    try:
        pending = {pool.submit(_probe, node, units, ssh_options): node for node in nodes}
        for future in as_completed(pending, timeout=budget_seconds):
            probed[pending[future]] = future.result()
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return probed


def _query(
    nodes: list[str],
    *,
    expected: int,
    budget_seconds: float = PROBE_BUDGET_SECONDS,
    units: tuple[str, ...] = AGENT_UNITS,
    ssh_options: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return provider-neutral BFX04-01 evidence for every configured node.

    ``expected`` is required and has no default on purpose. The one value that
    must never fill it in is ``len(nodes)``: the fleet size exists to be compared
    against the list being probed, so sourcing it from that list compares it to
    itself and any subset covers the fleet.
    """
    if not nodes:
        return {
            "success": True,
            "platform": "bare_metal",
            "test_name": TEST_NAME,
            "skipped": True,
            "skip_reason": "No GPU nodes configured for health-agent inspection",
            "agents_observable": False,
            "agents": [],
        }
    # Enforced here rather than only at the CLI boundary so no caller can reach
    # the contract below with a size that asserts nothing.
    if expected < 1:
        raise NodeHealthQueryError(
            f"Expected node count must be at least 1 with {len(nodes)} node(s) to probe, got {expected}; "
            "a platform reporting no GPU nodes cannot be covered by probing some"
        )
    probed = _sweep(nodes, budget_seconds, units, ssh_options)
    # Every node that yielded nothing is named, so a fleet-wide access problem
    # takes one run to diagnose rather than one run per node. Abandoned and
    # unreadable are reported apart: the first is this sweep running out of
    # time, the second is the node refusing to answer.
    abandoned = [node for node in nodes if node not in probed]
    unreadable = [node for node in nodes if node in probed and probed[node] is None]
    if abandoned:
        raise NodeHealthQueryError(
            f"Health agent query exceeded its {budget_seconds:g}s budget with "
            f"{len(abandoned)} node(s) unprobed: {', '.join(abandoned)}"
        )
    if unreadable:
        raise NodeHealthQueryError(f"Health agent query failed for {len(unreadable)} node(s): {', '.join(unreadable)}")
    return {
        "success": True,
        "platform": "bare_metal",
        "test_name": TEST_NAME,
        "agents_observable": True,
        "nodes_expected": expected,
        "agents": [{"node_id": node, "agent_name": probed[node], "running": bool(probed[node])} for node in nodes],
    }


def main() -> int:
    """Emit one structured BFX04-01 result for the configured nodes."""
    parser = ProviderArgumentParser(description="Query NVIDIA node-health agents")
    parser.add_argument(
        "--nodes",
        default="",
        help="Comma-separated GPU nodes to inspect over SSH",
    )
    parser.add_argument(
        "--expected-nodes",
        default="",
        help="How many GPU nodes the platform has, so partial coverage cannot pass unnoticed",
    )
    parser.add_argument(
        "--units",
        default="",
        help=f"Comma-separated systemd units that count as a GPU health agent (default: {', '.join(AGENT_UNITS)})",
    )
    parser.add_argument("--ssh-user", default="", help="SSH login name (default: the caller's SSH configuration)")
    parser.add_argument(
        "--key-file",
        default="",
        help="SSH private key for hosts provisioned with a run-generated key",
    )
    parser.add_argument(
        "--no-host-key-check",
        action="store_true",
        help="Neither check nor record host keys (for hosts provisioned by the run, whose key is not known yet)",
    )
    try:
        args = parser.parse_args()
        nodes = _parse_nodes(args.nodes)
        result = _query(
            nodes,
            units=_parse_units(args.units),
            expected=_parse_expected(args.expected_nodes, nodes),
            ssh_options=_ssh_options(args.ssh_user, args.key_file, args.no_host_key_check),
        )
    except NodeHealthQueryError as exc:
        result = {
            "success": False,
            "platform": "bare_metal",
            "test_name": TEST_NAME,
            "error_type": "node_health_query_failed",
            "error": str(exc),
        }
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
