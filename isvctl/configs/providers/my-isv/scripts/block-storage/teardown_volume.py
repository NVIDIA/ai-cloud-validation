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

"""Detach and delete the block-storage fixture volume (teardown).

Provider-agnostic template - replace the TODO block with your platform's
detach + delete API calls for the fixture volume created by
``create_volume.py``.

Required JSON output fields:
  success            (bool) - whether cleanup succeeded
  platform           (str)  - "block_storage"
  test_name          (str)  - "teardown_volume"
  resources_deleted  (list) - identifiers of volumes that were deleted
  message            (str)  - human-readable summary

Usage:
    python teardown_volume.py --volume-id <id> --region <region>

Reference implementation (AWS):
    ../aws/block-storage/teardown_volume.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Detach and delete the fixture volume; print JSON result."""
    parser = argparse.ArgumentParser(description="Teardown block-storage fixture volume")
    parser.add_argument("--region", default="", help="Cloud region")
    parser.add_argument("--volume-id", default="", help="Fixture volume to delete")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual destroy")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "block_storage",
        "test_name": "teardown_volume",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result["success"] = True
        result["message"] = f"Volume {args.volume_id} preserved (--skip-destroy)"
        print(json.dumps(result, indent=2))
        return 0

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation     ║
    # ║                                                                   ║
    # ║  1. Detach the volume from the instance (if still attached)       ║
    # ║  2. Delete the volume                                             ║
    # ║       result["resources_deleted"].append(args.volume_id)          ║
    # ║  3. result["success"] = True                                      ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        if args.volume_id:
            result["resources_deleted"].append(args.volume_id)
        result["message"] = "Fixture volume deleted"
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's detach + delete logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
