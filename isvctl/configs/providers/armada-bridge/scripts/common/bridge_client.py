#!/usr/bin/env python3
"""BridgeClient — HTTP client for the Armada Bridge auth-gateway API.

Auth note: POST /auth/login sets a session cookie only — it does NOT return a
Bearer token. All scripts authenticate via the Keycloak ROPC token endpoint
(KC_TOKEN_URL) with grant_type=password. The resulting access_token becomes
Authorization: Bearer <token> on all auth-gateway requests.
"""
from __future__ import annotations

from typing import Any


class BridgeClient:
    """HTTP client for the Bridge auth-gateway API.

    Authenticates via the Keycloak ROPC token endpoint (KC_TOKEN_URL) using
    grant_type=password. The resulting access_token is sent as
    Authorization: Bearer <token> on all auth-gateway requests.
    """

    @classmethod
    def from_env(cls) -> BridgeClient:
        """Construct and authenticate a client from environment variables.

        Reads: BRIDGE_URL, BRIDGE_USERNAME (email), BRIDGE_PASSWORD,
               KC_TOKEN_URL, KC_CLIENT_ID, KC_CLIENT_SECRET.
        Calls self.login() before returning.
        """
        raise NotImplementedError(
            "BridgeClient.from_env() not yet implemented. "
            "Read env vars, set self.base_url / self.token, call self.login()."
        )

    def login(self) -> None:
        """Authenticate via Keycloak ROPC grant.

        POST {KC_TOKEN_URL}
        Content-Type: application/x-www-form-urlencoded
        Body: grant_type=password, client_id, client_secret,
              username=<BRIDGE_USERNAME (email)>, password=<BRIDGE_PASSWORD>
        Stores access_token on self.token.
        """
        raise NotImplementedError

    def login_as(self, email: str, password: str) -> BridgeClient:
        """Return a new BridgeClient authenticated as a different user.

        Used in IAM create_user flow: POST /key-manager/api-key must be called
        by the target user (not the admin). Same ROPC call as login() but with
        different credentials.
        """
        raise NotImplementedError

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET {self.base_url}{path} with Authorization: Bearer {self.token}."""
        raise NotImplementedError

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST {self.base_url}{path} with Bearer auth."""
        raise NotImplementedError

    def delete(self, path: str) -> dict[str, Any]:
        """DELETE {self.base_url}{path} with Bearer auth."""
        raise NotImplementedError

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH {self.base_url}{path} with Bearer auth."""
        raise NotImplementedError

    def wait_for_state(
        self,
        path: str,
        target_state: str,
        *,
        state_field: str = "state",
        timeout: int = 300,
        interval: int = 10,
    ) -> dict[str, Any]:
        """Poll GET path until response[state_field] == target_state or timeout.

        Returns the final response dict.
        """
        raise NotImplementedError
