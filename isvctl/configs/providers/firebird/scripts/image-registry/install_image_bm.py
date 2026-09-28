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

"""Install the uploaded custom image on a bare-metal server (BOOT01-03).

Runs the bare_metal launch path (``common.provision``) with the uploaded image:
select an AVAILABLE tenant BM, attach it to the project and subnet if needed,
``POST .../provision`` the image with cloud-init user-data carrying an SSH key
generated for this run (in a fresh private directory; ``generated_key: true``),
and wait until the BM is RUNNING and answers SSH. The BM must then report
the uploaded image as its provisioned image.

It always provisions: an already-provisioned BM (``BM_INSTANCE_ID``) proves
nothing about the custom image. The ``owned`` / ``attached_*`` flags drive the
bare_metal teardown script, which deprovisions the BM - required before the
image can be deleted. Skips when the upload step was skipped.

Usage:
    python install_image_bm.py --image-id image.xxx --vpc-id vpc.xxx --subnet-id subnet.xxx

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "image_id": "image.xxx",
    "instance_state": "running",
    "state": "running",
    "key_file": "/tmp/isv-ir-test-bm-abc123/isv-ir-test-bm-key",
    "generated_key": true,
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

from common.firebird_client import FirebirdClient, log, to_state
from common.provision import provision_bm


def main() -> int:
    """Provision the uploaded image on a BM and emit JSON.

    Returns:
        0 on success or skip, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Install a custom image on a Firebird BM")
    parser.add_argument("--image-id", default="", help="Uploaded image (image.ULID)")
    parser.add_argument("--upload-skipped", action="store_true", help="The upload step was skipped")
    parser.add_argument("--vpc-id", default="", help="VPC of the subnet (vpc.ULID)")
    parser.add_argument("--subnet-id", default="", help="Subnet to attach the BM to (subnet.ULID)")
    parser.add_argument("--instance-type", default="", help="Machine-type ID used to pick a BM (optional)")
    parser.add_argument("--bm-id", default=os.environ.get("FIREBIRD_BM_ID", ""), help="BM to use (bm.ULID)")
    parser.add_argument("--name", default="isv-ir-test-bm", help="Value for the Name tag")
    parser.add_argument("--ssh-user", default="ubuntu", help="Default user of the image")
    parser.add_argument(
        "--key-file", default="", help="Existing SSH private key to reuse (default: generate one for this run)"
    )
    parser.add_argument("--timeout", type=int, default=3900, help="Overall timeout (keep below the step timeout)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": "",
        "image_id": args.image_id,
        "instance_state": "",
        "vpc_id": args.vpc_id,
        "subnet_id": args.subnet_id,
        "key_file": args.key_file,
        "generated_key": False,
        "ssh_user": args.ssh_user,
        "owned": False,
        "attached_project": False,
        "attached_subnet": False,
    }
    if args.upload_skipped:
        result.update(success=True, skipped=True, skip_reason="No custom image: upload_image was skipped")
        print(json.dumps(result, indent=2))
        return 0

    deadline = time.monotonic() + args.timeout
    try:
        if not (args.image_id and args.subnet_id):
            raise RuntimeError("no uploaded image or subnet (did upload_image or create_network fail?)")
        client = FirebirdClient()
        provision_bm(
            client,
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
        bm = client.get_bm(result["instance_id"])
        result["instance_state"] = result["state"] = to_state(bm)
        provisioned = (bm.get("spec") or {}).get("imageId", "")
        if provisioned != args.image_id:
            raise RuntimeError(f"BM {result['instance_id']} reports image {provisioned or 'none'}, not {args.image_id}")
        result["success"] = result["state"] == "running"
        if not result["success"]:
            result["error"] = f"BM {result['instance_id']} is {result['state']} after provisioning"
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
