#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared plumbing for the IMEX host checks (SDN17-01, SDN21-01).

Both IMEX checks sweep the same set of SSH-reachable nodes with the same
bounded-concurrency policy and the same rule for when an under-configured run
should skip rather than fail. Only what each one *asks* a node differs, so that
is all the callers implement; everything around it lives here so the two cannot
drift apart.

CLI flags are deliberately NOT declared here. ``test_stub_contracts`` statically
walks each stub for ``add_argument`` calls to prove the wired YAML only passes
flags the script accepts, and that guard is worth more than the handful of lines
sharing them would save - so each script declares its own flags, using the
defaults below so the values cannot drift.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from common.ssh_utils import ssh_run

DEFAULT_SSH_USER = "ubuntu"
DEFAULT_TIMEOUT_SECONDS = 30
# Cap simultaneous ssh processes so a large IMEX domain or allocation does not
# fan out into hundreds of them at once.
MAX_PARALLEL_QUERIES = 16
# Overall sweep deadline. Kept well under the wired step timeout so the caller
# always wins the race and prints its JSON contract instead of being killed.
DEFAULT_DEADLINE_SECONDS = 90


def parse_node_ids(raw: str) -> list[str]:
    """Split a comma-separated ``--node-ids`` value into clean node IDs."""
    return [node_id.strip() for node_id in raw.split(",") if node_id.strip()]


def config_gate(node_ids: list[str], key_file: str, *, subject: str) -> dict[str, str] | None:
    """Decide whether an under-configured run should skip or fail.

    The IMEX checks need a pre-existing cluster that a normal network run does
    not provision, so a run that was simply never pointed at one must skip
    rather than fail every unrelated check in the suite. A *partially*
    configured run is different: it means someone aimed the check at a cluster
    and got it wrong, which should not pass silently.

    Returns ``None`` when the run is fully configured, otherwise a dict with
    either ``skip_reason`` or ``error``.
    """
    if not node_ids:
        detail = "no node IDs or SSH key set" if not key_file else "no node IDs set"
        return {"skip_reason": f"{subject} not configured for this run ({detail})"}
    if not key_file:
        return {"error": "--key-file (or AWS_IMEX_KEY_FILE) is required to SSH into the node(s)"}
    return None


def run_remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
    """Run one command on a node, classifying failure into the sweep's result shape.

    Returns ``{"host", "ok": True, "stdout"}`` on success, or
    ``{"host", "ok": False, "error"}`` on a non-zero exit. Callers layer their
    own parsing on top; failing here is reported rather than raised so one bad
    node never takes down the whole sweep.
    """
    exit_code, stdout, stderr = ssh_run(host, user, key_file, command, timeout=timeout)
    if exit_code != 0:
        return {"host": host, "ok": False, "error": stderr.strip() or f"exit code {exit_code}"}
    return {"host": host, "ok": True, "stdout": stdout}


def sweep_nodes(
    hosts: list[str],
    query: Callable[[str], dict[str, Any]],
    *,
    deadline: int,
    action: str = "query",
) -> dict[str, dict[str, Any]]:
    """Run ``query`` against every host concurrently, one result per host.

    Concurrency is capped (rather than one thread per node) so a large fleet
    does not fan out into hundreds of simultaneous ssh processes. That cap means
    wall-clock time is roughly ``ceil(len(hosts) / MAX_PARALLEL_QUERIES) *
    per-node timeout``, which against an unresponsive fleet could otherwise run
    past the orchestrator's step timeout and get the caller killed before it
    prints anything. So the whole sweep is also bounded by ``deadline``: hosts
    that have not answered by then come back as timed-out errors and the caller
    still emits its full structured JSON contract.

    Args:
        hosts: Node IDs to sweep, also passed to ``query`` as the SSH target.
        query: Per-host callable returning a result dict; it owns whatever the
            check actually asks the node, and is expected to report its own
            failures as ``{"ok": False, "error": ...}`` rather than raising.
        deadline: Overall wall-clock budget for the whole sweep, in seconds.
        action: Noun used in the timeout message ("query", "probe", ...).
    """
    results: dict[str, dict[str, Any]] = {}
    if not hosts:
        return results

    pool = ThreadPoolExecutor(max_workers=min(len(hosts), MAX_PARALLEL_QUERIES))
    try:
        futures = {pool.submit(query, host): host for host in hosts}
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
                    "error": f"{action} did not complete within the {deadline}s deadline",
                }
    finally:
        # Do not block on stragglers - each in-flight ssh_run is already bounded
        # by its own per-node timeout, and queued work is dropped outright.
        pool.shutdown(wait=False, cancel_futures=True)
    return results
