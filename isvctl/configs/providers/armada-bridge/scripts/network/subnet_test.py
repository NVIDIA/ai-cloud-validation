#!/usr/bin/env python3
"""subnet_test — Armada Bridge network suite, test phase.

Validates subnet creation across availability zones.

SubnetConfigCheck requires:
  tests: {create_subnets, az_distribution, subnets_available}
  subnets: list (min 4 per suite YAML; az_distribution.az_count >= 2)

Output: {success, platform, tests: {...}, subnets: [...]}
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient  # noqa: F401 — used in the live impl block
from common.errors import handle_bridge_errors

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--vpc-id", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "tests": {
                    "create_subnets": {"passed": True},
                    "az_distribution": {"passed": True, "az_count": 2, "azs": ["demo-az-a", "demo-az-b"]},
                    "subnets_available": {"passed": True},
                },
                "subnets": [
                    {"subnet_id": "demo-subnet-0001", "cidr": "10.100.1.0/24", "az": "demo-az-a"},
                    {"subnet_id": "demo-subnet-0002", "cidr": "10.100.2.0/24", "az": "demo-az-b"},
                    {"subnet_id": "demo-subnet-0003", "cidr": "10.100.3.0/24", "az": "demo-az-a"},
                    {"subnet_id": "demo-subnet-0004", "cidr": "10.100.4.0/24", "az": "demo-az-b"},
                ],
            }
        )
    else:
        raise NotImplementedError(
            "subnet_test: uncomment the Bridge implementation block. "
            "Use BridgeClient.from_env() to test subnet operations."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
