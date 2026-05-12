#!/usr/bin/env python3
"""test_access_key — Armada Bridge control-plane suite, test phase.

Authenticates using the provided access key credentials via:
  POST <KC_TOKEN_URL>  (grant_type=client_credentials or password)
  Validates that a token is returned.

Output: {success, platform, authenticated, identity_id, caller_arn}
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
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--secret-access-key", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "authenticated": True,
                "identity_id": "demo-user-uuid-ctrl-0001",
                "caller_arn": "demo-user-uuid-ctrl-0001",
            }
        )
    else:
        raise NotImplementedError(
            "test_access_key: uncomment the Bridge implementation block. "
            "Authenticate with the provided credentials via BridgeClient."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
