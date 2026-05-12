#!/usr/bin/env python3
"""floating_ip_test — Armada Bridge network suite, test phase.

Validates floating IP allocation and assignment.

NOTE: Bridge API gap — no floating IP endpoint available.

FloatingIpCheck requires tests: {allocate_eip, associate_to_a, verify_on_a,
  reassociate_to_b, verify_on_b, verify_not_on_a}
reassociate_to_b must have switch_seconds <= max_switch_seconds (default 10).

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
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "tests": {
                    "allocate_eip": {"passed": True, "public_ip": "203.0.113.1"},
                    "associate_to_a": {"passed": True},
                    "verify_on_a": {"passed": True},
                    "reassociate_to_b": {"passed": True, "switch_seconds": 1},
                    "verify_on_b": {"passed": True},
                    "verify_not_on_a": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no floating IP endpoint. "
            "See bridge-isv-ncp-status.md Network suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
