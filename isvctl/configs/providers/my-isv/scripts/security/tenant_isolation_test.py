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

"""Tenant isolation test (SEC11-01) - TEMPLATE (replace with your platform implementation).

Verifies hard logical isolation between two tenants across four orthogonal
surfaces:

  * ``network_isolated``  -- tenant A's network has no route to tenant B
    (no peering, no shared route, SG/NACL deny).
  * ``data_isolated``     -- tenant A is denied ``kms:Decrypt`` on tenant
    B's CMK and ``s3:GetObject`` (or equivalent) on tenant B's bucket.
  * ``compute_isolated``  -- tenant A is denied ``ec2:*`` /
    ``ssm:StartSession`` (or equivalent) against tenant B's instance.
  * ``storage_isolated``  -- tenant A is denied ``ebs:*`` snapshot/attach
    (or equivalent) against tenant B's volume.

Physical isolation (bare-metal) and switch-fabric isolation are out of
scope for this test (covered by SDN04-04/05).

Provision two ephemeral tenants in a try / finally and run negative
probes from tenant A against tenant B's resources, asserting each probe
is denied (AccessDenied / forbidden / timeout / refused).

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "tenant_isolation_test",
    "tenant_a_id": "<source tenant id>",
    "tenant_b_id": "<target tenant id>",
    "tests": {
      "network_isolated":  {"passed": true},
      "data_isolated":     {"passed": true},
      "compute_isolated":  {"passed": true},
      "storage_isolated":  {"passed": true}
    }
  }

Usage:
    python tenant_isolation_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Tenant isolation test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Tenant isolation test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "tenant_isolation_test",
        "region": args.region,
        "tenant_a_id": "",
        "tenant_b_id": "",
        "tests": {
            "network_isolated": {"passed": False},
            "data_isolated": {"passed": False},
            "compute_isolated": {"passed": False},
            "storage_isolated": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's tenant isolation  ║
    # ║  test.                                                           ║
    # ║                                                                  ║
    # ║  Pseudocode (adapt to your platform's IAM/network/storage APIs): ║
    # ║                                                                  ║
    # ║    tenant_a = provision_tenant("isv-sec11-test-A")               ║
    # ║    tenant_b = provision_tenant("isv-sec11-test-B")               ║
    # ║    try:                                                          ║
    # ║      assert not tenant_a.can_reach(tenant_b.network)             ║
    # ║      assert not tenant_a.can_decrypt(tenant_b.cmk)               ║
    # ║      assert not tenant_a.can_get_object(tenant_b.bucket)         ║
    # ║      assert not tenant_a.can_describe(tenant_b.instance)         ║
    # ║      assert not tenant_a.can_attach_volume(tenant_b.volume)      ║
    # ║    finally:                                                      ║
    # ║      teardown(tenant_a); teardown(tenant_b)                      ║
    # ║                                                                  ║
    # ║  Emit one boolean per surface in `tests`. The validation also    ║
    # ║  requires non-empty ``tenant_a_id`` and ``tenant_b_id``.         ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["tenant_a_id"] = "isv-sec11-test-aaaa1111"
        result["tenant_b_id"] = "isv-sec11-test-bbbb2222"
        result["tests"] = {
            "network_isolated": {"passed": True, "message": "demo: tenant A's VPC has no route to tenant B's VPC"},
            "data_isolated": {"passed": True, "message": "demo: kms:Decrypt and s3:GetObject denied across tenants"},
            "compute_isolated": {"passed": True, "message": "demo: ec2:* and ssm:StartSession denied across tenants"},
            "storage_isolated": {"passed": True, "message": "demo: ebs snapshot/attach denied across tenants"},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's tenant isolation test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
