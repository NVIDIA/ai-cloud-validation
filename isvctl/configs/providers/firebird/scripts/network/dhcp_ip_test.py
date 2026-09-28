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

"""Hand a provisioned BM to the DHCP/IP management check (IPAM01-01).

The check itself SSHes in and compares the host's DHCP lease and addresses with
the IP the platform reports, so this step only resolves the BM through the API
(``GET /projects/{p}/compute/bms/{bm}``) and passes its API-reported IP, key,
and user along. Skips when no provisioned BM is configured.

Usage:
    python dhcp_ip_test.py --instance-id bm.xxx --key-file /tmp/key [--ssh-user ubuntu]

Output JSON:
{
    "success": true,
    "platform": "network",
    "instance_id": "bm.xxx",
    "public_ip": "172.16.240.10",
    "private_ip": "172.16.240.10",
    "key_file": "/tmp/key",
    "ssh_user": "ubuntu"
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log
from common.probes import PRIMARY_MISSING, running_host, skipped


def main() -> int:
    """Resolve the BM and emit its SSH target and platform-reported IP.

    Returns:
        0 on success or skip, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Firebird DHCP/IP management target")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID)")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network", "instance_id": args.instance_id}
    if not (args.instance_id and args.key_file):
        print(json.dumps(skipped(result, PRIMARY_MISSING), indent=2))
        return 0
    try:
        host = running_host(FirebirdClient(), args.instance_id, args.ssh_user, args.key_file)
        result.update({"public_ip": host.ip, "private_ip": host.ip, "key_file": host.key_file, "ssh_user": host.user})
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
