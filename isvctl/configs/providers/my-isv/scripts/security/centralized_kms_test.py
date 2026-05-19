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

"""Centralized KMS test - TEMPLATE.

Verifies that encrypted resources reference centralized KMS-backed keys
instead of legacy or disabled keystores.

Usage:
    python centralized_kms_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Centralized KMS test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Centralized KMS test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "centralized_kms_test",
        "kms_keys_total": 0,
        "encrypted_resources_inspected": 0,
        "non_kms_resources": 0,
        "tests": {
            "kms_service_reachable": {"passed": False},
            "kms_keys_present": {"passed": False},
            "all_encrypted_resources_use_kms": {"passed": False},
        },
    }

    # TODO: Replace this block with your platform's centralized KMS checks.
    # Inventory encrypted resources and verify each configured encryption key
    # resolves through the centralized KMS service.

    if DEMO_MODE:
        result["kms_keys_total"] = 3
        result["encrypted_resources_inspected"] = 2
        result["tests"] = {
            "kms_service_reachable": {"passed": True, "message": "KMS service reachable"},
            "kms_keys_present": {"passed": True, "message": "Demo KMS keys present"},
            "all_encrypted_resources_use_kms": {"passed": True, "message": "Demo encrypted resources use KMS"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's centralized KMS test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
