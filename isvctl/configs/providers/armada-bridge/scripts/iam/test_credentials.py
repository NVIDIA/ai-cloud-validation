#!/usr/bin/env python3
"""test_credentials — Armada Bridge IAM suite, test phase.

Proves the API key from create_user is valid via the auth-gateway only:

  Step 1: POST /auth/login
          Body: {email: credential_id, password: TEST_PASSWORD}
          Establishes a session cookie — proves the user account is active.

  Step 2: GET /key-manager/api-key
          Cookie: <session>
          Returns the stored API key as plain text.
          Compared against credential_secret — proves the issued key is stored.

  Note: x-api-key header auth is a feature gap in the current auth-gateway
  build (ApiKeyAuthMiddleware is defined but never registered).

Output: {success, authenticated, account_id, identity_id, platform: "iam"}
"""
import argparse
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.errors import handle_bridge_errors
from common.constants import TEST_PASSWORD

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
        bridge_url = os.environ["BRIDGE_URL"].rstrip("/")

        ssl_context: ssl.SSLContext | None = None
        if os.environ.get("BRIDGE_INSECURE") == "1":
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        jar = http.cookiejar.CookieJar()
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(jar)]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        opener = urllib.request.build_opener(*handlers)

        # Step 1: authenticate as the test user
        try:
            login_body = json.dumps({"email": args.credential_id, "password": TEST_PASSWORD}).encode()
            login_req = urllib.request.Request(
                f"{bridge_url}/auth/login",
                data=login_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(login_req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            e.read()
            result.update(
                {
                    "error": f"Login failed (HTTP {e.code}): user account inactive or credentials invalid",
                    "authenticated": False,
                }
            )
            print(json.dumps(result, indent=2))
            return 1

        # Step 2: retrieve and verify the stored API key
        try:
            get_req = urllib.request.Request(
                f"{bridge_url}/key-manager/api-key",
                method="GET",
            )
            with opener.open(get_req, timeout=30) as resp:
                stored_key = resp.read().decode().strip()
        except urllib.error.HTTPError as e:
            e.read()
            result.update(
                {
                    "error": f"API key retrieval failed (HTTP {e.code})",
                    "authenticated": False,
                }
            )
            print(json.dumps(result, indent=2))
            return 1

        if not stored_key:
            result.update({"error": "No API key stored for user", "authenticated": False})
            print(json.dumps(result, indent=2))
            return 1

        if stored_key != args.credential_secret:
            result.update({"error": "Stored API key does not match issued credential", "authenticated": False})
            print(json.dumps(result, indent=2))
            return 1

        result.update(
            {
                "success": True,
                "authenticated": True,
                "account_id": args.credential_id,
                "identity_id": args.credential_id,
            }
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
