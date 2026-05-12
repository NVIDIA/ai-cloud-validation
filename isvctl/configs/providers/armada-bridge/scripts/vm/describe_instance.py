#!/usr/bin/env python3
"""describe_instance — Armada Bridge VM suite, test phase.

Retrieves detailed information about a VM instance via:
  GET /tenants/<tenant>/vms/<vm_id>

Output: {success, platform, instance_id, state, public_ip, key_file, ssh_user,
         os, gpu_count, driver_version, cpu_count, container_runtime}
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
                "state": "running",
                "public_ip": "203.0.113.10",
                "key_file": "/tmp/demo-key.pem",
                "ssh_user": "ubuntu",
                "os": "ubuntu",
                "gpu_count": 1,
                "driver_version": "535.129.03",
                "cpu_count": 8,
                "container_runtime": "docker",
            }
        )
    else:
        raise NotImplementedError(
            "describe_instance: uncomment the Bridge implementation block. "
            "GET /tenants/<tenant>/vms/<vm_id> with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
