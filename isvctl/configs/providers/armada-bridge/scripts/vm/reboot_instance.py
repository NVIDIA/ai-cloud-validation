#!/usr/bin/env python3
"""reboot_instance — Armada Bridge VM suite, test phase.

Reboots a running VM instance via:
  POST /tenants/<tenant>/vms/<vm_id>/power/reset

Note: The Bridge endpoint is /power/reset (not /reboot). This is the correct
endpoint for triggering a VM reset/reboot on the Armada Bridge platform.

Output: {success, platform, instance_id, reboot_initiated, state, ssh_ready,
         uptime_seconds, reboot_confirmed, public_ip}
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
                "reboot_initiated": True,
                "state": "running",
                "ssh_ready": True,
                "uptime_seconds": 30,
                "reboot_confirmed": True,
                "public_ip": "203.0.113.10",
            }
        )
    else:
        raise NotImplementedError(
            "reboot_instance: uncomment the Bridge implementation block. "
            "POST /tenants/<tenant>/vms/<vm_id>/power/reset with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
