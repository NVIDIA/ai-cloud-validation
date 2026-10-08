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

"""Default-deny across a tenant-to-provider boundary test (SEC30-01) - TEMPLATE.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Place a source in a tenant segment and a target in a provider segment,
     with a network path between them (so a block is policy, not routing)
  2. Allow exactly one flow from the tenant source to the provider target
     (the positive control) and leave everything else at the default policy
  3. Probe the positive control first, then each prohibited flow
  4. Tear down what you created and print a JSON object to stdout

Report each probe as observed: "connected" (handshake or reply), "refused"
(the target host rejected it - the packet got through), "timeout" (dropped),
or "error" (the probe could not run). Only "timeout" counts as denied.

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "segment_boundary",
    "positive_control": {"protocol": "tcp", "port": 22, "result": "connected"},
    "prohibited_flows": [
      {"protocol": "icmp", "result": "timeout"},
      {"protocol": "tcp", "port": 443, "result": "timeout"}
    ]
  }

Usage:
    python segment_boundary_test.py --region <region> --cidr <cidr>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Probe the tenant-to-provider boundary and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Segment boundary default-deny test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", required=True, help="CIDR for the test network")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "segment_boundary",
        "region": args.region,
    }

    # TODO: Replace with your platform's tenant/provider segment setup and probes.

    if DEMO_MODE:
        result["positive_control"] = {"protocol": "tcp", "port": 22, "result": "connected"}
        result["prohibited_flows"] = [
            {"protocol": "icmp", "result": "timeout"},
            {"protocol": "tcp", "port": 443, "result": "timeout"},
            {"protocol": "tcp", "port": 8080, "result": "timeout"},
        ]
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's segment boundary probes"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
