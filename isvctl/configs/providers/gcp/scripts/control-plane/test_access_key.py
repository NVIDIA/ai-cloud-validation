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

"""Authenticate a Cloud Storage HMAC key against the S3-compatible endpoint.

The AWS reference authenticates an access-key pair with an S3-style signed
request. Cloud Storage accepts Amazon S3 SDK requests at
``https://storage.googleapis.com`` when they are signed with a Cloud Storage HMAC
access id + secret. This step:

  * Builds an ISOLATED boto3 S3 client with ONLY the supplied HMAC credentials and
    ``endpoint_url=https://storage.googleapis.com`` (never falling back to ADC for
    the signed request).
  * Polls a lightweight signed request until it returns success OR ``AccessDenied``
    — both prove the signature was accepted (``AccessDenied`` just means the new
    service account has no object-storage role yet). ``InvalidAccessKeyId``,
    ``InvalidSecurity``, and ``SignatureDoesNotMatch`` are credential rejection and
    do NOT authenticate; because a freshly-created HMAC key can take up to ~60s to
    activate, those are treated as not-yet-active and polled until the deadline.
  * After signature acceptance, loads the HMAC metadata by access id with ADC to
    report ``identity_id`` (the owning service-account email) and ``account_id``
    (the resolved project). ``authenticated`` is NEVER derived from an ADC request.
    A successful identity readback with a non-empty ``service_account_email`` is
    REQUIRED for top-level ``success`` (mirroring the AWS oracle, which derives
    success together with the returned identity): a metadata lookup failure, or
    metadata carrying no email, yields an unsuccessful structured result even
    though the signature was accepted.

NOTE: Left UNWIRED in the baseline GCP provider config; re-enable only after a
preflight proves full control over a disposable, run-owned HMAC key.

Usage:
    python3 test_access_key.py --access-key-id GOOG1E... --secret-access-key ... \
        --region us-central1 --wait 5 --retries 5

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "authenticated": true,
    "identity_id": "isv-ak-...@my-project.iam.gserviceaccount.com",
    "account_id": "my-project"
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

import boto3
from botocore.exceptions import ClientError
from common.compute import resolve_project
from common.errors import classify_gcp_error, handle_gcp_errors, retry_idempotent
from google.cloud import storage

_GCS_S3_ENDPOINT = "https://storage.googleapis.com"

# Signature accepted (authenticated) even if unauthorized for the resource.
_SIGNATURE_ACCEPTED_CODES = frozenset({"AccessDenied"})
# Credential rejection: the endpoint refused the signature / key.
_REJECTION_CODES = frozenset({"InvalidAccessKeyId", "InvalidSecurity", "SignatureDoesNotMatch"})

# Cloud Storage documents up to ~60s for a newly-created HMAC key to activate, so
# poll against a MONOTONIC 60s deadline and make a final probe at or after it —
# an attempt-counted exponential backoff otherwise gives up around 35s and can
# falsely report a still-propagating valid key as unusable.
_ACTIVATION_DEADLINE_SECONDS = 60


@handle_gcp_errors
def main() -> int:
    """Authenticate the supplied HMAC key against Cloud Storage and print a result."""
    parser = argparse.ArgumentParser(description="Authenticate a Cloud Storage HMAC key (S3-compat)")
    parser.add_argument("--access-key-id", required=True, help="Cloud Storage HMAC access id")
    parser.add_argument("--secret-access-key", required=True, help="Cloud Storage HMAC secret")
    parser.add_argument("--region", default="us-central1", help="Signature region for the S3 client")
    parser.add_argument(
        "--wait", type=int, default=5, help="Initial wait (s) before the first probe; also the poll interval"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Advisory minimum probe count; polling is bounded by the 60s activation deadline",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "authenticated": False,
        "identity_id": "",
        "account_id": "",
    }

    # Resolve the effective project BEFORE the first probe. Cloud Storage's
    # project-wide GET Service request (boto3 list_buckets) requires
    # x-goog-project-id unless an interoperable-access default project is
    # configured; relying on that ambient default makes the credential signal
    # non-portable. ADC resolves only the project STRING here — it never signs
    # the probe below (authenticated is derived solely from the HMAC signature).
    project = resolve_project(None)

    # Isolated S3 client: ONLY the supplied HMAC credentials, GCS endpoint. No ADC.
    s3 = boto3.client(
        "s3",
        aws_access_key_id=args.access_key_id,
        aws_secret_access_key=args.secret_access_key,
        region_name=args.region,
        endpoint_url=_GCS_S3_ENDPOINT,
    )

    # Scope every signed list_buckets request to the resolved project by adding
    # x-goog-project-id BEFORE signing (so it is covered by the signature),
    # including on every retry. del-then-set guarantees exactly one header.
    def _add_project_scope(request: Any, **_kwargs: Any) -> None:
        del request.headers["x-goog-project-id"]
        request.headers["x-goog-project-id"] = project

    s3.meta.events.register("before-sign.s3.ListBuckets", _add_project_scope)

    if args.wait > 0:
        time.sleep(args.wait)

    interval = max(args.wait, 1)
    deadline = time.monotonic() + _ACTIVATION_DEADLINE_SECONDS
    last_code: str | None = None
    attempt = 0
    while True:
        try:
            s3.list_buckets()
            result["authenticated"] = True
            break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "Unknown")
            last_code = code
            if code in _SIGNATURE_ACCEPTED_CODES:
                # Signature accepted (authenticated); authorization is a separate axis.
                result["authenticated"] = True
                break
        attempt += 1
        # A rejection code may just mean the new key has not activated yet. Keep
        # polling until a probe lands at or after the monotonic activation
        # deadline (and the advisory minimum probe count is met).
        if time.monotonic() >= deadline and attempt >= args.retries:
            break
        time.sleep(interval)

    if not result["authenticated"]:
        result["error"] = f"HMAC signature not accepted (last code: {last_code})"
        if last_code in _REJECTION_CODES:
            result["error_code"] = last_code
        print(json.dumps(result, indent=2))
        return 1

    # Signature accepted: resolve the identity via ADC metadata (NOT via the
    # signed request). authenticated is already fixed above from the signed probe;
    # project was resolved up front and drove the project-scope hook.
    result["account_id"] = project

    # The identity readback is REQUIRED for top-level success, not best-effort
    # context. Like the AWS oracle -- which derives success together with the
    # returned identity fields (identity_id/account_id) in one block -- and the
    # identity requirement to emit the HMAC key's owning service_account_email as
    # identity_id, a completed signature probe alone does not establish the
    # verified identity. A metadata lookup failure, or metadata with no
    # service-account email, leaves identity_id empty, so the step reports an
    # unsuccessful structured result (authenticated stays truthfully True) rather
    # than claiming verified success on the signature alone.
    try:
        meta = retry_idempotent(
            storage.Client(project=project).get_hmac_key_metadata,
            access_id=args.access_key_id,
            project_id=project,
            op_desc="get_hmac_key_metadata",
        )
    except Exception as e:
        result["identity_lookup_error"] = classify_gcp_error(e)[1]
        result["error"] = f"HMAC signature accepted but identity readback failed: {result['identity_lookup_error']}"
        print(json.dumps(result, indent=2))
        return 1

    result["identity_id"] = meta.service_account_email
    if not result["identity_id"]:
        # Metadata resolved but carried no owning service-account email: the identity
        # is not established, so verified success is not warranted.
        result["error"] = "HMAC metadata returned no service_account_email; identity unverified"
        print(json.dumps(result, indent=2))
        return 1

    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
