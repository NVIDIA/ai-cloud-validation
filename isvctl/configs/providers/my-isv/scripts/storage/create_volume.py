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

"""Block-storage fixture: create, attach, format, mount, and seed a volume.

Provider-agnostic template - replace the TODO block with your platform's
block-volume API calls plus in-guest formatting. This single fixture is
shared by the snapshot, resize, and persistence tests, so the volume must
be left attached, formatted, mounted, and seeded with the sentinel file.

Required JSON output fields:
  success           (bool) - true iff every operation passed
  platform          (str)  - "storage"
  test_name         (str)  - "create_volume"
  volume_id         (str)  - identifier of the created volume (consumed by later steps)
  mount_point       (str)  - where the volume is mounted in the guest
  sentinel_content  (str)  - exact content written to the sentinel file (consumed by later steps)
  operations: {
      "create":         {"passed": bool, "error": str?},
      "attach":         {"passed": bool, "error": str?},
      "format":         {"passed": bool, "error": str?},
      "mount":          {"passed": bool, "error": str?},
      "write_sentinel": {"passed": bool, "error": str?}
  }

Usage:
    python create_volume.py --instance-id <id> --region <region> --key-file <key> --size-gib 10

Reference implementation (AWS):
    ../aws/storage/create_volume.py
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Create + attach + format + mount + seed a block volume; print JSON."""
    parser = argparse.ArgumentParser(description="Block-storage fixture: create + seed a volume")
    parser.add_argument("--instance-id", default="", help="Instance to attach the volume to")
    parser.add_argument("--region", default="", help="Cloud region")
    parser.add_argument("--key-file", default="", help="Path to SSH private key")
    parser.add_argument("--size-gib", type=int, default=10, help="Volume size in GiB")
    parser.add_argument("--mount-point", default="/mnt/isv-block", help="In-guest mount point")
    args = parser.parse_args()

    sentinel_content = f"isv-ncp-validate-storage-{uuid.uuid4().hex}"
    operations: dict[str, dict[str, Any]] = {
        "create": {"passed": False},
        "attach": {"passed": False},
        "format": {"passed": False},
        "mount": {"passed": False},
        "write_sentinel": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "create_volume",
        "volume_id": "",
        "mount_point": args.mount_point,
        "size_gib": args.size_gib,
        "sentinel_content": sentinel_content,
        "operations": operations,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation     ║
    # ║                                                                   ║
    # ║  1. CreateVolume(size_gib) in the instance's zone                 ║
    # ║       -> operations["create"]["passed"] = True                    ║
    # ║  2. AttachVolume(volume_id, instance_id)                          ║
    # ║       -> operations["attach"]["passed"] = True                    ║
    # ║  3. Over SSH: partition + mkfs the attached device                ║
    # ║       -> operations["format"]["passed"] = True                    ║
    # ║  4. Over SSH: mount it at args.mount_point                        ║
    # ║       -> operations["mount"]["passed"] = True                     ║
    # ║  5. Over SSH: write sentinel_content to <mount>/isv-sentinel.txt  ║
    # ║       -> operations["write_sentinel"]["passed"] = True            ║
    # ║  6. result["volume_id"] = volume_id                               ║
    # ║  7. result["success"] = all(op["passed"] for op in operations…)   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["volume_id"] = f"dummy-vol-{uuid.uuid4().hex[:8]}"
        for op in operations.values():
            op["passed"] = True
        result["success"] = True
    else:
        operations["create"]["error"] = "Not implemented"
        result["error"] = "Not implemented - replace with your platform's create/attach/format/mount logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
