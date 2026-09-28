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

"""Report each network-attached BM's stable IP from the tenant BM pool (STG03-01).

Reads ``GET /compute/bms`` (the tenant BM pool). A BM's ``ipAddress`` is its
primary IPv4 address on the tenant subnet. The address is reserved when the BM
is first provisioned, not when it is attached, and the reservation outlives a
deprovision: an attached ``AVAILABLE`` BM (no OS) has no address if it was never
provisioned and may keep one if it was. Neither is a host, so in scope are the
provisioned BMs (``RUNNING`` or ``STOPPED``) on a subnet; every one must report
an IP, and one that does not is reported with none so the check fails on it.

Usage:
    python query_stable_ips.py

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "query_stable_ips",
    "hosts_checked": 1,
    "hosts": [{"host_id": "bm.xxx", "primary_ip_addresses": ["172.16.240.10"]}]
}
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log

TEST_NAME = "query_stable_ips"
# Settled states of a BM holding an OS (and so its subnet address).
PROVISIONED_STATES = ("RUNNING", "STOPPED")


def main() -> int:
    """Emit the stable IP JSON contract.

    Returns:
        0 on success, 1 on failure
    """
    result: dict[str, Any] = {"success": False, "platform": "bm", "test_name": TEST_NAME, "hosts": []}
    try:
        bms = FirebirdClient().paginate("/compute/bms", "items")
        result["hosts"] = [
            {"host_id": bm.get("id") or "", "primary_ip_addresses": [bm["ipAddress"]] if bm.get("ipAddress") else []}
            for bm in bms
            if bm.get("subnetId") and bm.get("state") in PROVISIONED_STATES
        ]
        result["hosts_checked"] = len(result["hosts"])
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
