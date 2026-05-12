#!/usr/bin/env python3
"""describe_instance — Armada Bridge bare metal suite, test phase.

Describes a bare metal node via:
  GET /tenants/<tenant>/metal/<compute_node_id>

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
    parser.add_argument("--compute-node-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "bare_metal",
                "instance_id": "demo-bm-node01",
                "state": "running",
                "public_ip": "203.0.114.20",
                "key_file": "/tmp/demo-bm-key.pem",
                "ssh_user": "ubuntu",
                "os": "ubuntu",
                "gpu_count": 8,
                "driver_version": "535.129.03",
                "cpu_count": 128,
                "container_runtime": "docker",
            }
        )
    else:
        raise NotImplementedError(
            "describe_instance: uncomment the Bridge implementation block. "
            "GET /tenants/<tenant>/metal/<id> with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
