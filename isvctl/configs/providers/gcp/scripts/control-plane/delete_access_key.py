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

"""Delete the exact run-owned HMAC key and its owning service account.

The AWS reference deletes the exact access key and its run-owned identity;
missing resources are idempotent success and no unrelated resource is touched.
On GCP a Cloud Storage HMAC key must be INACTIVE before it can be deleted, and
state changes can take up to ~3 minutes to propagate. This step:

  * Loads the exact HMAC metadata (a NotFound is already-deleted success).
  * If the key is not INACTIVE, sets INACTIVE and updates it as a teardown
    fallback, then retries the idempotent HMAC delete boundedly while the state
    change propagates.
  * After the HMAC cleanup succeeds, deletes
    ``projects/-/serviceAccounts/{username}``; NotFound is idempotent success.
  * Gates top-level ``success`` on BOTH cleanup results, and deletes only the
    exact access id + service-account email forwarded from create_access_key.

NOTE: Left UNWIRED in the baseline GCP provider config; re-enable only after a
preflight proves full control over a disposable, run-owned HMAC key.

Usage:
    python3 delete_access_key.py --username sa@proj.iam.gserviceaccount.com \
        --access-key-id GOOG1E... --region us-central1

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "resources_deleted": ["GOOG1E...", "sa@proj.iam.gserviceaccount.com"],
    "message": "Deleted HMAC key and owning service account"
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import TRANSIENT_EXCEPTIONS, classify_gcp_error, handle_gcp_errors, retry_idempotent
from common.service_account import delete_service_account, service_account_absent
from google.api_core import exceptions as gax
from google.cloud import storage

# Retry the HMAC delete while the INACTIVE state propagates (Cloud Storage
# documents up to ~3 min). Bound by a MONOTONIC 180s deadline with a final delete
# attempt at or after it: a fixed 12x15s attempt loop makes its last attempt near
# 165s and can falsely report a still-propagating key as undeletable.
_HMAC_DELETE_DEADLINE_SECONDS = 180
_HMAC_DELETE_INTERVAL = 15  # seconds between delete attempts

# The HMAC "key must be inactive" precondition surfaces as a 400 while the
# just-set INACTIVE state propagates; it is retryable here (reload + retry).
_HMAC_RETRYABLE = (*TRANSIENT_EXCEPTIONS, gax.BadRequest, gax.FailedPrecondition)


def _cleanup_hmac(
    client: storage.Client,
    access_id: str,
    project: str,
    resources_deleted: list[str],
    cleanup_errors: list[str],
) -> bool:
    """Deactivate (if needed) and delete the exact HMAC key. Return True iff it is gone."""
    try:
        meta = client.get_hmac_key_metadata(access_id=access_id, project_id=project)
    except gax.NotFound:
        return True  # already deleted -> idempotent success

    # Delete requires INACTIVE; set it as a teardown fallback if still active.
    # meta.update() sends only {"state": ...} with no etag, so the SDK's default
    # retry (DEFAULT_RETRY_IF_ETAG_IN_JSON) never engages; re-persisting the same
    # INACTIVE state is idempotent, so wrap it in the bounded transient-retry
    # envelope so a single 429/5xx does not spuriously fail teardown cleanup.
    if meta.state != meta.INACTIVE_STATE:
        try:
            meta.state = meta.INACTIVE_STATE
            retry_idempotent(meta.update, op_desc="hmac update (teardown deactivate)")
        except gax.NotFound:
            return True
        except Exception as e:  # could not deactivate -> surface, do not delete SA
            cleanup_errors.append(f"deactivate HMAC {access_id} failed: {classify_gcp_error(e)[1]}")
            return False

    deadline = time.monotonic() + _HMAC_DELETE_DEADLINE_SECONDS
    while True:
        try:
            meta.delete()
            resources_deleted.append(access_id)
            return True
        except gax.NotFound:
            return True
        except _HMAC_RETRYABLE as e:
            # Give up only once a delete attempt has been made at or after the
            # monotonic propagation deadline.
            if time.monotonic() >= deadline:
                cleanup_errors.append(f"delete HMAC {access_id} failed: {classify_gcp_error(e)[1]}")
                return False
            time.sleep(_HMAC_DELETE_INTERVAL)
            try:
                meta.reload()
            except gax.NotFound:
                return True
            continue
        except Exception as e:  # non-retryable error -> surface
            cleanup_errors.append(f"delete HMAC {access_id} failed: {classify_gcp_error(e)[1]}")
            return False


@handle_gcp_errors
def main() -> int:
    """Delete the exact HMAC key + owning service account and print a teardown result."""
    parser = argparse.ArgumentParser(description="Delete a Cloud Storage HMAC key and its service account")
    parser.add_argument("--username", default="", help="Service-account email from create_access_key")
    parser.add_argument("--access-key-id", required=True, help="Cloud Storage HMAC access id from create_access_key")
    parser.add_argument("--region", default="", help="Accepted for contract parity; no routing effect")
    parser.add_argument("--project", default="", help="GCP project id (falls back to ADC)")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve resources (run teardown later)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        result["message"] = "Teardown skipped (--skip-destroy)"
        print(json.dumps(result, indent=2))
        return 0

    # The owning service account MUST be named: an empty --username would silently
    # skip the identity delete and still report clean teardown, leaking a
    # project-level SA. Require it explicitly.
    username = args.username.strip()
    if not username:
        result["error"] = classify_gcp_error(
            RuntimeError("delete_access_key requires a non-empty --username (owning service-account email)")
        )[1]
        result["message"] = "Missing required --username"
        print(json.dumps(result, indent=2))
        return 1

    project = resolve_project(args.project or None)
    client = storage.Client(project=project)

    resources_deleted: list[str] = []
    cleanup_errors: list[str] = []

    hmac_ok = _cleanup_hmac(client, args.access_key_id, project, resources_deleted, cleanup_errors)

    # Delete the owning service account only after the HMAC key is gone.
    if hmac_ok:
        if delete_service_account(username, project=project):
            # The helper already fails closed on an existence-hiding 403. This
            # second project-scoped readback also confirms an acknowledged delete
            # has converged: only True proves the account is gone; False or None
            # remains a cleanup failure rather than a claimed deletion.
            absent = service_account_absent(project, username)
            if absent is True:
                resources_deleted.append(username)
            elif absent is False:
                cleanup_errors.append(
                    f"service account {username} still present after delete (permission denied, not deleted)"
                )
            else:  # None -> SA list unreadable, absence not proven
                cleanup_errors.append(f"service account {username} deletion unconfirmed (SA list unreadable)")
        else:
            cleanup_errors.append(f"delete service account {username} failed")

    result["resources_deleted"] = resources_deleted
    result["success"] = not cleanup_errors
    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors
        result["error"] = classify_gcp_error(RuntimeError("; ".join(cleanup_errors)))[1]
        result["message"] = "Access-key cleanup incomplete"
    elif resources_deleted:
        result["message"] = "Deleted HMAC key and owning service account"
    else:
        result["message"] = "Access key already absent (idempotent success)"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
