#!/usr/bin/env python3
"""list_tenants — Armada Bridge control-plane suite, test phase.

Lists tenants and verifies the target tenant is present via:
  GET /tenants

Output: {success, platform, found_target, target_tenant, count}
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
    parser.add_argument("--tenant-name", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "found_target": True,
                "target_tenant": "isv-test-tenant",
                "count": 1,
            }
        )
    else:
        raise NotImplementedError(
            "list_tenants: uncomment the Bridge implementation block. "
            "GET /tenants with BridgeClient.from_env() and search for args.tenant_name."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
