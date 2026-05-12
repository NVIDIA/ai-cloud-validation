#!/usr/bin/env python3
"""stable_ip_test — Armada Bridge network suite, test phase.

Validates that an instance retains its private IP across stop/start cycles.

Output: {success, platform, tests: {create_instance, record_ip, stop_instance,
         start_instance, ip_unchanged}}
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
                    "create_instance": {"passed": True},
                    "record_ip": {"passed": True},
                    "stop_instance": {"passed": True},
                    "start_instance": {"passed": True},
                    "ip_unchanged": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "stable_ip_test: uncomment the Bridge implementation block. "
            "Use BridgeClient.from_env() to validate stable IP across stop/start."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
