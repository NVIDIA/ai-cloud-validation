#!/usr/bin/env python3
"""delete_user — Armada Bridge IAM suite, teardown phase.

Bridge endpoint: DELETE /users/:userId
  Deletes user AND all their Keycloak resources (API keys included).
  Note: spec says call key delete before user delete, but DELETE /users/:userId
  is idempotent for the whole user. Still call DELETE /key-manager/api-key
  first for defense-in-depth.

Output: {success, platform: "iam"}
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
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--credential-id", required=True)
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "iam"}

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
    elif DEMO_MODE:
        result["success"] = True
    else:
        # --- Bridge implementation ---
        # client = BridgeClient.from_env()
        # client.delete("/key-manager/api-key")  # delete the api key first
        # client.delete(f"/users/{args.user_id}")
        # result["success"] = True
        raise NotImplementedError(
            "delete_user: call DELETE /key-manager/api-key then DELETE /users/:userId. "
            "See bridge-isv-ncp-status.md IAM suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
