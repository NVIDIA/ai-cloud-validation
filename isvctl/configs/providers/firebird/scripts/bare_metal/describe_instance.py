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

"""Describe a Firebird bare-metal server and pass through SSH details.

SSH, GPU, and host-OS validations bind to this step's output.

Usage:
    python describe_instance.py --instance-id bm.xxx --key-file /tmp/key

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "state": "running",
    "power_state": "on",
    "public_ip": "10.x.x.x",
    "private_ip": "10.x.x.x",
    "key_file": "/tmp/key",
    "ssh_user": "ubuntu"
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log, to_state


def main() -> int:
    """Describe the BM and emit its current state and SSH connection details.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Describe Firebird BM")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "key_file": args.key_file,
        "ssh_user": args.ssh_user,
    }

    try:
        bm = FirebirdClient().get_bm(args.instance_id)
        result["state"] = to_state(bm)
        result["power_state"] = str(bm.get("powerState", "")).lower()
        result["instance_type"] = bm.get("machineTypeId")
        result["public_ip"] = bm.get("ipAddress")
        result["private_ip"] = bm.get("ipAddress")
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
