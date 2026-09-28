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

"""Structured skip for a storage check the platform cannot run, with the reason given.

DIR02-01 (NFSv4 home-directory storage) needs NFS served by the filesystem
cluster, and NFS is not enabled on it, so its step reports ``skipped`` with that reason
instead of a result. Makes no API call and touches no host.

Usage:
    python skip_check.py --test-name nfs_home_directory --reason "NFS is not enabled on the filesystem cluster"

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "nfs_home_directory",
    "skipped": true,
    "skip_reason": "NFS is not enabled on the filesystem cluster"
}
"""

import argparse
import json
import sys


def main() -> int:
    """Emit the structured skip.

    Returns:
        0
    """
    parser = argparse.ArgumentParser(description="Skip a storage check the platform cannot run")
    parser.add_argument("--test-name", required=True, help="Suite step name the skip stands for")
    parser.add_argument("--reason", required=True, help="Why the check cannot run")
    args = parser.parse_args()

    result = {
        "success": True,
        "platform": "storage",
        "test_name": args.test_name,
        "skipped": True,
        "skip_reason": args.reason,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
