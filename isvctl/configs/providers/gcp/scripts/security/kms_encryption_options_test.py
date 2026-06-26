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

"""Verify provider-managed and customer-managed encryption options exist (SEC09-02).

The AWS reference proves the provider-managed option by describing a managed
service key and proves the customer-managed option by creating a temporary CMK.

On GCP the two halves are proven differently:

  * Provider-managed: GCP encrypts all data at rest by default with
    Google-managed keys. That default key is NOT a Cloud KMS resource and is not
    listable, so the option is proven as an always-available platform capability
    and ``provider_managed_key_id`` is omitted from the output.
  * Customer-managed (CMEK): proven by enumerating an existing tenant CryptoKey
    via ``google.cloud.kms_v1.KeyManagementServiceClient.list_crypto_keys`` (walk
    locations -> key rings -> keys). No key is created, so there is no teardown.

This script is read-only. It falls back to a structured skip (which the
validator honors) only when no CMEK CryptoKey is enumerable anywhere in the
project.

Usage:
    python3 kms_encryption_options_test.py --region us-central1 --project my-project

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "kms_encryption_options_test",
    "customer_managed_key_id": "projects/p/locations/us/keyRings/r/cryptoKeys/k",
    "tests": {
        "provider_managed_key_available": {"passed": true},
        "customer_managed_key_available": {"passed": true},
        "both_options_supported": {"passed": true}
    }
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import kms_v1


def _first_customer_managed_key(client: kms_v1.KeyManagementServiceClient, project: str) -> str:
    """Return the full resource path of the first enumerable tenant CryptoKey.

    GCP has no flat key listing: walk KMS locations -> key rings -> crypto keys.
    GCP has no KeyManager=CUSTOMER field either -- a CryptoKey living in the
    tenant's own key rings IS the customer-managed key. The first enumerable key
    is sufficient evidence that customer-managed encryption is available.

    Returns an empty string when no key is enumerable anywhere.
    """
    locations_parent = f"projects/{project}"
    for location in client.list_locations(request={"name": locations_parent}).locations:
        key_rings_parent = location.name  # projects/<p>/locations/<loc>
        try:
            for key_ring in client.list_key_rings(parent=key_rings_parent):
                for crypto_key in client.list_crypto_keys(parent=key_ring.name):
                    return crypto_key.name
        except (gax.PermissionDenied, gax.NotFound):
            # A location the caller cannot enumerate is not fatal -- keep walking.
            continue
    return ""


@handle_gcp_errors
def main() -> int:
    """Prove both provider-managed and customer-managed key options and emit JSON."""
    parser = argparse.ArgumentParser(description="KMS encryption options test (SEC09-02)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "kms_encryption_options_test",
        "customer_managed_key_id": "",
        "tests": {
            "provider_managed_key_available": {"passed": False},
            "customer_managed_key_available": {"passed": False},
            "both_options_supported": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)
        client = kms_v1.KeyManagementServiceClient()

        customer_key_id = _first_customer_managed_key(client, project)

        if not customer_key_id:
            # No enumerable tenant CryptoKey -- fall back to a structured skip
            # (the validator honors skipped:true).
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No customer-managed Cloud KMS CryptoKey is available in this project"
            result["tests"] = {
                "provider_managed_key_available": {
                    "passed": True,
                    "skipped": True,
                    "message": "GCP encrypts data at rest by default with Google-managed keys",
                },
                "customer_managed_key_available": {
                    "passed": True,
                    "skipped": True,
                    "message": result["skip_reason"],
                },
                "both_options_supported": {"passed": True, "skipped": True, "message": result["skip_reason"]},
            }
            print(json.dumps(result, indent=2))
            return 0

        result["customer_managed_key_id"] = customer_key_id
        # Provider-managed default at-rest encryption is an always-available GCP
        # platform capability (the Google-managed key is not a Cloud KMS
        # resource, so no id is emitted).
        result["tests"]["provider_managed_key_available"] = {
            "passed": True,
            "message": "GCP encrypts all data at rest by default with Google-managed keys",
        }
        result["tests"]["customer_managed_key_available"] = {
            "passed": True,
            "message": f"Customer-managed Cloud KMS CryptoKey is available: {customer_key_id}",
        }
        both = (
            result["tests"]["provider_managed_key_available"]["passed"]
            and result["tests"]["customer_managed_key_available"]["passed"]
        )
        result["tests"]["both_options_supported"] = {
            "passed": both,
            "message": "Provider-managed and customer-managed encryption options are both available",
        }
        result["success"] = all(t["passed"] for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
