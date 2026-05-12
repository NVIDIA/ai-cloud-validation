#!/usr/bin/env python3
"""console_rbac — Armada Bridge VM suite, test phase.

Validates RBAC enforcement for VM console access via OPA policy checks.

BLOCKED: Bridge does not expose a console RBAC validation API.
This step is best_effort: true in vm.yaml.

Output: {success, platform, instance_id, access_restricted, restricted_actions,
         rbac_model, tests: {denied_principal_cannot_access_console,
         allowed_principal_can_access_console, allowed_principal_is_resource_scoped}}
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
    parser.add_argument("--vm-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "vm"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instance_id": "demo-vm-abc123",
                "access_restricted": True,
                "restricted_actions": ["vm:connect-console"],
                "rbac_model": "OPA",
                "tests": {
                    "denied_principal_cannot_access_console": {
                        "passed": True,
                        "message": "Unauthorized user denied at OPA policy check",
                    },
                    "allowed_principal_can_access_console": {
                        "passed": True,
                        "message": "Authorized user with vm:connect-console role allowed",
                    },
                    "allowed_principal_is_resource_scoped": {
                        "passed": True,
                        "message": "Policy scoped to specific VM resource ID",
                    },
                },
            }
        )
    else:
        raise NotImplementedError(
            "console_rbac: Bridge has no console RBAC validation API. "
            "This step is blocked (best_effort: true). "
            "Implement when Bridge exposes OPA policy check endpoints for VM console access."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
