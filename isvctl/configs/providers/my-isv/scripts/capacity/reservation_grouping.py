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

"""Capacity reservation grouping test - template."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _positive_int(value: str) -> int:
    """Parse a positive integer argparse value."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return parsed


def _payload(args: argparse.Namespace, *, success: bool) -> dict[str, Any]:
    """Build the capacity reservation JSON contract."""
    result: dict[str, Any] = {
        "success": success,
        "platform": "my-isv",
        "test_name": "capacity_reservation_grouping",
    }
    if success:
        result.update(
            {
                "reservation_id": args.reservation_id,
                "account_id": args.account_id,
                "resources": [
                    {
                        "resource_id": f"demo-capacity-{index}",
                        "resource_type": "compute",
                        "account_id": args.account_id,
                        "pinned": True,
                    }
                    for index in range(1, args.resource_count + 1)
                ],
                "pinned": True,
                "isolation_enforced": True,
            }
        )
    else:
        # TODO: Replace this block with your platform's implementation.
        # Query the reservation, verify all grouped resources are pinned to
        # the requested account, then populate the JSON contract above.
        result["error"] = "Not implemented - replace with your platform's capacity reservation grouping API calls"
    return result


def main() -> int:
    """Run the template or demo capacity reservation check."""
    parser = argparse.ArgumentParser(description="Validate capacity reservation grouping")
    parser.add_argument("--account-id", required=True, help="Tenant/account that owns the reserved resources")
    parser.add_argument("--reservation-id", required=True, help="Reservation or allocation identifier")
    parser.add_argument("--resource-count", type=_positive_int, required=True, help="Grouped resource count")
    args = parser.parse_args()

    result = _payload(args, success=DEMO_MODE)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
