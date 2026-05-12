#!/usr/bin/env python3
"""create_vpc — Armada Bridge network suite, setup phase.

Creates a VPC with subnet and security group via the Bridge network API.

Output: {success, platform, vpc_id, subnet_id, security_group_id, cidr}
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
                "network_id": "demo-vpc-0001",
                "vpc_id": "demo-vpc-0001",
                "cidr": "10.100.0.0/16",
                "subnets": [
                    {
                        "subnet_id": "demo-subnet-0001",
                        "cidr": "10.100.1.0/24",
                        "az": "demo-az-a",
                        "auto_assign_public_ip": True,
                        "available_ips": 251,
                    },
                    {
                        "subnet_id": "demo-subnet-0002",
                        "cidr": "10.100.2.0/24",
                        "az": "demo-az-b",
                        "auto_assign_public_ip": False,
                        "available_ips": 251,
                    },
                ],
                "subnet_id": "demo-subnet-0001",
                "security_group_id": "demo-sg-0001",
                "dhcp_options": {
                    "dhcp_options_id": "demo-dopt-0001",
                    "domain_name": "internal.armada.demo",
                    "domain_name_servers": ["10.100.0.2"],
                    "ntp_servers": [],
                },
            }
        )
    else:
        raise NotImplementedError(
            "create_vpc: uncomment the Bridge implementation block. "
            "POST /tenants/<tenant>/vpcs with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
