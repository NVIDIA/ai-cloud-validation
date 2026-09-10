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

"""IMEX service/tooling presence test (host model) - TEMPLATE.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Resolve which nodes the allocation covers
  2. On each node, probe for the IMEX daemon and its control tooling
  3. Ask the node's service manager whether the service is registered
  4. Print a JSON object to stdout

All parsing of vendor output belongs here in the provider script; the
validation sees only the normalized shape below.

Two things worth getting right, both of which the validation cannot fix for you:

  * `in_nvlink_allocation` must come from the ALLOCATION, not from the node's
    own view of its NVLink support. A node must not be able to self-report its
    way out of scope - inside a multi-node NVLink allocation that is a FAIL,
    not a skip.
  * Probe for the artifacts by ROLE, not by a fixed package name. The daemon
    ships branch-versioned and the control tool is a binary inside that
    package, so a hardcoded package name goes stale every driver branch.
    Query the service manager rather than looking for a unit file on disk,
    so an absent service can be told apart from a masked one.

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "imex_service",
    "nodes_checked": 2,
    "nodes_validated": 2,
    "nodes": [
      {
        "node_id": "compute-node-1",
        "in_nvlink_allocation": true,
        "service_present": true,
        "control_tooling_present": true,
        "service_registration": "loaded",
        "boot_disposition": "disabled"
      }
    ]
  }

`service_registration` is a normalized enum: loaded, masked, not_found, error.
Only "loaded" passes; "masked" is a deployment-model mismatch, not a missing
package.

`boot_disposition` is a normalized enum: enabled, disabled, static, none,
unknown. It is reported as evidence and never asserted on - whether IMEX starts
at boot, and whether it is currently running, are both out of scope.

Usage:
    python imex_service_test.py --region <region> --node-ids <id1>,<id2>[,...]
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Probe IMEX service/tooling presence and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="IMEX service presence test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument(
        "--node-ids",
        required=True,
        help="Comma-separated node IDs covered by the allocation",
    )
    args = parser.parse_args()
    node_ids = [node_id.strip() for node_id in args.node_ids.split(",") if node_id.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_service",
        "region": args.region,
        "nodes_checked": 0,
        "nodes_validated": 0,
        "nodes": [],
    }

    # TODO: Replace with your platform's probe. Typically this means, per node,
    # checking that the IMEX daemon and control-tool binaries exist and that the
    # service manager reports a loaded definition for the service.

    if DEMO_MODE:
        result["nodes"] = [
            {
                "node_id": node_id,
                "in_nvlink_allocation": True,
                "service_present": True,
                "control_tooling_present": True,
                "service_registration": "loaded",
                "boot_disposition": "disabled",
            }
            for node_id in node_ids
        ]
        result["nodes_checked"] = len(node_ids)
        result["nodes_validated"] = len(node_ids)
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's IMEX service probe"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
