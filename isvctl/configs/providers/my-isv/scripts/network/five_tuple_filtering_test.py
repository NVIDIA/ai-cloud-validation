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

"""Five-tuple traffic filtering test (SEC15-01) - TEMPLATE.

This script is called during the "test" phase. It is SELF-CONTAINED:
  1. Place a source and a target on one network path (so a block is policy,
     not routing), plus a second source and a second target address
  2. With your security groups and/or ACLs, allow exactly one flow - the
     baseline - matching protocol, source IP, destination IP, and
     destination port (and source port, if your platform can match it)
  3. Probe the baseline first, then one variant per dimension that differs
     from the baseline in that dimension only
  4. Tear down what you created and print a JSON object to stdout

Report each probe as observed: "connected" (handshake or reply), "refused"
(the target host rejected it - the packet got through), "timeout" (dropped),
or "error" (the probe could not run). Only "timeout" counts as filtered.

Required JSON output fields:
  {
    "success": true,
    "platform": "network",
    "test_name": "five_tuple_filtering",
    "baseline": {"protocol": "tcp", "source_ip": "10.85.1.10",
                 "destination_ip": "10.85.2.10", "source_port": 40000,
                 "destination_port": 8443, "result": "connected"},
    "variants": [
      {"dimension": "protocol", "value": "udp", "result": "timeout"},
      {"dimension": "source_ip", "value": "10.85.1.11", "result": "timeout"},
      {"dimension": "destination_ip", "value": "10.85.2.11", "result": "timeout"},
      {"dimension": "destination_port", "value": 8444, "result": "timeout"}
    ]
  }

Usage:
    python five_tuple_filtering_test.py --region <region> --cidr <cidr>
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Probe the baseline and per-dimension variant flows and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Five-tuple traffic filtering test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--cidr", required=True, help="CIDR for the test network")
    parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "five_tuple_filtering",
    }

    # TODO: Replace with your platform's filtering rules and flow probes.

    if DEMO_MODE:
        result["baseline"] = {
            "protocol": "tcp",
            "source_ip": "10.85.1.10",
            "destination_ip": "10.85.2.10",
            "source_port": 40000,
            "destination_port": 8443,
            "result": "connected",
        }
        result["variants"] = [
            {"dimension": "protocol", "value": "udp", "result": "timeout"},
            {"dimension": "source_ip", "value": "10.85.1.11", "result": "timeout"},
            {"dimension": "destination_ip", "value": "10.85.2.11", "result": "timeout"},
            {"dimension": "destination_port", "value": 8444, "result": "timeout"},
        ]
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's five-tuple filtering probes"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
