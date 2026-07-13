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

"""DATASVC-XX-01 object data path: put -> get (byte compare) -> delete on Cloud Storage.

The GCP port of the AWS reference ``s3_object_lifecycle``. It creates a temporary
Cloud Storage bucket, uploads a small generated payload, downloads it and
byte-compares against what was uploaded (detecting corruption), deletes the
object, then deletes the bucket in a ``finally`` block. Each operation reports
its own ``passed`` state derived from the real API / readback result; ``success``
is true only when put/get/delete all pass AND the bucket cleanup succeeds.

This validates the provider-native object data path with Application Default
Credentials (``google-cloud-storage``). It is intentionally separate from the
S3-compatible signed-HMAC authentication path (the excluded access-key
lifecycle), so it never depends on a credential that lifecycle intentionally
disables.

Usage:
    python3 s3_object_lifecycle.py --region us-central1

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "test_name": "s3_object_lifecycle",
    "bucket_name": "isv-validate-gcs-1a2b3c4d",
    "object_key": "isv-validate-1a2b3c4d.txt",
    "operations": {
        "put":    {"passed": true},
        "get":    {"passed": true, "content_matches": true},
        "delete": {"passed": true}
    }
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
from common.errors import classify_gcp_error, handle_gcp_errors, retry_idempotent
from google.api_core import exceptions as gax
from google.cloud import storage


def _fail(op: dict[str, Any], error: Exception) -> None:
    """Mark an operation failed and attach a classified, visible error."""
    bucket, message = classify_gcp_error(error)
    op["passed"] = False
    op["error_code"] = bucket
    op["error"] = message


def _delete_bucket_best_effort(bucket: storage.Bucket) -> str | None:
    """Delete a bucket (emptying it first). Returns an error message or None on success.

    ``force=True`` deletes any remaining contained objects before the bucket, and
    the delete is wrapped in the idempotent transient retry envelope. A NotFound
    is the desired terminal state and is treated as success.
    """
    try:
        retry_idempotent(bucket.delete, force=True, op_desc=f"delete bucket {bucket.name}")
        return None
    except gax.NotFound:
        return None
    except Exception as e:  # cleanup failure is surfaced, not raised
        return classify_gcp_error(e)[1]


@handle_gcp_errors
def main() -> int:
    """Exercise the Cloud Storage object lifecycle and print a structured JSON result."""
    parser = argparse.ArgumentParser(description="Exercise Cloud Storage object lifecycle (DATASVC-XX-01)")
    parser.add_argument("--region", default="", help="Cloud Storage bucket location")
    parser.add_argument("--bucket-prefix", default="isv-validate-gcs", help="Bucket name prefix")
    args = parser.parse_args()

    project = resolve_project(None)
    bucket_name = unique_suffix(args.bucket_prefix)
    object_key = f"{unique_suffix('isv-validate')}.txt"
    expected_body = f"isv-ncp-validate gcs datasvc-xx-01 {uuid.uuid4().hex}".encode()

    operations: dict[str, dict[str, Any]] = {
        "put": {"passed": False},
        "get": {"passed": False},
        "delete": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "test_name": "s3_object_lifecycle",
        "bucket_name": bucket_name,
        "object_key": object_key,
        "operations": operations,
    }

    client = storage.Client(project=project)
    bucket: storage.Bucket | None = None
    # Cleanup ownership flag: only True after THIS invocation's own create_bucket
    # succeeds. Cloud Storage bucket names are a single global namespace, so a name
    # collision is NOT provably this run's leftover -- the colliding bucket may
    # belong to another project entirely. We therefore never adopt (and never
    # force-delete) a bucket we did not create; the finally block acts only on a
    # bucket we own.
    bucket_created = False
    try:
        try:
            bucket = client.create_bucket(bucket_name, location=args.region or None)
            bucket_created = True
        except gax.Conflict as e:
            # An "already exists" on create is a failure, not success. With no
            # explicit, readback-verified reuse opt-in we must not assume the
            # colliding bucket is ours (the run-id-suffixed name is not an ownership
            # proof across a global namespace), and we must never force-delete an
            # unverified candidate. Fail the create like the object-store reference
            # does, and leave cleanup ownership unset.
            _bucket, message = classify_gcp_error(e)
            result["error"] = f"CreateBucket failed (bucket name already in use): {message}"
            print(json.dumps(result, indent=2))
            return 1
        except Exception as e:  # surface create failure as a structured result
            _bucket, message = classify_gcp_error(e)
            result["error"] = f"CreateBucket failed: {message}"
            print(json.dumps(result, indent=2))
            return 1

        if bucket is None:
            result["error"] = "CreateBucket returned no bucket handle"
            print(json.dumps(result, indent=2))
            return 1

        blob = bucket.blob(object_key)

        # PUT
        try:
            blob.upload_from_string(expected_body)
            operations["put"]["passed"] = True
        except Exception as e:  # record op failure, keep the result visible
            _fail(operations["put"], e)

        # GET + byte compare (only meaningful after a successful put)
        if operations["put"]["passed"]:
            try:
                body = retry_idempotent(blob.download_as_bytes, op_desc="download_as_bytes")
                content_matches = body == expected_body
                operations["get"]["content_matches"] = content_matches
                operations["get"]["passed"] = content_matches
                if not content_matches:
                    operations["get"]["error"] = "GetObject body does not match PutObject body"
                    operations["get"]["error_code"] = "content_mismatch"
            except Exception as e:  # record op failure, keep the result visible
                _fail(operations["get"], e)

            # DELETE
            try:
                retry_idempotent(blob.delete, op_desc="delete blob")
                operations["delete"]["passed"] = True
            except gax.NotFound:
                # Object already gone is the desired terminal state.
                operations["delete"]["passed"] = True
            except Exception as e:  # record op failure, keep the result visible
                _fail(operations["delete"], e)

        result["success"] = all(op["passed"] for op in operations.values())
    finally:
        if bucket_created and bucket is not None:
            cleanup_error = _delete_bucket_best_effort(bucket)
            if cleanup_error:
                result.setdefault("cleanup_errors", []).append(cleanup_error)
                cleanup_msg = f"Cleanup failed: {cleanup_error}"
                result["error"] = f"{result['error']}; {cleanup_msg}" if result.get("error") else cleanup_msg
                result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
