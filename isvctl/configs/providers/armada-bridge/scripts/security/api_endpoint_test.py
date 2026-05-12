#!/usr/bin/env python3
"""api_endpoint_test — Armada Bridge security suite, test phase.

Verifies API endpoint isolation (best_effort).

Output: {success, platform, endpoints_tested, tests}
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
    parser.add_argument("--bridge-url", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "security"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "security",
                "endpoints_tested": 2,
                "tests": {
                    "probe_api_from_public": {"passed": True},
                    "probe_mgmt_from_public": {"passed": True},
                    "verify_private_only": {"passed": True},
                    "dns_not_public": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "api_endpoint_test: implement Bridge API endpoint isolation checks with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
