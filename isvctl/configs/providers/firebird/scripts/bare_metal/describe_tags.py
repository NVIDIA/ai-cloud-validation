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

"""Retrieve user-defined tags on a Firebird bare-metal server.

Usage:
    python describe_tags.py --instance-id bm.xxx

Output JSON:
{
    "success": true,
    "platform": "bm",
    "instance_id": "bm.xxx",
    "tags": {"Name": "isv-bm-test-gpu", "CreatedBy": "isvctl"},
    "tag_count": 2
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log


def main() -> int:
    """Retrieve BM tags and print structured JSON output."""
    parser = argparse.ArgumentParser(description="Describe Firebird BM tags")
    parser.add_argument("--instance-id", required=True, help="BM ID (bm.ULID)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "instance_id": args.instance_id,
        "tags": {},
        "tag_count": 0,
    }

    try:
        tags = FirebirdClient().get_bm(args.instance_id).get("tags") or {}
        result["tags"] = tags
        result["tag_count"] = len(tags)
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
