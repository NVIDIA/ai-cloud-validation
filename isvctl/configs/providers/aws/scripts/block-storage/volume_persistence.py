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

"""DATASVC-XX-04: persistent volumes survive instance restarts (issue #323).

Stops and starts the instance, then confirms the attached block volume is
still attached after the restart and that its sentinel data is intact. The
volume is re-mounted in the guest (a manual mount does not survive a reboot)
and the sentinel file is byte-compared against the fixture content.

Output JSON:
{
    "success": true,
    "platform": "block_storage",
    "test_name": "volume_persistence",
    "volume_id": "vol-xxx",
    "operations": {
        "stop":            {"passed": true},
        "start":           {"passed": true},
        "verify_attached": {"passed": true},
        "verify_data":     {"passed": true, "content_matches": true}
    }
}
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/aws/scripts/ (for common.*)

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from common import ebs
from common.ec2 import wait_for_public_ip
from common.ssh_utils import wait_for_ssh


def _fail(op: dict[str, Any], message: str) -> None:
    """Mark an operation failed with a message."""
    op["passed"] = False
    op["error"] = message


def main() -> int:
    """Stop/start the instance and verify the volume + data survive; print JSON."""
    parser = argparse.ArgumentParser(description="Verify block volume persistence across restart (DATASVC-XX-04)")
    parser.add_argument("--instance-id", required=True, help="EC2 instance to restart")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--volume-id", required=True, help="Volume expected to persist (fixture volume)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="/mnt/isv-block", help="In-guest mount point")
    parser.add_argument("--expected-content", required=True, help="Sentinel content written by the fixture")
    args = parser.parse_args()

    operations: dict[str, dict[str, Any]] = {
        "stop": {"passed": False},
        "start": {"passed": False},
        "verify_attached": {"passed": False},
        "verify_data": {"passed": False},
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "block_storage",
        "test_name": "volume_persistence",
        "volume_id": args.volume_id,
        "instance_id": args.instance_id,
        "operations": operations,
    }

    ec2 = boto3.client("ec2", region_name=args.region)

    try:
        ec2.stop_instances(InstanceIds=[args.instance_id])
        ec2.get_waiter("instance_stopped").wait(
            InstanceIds=[args.instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )
        operations["stop"]["passed"] = True

        ec2.start_instances(InstanceIds=[args.instance_id])
        ec2.get_waiter("instance_status_ok").wait(
            InstanceIds=[args.instance_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )
        instances = ec2.describe_instances(InstanceIds=[args.instance_id])
        instance = instances["Reservations"][0]["Instances"][0]
        public_ip = instance.get("PublicIpAddress") or wait_for_public_ip(ec2, args.instance_id)
        if not public_ip:
            _fail(operations["start"], "No public IP after restart")
            result["error"] = "No public IP after restart"
            print(json.dumps(result, indent=2))
            return 1
        if not wait_for_ssh(public_ip, args.ssh_user, args.key_file, max_attempts=40, interval=15):
            _fail(operations["start"], "SSH not ready after restart")
            result["error"] = "SSH not ready after restart"
            print(json.dumps(result, indent=2))
            return 1
        operations["start"]["passed"] = True
    except (ClientError, NoCredentialsError) as e:
        result["error"] = f"Restart failed: {e}"
        print(json.dumps(result, indent=2))
        return 1

    if ebs.is_volume_attached_to(ec2, args.volume_id, args.instance_id):
        operations["verify_attached"]["passed"] = True
    else:
        _fail(operations["verify_attached"], "Volume not attached after restart")

    if operations["verify_attached"]["passed"]:
        rc, out, err = ebs.mount_and_read_sentinel(
            public_ip, args.ssh_user, args.key_file, args.volume_id, args.mount_point
        )
        if rc != 0:
            _fail(operations["verify_data"], f"Could not read sentinel after restart (rc={rc}): {err.strip()[:300]}")
        else:
            content_matches = out.strip() == args.expected_content.strip()
            operations["verify_data"]["content_matches"] = content_matches
            if content_matches:
                operations["verify_data"]["passed"] = True
            else:
                _fail(operations["verify_data"], "Sentinel content changed across restart")

    result["success"] = all(op["passed"] for op in operations.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
