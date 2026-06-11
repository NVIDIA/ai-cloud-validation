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

"""Topology block atomic allocation test for capacity reservations - template."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _non_negative_int(value: str) -> int:
    """Parse a non-negative integer argparse value."""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return parsed


def _resources(tenant_id: str, block_id: str, compute: int, network: int, storage: int) -> list[dict[str, str]]:
    """Build deterministic demo resource records for the neutral contract."""
    resources: list[dict[str, str]] = []
    for index in range(1, compute + 1):
        resources.append(
            {
                "resource_id": f"demo-node-{index}",
                "resource_type": "compute",
                "tenant_id": tenant_id,
                "topology_block_id": block_id,
                "performance_domain": "demo-performance-domain",
                "isolation_boundary": tenant_id,
            }
        )
    for index in range(1, network + 1):
        resources.append(
            {
                "resource_id": f"demo-fabric-{index}",
                "resource_type": "network",
                "tenant_id": tenant_id,
                "topology_block_id": block_id,
                "performance_domain": "demo-performance-domain",
                "isolation_boundary": tenant_id,
            }
        )
    for index in range(1, storage + 1):
        resources.append(
            {
                "resource_id": f"demo-storage-{index}",
                "resource_type": "storage",
                "tenant_id": tenant_id,
                "topology_block_id": block_id,
                "performance_domain": "demo-performance-domain",
                "isolation_boundary": tenant_id,
            }
        )
    return resources


def _payload(args: argparse.Namespace, *, success: bool) -> dict[str, Any]:
    """Build the CAP04-02 JSON contract."""
    requested = {
        "compute": args.requested_compute,
        "network": args.requested_network,
        "storage": args.requested_storage,
    }
    result: dict[str, Any] = {
        "success": success,
        "platform": "my-isv",
        "test_name": "topology_block_atomic_allocation",
    }
    if success:
        result["topology_block"] = {
            "block_id": args.topology_block_id,
            "reservation_id": "demo-reservation",
            "tenant_id": args.tenant_id,
            "allocated_as_unit": True,
            "partial_allocation": False,
            "homogeneous": True,
            "isolation_enforced": True,
            "requested": requested,
            "allocated": requested,
            "resources": _resources(
                args.tenant_id,
                args.topology_block_id,
                args.requested_compute,
                args.requested_network,
                args.requested_storage,
            ),
        }
    else:
        # TODO: Replace this block with your platform's implementation.
        # Query the topology block allocation, verify it was allocated as one
        # homogeneous unit, then populate the JSON contract above.
        result["error"] = "Not implemented - replace with your platform's capacity reservation API calls"
    return result


def main() -> int:
    """Run the template or demo CAP04-02 check."""
    parser = argparse.ArgumentParser(description="Validate atomic topology block allocation")
    parser.add_argument("--tenant-id", required=True, help="Tenant/account that owns the reserved block")
    parser.add_argument("--topology-block-id", required=True, help="Topology block identifier")
    parser.add_argument(
        "--requested-compute", type=_non_negative_int, required=True, help="Requested compute resources"
    )
    parser.add_argument(
        "--requested-network", type=_non_negative_int, required=True, help="Requested network resources"
    )
    parser.add_argument(
        "--requested-storage", type=_non_negative_int, required=True, help="Requested storage resources"
    )
    args = parser.parse_args()

    result = _payload(args, success=DEMO_MODE)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
