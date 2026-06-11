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

"""Stop a VM instance and verify it reaches the stopped state.

Template stub for ISV NCP Validation. Replace the TODO section with your
platform's API calls to stop an instance without destroying it.

This script must:
  1. Stop the instance via your platform's API (NOT delete/terminate)
  2. Wait for the instance to reach "stopped" state
  3. Confirm the instance still exists (is not destroyed)

Required JSON output fields:
  success          (bool) - whether the operation succeeded
  platform         (str)  - always "vm"
  instance_id      (str)  - the stopped instance ID
  state            (str)  - must be "stopped"
  stop_initiated   (bool) - whether the stop API call succeeded
  error            (str, optional) - human-readable error message when success is false

Usage:
    python stop_instance.py --instance-id <id> --region <region>

Reference implementation (AWS):
    ../aws/vm/stop_instance.py
"""

import argparse
import json
import os
import sys

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Stop VM instance without destroying it and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Stop VM instance without destroying it")
    parser.add_argument("--instance-id", required=True, help="Instance ID to stop")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "state": "",
        "stop_initiated": False,
    }

    try:
        # ╔══════════════════════════════════════════════════════════════╗
        # ║  TODO: Replace this block with your platform's API calls     ║
        # ║                                                              ║
        # ║  1. Stop the instance (do NOT delete/terminate it)           ║
        # ║     stop_instance(args.instance_id, region=args.region)      ║
        # ║     result["stop_initiated"] = True                          ║
        # ║                                                              ║
        # ║  2. Wait for the instance to reach "stopped" state           ║
        # ║     wait_for_stopped(args.instance_id)                       ║
        # ║                                                              ║
        # ║  3. Populate result                                          ║
        # ║     result["state"] = "stopped"                              ║
        # ║     result["success"] = True                                 ║
        # ╚══════════════════════════════════════════════════════════════╝

        if DEMO_MODE:
            result["instance_id"] = args.instance_id
            result["state"] = "stopped"
            result["stop_initiated"] = True
            result["success"] = True
        else:
            result["error"] = "Not implemented - replace with your platform's stop logic"

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
