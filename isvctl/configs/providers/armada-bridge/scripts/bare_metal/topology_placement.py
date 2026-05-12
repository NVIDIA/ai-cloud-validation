#!/usr/bin/env python3
"""topology_placement — Armada Bridge bare metal suite, test phase.

Checks placement group / topology support for a bare metal node via:
  GET /tenants/<tenant>/metal/<compute_node_id>/placement

BLOCKED: Bridge does not expose a placement group API for bare metal nodes.
This step is best_effort: true in bare_metal.yaml.

Output: {success, platform, instance_id, placement_supported, availability_zone,
         placement_strategy, operations}
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
    parser.add_argument("--compute-node-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "bare_metal",
                "instance_id": "demo-bm-node01",
                "placement_supported": True,
                "availability_zone": "bridge-zone-1a",
                "placement_strategy": "cluster",
                "operations": {
                    "create_placement_group": {"passed": True},
                    "place_instance": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "topology_placement: Bridge has no placement group API for bare metal. "
            "This step is blocked (best_effort: true). "
            "Implement when Bridge exposes GET /tenants/<tenant>/metal/<id>/placement."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
