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

"""Provisioned-BM access and SSH network probes for the Firebird network scripts.

The network suite does not provision BMs. Checks that need a host run against an
already-provisioned BM (``BM_INSTANCE_ID`` + ``BM_KEY_FILE``, as left by a
bare_metal run with ``BM_SKIP_TEARDOWN=true``), and the two-host checks also need
a peer BM (``FIREBIRD_PEER_BM_ID`` + ``FIREBIRD_PEER_KEY_FILE``). Without them
the check emits a structured skip naming what is missing.
"""

import argparse
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any

from common.firebird_client import FirebirdClient
from common.network import locate_subnet
from common.ssh_utils import ssh_run

PRIMARY_MISSING = "No provisioned BM configured (set BM_INSTANCE_ID and BM_KEY_FILE)"
PEER_MISSING = "No peer BM configured (set FIREBIRD_PEER_BM_ID and FIREBIRD_PEER_KEY_FILE)"

_RTT = re.compile(r"= [\d.]+/([\d.]+)/")


@dataclass(frozen=True)
class Host:
    """A running BM reachable over SSH."""

    bm_id: str
    ip: str
    subnet_id: str
    user: str
    key_file: str


def skipped(result: dict[str, Any], reason: str) -> dict[str, Any]:
    """Turn ``result`` into a structured skip."""
    result.update({"success": True, "skipped": True, "skip_reason": reason})
    return result


def running_host(client: FirebirdClient, bm_id: str, user: str, key_file: str) -> Host:
    """Return the BM as a Host; raise unless it is RUNNING with an IP."""
    bm = client.get_bm(bm_id)
    if bm.get("state") != "RUNNING" or not bm.get("ipAddress"):
        raise RuntimeError(f"BM {bm_id} is {bm.get('state')} with IP {bm.get('ipAddress') or 'none'}, expected RUNNING")
    return Host(bm_id, bm["ipAddress"], bm.get("subnetId") or "", user, key_file)


def run(host: Host, command: str, timeout: int = 40) -> tuple[int, str]:
    """Run ``command`` on ``host``; return (exit code, stdout)."""
    code, stdout, _ = ssh_run(host.ip, host.user, host.key_file, command, timeout=timeout)
    return code, stdout


def ping(host: Host, target: str, count: int = 3) -> tuple[bool, float | None]:
    """Ping ``target`` from ``host``; return (any reply received, average RTT in ms)."""
    code, stdout = run(host, f"ping -c {count} -W 2 {shlex.quote(target)}")
    match = _RTT.search(stdout)
    return code == 0, float(match.group(1)) if match else None


def wait_ping(host: Host, target: str, *, reachable: bool, timeout: int, interval: int = 5) -> bool:
    """Poll until pinging ``target`` from ``host`` succeeds (or fails); return whether it did in time.

    Rule enforcement lags the Operation, so a single probe right after a change
    would misreport it.
    """
    deadline = time.monotonic() + timeout
    while True:
        if ping(host, target, count=2)[0] == reachable:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def pair_missing(args: argparse.Namespace) -> str:
    """Return why the BM pair is not configured, or "" when it is."""
    if not (args.instance_id and args.key_file):
        return PRIMARY_MISSING
    if not (args.peer_id and args.peer_key_file):
        return PEER_MISSING
    return ""


def resolve_pair(client: FirebirdClient, args: argparse.Namespace) -> tuple[Host, Host, str]:
    """Return (primary, peer, VPC ID); raise unless both run in the same VPC."""
    primary = running_host(client, args.instance_id, args.ssh_user, args.key_file)
    peer = running_host(client, args.peer_id, args.ssh_user, args.peer_key_file)
    vpc_ids = {(locate_subnet(client, host.subnet_id)[0]).get("id") for host in (primary, peer)}
    if len(vpc_ids) != 1:
        raise RuntimeError(f"BMs {primary.bm_id} and {peer.bm_id} are in different VPCs; firewall rules are per VPC")
    return primary, peer, vpc_ids.pop()
