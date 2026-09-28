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

"""Provision a Firebird bare-metal GPU server for BMaaS testing.

Firebird BMs are pre-allocated to the tenant, so "launch" means: pick a BM,
attach it to the project and subnet (if not already), then provision an OS
image with cloud-init user-data that authorizes an SSH key.

BM selection:
    --bm-id (or FIREBIRD_BM_ID) names the server explicitly. Otherwise the
    first AVAILABLE BM in the tenant pool that is in no other project and on no
    other subnet is used - restricted to --instance-type (a machine-type ID)
    when one is given - preferring BMs already attached to the project.

SSH key:
    Without --key-file, a new key pair is generated in a fresh private
    directory (tempfile.mkdtemp, mode 0700) and reported with
    "generated_key": true; teardown deletes only such a key. --key-file reuses
    an existing key instead (it must exist) and is never deleted.

Instance reuse (dev workflow):
    Set BM_INSTANCE_ID and BM_KEY_FILE to skip provisioning and describe an
    already-provisioned BM instead.

Usage:
    python launch_instance.py --name isv-bm-test-gpu --instance-type <machine-type-id> \
        --vpc-id vpc.xxx --subnet-id subnet.xxx --image-id image.xxx

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "instance_type": "<machine-type-id>",
    "public_ip": "10.x.x.x",
    "private_ip": "10.x.x.x",
    "state": "running",
    "vpc_id": "vpc.xxx",
    "subnet_id": "subnet.xxx",
    "image_id": "image.xxx",
    "key_name": "isv-bm-test-gpu-key",
    "key_file": "/tmp/isv-bm-test-gpu-abc123/isv-bm-test-gpu-key",
    "generated_key": true,
    "ssh_user": "ubuntu",
    "owned": true,
    "attached_project": true,
    "attached_subnet": true
}
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, to_state
from common.provision import provision_bm


def reuse_existing_instance(args: argparse.Namespace) -> int:
    """Describe an existing BM instead of provisioning one.

    Used when BM_INSTANCE_ID and BM_KEY_FILE are set.

    Returns:
        0 on success, 1 on failure
    """
    instance_id = os.environ["BM_INSTANCE_ID"]
    print(f"Reusing existing instance {instance_id}", file=sys.stderr)
    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": instance_id,
        "key_file": os.environ["BM_KEY_FILE"],
        "key_name": Path(os.environ["BM_KEY_FILE"]).name,
        "generated_key": False,
        "ssh_user": args.ssh_user,
        "vpc_id": args.vpc_id,
        "reused": True,
        # Reuse is an explicit operator opt-in: teardown deprovisions this BM unless
        # BM_SKIP_TEARDOWN=true. Attachments made by an earlier run are left in place.
        "owned": True,
        "attached_project": False,
        "attached_subnet": False,
    }
    try:
        bm = FirebirdClient().get_bm(instance_id)
        result["state"] = to_state(bm)
        result["instance_type"] = bm.get("machineTypeId")
        result["public_ip"] = bm.get("ipAddress")
        result["private_ip"] = bm.get("ipAddress")
        result["subnet_id"] = bm.get("subnetId")
        result["success"] = result["state"] == "running"
        if not result["success"]:
            result["error"] = f"Instance {instance_id} is {result['state']}, expected running"
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


def main() -> int:
    """Provision a Firebird bare-metal GPU server and wait until SSH is ready.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Provision Firebird bare-metal GPU server")
    parser.add_argument("--name", default="isv-bm-test-gpu", help="Value for the Name tag")
    parser.add_argument("--instance-type", default="", help="Machine-type ID used to pick a BM (optional)")
    parser.add_argument("--bm-id", default=os.environ.get("FIREBIRD_BM_ID", ""), help="BM to use (bm.ULID)")
    parser.add_argument("--vpc-id", required=True, help="VPC of the subnet (vpc.ULID)")
    parser.add_argument("--subnet-id", required=True, help="Subnet to attach the BM to (subnet.ULID)")
    parser.add_argument("--image-id", required=True, help="OS image to provision (image.ULID)")
    parser.add_argument("--ssh-user", default="ubuntu", help="Default user of the image")
    parser.add_argument(
        "--key-file", default="", help="Existing SSH private key to reuse (default: generate one for this run)"
    )
    parser.add_argument(
        "--timeout", type=int, default=3900, help="Overall timeout in seconds (keep below the step timeout)"
    )
    args = parser.parse_args()

    if os.environ.get("BM_INSTANCE_ID") and os.environ.get("BM_KEY_FILE"):
        return reuse_existing_instance(args)

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": "",
        "instance_type": args.instance_type,
        "vpc_id": args.vpc_id,
        "subnet_id": args.subnet_id,
        "image_id": args.image_id,
        "key_name": Path(args.key_file).name if args.key_file else "",
        "key_file": args.key_file,
        # True once provision_bm generates this run's key; drives its deletion at teardown.
        "generated_key": False,
        "ssh_user": args.ssh_user,
        # Set by provision_bm when this run starts each mutation (see its docstring),
        # so teardown undoes exactly what this run started.
        "owned": False,
        "attached_project": False,
        "attached_subnet": False,
    }

    deadline = time.monotonic() + args.timeout
    try:
        provision_bm(
            FirebirdClient(),
            result,
            name=args.name,
            bm_id=args.bm_id,
            machine_type=args.instance_type,
            subnet_id=args.subnet_id,
            image_id=args.image_id,
            ssh_user=args.ssh_user,
            key_file=args.key_file,
            deadline=deadline,
        )
        result["success"] = True
        print("Provisioning completed successfully!", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
