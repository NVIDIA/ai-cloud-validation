#!/usr/bin/env python3
"""test_credentials — Armada Bridge IAM suite, test phase.

Bridge endpoint: GET /auth/token/verify
  Send the user's api-key as Bearer token (via login_as or stored token).
  Response: VerifiedUserResponse { user: { email, firstName, lastName,
            status, userId, username }, roles: ['AUTHORIZE'] }
  account_id ← user.userId

Output: {success, authenticated, account_id, identity_id, platform: "iam"}
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
    parser.add_argument("--credential-id", required=True)
    parser.add_argument("--credential-secret", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "iam"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "authenticated": True,
                "account_id": args.credential_id,
                "identity_id": args.credential_id,
            }
        )
    else:
        # --- Bridge implementation ---
        # client = BridgeClient.from_env()
        # Use credential_secret as the api-key. Bridge ApiKeyGuard validates it.
        # user_client = BridgeClient with x-api-key: args.credential_secret
        # OR re-authenticate via ROPC with stored email/password
        # resp = user_client.get("/auth/token/verify")
        # result.update({
        #     "success": True, "authenticated": True,
        #     "account_id": resp["user"]["userId"],
        #     "identity_id": resp["user"]["userId"],
        # })
        raise NotImplementedError(
            "test_credentials: use GET /auth/token/verify with the new user's Bearer token. "
            "See bridge-isv-ncp-status.md IAM suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
