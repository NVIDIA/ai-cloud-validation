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
from common.constants import TEST_PASSWORD
from iam_common import extract_user_from_users, extract_tenant_from_tenants, extract_tenant_org

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
        admin_client = BridgeClient.from_env()

        tenant_info = None
        user_email = f"{args.username}@{args.tenant}.example.com"

        # Step 1: Create user
        try:
            all_tenants = admin_client.get("/orchestrator/tenants")
            tenant_info = extract_tenant_from_tenants(all_tenants, args.tenant)
            if tenant_info is None:
                raise ValueError(f"Tenant '{args.tenant}' not found")

            keycloack_orgs_info = admin_client.get("/users/organizations")
            tenant_org = extract_tenant_org(keycloack_orgs_info, tenant_info["ID"])
            if tenant_org is None:
                raise ValueError(f"Tenant organization for tenant '{args.tenant}' not found")

            create_user_dto = CreateUserDTO(
                username=args.username,
                email=user_email,
                first_name=args.username,
                last_name="TestUser",
                password=TEST_PASSWORD,
                enabled=True,
                email_verified=False,
                org_id=tenant_org["id"],
                role_ref={
                    "scope": "tenant",
                    "name": "TenantAdmin",
                    "tenant_id": tenant_info["ID"]
                }
            )

            try:
                admin_client.post("/users/create", create_user_dto.to_dict())
            except Exception as e:
                if "status 409" not in str(e):
                    raise
                # user already exists from a prior run; proceed to lookup

        except Exception as e:
            result.update({"error": f"User create failed: {e}"})
            return result

        # step 2: Get user details
        user_info = None
        try:
            users = admin_client.get("/users")
            user_info = extract_user_from_users(users, user_email)
            if user_info is None:
                raise ValueError(f"User with email '{user_email}' not found")

            result["username"] = user_info["email"]
            result["user_id"] = user_info["id"]
            result["access_key_id"] = user_info["email"]

        except Exception as e:
            result.update({"error": f"Failed to find user {args.username} info: {e}"})
            return result

        # Step 3: Create API key
        try:
            user_client = admin_client.login_as(user_info["email"], TEST_PASSWORD)
            try:
                api_key = user_client.post("/key-manager/api-key", {})
                result["secret_access_key"] = api_key["key"]
            except Exception as e:
                if "status 409" not in str(e):
                    raise
                # key already exists from a prior run; rotate it so we return a known value
                user_client.delete("/key-manager/api-key")
                api_key = user_client.post("/key-manager/api-key", {})
                result["secret_access_key"] = api_key["key"]

        except Exception as e:
            result.update({"error": f"Failed to create API key for user {args.username}: {e}"})
            return result

        result["success"] = True

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1

class CreateUserDTO:
    def __init__(self, username, email, first_name, last_name, password, enabled, email_verified, org_id, role_ref):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.email = email
        self.password = password
        self.enabled = enabled
        self.email_verified = email_verified
        self.org_id = org_id
        self.role_ref = role_ref

    def to_dict(self):
        return {
            "username": self.username,
            "email": self.email,
            "firstName": self.first_name,
            "lastName": self.last_name,
            "password": self.password,
            "enabled": self.enabled,
            "emailVerified": self.email_verified,
            "organization": self.org_id,
            "roleRef": {
                "scope": self.role_ref["scope"],
                "name": self.role_ref["name"],
                "tenant_id": self.role_ref["tenant_id"],
            },
        }


if __name__ == "__main__":
    sys.exit(main())
