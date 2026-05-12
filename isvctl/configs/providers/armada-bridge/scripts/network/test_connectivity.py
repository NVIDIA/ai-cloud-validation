#!/usr/bin/env python3
"""test_connectivity — Armada Bridge network suite, test phase.

Validates network connectivity for instances.

NetworkConnectivityCheck requires: instances list (each with private_ip or public_ip).
Optional tests dict — all entries must pass.

Output: {success, platform, instances: [...], tests: {...}}
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
                "instances": [
                    {"instance_id": "demo-vm-0001", "private_ip": "10.100.1.10", "public_ip": "203.0.113.10"},
                ],
                "tests": {
                    "instance_attached_to_subnet": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "test_connectivity: uncomment the Bridge implementation block. "
            "Use BridgeClient.from_env() to validate instance subnet attachment."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
