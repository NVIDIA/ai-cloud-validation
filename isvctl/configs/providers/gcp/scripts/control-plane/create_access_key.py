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

"""Create a run-owned service account + Cloud Storage HMAC key (S3-compat credential).

The AWS reference creates an IAM user and an access key owned by that user. The
GCP analog is a Cloud Storage HMAC key created for a service account: the key's
access id + once-only secret sign S3-compatible requests to
``https://storage.googleapis.com``. This step:

  1. Creates a run-scoped service account (``IAMClient.create_service_account``).
  2. Creates an interoperability HMAC key for it
     (``storage.Client.create_hmac_key(service_account_email, project_id,
     retry=None)``). The create is NOT retried: a successful response is the only
     source of the secret, so a blind retry could mint a second key whose secret
     is lost.
  3. Both the service-account create and the HMAC create separate a DEFINITE
     rejection from an AMBIGUOUS transport/5xx outcome that may have committed
     server-side. A genuine conflict is foreign and never adopted; an ambiguous
     failure is reconciled against the deterministic, invocation-specific account
     email (the account id embeds a per-invocation discriminator, see
     ``_account_id``), so the possibly-committed identity is rolled back and
     confirmed absent, or preserved as the teardown handoff. Only the service
     account created by this invocation is ever touched (no unrelated cleanup).

Returns the service-account email as ``username``, the HMAC ``access_id`` as
``access_key_id``, and the once-only ``secret`` as ``secret_access_key``.

NOTE: This step is left UNWIRED in the baseline GCP provider config because the
enabled lifecycle does not require service-account administration, HMAC-key
administration, or organization policies that permit disposable HMAC keys. It
is a complete implementation kept for re-enablement where a preflight proves
the full run-owned key lifecycle is available.

Usage:
    python3 create_access_key.py --region us-central1

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "username": "isv-ak-ab12-1a2b3c4d@my-project.iam.gserviceaccount.com",
    "user_id": "projects/my-project/serviceAccounts/isv-ak-...@...",
    "access_key_id": "GOOG1E...",
    "secret_access_key": "..."
}
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import classify_gcp_error, handle_gcp_errors
from common.service_account import (
    create_service_account_resource,
    delete_service_account,
    service_account_absent,
)
from google.cloud import storage


def _account_id() -> str:
    """Build a service-account id within the documented 6-30 char limit.

    Combines the base prefix and a per-invocation discriminator with the full
    8-character run-id suffix produced by ``unique_suffix`` (``RUN_ID[:8]`` or
    an 8-char random hex fallback when RUN_ID is unset). Emitting the full
    canonical suffix — rather than a truncated 6-char slice — keeps a leaked
    service account groupable and recoverable by the same run-id suffix an
    operator sweep matches on. The full id is 20 chars, well under GCP's
    30-char account-id ceiling.
    """
    disc = uuid.uuid4().hex[:4]
    return unique_suffix(f"isv-ak-{disc}")  # e.g. isv-ak-ab12-1a2b3c4d (20 chars)


def _predicted_sa_email(project: str, account_id: str) -> str:
    """Return the deterministic email of the SA this invocation attempts to create.

    GCP derives a new service account's email deterministically from its account
    id and project: ``<account_id>@<project>.iam.gserviceaccount.com``. When an
    ambiguous SA create loses its response before returning the ServiceAccount,
    this predicted email is the exact, invocation-specific coordinate a
    project-scoped readback reconciles against -- the account id embeds a
    per-invocation random discriminator (see ``_account_id``), so no same-run
    foreign actor shares it.
    """
    return f"{account_id}@{project}.iam.gserviceaccount.com"


def _rollback_service_account(project: str, sa_email: str) -> list[str]:
    """Best-effort delete the SA this invocation created and confirm it is gone.

    Returns a list of rollback errors (empty only on a CONFIRMED-absent rollback).
    ``delete_service_account`` fails closed on an existence-hiding 403 unless
    project inventory proves absence. This second project-scoped readback also
    confirms an acknowledged delete has converged: only True proves the SA is
    gone. False (still present) and None (list unreadable) preserve the identity
    as a teardown handoff instead of silently orphaning a project-level account.
    """
    rollback_errors: list[str] = []
    if delete_service_account(sa_email, project=project):
        absent = service_account_absent(project, sa_email)
        if absent is False:
            rollback_errors.append(f"rollback service account {sa_email} still present after delete")
        elif absent is None:
            rollback_errors.append(f"rollback service account {sa_email} deletion unconfirmed (SA list unreadable)")
    else:
        rollback_errors.append(f"rollback delete service account {sa_email} failed")
    return rollback_errors


@handle_gcp_errors
def main() -> int:
    """Create a run-owned service account + HMAC key and print a structured result."""
    parser = argparse.ArgumentParser(description="Create a Cloud Storage HMAC access key (S3-compat)")
    parser.add_argument("--region", default="", help="Accepted for contract parity; no routing effect")
    parser.add_argument("--project", default="", help="GCP project id (falls back to ADC)")
    args = parser.parse_args()

    project = resolve_project(args.project or None)
    account_id = _account_id()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "username": "",
        "user_id": "",
        "access_key_id": "",
        "secret_access_key": "",
    }
    sa_owned = False

    def _record_sa_acceptance() -> None:
        nonlocal sa_owned
        sa_owned = True

    # 1. Create the run-owned service account that will own the HMAC key. The
    #    create is non-idempotent, so separate a DEFINITE rejection from an
    #    AMBIGUOUS transport/5xx outcome that may have committed the SA before the
    #    response was lost. On success, read the identity fields back from the
    #    server-returned ServiceAccount rather than reconstructing them, so the
    #    output is creation evidence, not a prediction.
    try:
        sa = create_service_account_resource(
            project,
            account_id,
            display_name="ISV control-plane HMAC test SA",
            on_accepted=_record_sa_acceptance,
        )
    except Exception as e:
        # The shared helper transfers ownership only after acknowledgement or an
        # ambiguous create whose exact invocation marker is read back. Roll back
        # that proven-owned coordinate; a conflict, definite rejection, or foreign
        # ambiguous readback leaves sa_owned false and must never be deleted.
        if sa_owned:
            predicted_email = _predicted_sa_email(project, account_id)
            rollback_errors = _rollback_service_account(project, predicted_email)
            if rollback_errors:
                result["username"] = predicted_email
                result["user_id"] = f"projects/{project}/serviceAccounts/{predicted_email}"
                result["cleanup_errors"] = rollback_errors
        result["error"] = classify_gcp_error(e)[1]
        print(json.dumps(result, indent=2))
        return 1
    sa_email = sa.email
    result["username"] = sa.email
    result["user_id"] = sa.name

    # 2. Create the interoperability HMAC key. Do NOT retry: the secret is
    #    returned exactly once, so a retried create could leak a key whose secret
    #    is lost. If the create fails, roll back only the SA this step created and
    #    confirm its absence; preserve the identity as the teardown handoff when
    #    the rollback cannot be confirmed.
    try:
        client = storage.Client(project=project)
        hmac_meta, secret = client.create_hmac_key(
            service_account_email=sa_email,
            project_id=project,
            retry=None,
        )
    except Exception as e:  # HMAC create failed after SA create -> roll back the SA
        rollback_errors = _rollback_service_account(project, sa_email)
        if rollback_errors:
            result["cleanup_errors"] = rollback_errors
        result["error"] = classify_gcp_error(e)[1]
        print(json.dumps(result, indent=2))
        return 1

    result["access_key_id"] = hmac_meta.access_id
    result["secret_access_key"] = secret
    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
