#!/usr/bin/env python3
"""security_test — Armada Bridge network suite, test phase.

Validates security group and NACL blocking rules.

SecurityBlockingCheck requires tests: {sg_default_deny_inbound,
  sg_allows_specific_ssh, sg_denies_vpc_icmp, nacl_explicit_deny,
  sg_restricted_egress}

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
    parser.add_argument("--security-group-id", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "tests": {
                    "sg_default_deny_inbound": {"passed": True},
                    "sg_allows_specific_ssh": {"passed": True},
                    "sg_denies_vpc_icmp": {"passed": True},
                    "nacl_explicit_deny": {"passed": True},
                    "sg_restricted_egress": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "security_test: uncomment the Bridge implementation block. "
            "Use BridgeClient.from_env() to validate security group rule creation."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
