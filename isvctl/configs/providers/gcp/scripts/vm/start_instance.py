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

"""Start a stopped Compute Engine VM and gate success on a stable guest.

Divergences from the AWS oracle:
  * Compute Engine reports raw ``TERMINATED`` for the canonical stopped
    state; use ``canonical_state(...)``.
  * Ephemeral external IPs are RELEASED on stop. ``--public-ip`` may be
    forwarded for diagnostics, but every post-start emission MUST come
    from a fresh ``instances.get`` / ``wait_for_public_ip`` read —
    public IP is NOT preserved across stop/start on Compute Engine.
  * First-SSH-success is not enough: the guest agent may rewrite
    authorized_keys mid-cloud-init replay. Gate on (1) cloud-init
    completion AND (2) N consecutive successful SSH probes —
    post-lifecycle steps gate on stability, not first SSH success.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    canonical_state,
    first_external_ip,
    first_internal_ip,
    get_instance,
    narrow_region_to_zone,
    poll_instance_state,
    resolve_project,
    retry_zonal_lifecycle_op,
    wait_for_public_ip,
)
from common.errors import handle_gcp_errors
from common.ssh_utils import wait_for_cloud_init, wait_for_ssh_stable
from google.cloud import compute_v1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Start a stopped Compute Engine VM")
    parser.add_argument("--instance-id", required=True, help="Instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
    parser.add_argument(
        "--public-ip",
        default=None,
        help="Pre-stop public IP (informational; re-read after start)",
    )
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "region": args.region,
        "zone": zone,
        "project": project,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "start_initiated": False,
        "ssh_ready": False,
    }

    try:
        # 1. Pre-check current state. The canonical stopped state is
        # required before issuing start; a previously-running VM or a
        # skipped/no-op stop step must not produce a green start,
        # otherwise the lifecycle test no longer proves stop→start
        # behavior.
        print("Verifying instance is stopped before start...", file=sys.stderr)
        inst = get_instance(project, zone, args.instance_id)
        cstate = canonical_state(inst.status)

        if cstate != "stopped":
            result["state"] = cstate
            result["error"] = f"Instance is {cstate!r}, expected stopped"
            print(json.dumps(result, indent=2, default=str))
            return 1

        # 2. Start; wait on zonal op then poll for canonical 'running'.
        # Lifecycle ops are zone-bound (cannot walk on STOCKOUT) — wrap
        # the sync+wait pair in the in-zone retry-with-backoff envelope
        # (zone_capacity_handling: 3 attempts, 60s/120s backoff). The
        # post-API stamp keeps
        # start_initiated tied to a real API acknowledgement rather than
        # firing speculatively.
        print(f"Starting instance {args.instance_id}...", file=sys.stderr)

        def _stamp_start_initiated() -> None:
            result["start_initiated"] = True

        client = compute_v1.InstancesClient()
        retry_zonal_lifecycle_op(
            lambda: client.start(project=project, zone=zone, instance=args.instance_id),
            project,
            zone,
            resource_desc=f"start {args.instance_id}",
            on_sync_success=_stamp_start_initiated,
        )

        print("Waiting for canonical 'running' state...", file=sys.stderr)
        result["state"] = poll_instance_state(
            project,
            zone,
            args.instance_id,
            target_canonical="running",
            timeout=300,
        )

        # 3. Re-read details from live state — public IP is the critical
        # one because Compute Engine releases the ephemeral on stop and
        # assigns a fresh one on start.
        inst = get_instance(project, zone, args.instance_id)
        result["private_ip"] = first_internal_ip(inst)
        fresh_ip = first_external_ip(inst) or wait_for_public_ip(project, zone, args.instance_id, timeout=120)
        if not fresh_ip:
            result["error"] = "Instance has no external IP after start (timed out polling)"
            print(json.dumps(result, indent=2, default=str))
            return 1
        result["public_ip"] = fresh_ip

        # 4. Stability gate. Consecutive SSH successes + cloud-init wait.
        print("Waiting for SSH to stabilize after start...", file=sys.stderr)
        ssh_ok = wait_for_ssh_stable(
            host=fresh_ip,
            user=args.ssh_user,
            key_file=args.key_file,
            consecutive=3,
            interval=10,
            max_attempts=36,
        )
        result["ssh_ready"] = ssh_ok
        if not ssh_ok:
            result["error"] = "SSH did not stabilize after start"
            print(json.dumps(result, indent=2, default=str))
            return 1

        cloud_init_ok = wait_for_cloud_init(
            host=fresh_ip,
            user=args.ssh_user,
            key_file=args.key_file,
            timeout_seconds=600,
        )
        result["cloud_init_ok"] = cloud_init_ok
        if not cloud_init_ok:
            result["error"] = "cloud-init did not complete after start (rc != 0/2)"
            print(json.dumps(result, indent=2, default=str))
            return 1

        result["success"] = True
        print("Start completed", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
