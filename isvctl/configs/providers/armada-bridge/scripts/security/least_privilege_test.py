#!/usr/bin/env python3
"""least_privilege_test — Armada Bridge security suite, test phase.

Validates least-privilege policy enforcement across user, resource, and network dimensions.

Output: {success, platform, test_identity, allowed_resource, allowed_source_cidr, tests}
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
                "test_identity": "demo-restricted-user@demo.example.com",
                "allowed_resource": "demo-vpc-0001",
                "allowed_source_cidr": "10.0.0.0/16",
                "tests": {
                    "policy_dimensions_user_based": {"passed": True},
                    "policy_dimensions_resource_based": {"passed": True},
                    "policy_dimensions_network_based": {"passed": True},
                    "policy_dimensions_allowed_action_succeeds": {"passed": True},
                    "out_of_scope_compute_denied": {"passed": True},
                    "out_of_scope_storage_denied": {"passed": True},
                    "out_of_scope_network_denied": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "least_privilege_test: implement least-privilege policy checks with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
