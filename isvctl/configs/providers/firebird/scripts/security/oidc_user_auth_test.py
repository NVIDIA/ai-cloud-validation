#!/usr/bin/env python3
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

"""OIDC token validation at the Firebird API (SEC01-01).

Uses the run's own access token (a JWT from the platform's OIDC issuer):
  discovery_and_jwks_reachable  ``<iss>/.well-known/openid-configuration`` names
                                the same issuer and a JWKS that holds the
                                token's signing key (``kid``)
  valid_token_accepted          ``GET /users/me`` with the token answers 200
  bad_signature_rejected        the token with a corrupted signature gets 401
  wrong_issuer_rejected, expired_token_rejected, missing_required_claim_rejected
                                the token with ``iss`` / ``exp`` changed or ``sub``
                                removed gets 401
  wrong_audience_rejected       reported failed, not supported: a tenant cannot
                                obtain a validly signed token for another
                                audience, so audience enforcement cannot be
                                proven from outside the API

A tenant cannot get a validly signed token with a wrong issuer or expiry, so
those tokens are forged by editing the claims and keeping the original
signature. They are rejected at signature verification: the tests show the API
refuses them, not that it checks each claim on its own. The output says so
(``claim_rejection_evidence: signature``) instead of implying more. A forged
wrong-audience token would be refused the same way, proving nothing about the
audience check - so that subtest is not probed. The step succeeds
when every other subtest passes; OidcUserAuthCheck fails on the audience.

Usage:
    python oidc_user_auth_test.py

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "oidc_user_auth_test",
    "issuer_url": "https://auth.example",
    "audience": "account",
    "target_url": "https://dgxc.firebird.ai/api/v1/users/me",
    "endpoints_tested": 1,
    "claim_rejection_evidence": "signature",
    "tests": {"valid_token_accepted": {"passed": true}, "bad_signature_rejected": {"passed": true}, ...,
              "wrong_audience_rejected": {"passed": false, "supported": false, "error": "not verifiable: ..."}}
}
"""

import base64
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import USER_AGENT, FirebirdClient, log
from common.service_accounts import status_of

KEYS = (
    "discovery_and_jwks_reachable",
    "valid_token_accepted",
    "bad_signature_rejected",
    "wrong_issuer_rejected",
    "wrong_audience_rejected",
    "expired_token_rejected",
    "missing_required_claim_rejected",
)
ME_PATH = "/users/me"
UNSUPPORTED = {
    "wrong_audience_rejected": (
        "not verifiable: a tenant cannot mint a validly signed token "
        "for another audience, so audience enforcement cannot be proven from outside the API"
    ),
}
FORGED_NOTE = (
    "the edited claim also invalidates the signature, so this shows the API refuses the token, "
    "not that it checks this claim on its own"
)


def b64url_decode(value: str) -> bytes:
    """Decode unpadded base64url."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(data: bytes) -> str:
    """Encode base64url without padding (JWS compact form)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_jwt(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (header, payload) of a compact JWS without verifying it."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("access token is not a compact JWS (header.payload.signature)")
    return json.loads(b64url_decode(parts[0])), json.loads(b64url_decode(parts[1]))


def with_claims(token: str, edit: Callable[[dict[str, Any]], None]) -> str:
    """Return ``token`` with its payload edited by ``edit`` and the original signature kept."""
    header, payload, signature = token.split(".")
    claims = json.loads(b64url_decode(payload))
    edit(claims)
    return f"{header}.{b64url_encode(json.dumps(claims, separators=(',', ':')).encode())}.{signature}"


def with_bad_signature(token: str) -> str:
    """Return ``token`` with the first signature byte flipped (claims untouched)."""
    header, payload, signature = token.split(".")
    raw = bytearray(b64url_decode(signature))
    raw[0] ^= 0xFF
    return f"{header}.{payload}.{b64url_encode(bytes(raw))}"


def fetch_json(url: str, timeout: int = 30) -> dict[str, Any]:
    """GET a public JSON document (OIDC discovery, JWKS)."""
    with urlopen(
        Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}), timeout=timeout
    ) as response:
        return json.loads(response.read().decode())


def discovery_test(header: dict[str, Any], issuer: str) -> dict[str, Any]:
    """Check the issuer's discovery document and that its JWKS holds the token's key."""
    config = fetch_json(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    if config.get("issuer") != issuer:
        return {"passed": False, "error": f"discovery names issuer {config.get('issuer')!r}, token has {issuer!r}"}
    jwks_uri = config.get("jwks_uri") or ""
    if not jwks_uri:
        return {"passed": False, "error": "discovery document has no jwks_uri"}
    kids = [key.get("kid") for key in fetch_json(jwks_uri).get("keys") or [] if isinstance(key, dict)]
    kid = header.get("kid")
    if not kids or (kid and kid not in kids):
        return {"passed": False, "error": f"JWKS at {jwks_uri} does not hold the token's key {kid!r}"}
    return {"passed": True, "message": f"discovery and JWKS ({len(kids)} key(s)) serve the signing key"}


def rejected_test(client: FirebirdClient, token: str, note: str = "") -> dict[str, Any]:
    """Send ``token`` to /users/me; pass only on HTTP 401."""
    status = status_of(lambda: client.request("GET", ME_PATH, bearer=token))
    if status == 401:
        return {"passed": True, "message": f"rejected (HTTP 401){'; ' + note if note else ''}"}
    return {"passed": False, "error": f"expected HTTP 401, got {status}"}


def main() -> int:
    """Probe discovery, then send the valid and tampered tokens to the API.

    Returns:
        0 when every supported probe passes, 1 otherwise
    """
    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "oidc_user_auth_test",
        "endpoints_tested": 0,
        "claim_rejection_evidence": "signature",
        "tests": tests,
    }
    try:
        client = FirebirdClient()
        token = client.access_token()
        header, claims = decode_jwt(token)
        issuer = str(claims.get("iss") or "")
        audience = claims.get("aud") or claims.get("azp") or ""
        result.update(
            issuer_url=issuer,
            audience=audience[0] if isinstance(audience, list) and audience else str(audience),
            target_url=f"{client.base_url}/api/v1{ME_PATH}",
        )
        if not issuer:
            raise RuntimeError("access token has no iss claim")

        try:
            tests["discovery_and_jwks_reachable"] = discovery_test(header, issuer)
        except Exception as e:  # an unreachable issuer fails this test, not the API probes
            tests["discovery_and_jwks_reachable"] = {"passed": False, "error": f"discovery: {e}"}

        me = client.request("GET", ME_PATH, bearer=token)
        result["endpoints_tested"] = 1
        tests["valid_token_accepted"] = {"passed": bool(me.get("userId")), "message": "HTTP 200 from /users/me"}

        tests["bad_signature_rejected"] = rejected_test(client, with_bad_signature(token))
        forged = {
            "wrong_issuer_rejected": lambda c: c.update(iss=f"{issuer.rstrip('/')}-forged"),
            "expired_token_rejected": lambda c: c.update(exp=int(time.time()) - 3600),
            "missing_required_claim_rejected": lambda c: c.pop("sub", None),
        }
        for key, edit in forged.items():
            tests[key] = rejected_test(client, with_claims(token, edit), FORGED_NOTE)
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    for key in KEYS:
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    for key, reason in UNSUPPORTED.items():
        tests[key] = {"passed": False, "supported": False, "error": reason}
    # The step succeeds when everything the API enforces was shown; the
    # unsupported audience check fails OidcUserAuthCheck on its own.
    result["success"] = "error" not in result and all(t["passed"] for k, t in tests.items() if k not in UNSUPPORTED)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
