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

"""Segment boundary default-deny test (SEC30-01 and SEC30-02) - TEMPLATE.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. For --boundary tenant-provider, place the source in a tenant segment and
     the target in a provider segment. For --boundary intra-tenant, place both
     endpoints in distinct segments belonging to the SAME tenant. Provide a
     network path between them (so a block is policy, not routing)
  2. Allow exactly one flow from the source to the target
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
    """Probe the selected boundary and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Segment boundary default-deny test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", required=True, help="CIDR for the test network")
    parser.add_argument("--boundary", choices=("tenant-provider", "intra-tenant"), default="tenant-provider")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "segment_boundary",
        "region": args.region,
    }

    # TODO: Select the topology using args.boundary and run fresh probes for it.
    # Never reuse tenant/provider or inter-tenant evidence for intra-tenant isolation.

    if DEMO_MODE:
        result["positive_control"] = {"protocol": "tcp", "port": 22, "result": "connected"}
        result["prohibited_flows"] = [
            {"protocol": "icmp", "result": "timeout"},
            {"protocol": "tcp", "port": 443, "result": "timeout"},
            {"protocol": "tcp", "port": 8080, "result": "timeout"},
        ]
        result["success"] = True
    else:
        result["error"] = f"Not implemented - replace with your platform's {args.boundary} segment probes"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
