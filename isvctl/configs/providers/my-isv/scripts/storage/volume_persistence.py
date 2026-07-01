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

"""DATASVC-XX-04: persistent volumes survive instance restarts (issue #323).

Provider-agnostic template - replace the TODO block with your platform's
stop/start API calls. Stop and start the instance, then confirm the block
volume is still attached and its sentinel data is intact (the volume must be
re-mounted after the restart).

Required JSON output fields:
  success    (bool) - true iff every operation passed
  platform   (str)  - "storage"
  test_name  (str)  - "volume_persistence"
  volume_id  (str)  - volume expected to persist
  operations: {
      "stop":            {"passed": bool, "error": str?},
      "start":           {"passed": bool, "error": str?},
      "verify_attached": {"passed": bool, "error": str?},
      "verify_data":     {"passed": bool, "content_matches": bool, "error": str?}
  }

Usage:
    python volume_persistence.py --instance-id <id> --volume-id <id> --expected-content <content>

Reference implementation (AWS):
    ../aws/storage/volume_persistence.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Stop/start the instance and verify the volume + data survive; print JSON."""
    parser = argparse.ArgumentParser(description="Verify block volume persistence across restart (DATASVC-XX-04)")
    parser.add_argument("--instance-id", default="", help="Instance to restart")
    parser.add_argument("--region", default="", help="Cloud region")
    parser.add_argument("--volume-id", default="", help="Volume expected to persist")
    parser.add_argument("--key-file", default="", help="Path to SSH private key")
    parser.add_argument("--mount-point", default="/mnt/isv-block", help="In-guest mount point")
    parser.add_argument("--expected-content", default="", help="Sentinel content written by the fixture")
    args = parser.parse_args()

    operations: dict[str, dict[str, Any]] = {
        "stop": {"passed": False},
        "start": {"passed": False},
        "verify_attached": {"passed": False},
        "verify_data": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "volume_persistence",
        "volume_id": args.volume_id,
        "instance_id": args.instance_id,
        "operations": operations,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation     ║
    # ║                                                                   ║
    # ║  1. Stop the instance; wait for it to reach stopped               ║
    # ║       -> operations["stop"]["passed"] = True                      ║
    # ║  2. Start the instance; wait for it to be reachable over SSH      ║
    # ║       -> operations["start"]["passed"] = True                     ║
    # ║  3. Confirm the volume is still attached to the instance          ║
    # ║       -> operations["verify_attached"]["passed"] = True           ║
    # ║  4. Over SSH: re-mount the volume, read <mount>/isv-sentinel.txt  ║
    # ║       matches = (content == args.expected_content)                ║
    # ║       -> operations["verify_data"]["content_matches"] = matches   ║
    # ║       -> operations["verify_data"]["passed"] = matches            ║
    # ║  5. result["success"] = all(op["passed"] for op in operations…)   ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        operations["stop"]["passed"] = True
        operations["start"]["passed"] = True
        operations["verify_attached"]["passed"] = True
        operations["verify_data"]["passed"] = True
        operations["verify_data"]["content_matches"] = True
        result["success"] = True
    else:
        operations["stop"]["error"] = "Not implemented"
        result["error"] = "Not implemented - replace with your platform's stop/start + verify logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
