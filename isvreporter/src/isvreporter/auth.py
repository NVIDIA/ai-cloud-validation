# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Authentication module for ISV Lab Service API."""

import base64
import json
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UPLOAD_SCOPES = "create-isv-lab-test-run update-isv-lab-test-run"


class TokenError(Exception):
    """SSA token request failed. The message never contains credentials.

    ``status`` is the HTTP code when the issuer answered, else ``None``.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """Store the safe-to-print ``message`` and the optional HTTP ``status``."""
        super().__init__(message)
        self.status = status


def request_jwt_token(ssa_issuer: str, client_id: str, client_secret: str, timeout: float = 30) -> str:
    """Obtain a JWT from SSA using client credentials, raising on failure.

    Args:
        ssa_issuer: SSA token issuer URL
        client_id: OAuth client ID
        client_secret: OAuth client secret
        timeout: Socket timeout in seconds

    Returns:
        JWT access token

    Raises:
        TokenError: If the issuer URL is invalid, unreachable, rejects the
            credentials, or returns an unexpected response
    """
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        request = Request(
            f"{ssa_issuer}/token",
            data=urlencode({"scope": UPLOAD_SCOPES, "grant_type": "client_credentials"}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    except ValueError as exc:
        raise TokenError("invalid SSA issuer URL") from exc
    # Unredirected: urllib copies ordinary headers onto redirects, which would hand the
    # client credentials to whatever host the issuer redirects to.
    request.add_unredirected_header("Authorization", f"Basic {basic}")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        raise TokenError(f"HTTP {exc.code}", exc.code) from exc
    except OSError as exc:
        raise TokenError(f"cannot reach SSA issuer: {getattr(exc, 'reason', exc)}") from exc
    except ValueError as exc:  # JSONDecodeError / UnicodeDecodeError
        raise TokenError("unexpected token response format") from exc
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise TokenError("unexpected token response format: no access_token")
    return token


def get_jwt_token(ssa_issuer: str, client_id: str, client_secret: str) -> str:
    """
    Obtain JWT token from SSA using client credentials.

    Args:
        ssa_issuer: SSA token issuer URL
        client_id: OAuth client ID
        client_secret: OAuth client secret

    Returns:
        JWT access token

    Raises:
        SystemExit: If token acquisition fails
    """
    try:
        return request_jwt_token(ssa_issuer, client_id, client_secret)
    except TokenError as e:
        print(f"ERROR: Failed to obtain JWT token ({e})", file=sys.stderr)
        if e.status is not None:
            print("       Please check your ISV_CLIENT_ID and ISV_CLIENT_SECRET credentials", file=sys.stderr)
        else:
            print(f"       SSA Issuer: {ssa_issuer}", file=sys.stderr)
            print("       This may be expected in network-restricted environments", file=sys.stderr)
        sys.exit(1)
