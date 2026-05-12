#!/usr/bin/env python3
"""bmc_bastion_access_test — Armada Bridge security suite, test phase.

Blocked: Bridge BMC bastion access API not yet available.

Output: {success, platform, management_networks_checked, tests}
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
                "management_networks_checked": 1,
                "tests": {
                    "bastion_identifiable": {"passed": True},
                    "management_ingress_via_bastion_only": {"passed": True},
                    "no_direct_public_route": {"passed": True},
                    "bastion_hardened": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no BMC bastion access endpoint. See bridge-isv-ncp-status.md Security suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
