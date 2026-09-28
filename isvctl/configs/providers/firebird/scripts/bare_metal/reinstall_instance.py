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

"""Reinstall a Firebird bare-metal server from its OS image.

Deprovisions the BM (wipes the OS), then provisions it again with the same
image, tags, and SSH key. The BM identity (ID, MAC) is preserved.

Usage:
    python reinstall_instance.py --instance-id bm.xxx --key-file /tmp/key [--image-id image.xxx]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "state": "running",
    "public_ip": "10.x.x.x",
    "key_file": "/tmp/key",
    "ssh_user": "ubuntu",
    "ssh_ready": true,
    "reinstall_method": "deprovision_provision",
    "reinstall_seconds": 1800
}
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, remaining, to_state
from common.ssh_utils import public_key, user_data_b64, wait_for_ssh


def main() -> int:
    """Deprovision and re-provision the BM, then confirm SSH is restored.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Reinstall Firebird BM")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--image-id", help="Image to install (default: the BM's current image)")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--timeout", type=int, default=7200, help="Overall timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
        "ssh_ready": False,
        "reinstall_method": "deprovision_provision",
    }

    try:
        client = FirebirdClient()
        bm = client.get_bm(args.instance_id)
        image_id = args.image_id or (bm.get("spec") or {}).get("imageId")
        if not image_id:
            raise RuntimeError(f"BM {args.instance_id} has no image to reinstall; pass --image-id")
        result["image_id"] = image_id
        tags = bm.get("tags") or {}
        # Resolve the key before wiping the OS, so a bad key cannot leave the BM blank.
        user_data = user_data_b64(public_key(args.key_file))

        started_at = time.monotonic()
        deadline = time.monotonic() + args.timeout
        print(f"Deprovisioning BM {args.instance_id}...", file=sys.stderr)
        client.bm_action(args.instance_id, "deprovision", {"force": False}, timeout=remaining(deadline))
        client.wait_bm(args.instance_id, ("AVAILABLE",), remaining(deadline))

        print(f"Provisioning image {image_id}...", file=sys.stderr)
        body = {"imageId": image_id, "userDataB64": user_data, "tags": tags}
        client.bm_action(args.instance_id, "provision", body, timeout=remaining(deadline))
        bm = client.wait_bm(args.instance_id, ("RUNNING",), remaining(deadline), power="ON", need_ip=True)
        result["instance_id"] = bm.get("id")
        result["state"] = to_state(bm)
        result["public_ip"] = bm["ipAddress"]
        result["private_ip"] = bm["ipAddress"]

        print("Waiting for SSH after reinstall...", file=sys.stderr)
        result["ssh_ready"] = wait_for_ssh(bm["ipAddress"], args.ssh_user, args.key_file, deadline)
        result["reinstall_seconds"] = int(time.monotonic() - started_at)
        if not result["ssh_ready"]:
            raise RuntimeError("SSH not ready after reinstall")

        result["success"] = True
        print(f"Reinstall completed in {result['reinstall_seconds']}s", file=sys.stderr)

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
