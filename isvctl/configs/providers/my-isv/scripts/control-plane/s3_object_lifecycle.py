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

"""DATASVC-XX-01 data-path: PutObject -> GetObject -> DeleteObject on the
platform's S3-compatible object store.

Provider-agnostic template - replace the TODO section with your platform's
PutObject / GetObject / DeleteObject calls. The GetObject result MUST be
byte-compared with the body that was put to detect data corruption.

The script creates a temp bucket, runs the lifecycle, byte-compares Get vs
Put, and deletes the bucket - self-contained so re-runs do not leak
resources.

Required JSON output:
{
    "success":     bool   - true iff all three operations passed AND bytes matched,
    "platform":    str    - "control_plane",
    "test_name":   str    - "s3_object_lifecycle",
    "bucket_name": str    - bucket the object was written to,
    "object_key":  str    - key of the test object,
    "operations": {
        "put":    {"passed": bool, "error": str?},
        "get":    {"passed": bool, "content_matches": bool, "error": str?},
        "delete": {"passed": bool, "error": str?}
    }
}

Usage:
    python s3_object_lifecycle.py --region <region>

AWS reference implementation:
    ../aws/control-plane/s3_object_lifecycle.py
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
    """Exercise the object lifecycle and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Verify S3 object lifecycle (Put/Get/Delete)")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    parser.add_argument("--bucket-prefix", default="isv-validate-s3")
    args = parser.parse_args()

    bucket_name = f"{args.bucket_prefix}-{uuid.uuid4().hex[:8]}"
    object_key = f"isv-validate-{uuid.uuid4().hex[:8]}.txt"

    operations: dict[str, dict[str, Any]] = {
        "put": {"passed": False},
        "get": {"passed": False},
        "delete": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "test_name": "s3_object_lifecycle",
        "bucket_name": bucket_name,
        "object_key": object_key,
        "operations": operations,
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. body = b"some small payload"                                 ║
    # ║  2. CreateBucket(bucket_name) in args.region                     ║
    # ║  3. PutObject(bucket_name, object_key, body)                     ║
    # ║       -> operations["put"]["passed"] = True (or False + error)   ║
    # ║  4. response = GetObject(bucket_name, object_key)                ║
    # ║       -> operations["get"]["content_matches"] = (response==body) ║
    # ║       -> operations["get"]["passed"] = content_matches           ║
    # ║  5. DeleteObject(bucket_name, object_key)                        ║
    # ║       -> operations["delete"]["passed"] = True                   ║
    # ║  6. Best-effort: DeleteBucket(bucket_name) in a finally block    ║
    # ║  7. result["success"] = all(op["passed"] for op in operations…)  ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        operations["put"]["passed"] = True
        operations["get"]["passed"] = True
        operations["get"]["content_matches"] = True
        operations["delete"]["passed"] = True
        result["success"] = True
    else:
        operations["put"]["error"] = "Not implemented"
        result["error"] = "Not implemented - replace with your platform's PutObject/GetObject/DeleteObject logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
