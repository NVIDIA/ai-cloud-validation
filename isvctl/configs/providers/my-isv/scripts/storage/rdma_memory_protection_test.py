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

"""RDMA memory-protection test - TEMPLATE (replace with your platform impl).

Proves HSS06-01: storage systems using RDMA enforce memory protection via
authorization keys for both local and remote access.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Confirm the storage data path uses RDMA
  2. Confirm local access requires a valid memory-protection key (L_Key)
  3. Confirm remote access requires a valid memory-protection key (R_Key)
  4. Confirm access with an invalid/absent key is rejected (protection fault)
  5. Print a JSON object to stdout

Required JSON output fields:
  {
    "success": true,
    "platform": "storage",
    "test_name": "rdma_memory_protection",
    "tests": {
      "rdma_enabled":               {"passed": true},
      "local_key_enforced":         {"passed": true},
      "remote_key_enforced":        {"passed": true},
      "unauthorized_access_blocked":{"passed": true}
    }
  }

Usage:
    python rdma_memory_protection_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Run the RDMA memory-protection test (template) and emit JSON."""
    parser = argparse.ArgumentParser(description="RDMA memory-protection test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "rdma_memory_protection",
        "tests": {
            "rdma_enabled": {"passed": False},
            "local_key_enforced": {"passed": False},
            "remote_key_enforced": {"passed": False},
            "unauthorized_access_blocked": {"passed": False},
        },
    }

    # TODO: Replace with your platform's RDMA memory-protection implementation

    if DEMO_MODE:
        result["tests"] = {
            "rdma_enabled": {"passed": True},
            "local_key_enforced": {"passed": True},
            "remote_key_enforced": {"passed": True},
            "unauthorized_access_blocked": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's RDMA memory-protection test logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
