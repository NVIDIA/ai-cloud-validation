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

"""DATASVC-XX-01 data-path: PutObject -> GetObject -> DeleteObject on S3.

Pairs with the existing control-plane ``check_api`` step (which calls
``s3.list_buckets()`` to prove authenticated S3-compatible API access).
This step creates a temp bucket, exercises the object lifecycle on a
small object, byte-compares Get vs Put to detect data corruption, and
deletes the bucket. The bucket is removed in the same step so this is
self-contained and re-runnable without extra teardown wiring.

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "test_name": "s3_object_lifecycle",
    "bucket_name": "isv-validate-s3-xxxxxxxx",
    "object_key": "isv-validate-xxxxxxxx.txt",
    "operations": {
        "put":    {"passed": true},
        "get":    {"passed": true, "content_matches": true},
        "delete": {"passed": true}
    }
}
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


def _fail(op: dict[str, Any], code: str, message: str) -> None:
    """Mark an operation as failed and attach normalized error details."""
    op["passed"] = False
    op["error_code"] = code
    op["error"] = message


def _create_bucket(s3: Any, bucket: str, region: str) -> None:
    """Create an S3 bucket, handling the us-east-1 LocationConstraint special case."""
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": region},
        )


def _delete_bucket_best_effort(s3: Any, bucket: str) -> str | None:
    """Empty and delete a bucket. Returns an error message or None on success."""
    try:
        paginator = s3.get_paginator("list_object_versions")
        batch: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=bucket):
            for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
                batch.append({"Key": v["Key"], "VersionId": v["VersionId"]})
                if len(batch) == 1000:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                    batch = []
        if batch:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
        s3.delete_bucket(Bucket=bucket)
        return None
    except ClientError as e:
        return str(e)


def main() -> int:
    """Run the S3 object lifecycle probe and print a structured JSON result."""
    parser = argparse.ArgumentParser(description="Exercise S3 object lifecycle (DATASVC-XX-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("S3_ENDPOINT_URL"),
        help="Custom S3-compatible endpoint URL (optional)",
    )
    parser.add_argument("--bucket-prefix", default="isv-validate-s3")
    args = parser.parse_args()

    bucket_name = f"{args.bucket_prefix}-{uuid.uuid4().hex[:8]}"
    object_key = f"isv-validate-{uuid.uuid4().hex[:8]}.txt"
    expected_body = f"isv-ncp-validate s3 datasvc-xx-01 {uuid.uuid4().hex}".encode()

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

    client_kwargs: dict[str, Any] = {"region_name": args.region}
    if args.endpoint_url:
        client_kwargs["endpoint_url"] = args.endpoint_url
    s3 = boto3.client("s3", **client_kwargs)

    bucket_created = False
    try:
        try:
            _create_bucket(s3, bucket_name, args.region)
            bucket_created = True
        except (ClientError, NoCredentialsError) as e:
            result["error"] = f"CreateBucket failed: {e}"
            print(json.dumps(result, indent=2))
            return 1

        try:
            s3.put_object(Bucket=bucket_name, Key=object_key, Body=expected_body)
            operations["put"]["passed"] = True
        except ClientError as e:
            _fail(operations["put"], e.response["Error"]["Code"], str(e))
        except NoCredentialsError as e:
            _fail(operations["put"], "NoCredentials", str(e))

        if operations["put"]["passed"]:
            try:
                response = s3.get_object(Bucket=bucket_name, Key=object_key)
                body = response["Body"].read()
                content_matches = body == expected_body
                operations["get"]["content_matches"] = content_matches
                if content_matches:
                    operations["get"]["passed"] = True
                else:
                    _fail(
                        operations["get"],
                        "ContentMismatch",
                        "GetObject body does not match PutObject body",
                    )
            except ClientError as e:
                _fail(operations["get"], e.response["Error"]["Code"], str(e))
            except NoCredentialsError as e:
                _fail(operations["get"], "NoCredentials", str(e))

            try:
                s3.delete_object(Bucket=bucket_name, Key=object_key)
                operations["delete"]["passed"] = True
            except ClientError as e:
                _fail(operations["delete"], e.response["Error"]["Code"], str(e))
            except NoCredentialsError as e:
                _fail(operations["delete"], "NoCredentials", str(e))

        result["success"] = all(op["passed"] for op in operations.values())

    finally:
        if bucket_created:
            cleanup_error = _delete_bucket_best_effort(s3, bucket_name)
            if cleanup_error:
                result.setdefault("cleanup_errors", []).append(cleanup_error)
                cleanup_msg = f"Cleanup failed: {cleanup_error}"
                result["error"] = f"{result['error']}; {cleanup_msg}" if result.get("error") else cleanup_msg
                result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
