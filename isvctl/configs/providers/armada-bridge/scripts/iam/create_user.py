#!/usr/bin/env python3
"""create_user — Armada Bridge IAM suite, setup phase.

Bridge 5-step flow (POST /users/create returns void):
  Step 1: POST /users/create
          Body: {username, email, firstName, lastName, password,
                 enabled: true, emailVerified: true}
          Response: void (204 / empty body)
  Step 2: GET /users
          Filter list by email → extract user_id ← matching item's .id field
  Step 3: POST {KC_TOKEN_URL}  (grant_type=password, as the NEW user)
          Extract: new_user_token ← access_token
  Step 4: POST /key-manager/api-key
          Authorization: Bearer <new_user_token>  ← MUST be new user, not admin
          Response: { key: "..." }
          Extract: secret_access_key ← key

Output: {success, user_id, username, access_key_id: user_id,
         secret_access_key, platform: "iam"}

Note: access_key_id equals user_id — Bridge has no separate key-ID concept.
      delete_user.py calls DELETE /users/:userId which cleans up API keys too.
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
    parser.add_argument("--username", default="isv-test-user")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "iam"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "user_id": "demo-user-uuid-0001",
                "username": f"{args.username}@demo.example.com",
                "access_key_id": "demo-user-uuid-0001",
                "secret_access_key": "demo-secret-key-armada-0001",
            }
        )
    else:
        # --- Bridge implementation ---
        # client = BridgeClient.from_env()
        #
        # Step 1: Create user (returns void)
        # email = f"{args.username}@{args.tenant}.example.com"
        # password = "TempPass!Bridge2026"
        # client.post("/users/create", {
        #     "username": args.username, "email": email,
        #     "firstName": "ISV", "lastName": "TestUser",
        #     "password": password, "enabled": True, "emailVerified": True
        # })
        #
        # Step 2: GET /users → filter by email to find user_id
        # users = client.get("/users")
        # user = next(u for u in users if u["email"] == email)
        # user_id = user["id"]
        #
        # Step 3: ROPC as new user to get their token
        # user_client = client.login_as(email, password)
        #
        # Step 4: POST /key-manager/api-key AS the new user
        # key_resp = user_client.post("/key-manager/api-key", {})
        # secret_access_key = key_resp["key"]
        #
        # result.update({
        #     "success": True, "user_id": user_id,
        #     "username": email, "access_key_id": user_id,
        #     "secret_access_key": secret_access_key,
        # })
        raise NotImplementedError(
            "create_user: uncomment the Bridge implementation block above. "
            "See bridge-isv-ncp-status.md IAM suite for the 5-step flow."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
