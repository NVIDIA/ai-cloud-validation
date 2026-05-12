#!/usr/bin/env python3
"""create_access_key — Armada Bridge control-plane suite, setup phase.

Creates an API access key for the given tenant via:
  POST /key-manager/api-key
  Authorization: Bearer <admin_token>

Output: {success, platform, access_key_id, secret_access_key, username}
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
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "access_key_id": "demo-user-uuid-ctrl-0001",
                "secret_access_key": "demo-key-ctrl-0001",
                "username": "operator@demo.example.com",
            }
        )
    else:
        raise NotImplementedError(
            "create_access_key: uncomment the Bridge implementation block. "
            "POST /key-manager/api-key with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
