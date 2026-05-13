#!/usr/bin/env python3
"""delete_user — Armada Bridge IAM suite, teardown phase.

Bridge 2-step flow:
  Step 1: DELETE /key-manager/api-key
          Authorization: Bearer <user_token>  ← MUST be the user, not admin
          Idempotent server-side (no-op if key absent).
  Step 2: DELETE /users/:userId
          Authorization: Bearer <admin_token>
          Idempotent — 404 is swallowed by BridgeClient.delete().

Idempotency: if the user is not found in GET /users (already deleted),
  the script returns success immediately without hitting either DELETE endpoint.

Note: DELETE /users/:userId cascade-removes all Keycloak attrs including the
  API key, so step 1 is defense-in-depth only. Admin OPA scope must cover the
  target user's tenant for step 2 to succeed in a live environment.

Output: {success, platform: "iam"}
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient
from common.errors import handle_bridge_errors
from common.constants import TEST_PASSWORD
from iam_common import extract_user_from_users

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
        admin_client = BridgeClient.from_env()
        user_email = args.credential_id  # create_user stores email as access_key_id / credential_id

        # Step 1: Verify user still exists (idempotency guard)
        try:
            users = admin_client.get("/users")
            user_info = extract_user_from_users(users, user_email)
        except Exception as e:
            result.update({"error": f"Failed to list users: {e}"})
            print(json.dumps(result, indent=2))
            return 1

        if user_info is None:
            result["success"] = True
            result["already_deleted"] = True
            print(json.dumps(result, indent=2))
            return 0

        # Step 2: Delete the user's API key (requires user's own session)
        try:
            user_client = admin_client.login_as(user_email, TEST_PASSWORD)
            user_client.delete("/key-manager/api-key")
        except Exception:
            # Non-fatal: user DELETE below cascades-removes all Keycloak attrs
            pass

        # Step 3: Delete the user
        try:
            admin_client.delete(f"/users/{user_info['id']}")
        except Exception as e:
            result.update({"error": f"Failed to delete user: {e}"})
            print(json.dumps(result, indent=2))
            return 1

        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
