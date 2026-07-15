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

"""GCP IAM cleanup of test service accounts by prefix (optional ISV-facing scaffold).

Counterpart to the AWS reference `teardown.sh`. Deletes service accounts whose
email local-part starts with ``--prefix``, so an operator can sweep leftovers
from interrupted runs. This script is NOT invoked by suites/iam.yaml — the
wired teardown-phase step is `delete_user`, which deletes only the specific
service account `create_user` made. This prefix sweep is a broader, opt-in
helper an ISV can run by hand.

When no project / Application Default Credentials resolve, there is nothing to
sweep and the script succeeds with an empty result. ``--skip-destroy`` returns
success without deleting anything.

Usage:
    python3 teardown.py --project=my-project --prefix=isv-test-user-

Output JSON:
{
    "success": true,
    "platform": "iam",
    "resources_deleted": ["isv-test-user-...@my-project.iam.gserviceaccount.com"],
    "message": "Cleaned up 1 service account(s)"
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import classify_gcp_error, handle_gcp_errors
from common.service_account import delete_service_account
from google.cloud import iam_admin_v1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="GCP IAM prefix cleanup (optional scaffold)")
    parser.add_argument("--project", default="", help="GCP project (falls back to env/ADC when blank)")
    parser.add_argument(
        "--prefix",
        default="isv-test-user-",
        help="Delete service accounts whose ID starts with this prefix",
    )
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual destroy")
    args = parser.parse_args()

    result: dict = {
        "success": True,
        "platform": "iam",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result["message"] = "Cleanup skipped (--skip-destroy flag)"
        print(json.dumps(result, indent=2))
        return 0

    if not args.prefix:
        result["success"] = False
        result["error_type"] = "unknown_error"
        result["error"] = "--prefix must be non-empty to avoid deleting unrelated service accounts"
        result["message"] = "Refusing to sweep with an empty prefix"
        print(json.dumps(result, indent=2))
        return 1

    try:
        project = resolve_project(args.project or None)
    except Exception as e:
        # Nothing to sweep without a resolvable project; succeed as a no-op.
        error_type, error_msg = classify_gcp_error(e)
        result["message"] = f"cleanup skipped ({error_type}): {error_msg}"
        print(json.dumps(result, indent=2))
        return 0

    iam = iam_admin_v1.IAMClient()
    failures = 0
    for service_account in iam.list_service_accounts(name=f"projects/{project}"):
        if not service_account.email.split("@", 1)[0].startswith(args.prefix):
            continue
        if delete_service_account(service_account.email, project=project):
            result["resources_deleted"].append(service_account.email)
        else:
            failures += 1

    deleted = len(result["resources_deleted"])
    result["success"] = failures == 0
    result["message"] = f"Cleaned up {deleted} service account(s)"
    if failures:
        result["message"] += f"; {failures} failed to delete"
        result["error_type"] = "api_error"
        result["error"] = f"{failures} service account(s) failed to delete after bounded retry"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
