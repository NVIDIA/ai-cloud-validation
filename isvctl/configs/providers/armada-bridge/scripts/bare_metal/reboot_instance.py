#!/usr/bin/env python3
"""reboot_instance — Armada Bridge bare metal suite, test phase.

Reboots a bare metal node via:
  POST /tenants/<tenant>/metal/<compute_node_id>/reboot

BLOCKED: Bridge does not expose a reboot API for bare metal nodes.
This step is best_effort: true in bare_metal.yaml.

Output: {success, platform, instance_id, reboot_initiated, state, ssh_ready,
         uptime_seconds, reboot_confirmed}
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
                "reboot_initiated": True,
                "state": "running",
                "ssh_ready": True,
                "uptime_seconds": 30,
                "reboot_confirmed": True,
            }
        )
    else:
        raise NotImplementedError(
            "reboot_instance: Bridge has no reboot API for bare metal nodes. "
            "This step is blocked (best_effort: true). "
            "Implement when Bridge exposes POST /tenants/<tenant>/metal/<id>/reboot."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
