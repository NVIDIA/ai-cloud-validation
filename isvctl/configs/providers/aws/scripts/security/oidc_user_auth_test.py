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

This AWS reference step is a black-box probe against a configured platform
endpoint. It fetches the platform issuer's real discovery document and JWKS,
checks that a supplied valid JWT chains to that issuer/audience, then sends
valid and invalid bearer tokens to the configured target endpoint.

The step is intentionally fail-closed: it never simulates an OIDC provider
locally. When no real issuer, audience, target endpoint, or valid test token
is configured, it emits a structured ``skipped`` result (exit 0) so the
orchestrator and validation can skip the check rather than fabricate a pass.

Usage:
    OIDC_VALID_TOKEN=... \\
    OIDC_WRONG_ISSUER_TOKEN=... \\
    OIDC_WRONG_AUDIENCE_TOKEN=... \\
    OIDC_EXPIRED_TOKEN=... \\
    OIDC_MISSING_REQUIRED_CLAIM_TOKEN=... \\
    python oidc_user_auth_test.py \\
      --region us-west-2 \\
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

DEFAULT_ACCEPT_STATUSES = "200-299"
DEFAULT_REJECT_STATUSES = "401,403"
DEFAULT_HTTP_TIMEOUT = 10
REQUIRED_CLAIMS = ("iss", "sub", "aud", "exp", "iat")
REQUIRED_PROBES = (
    "valid_token_accepted",
    "bad_signature_rejected",
    "wrong_issuer_rejected",
    "wrong_audience_rejected",
    "expired_token_rejected",
    "missing_required_claim_rejected",
    "discovery_and_jwks_reachable",
)
INVALID_TOKEN_ENV = {
    "wrong_issuer_rejected": "OIDC_WRONG_ISSUER_TOKEN",
    "wrong_audience_rejected": "OIDC_WRONG_AUDIENCE_TOKEN",
    "expired_token_rejected": "OIDC_EXPIRED_TOKEN",
    "missing_required_claim_rejected": "OIDC_MISSING_REQUIRED_CLAIM_TOKEN",
}


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


def _int_to_b64url(value: int) -> str:
    """Encode an integer as base64url bytes."""
    length = (value.bit_length() + 7) // 8
    return _b64url_encode(value.to_bytes(length, "big"))


def _public_jwk(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, str]:
    """Return a public RSA key as an RS256 JWK."""
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_to_b64url(numbers.n),
        "e": _int_to_b64url(numbers.e),
    }


def _jwk_to_public_key(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    """Convert an RSA JWK into a cryptography public key."""
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e=e, n=n).public_key()


def _sign_jwt(
    claims: dict[str, Any],
    private_key: rsa.RSAPrivateKey,
    kid: str,
    *,
    drop_claims: tuple[str, ...] = (),
) -> str:
    """Sign a JWT with RS256 for unit tests and local fixture generation."""
    payload = {k: v for k, v in claims.items() if k not in drop_claims}
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    signing_input = (
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64url_encode(signature)


def _verify_jwt(
    token: str,
    jwks: dict[str, Any],
    expected_issuer: str,
    expected_audience: str,
    *,
    now: int | None = None,
) -> tuple[bool, str]:
    """Strict OIDC verifier: signature via JWKS lookup, then claim checks."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return False, "malformed token"

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(header, dict):
            raise ValueError(f"JWT header is not an object: {type(header).__name__}")
        if not isinstance(payload, dict):
            raise ValueError(f"JWT payload is not an object: {type(payload).__name__}")
        signature = _b64url_decode(signature_b64)
    except (ValueError, UnicodeDecodeError) as e:
        return False, f"decode error: {e}"

    if header.get("alg") != "RS256":
        return False, f"unsupported alg: {header.get('alg')}"

    kid = header.get("kid")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        return False, "JWKS keys is not a list"

    matching = [k for k in keys if isinstance(k, Mapping) and k.get("kty") == "RSA" and k.get("kid") == kid]
    if not matching:
        return False, f"kid not found in JWKS: {kid}"

    public_key = _jwk_to_public_key(matching[0])
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False, "invalid signature"

    for required in REQUIRED_CLAIMS:
        if required not in payload:
            return False, f"missing required claim: {required}"

    if payload.get("iss") != expected_issuer:
        return False, f"issuer mismatch: {payload.get('iss')!r}"
    aud = payload.get("aud")
    aud_values = aud if isinstance(aud, list) else [aud]
    if expected_audience not in aud_values:
        return False, f"audience mismatch: {aud!r}"

    current = now if now is not None else int(time.time())
    try:
        expires_at = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return False, f"invalid exp claim: {payload.get('exp')!r}"
    if expires_at <= current:
        return False, "token expired"

    return True, "ok"


def _verify_jwt_signature(token: str, jwks: dict[str, Any]) -> str | None:
    """Return None when a JWT has a valid RS256 signature in the configured JWKS."""
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


def _decode_jwt_unverified(token: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Decode JWT header and payload without verifying the signature."""
    try:
        header_b64, payload_b64, _signature_b64 = token.split(".")
    except ValueError:
        return None, None, "malformed token"

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError) as e:
        return None, None, f"decode error: {e}"

    if not isinstance(header, dict):
        return None, None, f"JWT header is not an object: {type(header).__name__}"
    if not isinstance(payload, dict):
        return None, None, f"JWT payload is not an object: {type(payload).__name__}"
    return header, payload, None


def _audience_values(payload: dict[str, Any]) -> list[Any]:
    """Return the audience claim as a list for membership checks."""
    aud = payload.get("aud")
    return aud if isinstance(aud, list) else [aud]


def _missing_required_claims(payload: dict[str, Any]) -> list[str]:
    """Return required OIDC claims absent from the payload."""
    return [claim for claim in REQUIRED_CLAIMS if claim not in payload]


def _expires_at(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return the integer exp claim or an error string."""
    try:
        return int(payload["exp"]), None
    except KeyError:
        return None, "missing required claim: exp"
    except (TypeError, ValueError):
        return None, f"invalid exp claim: {payload.get('exp')!r}"


def _validate_negative_fixture(
    probe_name: str,
    token: str,
    jwks: dict[str, Any],
    expected_issuer: str,
    expected_audience: str,
    *,
    now: int | None = None,
) -> str | None:
    """Validate that a signed negative JWT fixture exercises the intended defect."""
    _header, payload, decode_error = _decode_jwt_unverified(token)
    if decode_error:
        return decode_error
    if payload is None:
        return "missing JWT payload"

    signature_error = _verify_jwt_signature(token, jwks)
    if signature_error:
        return f"token signature invalid: {signature_error}"

    missing = _missing_required_claims(payload)
    if probe_name == "missing_required_claim_rejected":
        if not missing:
            return "token contains all required claims"

        current = now if now is not None else int(time.time())
        expires_at, exp_error = _expires_at(payload)
        if exp_error:
            return exp_error
        if expires_at is None:
            return "missing required claim: exp"

        is_expired = expires_at <= current
        has_expected_audience = expected_audience in _audience_values(payload)
        has_expected_issuer = payload.get("iss") == expected_issuer
        if not has_expected_issuer:
            return "token also has the wrong issuer"
        if not has_expected_audience:
            return "token also has the wrong audience"
        if is_expired:
            return "token is expired instead"
        return None

    if missing:
        return "token is missing required claims instead: " + ", ".join(missing)

    current = now if now is not None else int(time.time())
    expires_at, exp_error = _expires_at(payload)
    if exp_error:
        return exp_error
    if expires_at is None:
        return "missing required claim: exp"

    is_expired = expires_at <= current
    has_expected_audience = expected_audience in _audience_values(payload)
    has_expected_issuer = payload.get("iss") == expected_issuer

    if probe_name == "wrong_issuer_rejected":
        if has_expected_issuer:
            return "issuer matches the expected issuer"
        if not has_expected_audience:
            return "token also has the wrong audience"
        if is_expired:
            return "token is expired instead"
        return None

    if probe_name == "wrong_audience_rejected":
        if not has_expected_issuer:
            return "token also has the wrong issuer"
        if has_expected_audience:
            return "audience includes the expected audience"
        if is_expired:
            return "token is expired instead"
        return None

    if probe_name == "expired_token_rejected":
        if not has_expected_issuer:
            return "token also has the wrong issuer"
        if not has_expected_audience:
            return "token also has the wrong audience"
        if not is_expired:
            return "token is not expired"
        return None

    return f"unknown negative probe: {probe_name}"


def _tamper_signature(token: str) -> str:
    """Return the same JWT with a corrupted signature."""
    head, payload, sig = token.split(".")
    raw = bytearray(_b64url_decode(sig) or b"\x00")
    raw[0] ^= 0xFF
    return f"{head}.{payload}.{_b64url_encode(bytes(raw))}"


def _check_discovery_and_jwks(
    discovery: dict[str, Any], jwks: dict[str, Any], expected_issuer: str
) -> tuple[bool, str]:
    """Validate OIDC discovery metadata and JWKS shape."""
    if discovery.get("issuer") != expected_issuer:
        return False, "discovery issuer mismatch"

    jwks_uri = discovery.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        return False, "discovery missing jwks_uri"
    parsed_jwks_uri = urlsplit(jwks_uri)
    if parsed_jwks_uri.scheme not in {"http", "https"} or not parsed_jwks_uri.netloc:
        return False, f"discovery jwks_uri is not a valid URL: {jwks_uri}"

    signing_algorithms = discovery.get("id_token_signing_alg_values_supported", [])
    if not isinstance(signing_algorithms, list):
        return False, "discovery id_token_signing_alg_values_supported is not a list"
    if "RS256" not in signing_algorithms:
        return False, "discovery does not advertise RS256"

    keys = jwks.get("keys") or []
    if not isinstance(keys, list):
        return False, "JWKS keys is not a list"

    has_valid_rsa_key = False
    rsa_failures: list[str] = []
    for index, key in enumerate(keys):
        if not isinstance(key, Mapping):
            continue
        if key.get("kty") != "RSA":
            continue
        missing_fields = [field for field in ("kid", "n", "e") if not key.get(field)]
        if missing_fields:
            rsa_failures.append(f"JWKS RSA key at index {index} missing required fields: {', '.join(missing_fields)}")
            continue
        has_valid_rsa_key = True

    if rsa_failures:
        return False, "; ".join(rsa_failures)
    if not has_valid_rsa_key:
        return False, "JWKS has no usable RSA keys"
    return True, "ok"


def _parse_statuses(value: str) -> set[int]:
    """Parse comma-separated HTTP status codes or inclusive ranges."""
    statuses: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start = int(start_raw)
            end = int(end_raw)
            statuses.update(range(start, end + 1))
        else:
            statuses.add(int(token))
    if not statuses:
        msg = f"Invalid empty HTTP status set: {value!r}"
        raise ValueError(msg)
    return statuses


def _fetch_json(url: str, timeout: int) -> dict[str, Any]:
    """Fetch a JSON object from a URL."""
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except HTTPError as e:
        msg = f"HTTP {e.code} fetching {url}: {e.reason}"
        raise RuntimeError(msg) from e
    except URLError as e:
        msg = f"Failed to fetch {url}: {e.reason}"
        raise RuntimeError(msg) from e

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        msg = f"Failed to parse JSON from {url}: {e}"
        raise RuntimeError(msg) from e

    if not isinstance(payload, dict):
        msg = f"Expected JSON object from {url}, got {type(payload).__name__}"
        raise RuntimeError(msg)
    return payload


def _fetch_discovery_and_jwks(issuer: str, timeout: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch OIDC discovery metadata and the referenced JWKS."""
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    discovery = _fetch_json(discovery_url, timeout)
    jwks_uri = discovery.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        return discovery, {}
    return discovery, _fetch_json(jwks_uri, timeout)


def _probe_endpoint(
    target_url: str,
    token: str,
    *,
    method: str,
    timeout: int,
    expected_statuses: set[int],
) -> dict[str, Any]:
    """Send a bearer token to the target endpoint and validate the status code."""
    request = Request(
        target_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method=method.upper(),
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = response.getcode()
            reason = "OK"
    except HTTPError as e:
        status_code = e.code
        reason = e.reason
    except URLError as e:
        return {"passed": False, "error": f"Failed to probe target endpoint: {e.reason}"}

    if status_code in expected_statuses:
        return {"passed": True, "status_code": status_code}

    expected = ", ".join(str(code) for code in sorted(expected_statuses))
    return {
        "passed": False,
        "status_code": status_code,
        "error": f"Unexpected status {status_code} from target endpoint ({reason}); expected one of {expected}",
    }


def _failed_probe(error: str) -> dict[str, Any]:
    """Return a failed probe result with a consistent shape."""
    return {"passed": False, "error": error}


def _not_executed_probes(error: str) -> dict[str, dict[str, Any]]:
    """Return every required probe as failed because execution could not start."""
    return {name: _failed_probe(error) for name in REQUIRED_PROBES}


def _token_from_sources(value: str, file_path: str, env_var: str) -> str:
    """Resolve a token from a direct value, file path, or environment variable."""
    if value.strip():
        return value.strip()
    if file_path.strip():
        return Path(file_path).read_text(encoding="utf-8").strip()
    return os.environ.get(env_var, "").strip()


def _reject_probe(
    target_url: str,
    token: str,
    *,
    method: str,
    timeout: int,
    reject_statuses: set[int],
) -> dict[str, Any]:
    """Probe the target endpoint expecting an invalid token to be rejected."""
    if not token:
        return _failed_probe("Token not configured")
    return _probe_endpoint(
        target_url,
        token,
        method=method,
        timeout=timeout,
        expected_statuses=reject_statuses,
    )


def _negative_fixture_probe(
    probe_name: str,
    target_url: str,
    token: str,
    jwks: dict[str, Any],
    expected_issuer: str,
    expected_audience: str,
    *,
    method: str,
    timeout: int,
    reject_statuses: set[int],
    now: int,
) -> dict[str, Any]:
    """Validate a negative fixture locally, then expect the endpoint to reject it."""
    if not token:
        return _failed_probe("Token not configured")

    fixture_error = _validate_negative_fixture(
        probe_name,
        token,
        jwks,
        expected_issuer,
        expected_audience,
        now=now,
    )
    if fixture_error:
        return _failed_probe(f"{probe_name} fixture invalid: {fixture_error}")

    return _probe_endpoint(
        target_url,
        token,
        method=method,
        timeout=timeout,
        expected_statuses=reject_statuses,
    )


def run_probes(
    issuer: str,
    audience: str,
    target_url: str,
    valid_token: str,
    invalid_tokens: dict[str, str],
    *,
    method: str = "GET",
    timeout: int = DEFAULT_HTTP_TIMEOUT,
    accept_statuses: set[int] | None = None,
    reject_statuses: set[int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Execute OIDC probes against the configured issuer and target endpoint."""
    accept_codes = accept_statuses or _parse_statuses(DEFAULT_ACCEPT_STATUSES)
    reject_codes = reject_statuses or _parse_statuses(DEFAULT_REJECT_STATUSES)
    probes: dict[str, dict[str, Any]] = _not_executed_probes("Validation not executed")
    current = int(time.time())

    try:
        discovery, jwks = _fetch_discovery_and_jwks(issuer, timeout)
    except Exception as e:
        probes["discovery_and_jwks_reachable"] = _failed_probe(f"{type(e).__name__}: {e}")
        return probes

    ok, detail = _check_discovery_and_jwks(discovery, jwks, issuer)
    probes["discovery_and_jwks_reachable"] = {"passed": True} if ok else _failed_probe(detail)
    if not ok:
        return probes

    token_ok, token_detail = _verify_jwt(valid_token, jwks, issuer, audience)
    if not token_ok:
        probes["valid_token_accepted"] = _failed_probe(f"valid token failed local OIDC validation: {token_detail}")
    else:
        probes["valid_token_accepted"] = _probe_endpoint(
            target_url,
            valid_token,
            method=method,
            timeout=timeout,
            expected_statuses=accept_codes,
        )

    try:
        bad_signature_token = _tamper_signature(valid_token)
    except ValueError as e:
        probes["bad_signature_rejected"] = _failed_probe(f"could not tamper valid token: {e}")
    else:
        probes["bad_signature_rejected"] = _reject_probe(
            target_url,
            bad_signature_token,
            method=method,
            timeout=timeout,
            reject_statuses=reject_codes,
        )

    for probe_name in (
        "wrong_issuer_rejected",
        "wrong_audience_rejected",
        "expired_token_rejected",
        "missing_required_claim_rejected",
    ):
        probes[probe_name] = _negative_fixture_probe(
            probe_name,
            target_url,
            invalid_tokens.get(probe_name, ""),
            jwks,
            issuer,
            audience,
            method=method,
            timeout=timeout,
            reject_statuses=reject_codes,
            now=current,
        )

    return probes


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the OIDC probe."""
    parser = argparse.ArgumentParser(description="OIDC user authentication test (SEC01-01)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--issuer-url", default=os.environ.get("OIDC_ISSUER_URL", ""))
    parser.add_argument("--audience", default=os.environ.get("OIDC_AUDIENCE", ""))
    parser.add_argument("--target-url", default=os.environ.get("OIDC_TARGET_URL", ""))
    parser.add_argument("--method", default=os.environ.get("OIDC_TARGET_METHOD", "GET"))
    parser.add_argument("--http-timeout", type=int, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--accept-statuses", default=os.environ.get("OIDC_ACCEPT_STATUSES", DEFAULT_ACCEPT_STATUSES))
    parser.add_argument("--reject-statuses", default=os.environ.get("OIDC_REJECT_STATUSES", DEFAULT_REJECT_STATUSES))
    parser.add_argument("--valid-token", default="", help="Valid OIDC JWT; prefer OIDC_VALID_TOKEN or file input")
    parser.add_argument("--valid-token-file", default="", help="File containing a valid OIDC JWT")
    parser.add_argument("--wrong-issuer-token", default="", help="JWT expected to fail issuer validation")
    parser.add_argument("--wrong-issuer-token-file", default="", help="File containing a wrong-issuer JWT")
    parser.add_argument("--wrong-audience-token", default="", help="JWT expected to fail audience validation")
    parser.add_argument("--wrong-audience-token-file", default="", help="File containing a wrong-audience JWT")
    parser.add_argument("--expired-token", default="", help="Expired JWT expected to be rejected")
    parser.add_argument("--expired-token-file", default="", help="File containing an expired JWT")
    parser.add_argument("--missing-required-claim-token", default="", help="JWT missing a required claim")
    parser.add_argument("--missing-required-claim-token-file", default="", help="File containing a missing-claim JWT")
    return parser


def _missing_config_errors(issuer: str, audience: str, target_url: str, valid_token: str) -> list[str]:
    """Return missing mandatory configuration fields."""
    missing: list[str] = []
    if not issuer:
        missing.append("--issuer-url or OIDC_ISSUER_URL")
    if not audience:
        missing.append("--audience or OIDC_AUDIENCE")
    if not target_url:
        missing.append("--target-url or OIDC_TARGET_URL")
    if not valid_token:
        missing.append("--valid-token, --valid-token-file, or OIDC_VALID_TOKEN")
    return missing


def main() -> int:
    """Run OIDC user authentication test and emit JSON result."""
    parser = _build_parser()
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "oidc_user_auth_test",
        "issuer_url": args.issuer_url,
        "audience": args.audience,
        "target_url": args.target_url,
        "endpoints_tested": 1 if args.target_url else 0,
        "tests": {},
    }

    try:
        valid_token = _token_from_sources(args.valid_token, args.valid_token_file, "OIDC_VALID_TOKEN")
        invalid_tokens = {
            "wrong_issuer_rejected": _token_from_sources(
                args.wrong_issuer_token,
                args.wrong_issuer_token_file,
                INVALID_TOKEN_ENV["wrong_issuer_rejected"],
            ),
            "wrong_audience_rejected": _token_from_sources(
                args.wrong_audience_token,
                args.wrong_audience_token_file,
                INVALID_TOKEN_ENV["wrong_audience_rejected"],
            ),
            "expired_token_rejected": _token_from_sources(
                args.expired_token,
                args.expired_token_file,
                INVALID_TOKEN_ENV["expired_token_rejected"],
            ),
            "missing_required_claim_rejected": _token_from_sources(
                args.missing_required_claim_token,
                args.missing_required_claim_token_file,
                INVALID_TOKEN_ENV["missing_required_claim_rejected"],
            ),
        }

        missing = _missing_config_errors(args.issuer_url, args.audience, args.target_url, valid_token)
        if missing:
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "OIDC validation not configured; missing " + ", ".join(missing)
            result["endpoints_tested"] = 0
            print(json.dumps(result, indent=2))
            return 0
        else:
            result["tests"] = run_probes(
                args.issuer_url,
                args.audience,
                args.target_url,
                valid_token,
                invalid_tokens,
                method=args.method,
                timeout=args.http_timeout,
                accept_statuses=_parse_statuses(args.accept_statuses),
                reject_statuses=_parse_statuses(args.reject_statuses),
            )
            result["success"] = all(probe.get("passed") for probe in result["tests"].values())
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["tests"] = _not_executed_probes(result["error"])

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
