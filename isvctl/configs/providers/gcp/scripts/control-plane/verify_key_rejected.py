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

"""Verify a deactivated Cloud Storage HMAC key is rejected at the S3 endpoint.

The AWS reference re-attempts authentication with the disabled key until it is
rejected. Cloud Storage HMAC state changes can take up to ~3 minutes to
propagate, so this step polls the same isolated boto3 S3 client + supplied HMAC
credentials against ``https://storage.googleapis.com``:

  * Only ``InvalidAccessKeyId``, ``InvalidSecurity``, or ``SignatureDoesNotMatch``
    count as credential rejection (the success path).
  * A successful request or an ``AccessDenied`` means the key is STILL recognized
    (the deactivation has not propagated), so polling continues.
  * If the key remains recognized at the deadline, ``rejected=false`` and
    ``success=false`` — the deactivation was not observed.

NOTE: Left UNWIRED in the baseline GCP provider config; re-enable only after a
preflight proves full control over a disposable, run-owned HMAC key.

Usage:
    python3 verify_key_rejected.py --access-key-id GOOG1E... --secret-access-key ... \
        --region us-central1 --wait 5 --retries 5

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "rejected": true,
    "error_code": "InvalidAccessKeyId"
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
from common.errors import handle_gcp_errors

_GCS_S3_ENDPOINT = "https://storage.googleapis.com"

# Credential rejection: the endpoint refused the (now-disabled) key.
_REJECTION_CODES = frozenset({"InvalidAccessKeyId", "InvalidSecurity", "SignatureDoesNotMatch"})

# Cloud Storage documents up to ~3 minutes for an HMAC state change to propagate,
# so poll against a MONOTONIC 180s deadline and make a final probe at or after it
# — an attempt-counted backoff otherwise stops around 35s and can falsely report
# a still-active disabled key as already rejected (or vice versa).
_REJECTION_DEADLINE_SECONDS = 180


@handle_gcp_errors
def main() -> int:
    """Poll the disabled HMAC key until it is rejected and print a structured result."""
    parser = argparse.ArgumentParser(description="Verify a disabled Cloud Storage HMAC key is rejected")
    parser.add_argument("--access-key-id", required=True, help="Disabled Cloud Storage HMAC access id")
    parser.add_argument("--secret-access-key", required=True, help="Disabled Cloud Storage HMAC secret")
    parser.add_argument("--region", default="us-central1", help="Signature region for the S3 client")
    parser.add_argument(
        "--wait", type=int, default=5, help="Initial wait (s) before the first probe; also the poll interval"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Advisory minimum probe count; polling is bounded by the 180s propagation deadline",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "rejected": False,
        "error_code": "",
    }

    # Resolve the effective project BEFORE the first probe. Cloud Storage's
    # project-wide GET Service request (boto3 list_buckets) requires
    # x-goog-project-id unless an interoperable-access default project is
    # configured; relying on that ambient default makes the rejection signal
    # non-portable. This is the SAME project-scoped signed request as the
    # positive test — ADC resolves only the project STRING, never signs it.
    project = resolve_project(None)

    # Isolated S3 client: ONLY the supplied (disabled) HMAC credentials, GCS endpoint.
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
    deadline = time.monotonic() + _REJECTION_DEADLINE_SECONDS
    last_state = "recognized"
    attempt = 0
    while True:
        try:
            s3.list_buckets()
            # Still authenticated -> deactivation not yet propagated; keep polling.
            last_state = "active"
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "Unknown")
            if code in _REJECTION_CODES:
                result["rejected"] = True
                result["error_code"] = code
                result["success"] = True
                break
            # AccessDenied / other -> signature still recognized; keep polling.
            last_state = code
        attempt += 1
        # Keep polling until a probe lands at or after the monotonic propagation
        # deadline (and the advisory minimum probe count is met); only then is a
        # still-recognized key trustworthy evidence the disable did not propagate.
        if time.monotonic() >= deadline and attempt >= args.retries:
            break
        time.sleep(interval)

    if not result["success"]:
        result["error"] = f"disabled key still recognized after retries (last state: {last_state})"

    print(json.dumps(result, indent=2))
    # Always exit 0 and leave the semantic verdict to the validator, which consumes
    # the emitted `rejected` field (AccessKeyRejectedCheck). This mirrors the AWS
    # realism oracle (aws/scripts/control-plane/verify_key_rejected.py), whose
    # error path also exits zero after emitting an honest rejected=false result:
    # a still-recognized disabled key is a validator-reported assertion failure, not
    # a step-execution error, so the step must not fail at execution and short out
    # the run before the validator can report it.
    return 0


if __name__ == "__main__":
    sys.exit(main())
