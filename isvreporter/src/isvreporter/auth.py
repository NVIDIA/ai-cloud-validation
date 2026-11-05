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
from email.message import Message
from http.client import HTTPException
from typing import IO, Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

UPLOAD_SCOPES = "create-isv-lab-test-run update-isv-lab-test-run"

# Plain http is only tolerated for local test servers; anything else would send credentials in cleartext.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class _RejectRedirects(HTTPRedirectHandler):
    """Decline every redirect so urllib raises the 3xx as an ``HTTPError``."""

    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: Message, newurl: str
    ) -> Request | None:
        """Return None: never build a follow-up request."""
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects)


def open_url(request: Request, timeout: float) -> Any:
    """Open ``request`` like ``urlopen`` but never follow redirects.

    A credentialed ISV Lab Service call must not follow a redirect: keeping the
    credentials would leak them to the target, and dropping them turns a wrong URL
    (e.g. a login page) into a misleading 200 or 401. A 3xx raises ``HTTPError``.

    Args:
        request: Request to send
        timeout: Socket timeout in seconds

    Returns:
        The response object (a context manager, as from ``urlopen``)
    """
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def is_secure_url(url: str) -> bool:
    """Return True if ``url`` may carry credentials: https, or http to a loopback host.

    Args:
        url: Absolute URL to check

    Returns:
        True for ``https://host...`` or ``http://localhost|127.0.0.1|[::1]...``
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        return False
    if not host:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and host in _LOOPBACK_HOSTS)


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
        TokenError: If the issuer URL is not https (see ``is_secure_url``), is
            unreachable, redirects, rejects the credentials, or returns an
            unexpected response
    """
    if not is_secure_url(ssa_issuer):
        raise TokenError("SSA issuer must be an https URL")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = Request(
        f"{ssa_issuer}/token",
        data=urlencode({"scope": UPLOAD_SCOPES, "grant_type": "client_credentials"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
        method="POST",
    )
    try:
        with open_url(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise TokenError(f"SSA issuer answered with a redirect (HTTP {exc.code})") from exc
        raise TokenError(f"HTTP {exc.code}", exc.code) from exc
    except OSError as exc:
        raise TokenError(f"cannot reach SSA issuer: {getattr(exc, 'reason', exc)}") from exc
    except HTTPException as exc:  # malformed status line, truncated body, ...
        raise TokenError(f"malformed HTTP response from SSA issuer: {type(exc).__name__}") from exc
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
