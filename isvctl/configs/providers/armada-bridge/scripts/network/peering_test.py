#!/usr/bin/env python3
"""peering_test — Armada Bridge network suite, test phase.

Validates VPC peering creation and cross-VPC routing.

NOTE: Bridge API gap — no VPC peering endpoint available.

VpcPeeringCheck requires tests: {create_vpc_a, create_vpc_b, create_peering,
  accept_peering, add_routes, peering_active}
vpc_a and vpc_b should each have an id field.

Output: {success, platform, tests: {...}, vpc_a: {id}, vpc_b: {id}}
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
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "tests": {
                    "create_vpc_a": {"passed": True},
                    "create_vpc_b": {"passed": True},
                    "create_peering": {"passed": True},
                    "accept_peering": {"passed": True},
                    "add_routes": {"passed": True},
                    "peering_active": {"passed": True},
                },
                "vpc_a": {"id": "demo-vpc-0001"},
                "vpc_b": {"id": "demo-vpc-0002"},
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no VPC peering endpoint. "
            "See bridge-isv-ncp-status.md Network suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
