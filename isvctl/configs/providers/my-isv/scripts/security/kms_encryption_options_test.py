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

"""KMS encryption options test - TEMPLATE.

Verifies that the platform supports both provider-managed and
customer-managed encryption keys for control-plane encryption.

Usage:
    python kms_encryption_options_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """KMS encryption options test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="KMS encryption options test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "kms_encryption_options_test",
        "provider_managed_key_id": "",
        "customer_managed_key_id": "",
        "tests": {
            "provider_managed_key_available": {"passed": False},
            "customer_managed_key_available": {"passed": False},
            "both_options_supported": {"passed": False},
        },
    }

    # TODO: Replace this block with your platform's KMS option checks. Prove
    # that tenants can choose both provider-managed and customer-managed keys.
    # customer_managed_key_id must be a non-empty enumerable key id; the
    # provider-managed option is proven by the provider_managed_key_available
    # subtest (set provider_managed_key_id only if your platform exposes one).

    if DEMO_MODE:
        result["provider_managed_key_id"] = "my-isv-provider-managed-key"
        result["customer_managed_key_id"] = "my-isv-cmk-demo"
        result["tests"] = {
            "provider_managed_key_available": {"passed": True, "message": "Provider-managed key option exists"},
            "customer_managed_key_available": {"passed": True, "message": "Customer-managed key option exists"},
            "both_options_supported": {"passed": True, "message": "Both KMS encryption options are supported"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's KMS encryption options test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
