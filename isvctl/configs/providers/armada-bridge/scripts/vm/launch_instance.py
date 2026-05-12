#!/usr/bin/env python3
"""launch_instance — Armada Bridge VM suite, setup phase.

Launches a VM instance via:
  POST /tenants/<tenant>/vms

Output: {success, platform, instance_id, state, public_ip, private_ip,
         key_file, vpc_id, security_group_id, instance_type}

Note: vpc_id, security_group_id, and instance_type are echoed from the
request args because the Bridge response does not include them.
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
    parser.add_argument("--name", required=True)
    parser.add_argument("--flavor", required=True)
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--security-group-id", required=True)
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
                "private_ip": "10.0.0.10",
                "key_file": "/tmp/demo-key.pem",
                "vpc_id": args.vpc_id,
                "security_group_id": args.security_group_id,
                "instance_type": args.flavor,
            }
        )
    else:
        raise NotImplementedError(
            "launch_instance: uncomment the Bridge implementation block. "
            "POST /tenants/<tenant>/vms with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
