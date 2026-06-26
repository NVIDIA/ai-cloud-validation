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

"""Verify user authentication via OIDC for platform services (SEC01-01).

This is a provider-neutral black-box probe against a configured platform
endpoint. It fetches the issuer's real OIDC discovery document and JWKS, then
sends valid and invalid bearer tokens to a protected target endpoint and checks
that each is accepted or rejected as expected. The supplied valid token is first
verified locally against the fetched JWKS (signature, issuer, audience, expiry,
and required claims) so a malformed or mis-claimed "valid" fixture cannot be
treated as accepted merely because the endpoint returns 2xx. A token that must
be rejected only counts as rejected when the endpoint returns a configured
auth-rejection status (default 401/403; override via ``--reject-statuses`` /
``OIDC_REJECT_STATUSES``) -- any other non-2xx (e.g. a 500 crash or a 404) is
reported as inconclusive rather than fabricating a rejection signal. Each negative
fixture (except the deliberate bad-signature probe) is verified as RS256-signed
by the issuer JWKS and then proven to carry exactly its intended single defect
while every sibling defect (wrong issuer, wrong audience, expiry, missing claim)
is ruled out, so a rejection is attributable to the named defect rather than a
malformed token, an unrelated bad claim, or an unsigned one. It uses the Python
standard library (urllib/json/ssl) plus ``cryptography`` for JWKS signature
verification and reads no cloud SDK; on GCP the issuer is typically a Workforce
Identity Federation / Identity Platform OIDC provider and the target is a
Cloud Run / IAP / GKE endpoint (IAP returns 403 on authorization failure).

The step is intentionally fail-closed: it never simulates an OIDC provider
locally. When no issuer, audience, target endpoint, or valid test token is
configured, it emits a structured ``skipped`` result (exit 0) so the
orchestrator and validation skip the check rather than fabricate a pass.

Token fixtures are sensitive: each may be supplied via its flag or its matching
``OIDC_*_TOKEN`` environment variable. Token values are never printed.

Usage:
    OIDC_VALID_TOKEN=... \\
    OIDC_WRONG_ISSUER_TOKEN=... \\
    OIDC_WRONG_AUDIENCE_TOKEN=... \\
    OIDC_EXPIRED_TOKEN=... \\
    OIDC_MISSING_REQUIRED_CLAIM_TOKEN=... \\
    python3 oidc_user_auth_test.py \\
      --region us-central1 \\
      --issuer-url https://issuer.example/realms/isv \\
      --audience isv-validation \\
      --target-url https://platform.example/protected

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "oidc_user_auth_test",
    "issuer_url": "https://issuer.example/realms/isv",
    "audience": "isv-validation",
    "target_url": "https://platform.example/protected",
    "endpoints_tested": 1,
    "tests": {
      "valid_token_accepted":            {"passed": true},
      "bad_signature_rejected":          {"passed": true},
      "wrong_issuer_rejected":           {"passed": true},
      "wrong_audience_rejected":         {"passed": true},
      "expired_token_rejected":          {"passed": true},
      "missing_required_claim_rejected": {"passed": true},
      "discovery_and_jwks_reachable":    {"passed": true}
    }
  }
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.errors import handle_gcp_errors

# A 2xx is "accepted". A token that must be rejected has to come back with an
# auth-rejection status, NOT merely any non-2xx: an endpoint that 500s (or 404s)
# on an invalid token is broken/inconclusive, not proof that OIDC rejected the
# token. IAP returns 403 and most OIDC gateways return 401/403, so the reject set
# defaults to {401, 403} and stays operator-overridable (--reject-statuses /
# OIDC_REJECT_STATUSES) for gateways that signal auth failure with another code.
# Mirrors the AWS oracle's reject-status gate.
_HTTP_TIMEOUT_S = 10
_DEFAULT_REJECT_STATUSES = "401,403"
_REQUIRED_CLAIMS = ("iss", "sub", "aud", "exp", "iat")

# Probe name -> environment variable carrying that negative-fixture token. The
# valid token uses OIDC_VALID_TOKEN; the four named negatives map below.
_TOKEN_ENV = {
    "valid": "OIDC_VALID_TOKEN",
    "wrong_issuer_rejected": "OIDC_WRONG_ISSUER_TOKEN",
    "wrong_audience_rejected": "OIDC_WRONG_AUDIENCE_TOKEN",
    "expired_token_rejected": "OIDC_EXPIRED_TOKEN",
    "missing_required_claim_rejected": "OIDC_MISSING_REQUIRED_CLAIM_TOKEN",
}

_REQUIRED_PROBES = (
    "valid_token_accepted",
    "bad_signature_rejected",
    "wrong_issuer_rejected",
    "wrong_audience_rejected",
    "expired_token_rejected",
    "missing_required_claim_rejected",
    "discovery_and_jwks_reachable",
)


def _b64url_encode(data: bytes) -> str:
    """Base64url-encode bytes without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Base64url-decode a string, adding padding when needed."""
    try:
        pad = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + pad)
    except (TypeError, binascii.Error, ValueError) as e:
        raise ValueError(f"invalid base64url data: {e}") from e


def _decode_jwt_payload(token: str) -> tuple[dict[str, Any] | None, str | None]:
    """Decode the JWT payload (claims) without verifying the signature."""
    try:
        _header_b64, payload_b64, _signature_b64 = token.split(".")
    except ValueError:
        return None, "malformed token"
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"decode error: {e}"
    if not isinstance(payload, dict):
        return None, f"JWT payload is not an object: {type(payload).__name__}"
    return payload, None


def _audience_values(payload: dict[str, Any]) -> list[Any]:
    """Return the audience claim as a list for membership checks."""
    aud = payload.get("aud")
    return aud if isinstance(aud, list) else [aud]


def _tamper_signature(token: str) -> str:
    """Return the same JWT with a corrupted signature segment.

    Flips a byte of the decoded signature so the issuer can no longer verify it,
    without re-signing (no private key / crypto library needed).
    """
    head, payload, sig = token.split(".")
    raw = bytearray(_b64url_decode(sig) or b"\x00")
    raw[0] ^= 0xFF
    return f"{head}.{payload}.{_b64url_encode(bytes(raw))}"


def _jwk_to_public_key(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    """Convert an RSA JWK into a cryptography public key."""
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e=e, n=n).public_key()


def _verify_jwt_signature(token: str, jwks: dict[str, Any]) -> str | None:
    """Return None when a JWT carries a valid RS256 signature present in the JWKS.

    Looks up the signing key by ``kid`` and verifies the RS256 signature so a
    negative fixture is proven issuer-signed; the endpoint's later rejection is
    then attributable to the fixture's single semantic defect, not to a malformed
    or unsigned token.
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return "malformed token"

    try:
        header = json.loads(_b64url_decode(header_b64))
        signature = _b64url_decode(signature_b64)
    except (ValueError, UnicodeDecodeError) as e:
        return f"decode error: {e}"

    if not isinstance(header, dict):
        return f"JWT header is not an object: {type(header).__name__}"
    if header.get("alg") != "RS256":
        return f"unsupported alg: {header.get('alg')}"

    kid = header.get("kid")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return "JWKS keys is not a list"
    matching = [k for k in keys if isinstance(k, Mapping) and k.get("kty") == "RSA" and k.get("kid") == kid]
    if not matching:
        return f"kid not found in JWKS: {kid}"

    try:
        public_key = _jwk_to_public_key(matching[0])
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return "invalid signature"
    except (KeyError, ValueError) as e:
        return f"invalid JWK or signature: {e}"
    return None


def _expires_at(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return the integer ``exp`` claim, or an error string when absent/malformed."""
    try:
        return int(payload["exp"]), None
    except KeyError:
        return None, "missing required claim: exp"
    except (TypeError, ValueError):
        return None, f"invalid exp claim: {payload.get('exp')!r}"


def _verify_jwt(
    token: str,
    jwks: dict[str, Any],
    issuer: str,
    audience: str,
    *,
    now: int | None = None,
) -> tuple[bool, str]:
    """Strictly verify a "valid" OIDC JWT against the issuer JWKS before probing.

    Mirrors the AWS oracle's valid-token verifier: confirm the RS256 signature is
    present in the fetched JWKS, then require every claim in ``_REQUIRED_CLAIMS``
    and check issuer, audience, and expiry. A malformed, wrong-issuer,
    wrong-audience, expired, or missing-claim "valid" fixture is reported as a
    failed ``valid_token_accepted`` probe instead of being treated as accepted
    merely because the endpoint returned a 2xx.
    """
    payload, decode_error = _decode_jwt_payload(token)
    if decode_error or payload is None:
        return False, decode_error or "no payload"

    signature_error = _verify_jwt_signature(token, jwks)
    if signature_error:
        return False, signature_error

    missing = [claim for claim in _REQUIRED_CLAIMS if claim not in payload]
    if missing:
        return False, "missing required claim: " + ", ".join(missing)

    if payload.get("iss") != issuer:
        return False, f"issuer mismatch: {payload.get('iss')!r}"
    if audience not in _audience_values(payload):
        return False, f"audience mismatch: {payload.get('aud')!r}"

    expires_at, exp_error = _expires_at(payload)
    if exp_error:
        return False, exp_error
    if expires_at is None:
        return False, "missing required claim: exp"
    current = now if now is not None else int(time.time())
    if expires_at <= current:
        return False, "token expired"

    return True, "ok"


def _validate_negative_fixture(
    probe_name: str,
    token: str,
    payload: dict[str, Any],
    jwks: dict[str, Any],
    issuer: str,
    audience: str,
    *,
    now: int | None = None,
) -> str | None:
    """Confirm a negative fixture exercises exactly the defect its probe targets.

    Returns an error string when the fixture is wrong (e.g. a "wrong issuer"
    token that actually carries the expected issuer, or is also expired), else
    None. The fixture is first verified as RS256-signed by the issuer JWKS
    (these four probes are all issuer-signed; only the separate bad-signature
    probe is unsigned), then its single intended defect is confirmed while every
    sibling defect is ruled out. Mirrors the AWS oracle's negative-fixture
    validator so a rejection is attributable to the named defect alone -- a
    token rejected for an unrelated reason (wrong claim, expiry, bad signature)
    cannot pass the wrong subtest and dilute the released check's evidence.
    """
    signature_error = _verify_jwt_signature(token, jwks)
    if signature_error:
        return f"token signature invalid: {signature_error}"

    current = now if now is not None else int(time.time())
    missing = [claim for claim in _REQUIRED_CLAIMS if claim not in payload]

    if probe_name == "missing_required_claim_rejected":
        if not missing:
            return "fixture contains all required claims"
        # The only intended defect is the absent claim: prove the fixture is
        # otherwise valid (real issuer, real audience, unexpired, exp present).
        expires_at, exp_error = _expires_at(payload)
        if exp_error:
            return exp_error
        if expires_at is None:
            return "missing required claim: exp"
        if payload.get("iss") != issuer:
            return "fixture also has the wrong issuer"
        if audience not in _audience_values(payload):
            return "fixture also has the wrong audience"
        if expires_at <= current:
            return "fixture is expired instead"
        return None

    # The remaining three probes must carry every required claim; a missing
    # claim would be a second, unintended defect.
    if missing:
        return "fixture is missing required claims instead: " + ", ".join(missing)

    expires_at, exp_error = _expires_at(payload)
    if exp_error:
        return exp_error
    if expires_at is None:
        return "missing required claim: exp"

    is_expired = expires_at <= current
    has_issuer = payload.get("iss") == issuer
    has_audience = audience in _audience_values(payload)

    if probe_name == "wrong_issuer_rejected":
        if has_issuer:
            return "fixture issuer matches the expected issuer"
        if not has_audience:
            return "fixture also has the wrong audience"
        if is_expired:
            return "fixture is expired instead"
        return None
    if probe_name == "wrong_audience_rejected":
        if not has_issuer:
            return "fixture also has the wrong issuer"
        if has_audience:
            return "fixture audience includes the expected audience"
        if is_expired:
            return "fixture is expired instead"
        return None
    if probe_name == "expired_token_rejected":
        if not has_issuer:
            return "fixture also has the wrong issuer"
        if not has_audience:
            return "fixture also has the wrong audience"
        if not is_expired:
            return "fixture is not expired"
        return None
    return f"unknown negative probe: {probe_name}"


def _fetch_json(url: str, timeout: int) -> dict[str, Any]:
    """Fetch and parse a JSON object from a URL."""
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching {url}: {e.reason}") from e
    except URLError as e:
        raise RuntimeError(f"failed to fetch {url}: {e.reason}") from e

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"failed to parse JSON from {url}: {e}") from e
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object from {url}, got {type(payload).__name__}")
    return payload


def _fetch_discovery_and_jwks(issuer: str, timeout: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch the OIDC discovery document and the JWKS it references."""
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    discovery = _fetch_json(discovery_url, timeout)
    jwks_uri = discovery.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        return discovery, {}
    return discovery, _fetch_json(jwks_uri, timeout)


def _check_discovery_and_jwks(discovery: dict[str, Any], jwks: dict[str, Any], issuer: str) -> tuple[bool, str]:
    """Validate the OIDC discovery metadata and JWKS shape."""
    if discovery.get("issuer") != issuer:
        return False, "discovery issuer mismatch"

    jwks_uri = discovery.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        return False, "discovery missing jwks_uri"
    parsed = urlsplit(jwks_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, f"discovery jwks_uri is not a valid URL: {jwks_uri}"

    keys = jwks.get("keys")
    if not isinstance(keys, list) or not keys:
        return False, "JWKS has no keys"
    for key in keys:
        if isinstance(key, Mapping) and key.get("kid") and key.get("kty"):
            return True, "ok"
    return False, "JWKS has no usable signing keys"


def _parse_statuses(value: str) -> set[int]:
    """Parse comma-separated HTTP status codes or inclusive ranges (e.g. "401,403")."""
    statuses: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            statuses.update(range(int(start_raw), int(end_raw) + 1))
        else:
            statuses.add(int(token))
    if not statuses:
        raise ValueError(f"invalid empty HTTP status set: {value!r}")
    return statuses


def _probe_endpoint(target_url: str, token: str, timeout: int) -> int | str:
    """Send a bearer token to the target and return its HTTP status (or error string)."""
    request = Request(
        target_url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.getcode()
    except HTTPError as e:
        return e.code
    except URLError as e:
        return f"failed to probe target endpoint: {e.reason}"


def _accept_probe(target_url: str, token: str, timeout: int) -> dict[str, Any]:
    """Expect a 2xx status for a token that should be accepted."""
    status = _probe_endpoint(target_url, token, timeout)
    if isinstance(status, str):
        return {"passed": False, "error": status}
    if 200 <= status <= 299:
        return {"passed": True, "status_code": status}
    return {"passed": False, "status_code": status, "error": f"expected 2xx, got {status}"}


def _reject_probe(target_url: str, token: str, timeout: int, reject_statuses: set[int]) -> dict[str, Any]:
    """Expect an auth-rejection status (default 401/403) for a token that must be rejected.

    A non-2xx status outside the reject set (for example a 500 crash or a 404)
    is NOT treated as proof of rejection: it is reported as a failed/inconclusive
    probe so a broken endpoint cannot masquerade as enforcing OIDC.
    """
    status = _probe_endpoint(target_url, token, timeout)
    if isinstance(status, str):
        return {"passed": False, "error": status}
    if status in reject_statuses:
        return {"passed": True, "status_code": status}
    expected = ", ".join(str(code) for code in sorted(reject_statuses))
    return {
        "passed": False,
        "status_code": status,
        "error": f"expected rejection status in {{{expected}}}, got {status}",
    }


def _run_probes(
    issuer: str,
    audience: str,
    target_url: str,
    valid_token: str,
    negative_tokens: dict[str, str],
    timeout: int,
    reject_statuses: set[int],
) -> dict[str, dict[str, Any]]:
    """Execute the 7 OIDC probes against the issuer and the protected target."""
    probes: dict[str, dict[str, Any]] = {
        name: {"passed": False, "error": "probe not executed"} for name in _REQUIRED_PROBES
    }
    current = int(time.time())

    # Probe 7: discovery + JWKS both reachable and well-formed.
    try:
        discovery, jwks = _fetch_discovery_and_jwks(issuer, timeout)
    except Exception as e:
        probes["discovery_and_jwks_reachable"] = {"passed": False, "error": f"{type(e).__name__}: {e}"}
        return probes
    ok, detail = _check_discovery_and_jwks(discovery, jwks, issuer)
    probes["discovery_and_jwks_reachable"] = {"passed": True} if ok else {"passed": False, "error": detail}
    if not ok:
        return probes

    # Probe 1: the valid token must chain to the issuer/audience locally before
    # the endpoint accepts it (2xx). Mirror the AWS oracle -- verify signature,
    # issuer, audience, expiry, and required claims against the fetched JWKS
    # first, so a malformed/wrong-issuer/wrong-audience/expired/missing-claim
    # "valid" fixture cannot pass just because the endpoint returns 2xx.
    token_ok, token_detail = _verify_jwt(valid_token, jwks, issuer, audience, now=current)
    if not token_ok:
        probes["valid_token_accepted"] = {
            "passed": False,
            "error": f"valid token failed local OIDC validation: {token_detail}",
        }
    else:
        probes["valid_token_accepted"] = _accept_probe(target_url, valid_token, timeout)

    # Probe 2: a token with a tampered signature is rejected.
    try:
        bad_signature_token = _tamper_signature(valid_token)
    except ValueError as e:
        probes["bad_signature_rejected"] = {"passed": False, "error": f"could not tamper valid token: {e}"}
    else:
        probes["bad_signature_rejected"] = _reject_probe(target_url, bad_signature_token, timeout, reject_statuses)

    # Probes 3-6: each negative fixture exercises exactly one defect, then is rejected.
    for probe_name in (
        "wrong_issuer_rejected",
        "wrong_audience_rejected",
        "expired_token_rejected",
        "missing_required_claim_rejected",
    ):
        token = negative_tokens.get(probe_name, "")
        if not token:
            probes[probe_name] = {"passed": False, "error": "token not configured"}
            continue
        payload, decode_error = _decode_jwt_payload(token)
        if decode_error or payload is None:
            probes[probe_name] = {"passed": False, "error": f"fixture invalid: {decode_error or 'no payload'}"}
            continue
        fixture_error = _validate_negative_fixture(probe_name, token, payload, jwks, issuer, audience, now=current)
        if fixture_error:
            probes[probe_name] = {"passed": False, "error": f"fixture invalid: {fixture_error}"}
            continue
        probes[probe_name] = _reject_probe(target_url, token, timeout, reject_statuses)

    return probes


def _token_from_arg_or_env(value: str, env_var: str) -> str:
    """Resolve a token from its flag, falling back to the matching environment variable."""
    if value.strip():
        return value.strip()
    return os.environ.get(env_var, "").strip()


@handle_gcp_errors
def main() -> int:
    """Run the OIDC user authentication probe and emit JSON result."""
    parser = argparse.ArgumentParser(description="OIDC user authentication test (SEC01-01)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="", help="GCP project (accepted for parity; not used by this prober)")
    parser.add_argument("--issuer-url", default="")
    parser.add_argument("--audience", default="")
    parser.add_argument("--target-url", default="")
    parser.add_argument("--valid-token", default="", help="Valid OIDC JWT; prefer OIDC_VALID_TOKEN")
    parser.add_argument("--wrong-issuer-token", default="", help="JWT expected to fail issuer validation")
    parser.add_argument("--wrong-audience-token", default="", help="JWT expected to fail audience validation")
    parser.add_argument("--expired-token", default="", help="Expired JWT expected to be rejected")
    parser.add_argument("--missing-required-claim-token", default="", help="JWT missing a required claim")
    parser.add_argument(
        "--reject-statuses",
        default=os.environ.get("OIDC_REJECT_STATUSES", _DEFAULT_REJECT_STATUSES),
        help="HTTP statuses that count as an auth rejection (comma list or ranges; default 401,403)",
    )
    args = parser.parse_args()

    issuer = args.issuer_url.strip()
    audience = args.audience.strip()
    target_url = args.target_url.strip()
    valid_token = _token_from_arg_or_env(args.valid_token, _TOKEN_ENV["valid"])

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "oidc_user_auth_test",
        "issuer_url": issuer,
        "audience": audience,
        "target_url": target_url,
        "endpoints_tested": 1 if target_url else 0,
        "tests": {
            "valid_token_accepted": {"passed": False},
            "bad_signature_rejected": {"passed": False},
            "wrong_issuer_rejected": {"passed": False},
            "wrong_audience_rejected": {"passed": False},
            "expired_token_rejected": {"passed": False},
            "missing_required_claim_rejected": {"passed": False},
            "discovery_and_jwks_reachable": {"passed": False},
        },
    }

    # Fail closed: when the issuer, audience, target, or valid token is not
    # configured, emit a structured skip rather than fabricate a result. The
    # validation honors skipped:true and treats it as a pass-equivalent skip.
    missing: list[str] = []
    if not issuer:
        missing.append("--issuer-url")
    if not audience:
        missing.append("--audience")
    if not target_url:
        missing.append("--target-url")
    if not valid_token:
        missing.append("--valid-token or OIDC_VALID_TOKEN")
    if missing:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "OIDC validation not configured; missing " + ", ".join(missing)
        result["endpoints_tested"] = 0
        print(json.dumps(result, indent=2))
        return 0

    negative_tokens = {
        "wrong_issuer_rejected": _token_from_arg_or_env(args.wrong_issuer_token, _TOKEN_ENV["wrong_issuer_rejected"]),
        "wrong_audience_rejected": _token_from_arg_or_env(
            args.wrong_audience_token, _TOKEN_ENV["wrong_audience_rejected"]
        ),
        "expired_token_rejected": _token_from_arg_or_env(args.expired_token, _TOKEN_ENV["expired_token_rejected"]),
        "missing_required_claim_rejected": _token_from_arg_or_env(
            args.missing_required_claim_token, _TOKEN_ENV["missing_required_claim_rejected"]
        ),
    }

    try:
        reject_statuses = _parse_statuses(args.reject_statuses)
        result["tests"] = _run_probes(
            issuer, audience, target_url, valid_token, negative_tokens, _HTTP_TIMEOUT_S, reject_statuses
        )
        result["success"] = all(probe.get("passed") for probe in result["tests"].values())
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
