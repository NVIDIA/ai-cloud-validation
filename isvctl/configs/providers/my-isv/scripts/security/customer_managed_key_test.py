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

"""Customer-managed key / BYOK encryption test - TEMPLATE.

Verifies that the platform supports customer-managed keys and that a
provider resource can be encrypted with a customer-owned key instead of a
provider-managed default key. This covers the SEC09-04 requirement.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "customer_managed_key_test",
    "key_id": "cmk-123",
    "key_arn": "my-isv:kms:region:tenant:key/cmk-123",
    "encrypted_resource_id": "volume-123",
    "encrypted_resource_kms_key_id": "cmk-123",
    "tests": {
      "customer_managed_key_available": {"passed": true},
      "key_manager_is_customer": {"passed": true},
      "encrypt_decrypt_roundtrip": {"passed": true},
      "resource_encrypted_with_customer_key": {"passed": true},
      "provider_managed_key_not_used": {"passed": true}
    }
  }

Usage:
    python customer_managed_key_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Customer-managed key test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Customer-managed key / BYOK test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "customer_managed_key_test",
        "key_id": "",
        "key_arn": "",
        "encrypted_resource_id": "",
        "encrypted_resource_kms_key_id": "",
        "tests": {
            "customer_managed_key_available": {"passed": False},
            "key_manager_is_customer": {"passed": False},
            "encrypt_decrypt_roundtrip": {"passed": False},
            "resource_encrypted_with_customer_key": {"passed": False},
            "provider_managed_key_not_used": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's BYOK              ║
    # ║  implementation.                                                 ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    key = create_or_lookup_customer_managed_key(                  ║
    # ║        region=args.region                                        ║
    # ║    )                                                             ║
    # ║    metadata = describe_key(key.id)                               ║
    # ║    tests = result["tests"]                                       ║
    # ║    tests["customer_managed_key_available"]["passed"] = (         ║
    # ║        key.enabled                                               ║
    # ║    )                                                             ║
    # ║    tests["key_manager_is_customer"]["passed"] = (                ║
    # ║        metadata.owner == "customer"                              ║
    # ║    )                                                             ║
    # ║    ciphertext = encrypt_with_key(key.id, b"isv-validation")      ║
    # ║    plaintext = decrypt_with_key(key.id, ciphertext)              ║
    # ║    tests["encrypt_decrypt_roundtrip"]["passed"] = (              ║
    # ║        plaintext == b"isv-validation"                            ║
    # ║    )                                                             ║
    # ║    resource = create_small_encrypted_resource(key_id=key.id)     ║
    # ║    tests["resource_encrypted_with_customer_key"]["passed"] = (   ║
    # ║        resource.key_id == key.id                                 ║
    # ║    )                                                             ║
    # ║    tests["provider_managed_key_not_used"]["passed"] = (          ║
    # ║        resource.key_owner == "customer"                          ║
    # ║    )                                                             ║
    # ║    cleanup(resource)                                             ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["key_id"] = "my-isv-cmk-demo"
        result["key_arn"] = "my-isv:kms:my-isv-region-1:tenant:key/my-isv-cmk-demo"
        result["encrypted_resource_id"] = "my-isv-encrypted-volume-demo"
        result["encrypted_resource_kms_key_id"] = "my-isv-cmk-demo"
        result["tests"] = {
            "customer_managed_key_available": {"passed": True},
            "key_manager_is_customer": {"passed": True},
            "encrypt_decrypt_roundtrip": {"passed": True},
            "resource_encrypted_with_customer_key": {"passed": True},
            "provider_managed_key_not_used": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's customer-managed key test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
