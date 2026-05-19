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

"""Teardown AWS EC2 bare-metal instance and associated resources.

Mirrors the VM teardown script but sets platform to "bm". The actual
EC2 API calls are identical (terminate, delete SG, delete key pair).

Usage:
    python teardown.py --instance-id i-xxx --region us-west-2

Output JSON:
{
    "success": true,
    "platform": "bm",
    "resources_destroyed": true,
    "deleted": {
        "instances": ["i-xxx"],
        "security_groups": ["sg-xxx"],
        "key_pairs": ["key-name"]
    }
}
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/aws/scripts/ (for common.*)

import boto3
from botocore.exceptions import ClientError, WaiterError
from common.ec2 import sanitize_key_name


def main() -> int:
    """Terminate a bare-metal EC2 instance and clean up associated resources.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Teardown bare-metal EC2 instance")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--delete-key-pair", action="store_true", help="Also delete key pair")
    parser.add_argument("--delete-security-group", action="store_true", help="Also delete security group")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip actual destroy")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "bm",
        "resources_destroyed": False,
        "deleted": {
            "instances": [],
            "security_groups": [],
            "key_pairs": [],
        },
    }

    if args.skip_destroy:
        result["success"] = True
        result["message"] = (
            f"Teardown skipped. Instance {args.instance_id} is still running. "
            f"To teardown later, unset AWS_BM_SKIP_TEARDOWN and rerun."
        )
        print(json.dumps(result, indent=2))
        return 0

    ec2 = boto3.client("ec2", region_name=args.region)

    sg_ids: list[str] = []
    key_name: str | None = None

    try:
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        sg_ids = [sg["GroupId"] for sg in instance.get("SecurityGroups", [])]
        key_name = instance.get("KeyName")

        print(f"Terminating instance {args.instance_id}...", file=sys.stderr)
        ec2.terminate_instances(InstanceIds=[args.instance_id])
        result["deleted"]["instances"].append(args.instance_id)

        # Wait for termination - bare-metal can take 15-20+ minutes.
        # If the waiter times out, that's OK: the terminate call succeeded
        # and AWS will finish it. We still proceed to clean up other resources.
        try:
            waiter = ec2.get_waiter("instance_terminated")
            waiter.wait(
                InstanceIds=[args.instance_id],
                WaiterConfig={"Delay": 30, "MaxAttempts": 60},
            )
            print("  Instance terminated", file=sys.stderr)
        except WaiterError:
            print(
                "  Waiter timed out, but terminate was initiated. AWS will finish asynchronously.",
                file=sys.stderr,
            )
            result.setdefault("warnings", []).append("Termination waiter timed out; instance is still shutting down")

    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
            result["message"] = "Instance not found (already terminated)"
        else:
            result["error"] = str(e)
            print(json.dumps(result, indent=2))
            return 1
    except Exception as e:
        result["error"] = str(e)
        print(json.dumps(result, indent=2))
        return 1

    # Always attempt SG and key pair cleanup, even if waiter timed out.
    # Bare-metal instances can hold SG dependencies for 30-60s after
    # reaching "terminated" state, so retry with backoff.
    if args.delete_security_group and sg_ids:
        for sg_id in sg_ids:
            try:
                sg_info = ec2.describe_security_groups(GroupIds=[sg_id])
                if sg_info["SecurityGroups"] and sg_info["SecurityGroups"][0].get("GroupName") == "default":
                    continue
            except ClientError as e:
                if e.response["Error"]["Code"] == "InvalidGroup.NotFound":
                    continue
                result.setdefault("warnings", []).append(f"Could not describe SG {sg_id}: {e}")
                continue

            deleted = False
            for attempt in range(6):
                delay = min(5 * 2**attempt, 60)  # 5, 10, 20, 40, 60, 60s
                print(
                    f"  SG {sg_id}: {'retrying' if attempt > 0 else 'waiting'} {delay}s before delete (attempt {attempt + 1}/6)...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                try:
                    ec2.delete_security_group(GroupId=sg_id)
                    result["deleted"]["security_groups"].append(sg_id)
                    print(f"  SG {sg_id}: deleted", file=sys.stderr)
                    deleted = True
                    break
                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    if error_code == "InvalidGroup.NotFound":
                        deleted = True
                        break
                    if error_code != "DependencyViolation":
                        print(f"  SG {sg_id}: {error_code} - {e}", file=sys.stderr)
                        result.setdefault("warnings", []).append(f"Could not delete SG {sg_id}: {e}")
                        break
                    print(f"  SG {sg_id}: DependencyViolation", file=sys.stderr)

            if not deleted:
                result.setdefault("warnings", []).append(
                    f"SG {sg_id} still in use after retries; may need manual cleanup"
                )

    # AWS key pair names can include any printable ASCII (up to 255 chars),
    # so pass the raw name to EC2. Only sanitize when composing the local
    # /tmp/<name>.pem path, where a crafted name could traverse.
    if args.delete_key_pair and key_name:
        try:
            ec2.delete_key_pair(KeyName=key_name)
            result["deleted"]["key_pairs"].append(key_name)
            print(f"  Key pair {key_name}: deleted", file=sys.stderr)
        except ClientError as e:
            print(f"  Key pair {key_name}: {e}", file=sys.stderr)
            result.setdefault("warnings", []).append(f"Could not delete key pair: {e}")

        try:
            safe_key_name = sanitize_key_name(key_name)
            key_file = f"/tmp/{safe_key_name}.pem"
            if os.path.exists(key_file):
                os.remove(key_file)
        except ValueError as e:
            result.setdefault("warnings", []).append(f"Key pair deleted in AWS but local key filename is unsafe: {e}")

    result["success"] = True
    result["resources_destroyed"] = True
    result["message"] = "Bare-metal instance teardown completed"

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
