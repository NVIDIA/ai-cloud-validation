#!/usr/bin/env python3
"""backend_switch_fabric_test — Armada Bridge network suite, test phase.

Validates backend switch fabric topology accessibility.

NOTE: Bridge API gap — no backend switch fabric topology endpoint available.

BackendSwitchFabricCheck requires:
  tests: {node_resolved, leaf_switch_ids_present, spine_switch_ids_present,
          core_switch_ids_present}
  node_id: non-empty string
  fabric: {leaf_switch_ids: [...], spine_switch_ids: [...], core_switch_ids: [...]}
  (each list must be non-empty list of non-empty strings)

Output: {success, platform, node_id, fabric, tests}
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
                "node_id": "demo-node-0001",
                "fabric": {
                    "leaf_switch_ids": ["demo-leaf-sw-0001"],
                    "spine_switch_ids": ["demo-spine-sw-0001"],
                    "core_switch_ids": ["demo-core-sw-0001"],
                },
                "tests": {
                    "node_resolved": {"passed": True},
                    "leaf_switch_ids_present": {"passed": True},
                    "spine_switch_ids_present": {"passed": True},
                    "core_switch_ids_present": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no backend switch fabric topology endpoint. "
            "See bridge-isv-ncp-status.md Network suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
