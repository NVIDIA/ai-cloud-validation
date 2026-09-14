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

"""IMEX unaided-run / persistence / self-heal test - TEMPLATE.

DESTRUCTIVE. This kills a running IMEX daemon, so point it only at an idle or
drained domain, and restore prior state in the teardown.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Observe the node WITHOUT touching it: is IMEX already running, and is the
     node already an operational domain member?
  2. Read back whether the service is configured to return after a restart
  3. Kill the daemon and time how long the node takes to rejoin the domain
  4. Restore prior state, then print a JSON object to stdout

Three rules that are easy to get wrong:

  * NEVER start the service to make step 1 true. The property under test is
    that it was already running unaided; starting it destroys the observation.
    Report `started_by_test: false` so that is auditable, and let a
    not-running node fail - that indicts provisioning, and is not yours to
    repair here.
  * KILL the daemon; do not `systemctl stop` it. A correct supervisor
    deliberately will not restart a deliberate stop, so a stop-based version of
    this test fails on exactly the nodes that are behaving properly.
  * Report `elapsed_seconds` on failure as well as success, so "never came
    back" is distinguishable from "came back too slowly".

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "imex_resilience",
    "node_id": "compute-node-1",
    "operations": {
      "unaided_presence": {
        "running_on_arrival": true,
        "domain_member": true,
        "started_by_test": false,
        "boot_persistence_configured": true
      },
      "terminate": {"method": "kill", "confirmed": true},
      "recovery": {
        "domain_member": true,
        "elapsed_seconds": 9,
        "operator_intervention": false
      },
      "restore": {"restored_to": "active", "domain_member": true}
    }
  }

Derive the recovery bound from an observed restart baseline on your platform
rather than picking a round number, and confirm how your service manager
reports its restart policy before implementing the persistence read-back.

Usage:
    python imex_resilience_test.py --region <region> --node-ids <id>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Observe unaided running, persistence and self-heal; emit structured JSON."""
    parser = argparse.ArgumentParser(description="IMEX resilience test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--node-ids", required=True, help="Node to test (this check is per-node)")
    args = parser.parse_args()
    node_ids = [node_id.strip() for node_id in args.node_ids.split(",") if node_id.strip()]
    node_id = node_ids[0] if node_ids else ""

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_resilience",
        "region": args.region,
        "node_id": node_id,
        "operations": {},
    }

    # TODO: Replace with your platform's implementation. Observe first, never
    # start the service; then kill the daemon and time the rejoin.

    if DEMO_MODE:
        result["operations"] = {
            "unaided_presence": {
                "running_on_arrival": True,
                "domain_member": True,
                "started_by_test": False,
                "boot_persistence_configured": True,
            },
            "terminate": {"method": "kill", "confirmed": True},
            "recovery": {"domain_member": True, "elapsed_seconds": 9, "operator_intervention": False},
            "restore": {"restored_to": "active", "domain_member": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's IMEX resilience test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
