#!/usr/bin/env python3
"""isolation_test — Armada Bridge network suite, test phase.

Validates VPC tenant isolation.

VpcIsolationCheck requires:
  tests: {no_peering, no_cross_routes_a, no_cross_routes_b}

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
                    "no_peering": {"passed": True},
                    "no_cross_routes_a": {"passed": True},
                    "no_cross_routes_b": {"passed": True},
                },
                "vpc_a": {"id": "demo-vpc-0001"},
                "vpc_b": {"id": "demo-vpc-0002"},
            }
        )
    else:
        raise NotImplementedError(
            "isolation_test: uncomment the Bridge implementation block. "
            "Use BridgeClient.from_env() to validate VPC tenant isolation."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
