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

"""Delete the test service account (IAM teardown).

In the keyless primary path there is no user-managed key to delete — the
``secret_access_key`` minted by create_user is a short-lived OAuth2 access
token that self-expires — so teardown only deletes the service account.

Deletion is idempotent: a NotFound service account is the desired terminal
state and reports success (the bounded-retry cleanup helper absorbs the
eventual-consistency window). ``--skip-destroy`` returns success without
deleting anything, mirroring the AWS reference.

The service-account email emitted as create_user.username is enough to build
``projects/-/serviceAccounts/<email>`` (the ``-`` project wildcard), so
``--project`` is accepted for provenance but not required for the delete.

Usage:
    python3 delete_user.py --username sa@proj.iam.gserviceaccount.com --project=proj

Output JSON:
{
    "success": true,
    "platform": "iam",
    "resources_destroyed": true,
    "resources_deleted": ["service_account:sa@proj.iam.gserviceaccount.com"],
    "deleted": {"service_account": "sa@proj.iam.gserviceaccount.com"},
    "message": "Service account deleted (or already absent)"
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.errors import handle_gcp_errors
from common.service_account import delete_service_account


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Delete the test service account (IAM teardown)")
    parser.add_argument("--username", required=True, help="Service account email from create_user")
    parser.add_argument("--project", default="", help="GCP project (provenance only)")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual destroy")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "iam",
        "resources_destroyed": False,
        "resources_deleted": [],
        "deleted": {"service_account": None},
    }
    if args.project:
        result["project"] = args.project

    if args.skip_destroy:
        result["success"] = True
        result["message"] = "Destroy skipped (--skip-destroy flag)"
        print(json.dumps(result, indent=2))
        return 0

    sa_email = args.username
    # delete_service_account returns True when the SA was deleted now OR is
    # already absent (NotFound), and False only on a persistent transient
    # failure past the retry budget — fold that bool into success so a genuine
    # leak surfaces rather than being silently swallowed.
    deleted = delete_service_account(sa_email)
    if deleted:
        result["deleted"]["service_account"] = sa_email
        result["resources_deleted"].append(f"service_account:{sa_email}")
        result["resources_destroyed"] = True
        result["success"] = True
        result["message"] = "Service account deleted (or already absent)"
    else:
        result["success"] = False
        result["error_type"] = "api_error"
        result["error"] = f"failed to delete service account {sa_email} after bounded retry"
        result["message"] = "Service account deletion failed"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
