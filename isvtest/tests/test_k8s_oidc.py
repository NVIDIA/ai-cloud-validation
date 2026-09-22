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

import io
import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations import k8s_oidc
from isvtest.validations.k8s_oidc import K8sOidcIssuerCheck

ISSUER = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"{ISSUER}/keys"

VALID_OIDC_RESPONSE = {
    "issuer": ISSUER,
    "jwks_uri": JWKS_URL,
    "response_types_supported": ["id_token"],
    "subject_types_supported": ["public"],
    "id_token_signing_alg_values_supported": ["RS256"],
}

VALID_JWKS = {"keys": [{"kid": "abc123", "kty": "RSA", "alg": "RS256", "use": "sig"}]}


class _FakeResponse:
    """Minimal stand-in for the context manager returned by urlopen."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _http_error(url: str, code: int, msg: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url=url, code=code, msg=msg, hdrs={}, fp=io.BytesIO(b""))


def _install_http(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> list[Any]:
    """Route anonymous fetches to canned bodies or exceptions; record the requests."""
    seen: list[Any] = []

    def _urlopen(request: Any, timeout: int | None = None) -> _FakeResponse:
        seen.append(request)
        outcome = routes.get(request.full_url)
        if outcome is None:
            raise _http_error(request.full_url, 404, "Not Found")
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(json.dumps(outcome).encode() if not isinstance(outcome, bytes) else outcome)

    monkeypatch.setattr(k8s_oidc.urllib.request, "urlopen", _urlopen)
    return seen


def _make_check(
    stdout: str | None = None,
    stderr: str = "",
    exit_code: int = 0,
    config: dict[str, Any] | None = None,
) -> K8sOidcIssuerCheck:
    """Create a check whose kubectl issuer discovery returns the given output."""
    if stdout is None:
        stdout = json.dumps({"issuer": ISSUER})
    mock_runner = MagicMock()
    mock_runner.run.return_value = CommandResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration=0.1,
    )
    return K8sOidcIssuerCheck(runner=mock_runner, config=config or {})


class TestAnonymousReachability:
    """The requirement is an unauthenticated fetch from outside the cluster."""

    def test_success_fetches_discovery_and_jwks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: VALID_JWKS})
        result = _make_check().execute()
        assert result["passed"] is True
        assert "anonymously reachable" in result["output"]
        assert "1 signing key(s)" in result["output"]
        assert [r.full_url for r in seen] == [DISCOVERY_URL, JWKS_URL]

    def test_no_credentials_are_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: VALID_JWKS})
        _make_check().execute()
        assert seen, "expected at least one anonymous fetch"
        for request in seen:
            assert not request.has_header("Authorization")
            assert not request.has_header("Cookie")

    def test_discovery_requiring_authentication_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: _http_error(DISCOVERY_URL, 403, "Forbidden")})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "HTTP 403 Forbidden" in result["error"]

    def test_unreachable_issuer_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: urllib.error.URLError("name resolution failed")})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "not anonymously reachable" in result["error"]


class TestJwksDereference:
    """A jwks_uri that is merely present is not proof; it has to serve keys."""

    def test_missing_jwks_endpoint_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "HTTP 404 Not Found" in result["error"]
        assert JWKS_URL in result["error"]

    def test_empty_key_set_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: {"keys": []}})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "contains no signing keys" in result["error"]

    def test_jwks_without_keys_field_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: {"unexpected": True}})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "contains no signing keys" in result["error"]

    def test_non_https_jwks_uri_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        doc = dict(VALID_OIDC_RESPONSE, jwks_uri="http://insecure.example.com/keys")
        _install_http(monkeypatch, {DISCOVERY_URL: doc})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "jwks_uri is not a valid HTTPS URL" in result["error"]

    def test_malformed_jwks_json_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: b"not-json"})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "Failed to parse JWKS document as JSON" in result["error"]


class TestDiscoveryDocument:
    def test_missing_required_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        incomplete = {"issuer": ISSUER, "jwks_uri": JWKS_URL}
        _install_http(monkeypatch, {DISCOVERY_URL: incomplete, JWKS_URL: VALID_JWKS})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "missing required fields" in result["error"]
        assert "response_types_supported" in result["error"]

    def test_issuer_mismatch_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        doc = dict(VALID_OIDC_RESPONSE, issuer="https://attacker.example.com")
        _install_http(monkeypatch, {DISCOVERY_URL: doc, JWKS_URL: VALID_JWKS})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "does not match the endpoint it was served from" in result["error"]

    def test_malformed_discovery_json_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: b"not-json"})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "Failed to parse OIDC discovery document as JSON" in result["error"]

    def test_non_object_discovery_response_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: []})
        result = _make_check().execute()
        assert result["passed"] is False
        assert "must be a JSON object" in result["error"]

    def test_custom_required_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        doc = dict(VALID_OIDC_RESPONSE, custom_field="value")
        _install_http(monkeypatch, {DISCOVERY_URL: doc, JWKS_URL: VALID_JWKS})
        result = _make_check(config={"required_fields": ["issuer", "custom_field"]}).execute()
        assert result["passed"] is True

    def test_custom_required_fields_cannot_opt_out_of_jwks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        doc = {k: v for k, v in VALID_OIDC_RESPONSE.items() if k != "jwks_uri"}
        _install_http(monkeypatch, {DISCOVERY_URL: doc})
        result = _make_check(config={"required_fields": ["issuer"]}).execute()
        assert result["passed"] is False
        assert "jwks_uri is not a valid HTTPS URL" in result["error"]


class TestIssuerResolution:
    def test_configured_issuer_url_skips_kubectl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: VALID_JWKS})
        check = _make_check(config={"issuer_url": ISSUER})
        result = check.execute()
        assert result["passed"] is True
        check.runner.run.assert_not_called()

    def test_configured_issuer_url_tolerates_trailing_slash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: VALID_JWKS})
        result = _make_check(config={"issuer_url": f"{ISSUER}/"}).execute()
        assert result["passed"] is True

    def test_kubectl_command_failure(self) -> None:
        result = _make_check(stdout="", exit_code=1, stderr="connection refused").execute()
        assert result["passed"] is False
        assert "Failed to query OIDC discovery endpoint" in result["error"]
        assert "connection refused" in result["error"]

    def test_kubectl_invalid_json(self) -> None:
        result = _make_check(stdout="not-json").execute()
        assert result["passed"] is False
        assert "Failed to parse OIDC discovery response as JSON" in result["error"]

    def test_kubectl_non_object_json(self) -> None:
        result = _make_check(stdout="[]").execute()
        assert result["passed"] is False
        assert "must be a JSON object" in result["error"]

    def test_issuer_not_https(self) -> None:
        result = _make_check(stdout=json.dumps({"issuer": "http://insecure.example.com"})).execute()
        assert result["passed"] is False
        assert "OIDC issuer is not a valid HTTPS URL" in result["error"]

    def test_issuer_empty_string(self) -> None:
        result = _make_check(stdout=json.dumps({"issuer": ""})).execute()
        assert result["passed"] is False
        assert "OIDC issuer is not a valid HTTPS URL" in result["error"]

    def test_issuer_missing(self) -> None:
        result = _make_check(stdout=json.dumps({"jwks_uri": JWKS_URL})).execute()
        assert result["passed"] is False
        assert "OIDC issuer is not a valid HTTPS URL" in result["error"]


class TestConfigValidation:
    def test_required_fields_single_string_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: VALID_JWKS})
        result = _make_check(config={"required_fields": "issuer"}).execute()
        assert result["passed"] is True

    def test_required_fields_single_string_with_whitespace_is_trimmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_http(monkeypatch, {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: VALID_JWKS})
        result = _make_check(config={"required_fields": " issuer "}).execute()
        assert result["passed"] is True

    def test_required_fields_whitespace_string_is_rejected(self) -> None:
        result = _make_check(config={"required_fields": "   "}).execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_required_fields_none_is_rejected(self) -> None:
        result = _make_check(config={"required_fields": None}).execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_required_fields_non_string_items_are_rejected(self) -> None:
        result = _make_check(config={"required_fields": ["issuer", 1]}).execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_required_fields_whitespace_items_are_rejected(self) -> None:
        result = _make_check(config={"required_fields": ["issuer", "   "]}).execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_invalid_http_timeout_is_rejected(self) -> None:
        result = _make_check(config={"http_timeout": "soon"}).execute()
        assert result["passed"] is False
        assert "http_timeout" in result["error"]

    def test_http_timeout_is_passed_to_urlopen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        timeouts: list[int | None] = []

        def _urlopen(request: Any, timeout: int | None = None) -> _FakeResponse:
            timeouts.append(timeout)
            body = VALID_OIDC_RESPONSE if request.full_url == DISCOVERY_URL else VALID_JWKS
            return _FakeResponse(json.dumps(body).encode())

        monkeypatch.setattr(k8s_oidc.urllib.request, "urlopen", _urlopen)
        result = _make_check(config={"http_timeout": 3}).execute()
        assert result["passed"] is True
        assert timeouts == [3, 3]
