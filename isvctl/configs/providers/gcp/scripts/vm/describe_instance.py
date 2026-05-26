#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Describe a Compute Engine VM as the anchor for host-level validators.

Per the suite contract this step runs AFTER reboot so host-level checks
(ConnectivityCheck, GpuCheck, etc.) bind to the post-reboot guest. The
stub itself just reads the current state via ``instances.get`` and
forwards SSH ingredients.

Divergences:
  * Compute Engine reports the zone as a self-link; emit the short name
    under ``availability_zone`` for parity with the AWS oracle.
  * Use ``canonical_state(...)`` so downstream code branches on the
    documented vocabulary, not the raw GCE enum.
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
    resolve_project,
    short_name,
)
from common.errors import handle_gcp_errors
from google.api_core import exceptions as gax


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Describe a Compute Engine VM")
    parser.add_argument("--instance-id", required=True, help="Instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
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
    }

    try:
        inst = get_instance(project, zone, args.instance_id)
        result["state"] = canonical_state(inst.status)
        result["instance_type"] = short_name(inst.machine_type)
        result["public_ip"] = first_external_ip(inst)
        result["private_ip"] = first_internal_ip(inst)
        if inst.network_interfaces:
            result["vpc_id"] = short_name(inst.network_interfaces[0].network)
            if inst.network_interfaces[0].subnetwork:
                result["subnet_id"] = short_name(inst.network_interfaces[0].subnetwork)
        result["availability_zone"] = short_name(inst.zone)
        result["launch_time"] = getattr(inst, "creation_timestamp", None)
        result["success"] = True

    except gax.NotFound as e:
        result["error"] = f"Instance {args.instance_id} not found: {e}"
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
