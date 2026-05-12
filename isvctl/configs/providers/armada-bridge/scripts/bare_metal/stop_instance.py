#!/usr/bin/env python3
"""stop_instance — Armada Bridge bare metal suite, test phase.

Stops a bare metal node via:
  POST /tenants/<tenant>/metal/<compute_node_id>/stop

BLOCKED: Bridge does not expose a stop API for bare metal nodes.
This step is best_effort: true in bare_metal.yaml.

Output: {success, platform, instance_id, stop_initiated, state}
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
                "stop_initiated": True,
                "state": "stopped",
            }
        )
    else:
        raise NotImplementedError(
            "stop_instance: Bridge has no stop API for bare metal nodes. "
            "This step is blocked (best_effort: true). "
            "Implement when Bridge exposes POST /tenants/<tenant>/metal/<id>/stop."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
