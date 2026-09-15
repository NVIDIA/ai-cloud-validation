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

"""IMEX reboot rejoin test - TEMPLATE.

DESTRUCTIVE AND SLOW. This reboots a node, so point it only at an idle or
drained domain. Reboot ONE member: re-forming the whole domain from cold is
explicitly not a substitute, since it never exercises a single node rejoining
peers that stayed up.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Record the node's boot identity BEFORE the reboot (uptime, and a boot id
     if your platform exposes one)
  2. Reboot it
  3. Wait for it to answer again, then PROVE it actually restarted
  4. Wait for IMEX to come back and rejoin its domain on its own, timing it
  5. Print a JSON object to stdout

The rule that matters most:

  * CONFIRM the reboot affirmatively. Reachability proves nothing - a node that
    never went down answers just as happily as one that came back. Compare
    uptime across the reboot and require it to have gone BACKWARDS (or the boot
    id to have changed). Never infer a reboot from a successful connection.

Two more worth getting right:

  * Do NOT start the service yourself after boot. The property under test is
    that it returns unassisted; starting it destroys the observation.
  * Report `elapsed_seconds` on failure as well as success, so a platform that
    technically recovers but takes far too long stays visible.

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "imex_reboot",
    "node_id": "compute-node-1",
    "persistence_configured": true,
    "reboot_confirmed": true,
    "uptime_seconds": 94,
    "post_reboot": {
      "service_ready": true,
      "domain_member": true,
      "elapsed_seconds": 61,
      "intervention_required": false
    }
  }

Note the strict pass condition: a node shipped with IMEX not configured to start
at boot fails here. That is deliberate - shipping it enabled is the provider
obligation being checked.

Usage:
    python imex_reboot_test.py --region <region> --node-ids <id>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Reboot one node and confirm IMEX returns and rejoins; emit structured JSON."""
    parser = argparse.ArgumentParser(description="IMEX reboot rejoin test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--node-ids", required=True, help="The single node to reboot")
    args = parser.parse_args()
    node_ids = [node_id.strip() for node_id in args.node_ids.split(",") if node_id.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_reboot",
        "region": args.region,
        "node_id": node_ids[0] if node_ids else "",
        "persistence_configured": False,
        "reboot_confirmed": False,
        "uptime_seconds": 0,
        "post_reboot": {},
    }

    # This reboots a node, so exactly one must be named - never silently the
    # first of several, and never an empty selection that reports success.
    if len(node_ids) != 1:
        result["error"] = f"--node-ids must name exactly one node to reboot, got {len(node_ids)}"
        print(json.dumps(result, indent=2))
        return 1

    # TODO: Replace with your platform's implementation. Record boot identity,
    # reboot, prove it restarted, then wait for IMEX to return on its own.

    if DEMO_MODE:
        result["persistence_configured"] = True
        result["reboot_confirmed"] = True
        result["uptime_seconds"] = 94
        result["post_reboot"] = {
            "service_ready": True,
            "domain_member": True,
            "elapsed_seconds": 61,
            "intervention_required": False,
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's IMEX reboot test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
