#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Reboot a Compute Engine VM via ``instances.reset`` and affirm recovery.

Compute Engine has no soft-reboot equivalent of the AWS oracle's
``reboot_instances``; ``instances.reset`` is a HARD reset that returns
before the guest restarts. On async soft-reboot APIs, wait for SSH to
DROP before waiting for it to stabilize:

    1. Sample pre-reset uptime over SSH (best-effort).
    2. Pre-gate on ``cloud-init status --wait`` so the reset doesn't land
       mid-init (pre-lifecycle defensive gate).
    3. Issue ``instances.reset``; record the request timestamp.
    4. Wait for SSH to STOP responding (90s budget) — confirms the pre-
       reset sshd has dropped, so subsequent uptime/boot reads cannot
       falsely confirm reboot.
    5. Poll canonical 'running'; re-read the public IP from live state.
    6. Stability gate against the post-reset sshd.
    7. Sample post-reset uptime. Confirm via
       ``boot_started_at >= reboot_requested_at`` OR
       ``post_uptime < pre_uptime`` — emit ``reboot_confirmed`` as a real
       bool, never literal True.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
from common.ssh_utils import (
    get_uptime_via_ssh,
    wait_for_cloud_init,
    wait_for_ssh_drop,
    wait_for_ssh_stable,
)
from google.cloud import compute_v1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Reboot a Compute Engine VM via reset")
    parser.add_argument("--instance-id", required=True, help="Instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
    parser.add_argument("--public-ip", required=True, help="Pre-reset public IP (re-read after)")
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
        "reboot_initiated": False,
        "ssh_ready": False,
        "reboot_confirmed": False,
        "ssh_drop_observed": False,
    }

    try:
        # 1. Pre-check + pre-uptime sample (best-effort: a missing sample
        # is recoverable as long as the boot-timestamp check succeeds).
        print("Verifying instance is running before reboot...", file=sys.stderr)
        inst = get_instance(project, zone, args.instance_id)
        cstate = canonical_state(inst.status)
        if cstate != "running":
            result["state"] = cstate
            result["error"] = f"Instance is {cstate!r}, expected running"
            print(json.dumps(result, indent=2, default=str))
            return 1

        pre_uptime = get_uptime_via_ssh(args.public_ip, args.ssh_user, args.key_file)
        if pre_uptime is not None:
            result["pre_reboot_uptime"] = round(pre_uptime, 1)
            print(f"  pre-reboot uptime: {pre_uptime:.0f}s", file=sys.stderr)

        # 2. Pre-gate on cloud-init wait — refuse to reset mid-init.
        cloud_init_pre = wait_for_cloud_init(
            host=args.public_ip,
            user=args.ssh_user,
            key_file=args.key_file,
            timeout_seconds=600,
        )
        result["cloud_init_pre_reboot"] = cloud_init_pre
        if not cloud_init_pre:
            result["error"] = (
                "cloud-init did not complete cleanly (rc != 0/2) before reset; "
                "refusing to reset a guest in an unsettled state"
            )
            print(json.dumps(result, indent=2, default=str))
            return 1

        # 3. Reset. Stamp the request timestamp BEFORE the API call so
        # the post-reset boot_started_at >= reboot_requested_at check
        # has a stable comparison anchor. Lifecycle ops are zone-bound
        # (cannot walk on STOCKOUT); wrap sync+wait in the in-zone
        # retry-with-backoff envelope (3 attempts, 60s/120s backoff).
        print(f"Resetting instance {args.instance_id}...", file=sys.stderr)
        reboot_requested_at = time.time()

        def _stamp_reboot_initiated() -> None:
            result["reboot_initiated"] = True

        client = compute_v1.InstancesClient()
        retry_zonal_lifecycle_op(
            lambda: client.reset(project=project, zone=zone, instance=args.instance_id),
            project,
            zone,
            resource_desc=f"reset {args.instance_id}",
            on_sync_success=_stamp_reboot_initiated,
            op_timeout=300,
        )

        # 4. SSH-drop wait — best-effort observability signal. If the
        # guest reboots and sshd recovers faster than our probe interval,
        # we may miss the drop window entirely (the budget extends past
        # the canonical reset+boot timeline of a g2-standard-8 in
        # practice). The boot_started_at >= reboot_requested_at gate
        # below is the LOAD-BEARING reboot signal — it is robust whether
        # or not we observe the drop, because a lingering pre-reset
        # uptime would compute to a boot_started_at far earlier than
        # reboot_requested_at and the gate would correctly reject the
        # reboot. Matches the AWS oracle's no-SSH-drop pattern while
        # preserving the observation for diagnostics.
        print("Waiting for pre-reset SSH to drop (best-effort)...", file=sys.stderr)
        drop_observed = wait_for_ssh_drop(
            host=args.public_ip,
            user=args.ssh_user,
            key_file=args.key_file,
            max_attempts=18,
            interval=5,
        )
        result["ssh_drop_observed"] = drop_observed
        if not drop_observed:
            print(
                "  WARNING: SSH-drop not observed within budget; relying on "
                "boot_started_at >= reboot_requested_at to adjudicate reboot",
                file=sys.stderr,
            )

        # 5. Poll canonical 'running' + re-read public IP from live state.
        print("Waiting for canonical 'running' state...", file=sys.stderr)
        result["state"] = poll_instance_state(
            project,
            zone,
            args.instance_id,
            target_canonical="running",
            timeout=300,
        )
        inst = get_instance(project, zone, args.instance_id)
        result["private_ip"] = first_internal_ip(inst)
        public_ip = first_external_ip(inst) or wait_for_public_ip(project, zone, args.instance_id, timeout=120)
        if not public_ip:
            result["error"] = "Instance has no external IP after reset (timed out polling)"
            print(json.dumps(result, indent=2, default=str))
            return 1
        result["public_ip"] = public_ip

        # 6. Stability gate against the fresh sshd.
        print("Waiting for post-reset SSH to stabilize...", file=sys.stderr)
        ssh_ok = wait_for_ssh_stable(
            host=public_ip,
            user=args.ssh_user,
            key_file=args.key_file,
            consecutive=3,
            interval=10,
            max_attempts=36,
        )
        result["ssh_ready"] = ssh_ok
        if not ssh_ok:
            result["error"] = "SSH did not stabilize after reset"
            print(json.dumps(result, indent=2, default=str))
            return 1

        # 7. Post-reset uptime. The boot-started-at comparison is the
        # primary signal; uptime-decreased is a fallback when the
        # pre-reset sample succeeded.
        post_uptime = get_uptime_via_ssh(public_ip, args.ssh_user, args.key_file)
        if post_uptime is None:
            result["error"] = "Could not sample post-reset uptime via SSH"
            print(json.dumps(result, indent=2, default=str))
            return 1
        result["uptime_seconds"] = round(post_uptime, 1)
        print(f"  post-reboot uptime: {post_uptime:.0f}s", file=sys.stderr)

        boot_started_at = time.time() - post_uptime
        if boot_started_at >= reboot_requested_at:
            result["reboot_confirmed"] = True
            print("  reboot confirmed (boot time follows reset request)", file=sys.stderr)
        elif pre_uptime is not None and post_uptime < pre_uptime:
            result["reboot_confirmed"] = True
            print("  reboot confirmed (uptime decreased)", file=sys.stderr)
        else:
            result["reboot_confirmed"] = False
            result["error"] = (
                "Reboot not affirmed: post-reset boot time precedes reset request "
                "and pre-reset uptime sample missing or did not decrease"
            )

        result["success"] = result["reboot_confirmed"]
        if result["success"]:
            print("Reboot completed", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
