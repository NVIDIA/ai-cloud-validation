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

"""Shared serial console utilities for EC2 instances (VM and bare-metal).

Provides the core boto3 calls for checking serial console access and
retrieving console output. Used by both vm/serial_console.py and
bare_metal/serial_console.py.
"""

import json
import time
from typing import Any

from botocore.exceptions import ClientError

RETENTION_EVIDENCE_LIMITATION = "EC2 GetConsoleOutput does not prove one-month log retention"
SERIAL_ACCESS_DISABLED_SKIP_REASON = "EC2 serial console access is disabled for this account or region"
SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED = 30


def check_serial_access(ec2: Any) -> dict[str, Any]:
    """Check if EC2 serial console access is enabled for the account."""
    result: dict[str, Any] = {"enabled": False}
    try:
        response = ec2.get_serial_console_access_status()
        result["enabled"] = response.get("SerialConsoleAccessEnabled", False)
    except ClientError as e:
        result["error"] = str(e)
    return result


def get_console_output(ec2: Any, instance_id: str, retries: int = 3) -> dict[str, Any]:
    """Retrieve the serial console output for an instance.

    Retries with ``Latest=True`` to handle cases where output is not
    yet available (common on Nitro and bare-metal instances).
    """
    result: dict[str, Any] = {"available": False}
    try:
        for attempt in range(retries):
            response = ec2.get_console_output(InstanceId=instance_id, Latest=True)
            output = response.get("Output", "")

            if output:
                result["available"] = True
                result["output_length"] = len(output)
                result["output_snippet"] = output[-500:] if len(output) > 500 else output
                result["timestamp"] = str(response.get("Timestamp", ""))
                return result

            if attempt < retries - 1:
                time.sleep(10)

        result["output_length"] = 0
        result["message"] = "Console output empty after retries"
    except ClientError as e:
        result["error"] = str(e)
    return result


def run_serial_console_check(ec2: Any, instance_id: str, platform: str) -> tuple[dict[str, Any], int]:
    """Run the full serial console check and return (result_dict, exit_code).

    Args:
        ec2: boto3 EC2 client
        instance_id: EC2 instance ID
        platform: Platform identifier ("vm" or "bm")
    """
    result: dict[str, Any] = {
        "success": False,
        "platform": platform,
        "instance_id": instance_id,
        "console_available": False,
        "serial_access_enabled": False,
        "console_log_queryable": False,
        "retention_days_required": SERIAL_CONSOLE_RETENTION_DAYS_REQUIRED,
        "retention_days_configured": 0,
        "oldest_queryable_log_age_days": 0,
        "query_result_count": 0,
        "retention_evidence": RETENTION_EVIDENCE_LIMITATION,
    }

    try:
        access = check_serial_access(ec2)
        result["serial_access_enabled"] = access.get("enabled", False)
        if access.get("error"):
            result["serial_access_error"] = access["error"]

        console = get_console_output(ec2, instance_id)
        result["console_available"] = console.get("available", False)
        result["output_length"] = console.get("output_length", 0)

        if console.get("output_snippet"):
            result["output_snippet"] = console["output_snippet"]
        if console.get("timestamp"):
            result["timestamp"] = console["timestamp"]
        if console.get("error"):
            result["error"] = console["error"]

        result["success"] = result["console_available"] or result["serial_access_enabled"]

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return result, 0 if result["success"] else 1
