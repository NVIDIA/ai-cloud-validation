#!/usr/bin/env python3
"""traffic_test — Armada Bridge network suite, test phase.

Validates real network traffic flow.

TrafficFlowCheck requires tests: {traffic_allowed, traffic_blocked,
  internet_icmp, internet_http}

Output: {success, platform, tests: {...}}
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
                    "traffic_allowed": {"passed": True, "latency_ms": 1},
                    "traffic_blocked": {"passed": True},
                    "internet_icmp": {"passed": True},
                    "internet_http": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "traffic_test: uncomment the Bridge implementation block. "
            "Use BridgeClient.from_env() to validate traffic flow rules."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
