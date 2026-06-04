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

"""Stop a Compute Engine VM and verify the canonical 'stopped' state.

Divergences from the AWS oracle:
  * Compute Engine accepts ``instances.stop`` mid-cloud-init, leaving
    the guest dirty on next boot. Pre-gate on ``cloud-init status
    --wait`` over SSH (exit codes 0 and 2 are terminal).
  * ``instances.stop`` returns a zonal Operation — wait on the op,
    then poll ``instances.get`` until ``canonical_state == 'stopped'``
    (Compute Engine reports raw ``TERMINATED``).
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
    get_instance,
    narrow_region_to_zone,
    poll_instance_state,
    resolve_project,
    retry_zonal_lifecycle_op,
)
from common.errors import handle_gcp_errors
from common.ssh_utils import wait_for_cloud_init
from google.cloud import compute_v1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Stop a Compute Engine VM")
    parser.add_argument("--instance-id", required=True, help="Instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument(
        "--key-file",
        default=None,
        help="SSH private key for the pre-stop cloud-init wait",
    )
    parser.add_argument("--public-ip", default=None, help="Pre-stop public IP")
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
        "stop_initiated": False,
        "cloud_init_pre_stop": None,
    }

    try:
        # 1. Pre-check current state. Idempotent no-op when already stopped.
        print("Checking instance state before stop...", file=sys.stderr)
        inst = get_instance(project, zone, args.instance_id)
        cstate = canonical_state(inst.status)

        if cstate == "stopped":
            # Idempotent short-circuit: the instance is already in the
            # target state. ``stop_initiated`` is a CONTRACT-shaped field —
            # InstanceStopCheck reads it as "the stop end-state contract is
            # satisfied", not "an API call was issued this run" — so it MUST
            # be True here, matching the AWS oracle. Flipping it to False to
            # mirror "no API called" makes the validator report FAILED on an
            # instance that is in fact correctly stopped. The honest journal
            # of which path executed lives in the distinct ``already_stopped``
            # diagnostic flag, not in the contract-shaped field.
            result["state"] = cstate
            result["stop_initiated"] = True
            result["already_stopped"] = True
            result["success"] = True
            print(f"  {args.instance_id} already stopped (no-op)", file=sys.stderr)
            print(json.dumps(result, indent=2, default=str))
            return 0

        if cstate != "running":
            result["state"] = cstate
            result["error"] = f"Instance is {cstate!r}, expected running"
            print(json.dumps(result, indent=2, default=str))
            return 1

        # 2. Pre-gate on cloud-init wait. Compute Engine accepts stop mid-
        # cloud-init, but the next boot will be dirty. If SSH ingredients
        # are supplied, the bool MUST be surfaced — helpers that return
        # ``bool`` for batch-cleanup safety MUST surface the bool into
        # ``result['success']``.
        if args.public_ip and args.key_file:
            print("Pre-gating stop on cloud-init wait...", file=sys.stderr)
            cloud_init_pre = wait_for_cloud_init(
                host=args.public_ip,
                user=args.ssh_user,
                key_file=args.key_file,
                timeout_seconds=600,
            )
            result["cloud_init_pre_stop"] = cloud_init_pre
            if not cloud_init_pre:
                result["error"] = (
                    "cloud-init did not complete cleanly (rc != 0/2) before stop; refusing to stop a dirty guest"
                )
                print(json.dumps(result, indent=2, default=str))
                return 1

        # 3. Stop. The op is zonal — wait on completion, then poll state.
        # Lifecycle ops are zone-bound (cannot walk on STOCKOUT); the
        # in-zone retry envelope wraps sync+wait (3 attempts, 60s/120s
        # backoff) and stamps stop_initiated on first sync success.
        print(f"Stopping instance {args.instance_id}...", file=sys.stderr)

        def _stamp_stop_initiated() -> None:
            result["stop_initiated"] = True

        client = compute_v1.InstancesClient()
        retry_zonal_lifecycle_op(
            lambda: client.stop(project=project, zone=zone, instance=args.instance_id),
            project,
            zone,
            resource_desc=f"stop {args.instance_id}",
            on_sync_success=_stamp_stop_initiated,
        )

        # 4. Poll canonical 'stopped' (Compute Engine raw 'TERMINATED').
        print("Waiting for canonical 'stopped' state...", file=sys.stderr)
        result["state"] = poll_instance_state(
            project,
            zone,
            args.instance_id,
            target_canonical="stopped",
            timeout=600,
        )
        result["success"] = True
        print("Stop completed", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
