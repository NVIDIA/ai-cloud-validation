#!/usr/bin/env python3
"""mfa_enforcement_test — Armada Bridge security suite, test phase.

Blocked: Bridge MFA enforcement API not yet available.

Output: {success, platform, interfaces_checked, tests}
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

    result: dict[str, Any] = {"success": False, "platform": "security"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "security",
                "interfaces_checked": 3,
                "tests": {
                    "root_mfa_enabled": {"passed": True},
                    "console_users_mfa": {"passed": True},
                    "api_mfa_policy": {"passed": True},
                    "cli_mfa_policy": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no OS image write API. See bridge-isv-ncp-status.md Image Registry suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
