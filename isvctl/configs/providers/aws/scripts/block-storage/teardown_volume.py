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

Best-effort cleanup of the volume created by ``create_volume.py``. Runs
before the instance teardown so the volume is removed explicitly rather
than left dangling. The restored volume and snapshot from the snapshot
test clean themselves up in their own step.

Output JSON:
{
    "success": true,
    "platform": "block_storage",
    "test_name": "teardown_volume",
    "resources_deleted": ["vol-xxx"]
}
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/aws/scripts/ (for common.*)

import boto3
from common import ebs


def main() -> int:
    """Detach and delete the fixture volume; print JSON result."""
    parser = argparse.ArgumentParser(description="Teardown block-storage fixture volume")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--volume-id", required=True, help="Fixture volume to delete")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual destroy")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "block_storage",
        "test_name": "teardown_volume",
        "resources_deleted": [],
    }

    if args.skip_destroy:
        result["success"] = True
        result["message"] = f"Volume {args.volume_id} preserved (--skip-destroy); delete manually when done"
        print(json.dumps(result, indent=2))
        return 0

    ec2 = boto3.client("ec2", region_name=args.region)
    error = ebs.detach_and_delete_volume(ec2, args.volume_id)
    if error:
        result["error"] = error
        result["message"] = f"Failed to delete volume {args.volume_id}"
    else:
        result["success"] = True
        result["resources_deleted"].append(args.volume_id)
        result["message"] = f"Volume {args.volume_id} deleted"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
