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

"""Home-directory storage validation template for DIR01 and DIR02."""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

_TEST_NAMES = (
    "filesystem_quota_configured",
    "filesystem_quota_updated",
    "filesystem_quota_enforced",
    "uid_usage_accounted",
    "gid_usage_accounted",
    "identity_usage_isolated",
    "nfsv4_mounted",
    "nfs_read_write",
    "nfs_shared_visibility",
)


def main() -> int:
    """Run provider-specific home-directory probes and emit provider-neutral JSON."""
    parser = argparse.ArgumentParser(description="Home-directory storage validation template")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": DEMO_MODE,
        "platform": "storage",
        "test_name": "home_directory_storage",
        "tests": {name: {"passed": DEMO_MODE} for name in _TEST_NAMES},
    }
    if not DEMO_MODE:
        result["error"] = "Not implemented - replace with your platform's home-directory storage test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
