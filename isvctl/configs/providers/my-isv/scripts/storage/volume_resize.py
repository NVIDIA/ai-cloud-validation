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

"""DATASVC-XX-03: verify volume resizing (issue #322).

Provider-agnostic template - replace the TODO block with your platform's
volume-grow API call plus the in-guest partition + filesystem grow. Confirm
the larger capacity is visible to the guest after the resize.

Required JSON output fields:
  success    (bool) - true iff every operation passed
  platform   (str)  - "storage"
  test_name  (str)  - "volume_resize"
  volume_id  (str)  - volume that was resized
  operations: {
      "modify_volume":     {"passed": bool, "error": str?},
      "grow_partition":    {"passed": bool, "error": str?},
      "resize_filesystem": {"passed": bool, "error": str?},
      "verify_size":       {"passed": bool, "error": str?}
  }

Usage:
    python volume_resize.py --volume-id <id> --mount-point /mnt/isv-block

Reference implementation (AWS):
    ../aws/storage/volume_resize.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Grow the volume + in-guest filesystem and verify; print JSON result."""
    parser = argparse.ArgumentParser(description="Verify volume resizing (DATASVC-XX-03)")
    parser.add_argument("--instance-id", default="", help="Instance the volume is attached to")
    parser.add_argument("--region", default="", help="Cloud region")
    parser.add_argument("--volume-id", default="", help="Volume to resize (fixture volume)")
    parser.add_argument("--key-file", default="", help="Path to SSH private key")
    parser.add_argument("--mount-point", default="/mnt/isv-block", help="In-guest mount point")
    parser.add_argument("--grow-gib", type=int, default=5, help="GiB to add to the volume")
    args = parser.parse_args()

    operations: dict[str, dict[str, Any]] = {
        "modify_volume": {"passed": False},
        "grow_partition": {"passed": False},
        "resize_filesystem": {"passed": False},
        "verify_size": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "volume_resize",
        "volume_id": args.volume_id,
        "operations": operations,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation     ║
    # ║                                                                   ║
    # ║  1. Read the current volume size, then grow it by args.grow_gib   ║
    # ║       -> operations["modify_volume"]["passed"] = True             ║
    # ║  2. Over SSH: growpart the partition                              ║
    # ║       -> operations["grow_partition"]["passed"] = True            ║
    # ║  3. Over SSH: resize2fs (or equivalent) the filesystem            ║
    # ║       -> operations["resize_filesystem"]["passed"] = True         ║
    # ║  4. Confirm the guest sees the larger filesystem size             ║
    # ║       -> operations["verify_size"]["passed"] = True               ║
    # ║  5. result["success"] = all(op["passed"] for op in operations…)   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["old_size_gib"] = 10
        result["new_size_gib"] = 10 + args.grow_gib
        for op in operations.values():
            op["passed"] = True
        result["success"] = True
    else:
        operations["modify_volume"]["error"] = "Not implemented"
        result["error"] = "Not implemented - replace with your platform's resize + growpart/resize2fs logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
