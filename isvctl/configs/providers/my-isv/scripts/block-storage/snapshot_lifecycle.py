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

"""DATASVC-XX-02: verify volume snapshots (issue #321).

Provider-agnostic template - replace the TODO block with your platform's
snapshot + restore API calls. Snapshot the fixture volume, restore it to a
new volume, attach + mount that restore, and byte-compare the sentinel file
against the content the fixture wrote. Clean up the restored volume and the
snapshot afterward.

Required JSON output fields:
  success      (bool) - true iff every operation passed
  platform     (str)  - "block_storage"
  test_name    (str)  - "snapshot_lifecycle"
  volume_id    (str)  - source (fixture) volume that was snapshotted
  snapshot_id  (str)  - identifier of the created snapshot
  operations: {
      "create_snapshot": {"passed": bool, "error": str?},
      "restore_volume":  {"passed": bool, "error": str?},
      "verify_data":     {"passed": bool, "content_matches": bool, "error": str?}
  }

Usage:
    python snapshot_lifecycle.py --volume-id <id> --expected-content <content>

Reference implementation (AWS):
    ../aws/block-storage/snapshot_lifecycle.py
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
    """Snapshot, restore, and verify a volume; print JSON result."""
    parser = argparse.ArgumentParser(description="Verify volume snapshots (DATASVC-XX-02)")
    parser.add_argument("--instance-id", default="", help="Instance for the restored volume")
    parser.add_argument("--region", default="", help="Cloud region")
    parser.add_argument("--volume-id", default="", help="Source (fixture) volume to snapshot")
    parser.add_argument("--key-file", default="", help="Path to SSH private key")
    parser.add_argument("--expected-content", default="", help="Sentinel content written by the fixture")
    args = parser.parse_args()

    operations: dict[str, dict[str, Any]] = {
        "create_snapshot": {"passed": False},
        "restore_volume": {"passed": False},
        "verify_data": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "block_storage",
        "test_name": "snapshot_lifecycle",
        "volume_id": args.volume_id,
        "snapshot_id": "",
        "operations": operations,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation     ║
    # ║                                                                   ║
    # ║  1. CreateSnapshot(volume_id); wait until complete                ║
    # ║       -> operations["create_snapshot"]["passed"] = True           ║
    # ║  2. CreateVolume(from snapshot); attach to the instance           ║
    # ║       -> operations["restore_volume"]["passed"] = True            ║
    # ║  3. Over SSH: mount the restore, read <mount>/isv-sentinel.txt    ║
    # ║       matches = (content == args.expected_content)                ║
    # ║       -> operations["verify_data"]["content_matches"] = matches   ║
    # ║       -> operations["verify_data"]["passed"] = matches            ║
    # ║  4. Best-effort: delete the restored volume and the snapshot      ║
    # ║  5. result["success"] = all(op["passed"] for op in operations…)   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["snapshot_id"] = f"dummy-snap-{uuid.uuid4().hex[:8]}"
        result["restored_volume_id"] = f"dummy-vol-{uuid.uuid4().hex[:8]}"
        operations["create_snapshot"]["passed"] = True
        operations["restore_volume"]["passed"] = True
        operations["verify_data"]["passed"] = True
        operations["verify_data"]["content_matches"] = True
        result["success"] = True
    else:
        operations["create_snapshot"]["error"] = "Not implemented"
        result["error"] = "Not implemented - replace with your platform's snapshot/restore logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
