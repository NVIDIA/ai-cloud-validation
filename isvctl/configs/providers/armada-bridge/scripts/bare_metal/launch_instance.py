#!/usr/bin/env python3
"""launch_instance — Armada Bridge bare metal suite, setup phase.

Allocates a bare metal node via:
  POST /tenants/<tenant>/metal

Output: {success, platform, instance_id, state, public_ip, key_file, instance_type}
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
    parser.add_argument("--flavor", required=True)
    parser.add_argument("--name", required=True)
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
                "instance_type": "demo.bm.gpu.8x",
            }
        )
    else:
        raise NotImplementedError(
            "launch_instance: uncomment the Bridge implementation block. "
            "POST /tenants/<tenant>/metal with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
