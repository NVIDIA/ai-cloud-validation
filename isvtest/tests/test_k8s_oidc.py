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
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def _http_error(url: str, code: int, msg: str) -> urllib.error.HTTPError:
    """Build the error urlopen raises for a non-2xx response."""
    return urllib.error.HTTPError(url=url, code=code, msg=msg, hdrs={}, fp=io.BytesIO(b""))


class _FakeHttp:
    """Route anonymous fetches to canned bodies or exceptions; record the requests.

    Serves a valid discovery document and JWKS by default; a URL missing from
    ``routes`` answers 404.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {DISCOVERY_URL: VALID_OIDC_RESPONSE, JWKS_URL: VALID_JWKS}
        self.seen: list[Any] = []

    def open(self, request: Any, timeout: int | None = None) -> io.BytesIO:
        """Record the request and answer from ``routes``, raising 404 for unknown URLs."""
        request.timeout = timeout
        self.seen.append(request)
        outcome = self.routes.get(request.full_url)
        if outcome is None:
            raise _http_error(request.full_url, 404, "Not Found")
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())


@pytest.fixture(autouse=True)
def http(monkeypatch: pytest.MonkeyPatch) -> _FakeHttp:
    """Keep every test off the real network."""
    fake = _FakeHttp()
    monkeypatch.setattr(k8s_oidc._OPENER, "open", fake.open)
    return fake


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

    def test_success_fetches_discovery_and_jwks_without_credentials(self, http: _FakeHttp) -> None:
        result = _make_check().execute()
        assert result["passed"] is True
        assert "anonymously reachable" in result["output"]
        assert "1 signing key(s)" in result["output"]
        assert [r.full_url for r in http.seen] == [DISCOVERY_URL, JWKS_URL]
        assert not any(r.has_header("Authorization") for r in http.seen)

    def test_discovery_requiring_authentication_fails(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = _http_error(DISCOVERY_URL, 403, "Forbidden")
        result = _make_check().execute()
        assert result["passed"] is False
        assert "HTTP 403 Forbidden" in result["error"]

    def test_unreachable_issuer_fails(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = urllib.error.URLError("name resolution failed")
        result = _make_check().execute()
        assert result["passed"] is False
        assert "not anonymously reachable" in result["error"]

    def test_non_https_redirect_fails(self, http: _FakeHttp) -> None:
        http.routes[JWKS_URL] = k8s_oidc._NonHttpsRedirect("http://insecure.example.com/keys")
        result = _make_check().execute()
        assert result["passed"] is False
        assert "redirected to a non-HTTPS URL: http://insecure.example.com/keys" in result["error"]


class TestHttpsOnlyRedirects:
    """Every redirect hop must stay on HTTPS, not just the final URL."""

    def test_handler_refuses_http_hop(self) -> None:
        request = urllib.request.Request(DISCOVERY_URL)
        with pytest.raises(k8s_oidc._NonHttpsRedirect):
            k8s_oidc._HttpsOnlyRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "http://insecure.example.com/hop"
            )

    def test_handler_follows_https_hop(self) -> None:
        request = urllib.request.Request(DISCOVERY_URL)
        followed = k8s_oidc._HttpsOnlyRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://keys.example.com/jwks"
        )
        assert followed is not None
        assert followed.full_url == "https://keys.example.com/jwks"

    def test_opener_stops_before_requesting_http_hop(self) -> None:
        requested: list[str] = []

        class _Redirector(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requested.append(self.path)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/hop")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), _Redirector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            opener = urllib.request.build_opener(k8s_oidc._HttpsOnlyRedirectHandler)
            with pytest.raises(k8s_oidc._NonHttpsRedirect):
                opener.open(f"http://127.0.0.1:{server.server_port}/start", timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        assert requested == ["/start"]


class TestJwksDereference:
    """A jwks_uri that is merely present is not proof; it has to serve keys."""

    def test_missing_jwks_endpoint_fails(self, http: _FakeHttp) -> None:
        del http.routes[JWKS_URL]
        result = _make_check().execute()
        assert result["passed"] is False
        assert "HTTP 404 Not Found" in result["error"]
        assert JWKS_URL in result["error"]

    def test_empty_key_set_fails(self, http: _FakeHttp) -> None:
        http.routes[JWKS_URL] = {"keys": []}
        result = _make_check().execute()
        assert result["passed"] is False
        assert "contains no signing keys" in result["error"]

    @pytest.mark.parametrize("entries", [[None], [{}], [{"kty": ""}], [{"kty": 1}], ["RSA"]])
    def test_malformed_key_entries_fail(self, http: _FakeHttp, entries: list[Any]) -> None:
        http.routes[JWKS_URL] = {"keys": entries}
        result = _make_check().execute()
        assert result["passed"] is False
        assert "contains no signing keys" in result["error"]

    def test_only_valid_key_entries_are_counted(self, http: _FakeHttp) -> None:
        http.routes[JWKS_URL] = {"keys": [{}, *VALID_JWKS["keys"], None]}
        result = _make_check().execute()
        assert result["passed"] is True
        assert "1 signing key(s)" in result["output"]

    def test_jwks_without_keys_field_fails(self, http: _FakeHttp) -> None:
        http.routes[JWKS_URL] = {"unexpected": True}
        result = _make_check().execute()
        assert result["passed"] is False
        assert "contains no signing keys" in result["error"]

    def test_non_https_jwks_uri_fails(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = dict(VALID_OIDC_RESPONSE, jwks_uri="http://insecure.example.com/keys")
        result = _make_check().execute()
        assert result["passed"] is False
        assert "jwks_uri is not a valid HTTPS URL" in result["error"]

    def test_malformed_jwks_json_fails(self, http: _FakeHttp) -> None:
        http.routes[JWKS_URL] = b"not-json"
        result = _make_check().execute()
        assert result["passed"] is False
        assert "Failed to parse JWKS document as JSON" in result["error"]


class TestDiscoveryDocument:
    """The anonymously fetched discovery document must be complete and self-consistent."""

    def test_missing_required_fields(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = {"issuer": ISSUER, "jwks_uri": JWKS_URL}
        result = _make_check().execute()
        assert result["passed"] is False
        assert "missing required fields" in result["error"]
        assert "response_types_supported" in result["error"]

    def test_issuer_mismatch_fails(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = dict(VALID_OIDC_RESPONSE, issuer="https://attacker.example.com")
        result = _make_check().execute()
        assert result["passed"] is False
        assert "does not match the endpoint it was served from" in result["error"]

    def test_malformed_discovery_json_fails(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = b"not-json"
        result = _make_check().execute()
        assert result["passed"] is False
        assert "Failed to parse OIDC discovery document as JSON" in result["error"]

    def test_non_object_discovery_response_fails(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = []
        result = _make_check().execute()
        assert result["passed"] is False
        assert "must be a JSON object" in result["error"]

    def test_custom_required_fields(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = dict(VALID_OIDC_RESPONSE, custom_field="value")
        result = _make_check(config={"required_fields": ["issuer", "custom_field"]}).execute()
        assert result["passed"] is True

    def test_custom_required_fields_cannot_opt_out_of_jwks(self, http: _FakeHttp) -> None:
        http.routes[DISCOVERY_URL] = {k: v for k, v in VALID_OIDC_RESPONSE.items() if k != "jwks_uri"}
        result = _make_check(config={"required_fields": ["issuer"]}).execute()
        assert result["passed"] is False
        assert "jwks_uri is not a valid HTTPS URL" in result["error"]


class TestIssuerResolution:
    """kubectl supplies the issuer URL, which must be HTTPS and match exactly."""

    def test_trailing_slash_issuer_matches_exactly(self, http: _FakeHttp) -> None:
        aks_issuer = "https://eastus.oic.prod-aks.azure.com/TENANT/CLUSTER/"
        aks_jwks = "https://eastus.oic.prod-aks.azure.com/TENANT/CLUSTER/openid/v1/jwks"
        http.routes = {
            "https://eastus.oic.prod-aks.azure.com/TENANT/CLUSTER/.well-known/openid-configuration": dict(
                VALID_OIDC_RESPONSE, issuer=aks_issuer, jwks_uri=aks_jwks
            ),
            aks_jwks: VALID_JWKS,
        }
        result = _make_check(stdout=json.dumps({"issuer": aks_issuer})).execute()
        assert result["passed"] is True

    def test_trailing_slash_mismatch_fails(self, http: _FakeHttp) -> None:
        result = _make_check(stdout=json.dumps({"issuer": f"{ISSUER}/"})).execute()
        assert result["passed"] is False
        assert "does not match the endpoint it was served from" in result["error"]

    def test_kubectl_command_failure(self) -> None:
        result = _make_check(stdout="", exit_code=1, stderr="connection refused").execute()
        assert result["passed"] is False
        assert "Failed to query OIDC discovery endpoint" in result["error"]
        assert "connection refused" in result["error"]

    def test_kubectl_invalid_json(self) -> None:
        result = _make_check(stdout="not-json").execute()
        assert result["passed"] is False
        assert "Failed to parse OIDC discovery response" in result["error"]

    def test_kubectl_non_object_json(self) -> None:
        result = _make_check(stdout="[]").execute()
        assert result["passed"] is False
        assert "expected JSON object" in result["error"]

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
    """Malformed ``required_fields`` and ``http_timeout`` config fails the check."""

    def test_required_fields_single_string_is_accepted(self) -> None:
        result = _make_check(config={"required_fields": "issuer"}).execute()
        assert result["passed"] is True

    def test_required_fields_single_string_with_whitespace_is_trimmed(self) -> None:
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

    def test_http_timeout_is_passed_to_urlopen(self, http: _FakeHttp) -> None:
        result = _make_check(config={"http_timeout": 3}).execute()
        assert result["passed"] is True
        assert [r.timeout for r in http.seen] == [3, 3]
