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

"""IMEX deliberate node departure test - TEMPLATE.

DESTRUCTIVE. This removes a node from a live domain, so point it only at an
idle or drained domain, and restore prior state in the teardown.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Stop the IMEX service on the target node GRACEFULLY, via the service
     manager, and confirm it shut down cleanly
  2. Poll a surviving member until it reports the target gone, timing it
  3. Check the domain is still operational among the survivors
  4. Restore the target, then print a JSON object to stdout

Three rules that are easy to get wrong:

  * STOP the service; do not kill it. This is the opposite stimulus to
    SDN18-01, where a kill must be recovered from. A supervisor restarting
    this deliberate stop would be wrong, so do not treat a restart as success.
  * NORMALIZE the peer view yourself. Map whatever your platform reports onto
    `available` / `unavailable` / `unknown`. The validation never
    substring-matches vendor output. Confirm the real state values your
    platform emits before writing the matcher - pick a signal that actually
    changes when a node leaves, not one that merely looks like it should.
  * RESTORE is mandatory, not best-effort, and must run on every path that may
    have stopped the service. This test leaves the domain a member short by
    design; abandoning it there hands the next test a degraded domain.

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "imex_departure",
    "target_node": "compute-node-1",
    "operations": {
      "stop": {"requested": true, "clean_exit": true},
      "peer_convergence": {
        "observed_from": "compute-node-2",
        "target_reported": "unavailable",
        "elapsed_seconds": 8,
        "surviving_members_operational": true
      },
      "restore": {"restored_to": "active", "domain_member": true}
    }
  }

Usage:
    python imex_departure_test.py --region <region> \
        --target-node <id> --observer-node <id>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Stop one node deliberately, observe peer convergence, restore; emit JSON."""
    parser = argparse.ArgumentParser(description="IMEX node departure test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--target-node", required=True, help="Node whose IMEX service is stopped")
    parser.add_argument("--observer-node", required=True, help="Surviving member the departure is observed from")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "imex_departure",
        "region": args.region,
        "target_node": args.target_node,
        "operations": {},
    }

    # TODO: Replace with your platform's implementation. Stop gracefully,
    # observe from a survivor, normalize the peer view, then always restore.

    if DEMO_MODE:
        result["operations"] = {
            "stop": {"requested": True, "clean_exit": True},
            "peer_convergence": {
                "observed_from": args.observer_node,
                "target_reported": "unavailable",
                "elapsed_seconds": 8,
                "surviving_members_operational": True,
            },
            "restore": {"restored_to": "active", "domain_member": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's IMEX departure test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
