#!/usr/bin/env python3
"""sg_scoping_test — Armada Bridge network suite, test phase.

Validates security group scoping for a given scope type
(workload / node / subnet / service).

NOTE: Bridge API gap — security group scoping not yet implemented.
This script is used for 4 YAML steps: sg_workload_scoping, sg_node_scoping,
sg_subnet_scoping, sg_service_scoping.

SgWorkloadScopingCheck requires tests: {create_sg, apply_workload_rule,
  workload_allowed, other_workload_blocked, cleanup}
SgNodeScopingCheck requires tests: {create_sg, apply_node_rule,
  target_node_allowed, other_node_blocked, cleanup}
SgSubnetScopingCheck requires tests: {create_sg, apply_subnet_rule,
  subnet_allowed, other_subnet_blocked, cleanup}
SgServiceScopingCheck requires tests: {create_sg, apply_service_rule,
  service_endpoint_allowed, other_endpoint_blocked, cleanup}

Output: {success, platform, tests: {...}, scope}
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

_SCOPE_TESTS: dict[str, dict[str, Any]] = {
    "workload": {
        "create_sg": {"passed": True},
        "apply_workload_rule": {"passed": True},
        "workload_allowed": {"passed": True},
        "other_workload_blocked": {"passed": True},
        "cleanup": {"passed": True},
    },
    "node": {
        "create_sg": {"passed": True},
        "apply_node_rule": {"passed": True},
        "target_node_allowed": {"passed": True},
        "other_node_blocked": {"passed": True},
        "cleanup": {"passed": True},
    },
    "subnet": {
        "create_sg": {"passed": True},
        "apply_subnet_rule": {"passed": True},
        "subnet_allowed": {"passed": True},
        "other_subnet_blocked": {"passed": True},
        "cleanup": {"passed": True},
    },
    "service": {
        "create_sg": {"passed": True},
        "apply_service_rule": {"passed": True},
        "service_endpoint_allowed": {"passed": True},
        "other_endpoint_blocked": {"passed": True},
        "cleanup": {"passed": True},
    },
}


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "--scope-type",
        required=True,
        choices=["workload", "node", "subnet", "service"],
    )
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "scope": args.scope_type,
                "tests": _SCOPE_TESTS[args.scope_type],
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: security group scoping not yet implemented. "
            "See bridge-isv-ncp-status.md Network suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
