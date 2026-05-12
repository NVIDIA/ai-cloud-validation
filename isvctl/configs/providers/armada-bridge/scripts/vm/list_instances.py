#!/usr/bin/env python3
"""list_instances — Armada Bridge VM suite, test phase.

Lists VM instances for a tenant/VPC via:
  GET /tenants/<tenant>/vms?vpcId=<vpc_id>

Output: {success, platform, instances, count, found_target, target_instance}
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
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "vm"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instances": [
                    {
                        "instance_id": "demo-vm-abc123",
                        "state": "running",
                        "vpc_id": args.vpc_id,
                    }
                ],
                "count": 1,
                "found_target": True,
                "target_instance": "demo-vm-abc123",
            }
        )
    else:
        raise NotImplementedError(
            "list_instances: uncomment the Bridge implementation block. "
            "GET /tenants/<tenant>/vms with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
