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

"""Tests for the Firebird security config and scripts.

API calls go through the fake-API harness (``iam_fake.FakeIam`` for service
accounts); OIDC discovery is stubbed in the script's namespace and the TLS probe
talks to an in-memory socket, so no test opens a real connection (the harness
fails any that tries). Script output feeds the real validation classes.
"""

from __future__ import annotations

import base64
import io
import json
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from isvtest.validations.capacity import CapacityReservationGroupingCheck
from isvtest.validations.iam import ServiceAccountCredentialCheck
from isvtest.validations.security import (
    AuditLogEntryCheck,
    AuditLogRetentionCheck,
    InsecureProtocolsCheck,
    LeastPrivilegePolicyCheck,
    MinimalRoleEnforcementCheck,
    OidcUserAuthCheck,
)

from .harness import FIREBIRD, PROJECT, SUITES, HttpError, Route, WithToken, config_steps, load, render, run, validate
from .iam_fake import TENANT, FakeIam

ISSUER = "https://auth.test/issuer"
OTHER_PROJECT = "project.B"


def _check(check_cls: type, output: dict[str, Any]) -> Any:
    """Run a security-suite validation class on step output."""
    return validate("security", check_cls, output)


# ── Config ────────────────────────────────────────────────────────────


def test_config_wires_suite_steps_and_leaves_gaps_unwired() -> None:
    """Only app rows are wired; BMC/KMS/mTLS/MFA/tenant-isolation gaps stay step_not_configured."""
    suite_steps = set()
    for group in yaml.safe_load((SUITES / "security.yaml").read_text())["tests"]["validations"].values():
        suite_steps.add(group.get("step"))
        suite_steps.update(check.get("step") for check in (group.get("checks") or {}).values())
    steps = config_steps("security", "security")

    assert set(steps) - {"sweep_leftovers", "preflight"} == {
        "sa_credential_test",
        "oidc_user_auth_test",
        "least_privilege_test",
        "audit_logging_test",
        "insecure_protocols_test",
        "capacity_reservation_grouping",
        "teardown",
    }
    assert set(steps) - {"sweep_leftovers", "preflight"} <= suite_steps
    names = list(steps)
    assert names[:2] == ["sweep_leftovers", "preflight"]
    assert steps["sweep_leftovers"].phase == steps["preflight"].phase == "setup"
    assert names[-1] == "teardown"
    assert "labels:" not in (FIREBIRD / "config" / "security.yaml").read_text()


def test_teardown_deletes_every_recorded_account_and_project() -> None:
    """Accounts and projects recorded by any step, failed or not, reach teardown."""
    outputs = {
        "sa_credential_test": {"success": False, "created_service_account_ids": ["service-account.1"]},
        "least_privilege_test": {
            "created_service_account_ids": ["service-account.2"],
            "created_project_ids": ["project.B"],
        },
        "audit_logging_test": {"created_service_account_ids": ["service-account.3"]},
    }

    assert render("security", "security", "teardown", outputs) == [
        "--service-account-ids=service-account.1,service-account.2,service-account.3",
        "--project-ids=project.B",
    ]
    assert render("security", "security", "teardown", {}) == ["--service-account-ids=", "--project-ids="]


# ── SEC03-01 service-account credential ──────────────────────────────


def test_sa_credential_authenticates_as_the_new_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The token from the account's credentials resolves to that account."""
    iam = FakeIam()
    code, out, api = run(monkeypatch, capsys, "security/sa_credential_test.py", iam.routes())

    assert code == 0, out
    assert out["identity"] == "service-account.1"
    assert out["created_service_account_ids"] == ["service-account.1"]
    assert api.tokens[-1] == "tok:service-account.1"
    assert out["credential_source"] == "long_lived_key" and out["expires_at"].endswith("Z")
    assert _check(ServiceAccountCredentialCheck, out)._passed


def test_sa_credential_records_the_account_when_login_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused login fails SEC03-01 but still hands the account to teardown."""
    routes = {**FakeIam().routes(), "POST /auth/token": 401}

    code, out, _ = run(monkeypatch, capsys, "security/sa_credential_test.py", routes, ["--retries", "2", "--wait", "0"])

    assert code == 1
    assert out["created_service_account_ids"] == ["service-account.1"]
    assert not _check(ServiceAccountCredentialCheck, out)._passed


# ── SEC01-01 OIDC ─────────────────────────────────────────────────────


def _b64(data: dict[str, Any]) -> str:
    """Encode a JWT segment."""
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _jwt(**claims: Any) -> str:
    """Return a signed-looking compact JWS for the run's token."""
    payload = {"iss": ISSUER, "aud": "account", "sub": "user-1", "exp": int(time.time()) + 300, **claims}
    signature = base64.urlsafe_b64encode(b"sig" * 20).rstrip(b"=").decode()
    return f"{_b64({'alg': 'RS256', 'kid': 'k1'})}.{_b64(payload)}.{signature}"


def _discovery(issuer: str = ISSUER, kids: tuple[str, ...] = ("k1",)) -> Any:
    """Return a fetch_json stand-in serving discovery and JWKS documents."""

    def fetch(url: str, timeout: int = 30) -> dict[str, Any]:
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return {"issuer": issuer, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}
        if url == f"{ISSUER}/protocol/openid-connect/certs":
            return {"keys": [{"kid": kid, "kty": "RSA"} for kid in kids]}
        raise AssertionError(f"unexpected fetch {url}")

    return fetch


def _claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload."""
    segment = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _oidc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    accepts: Any,
    fetch: Any = None,
    refusal: int = 401,
) -> tuple[int, dict[str, Any], list[str]]:
    """Run the OIDC probe with the run's token set to a JWT; return (code, output, tokens sent).

    ``accepts(sent, issued)`` models the API's verifier.
    """
    token = _jwt()
    sent_tokens: list[str] = []

    def me(_b: Any, _q: Any, sent: str | None) -> dict[str, Any]:
        sent_tokens.append(sent or "")
        if accepts(sent, token):
            return {"userId": "user.U"}
        raise HttpError(refusal)

    def prepare(module: Any) -> None:
        monkeypatch.setenv("FIREBIRD_BEARER_TOKEN", token)
        monkeypatch.setattr(module, "fetch_json", fetch or _discovery())

    code, out, _ = run(
        monkeypatch, capsys, "security/oidc_user_auth_test.py", {"GET /users/me": WithToken(me)}, prepare=prepare
    )
    return code, out, sent_tokens


def test_oidc_accepts_the_issued_token_and_rejects_every_tampered_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every enforced probe passes, flagged as signature evidence; the audience is reported not supported."""
    code, out, sent = _oidc(monkeypatch, capsys, lambda sent, token: sent == token)

    assert code == 0, out
    valid, bad_signature, *forged = sent
    assert bad_signature.rsplit(".", 1)[0] == valid.rsplit(".", 1)[0] != bad_signature
    original = _claims(valid)
    wrong_iss, expired, missing = (_claims(t) for t in forged)
    assert wrong_iss["iss"] != original["iss"]
    assert expired["exp"] < time.time() < original["exp"]
    assert "sub" in original and "sub" not in missing
    # Only the edited claim differs, and every forged token keeps the original signature.
    assert {k: v for k, v in wrong_iss.items() if k != "iss"} == {k: v for k, v in original.items() if k != "iss"}
    assert all(t.rsplit(".", 1)[1] == valid.rsplit(".", 1)[1] for t in forged)
    assert out["issuer_url"] == ISSUER and out["audience"] == "account"
    assert out["claim_rejection_evidence"] == "signature"
    for key in ("wrong_issuer_rejected", "expired_token_rejected", "missing_required_claim_rejected"):
        assert "not that it checks this claim" in out["tests"][key]["message"]

    # No forged wrong-audience token is sent: its signature failure would prove nothing about the audience.
    assert all(_claims(t).get("aud") == "account" for t in sent)
    assert out["tests"]["wrong_audience_rejected"] == {
        "passed": False,
        "supported": False,
        "error": UNSUPPORTED_AUDIENCE,
    }
    check = _check(OidcUserAuthCheck, out)
    assert not check._passed
    assert check._error == (  # the only failure
        "OIDC user auth tests failed: wrong_audience_rejected: " + UNSUPPORTED_AUDIENCE
    )


UNSUPPORTED_AUDIENCE = (
    "not verifiable: a tenant cannot mint a validly signed token "
    "for another audience, so audience enforcement cannot be proven from outside the API"
)


def test_oidc_fails_a_verifier_that_ignores_the_signature(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accepting a token whose signature or claims were altered fails the matching tests."""
    code, out, _ = _oidc(monkeypatch, capsys, lambda sent, token: bool(sent))

    assert code == 1
    for key in ("bad_signature_rejected", "wrong_issuer_rejected", "expired_token_rejected"):
        assert out["tests"][key]["passed"] is False
        assert "expected HTTP 401, got 200" in out["tests"][key]["error"]
    assert not _check(OidcUserAuthCheck, out)._passed


def test_oidc_does_not_count_a_403_as_a_rejection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """403 means the token was accepted and authorization refused; only 401 is a rejected token."""
    code, out, _ = _oidc(monkeypatch, capsys, lambda sent, token: sent == token, refusal=403)

    assert code == 1
    for key in ("bad_signature_rejected", "wrong_issuer_rejected", "expired_token_rejected"):
        assert "expected HTTP 401, got 403" in out["tests"][key]["error"]


def test_oidc_fails_a_verifier_that_skips_signature_checks_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad signature over the untouched claims is caught on its own."""
    code, out, _ = _oidc(monkeypatch, capsys, lambda sent, token: sent.rsplit(".", 1)[0] == token.rsplit(".", 1)[0])

    assert code == 1
    assert out["tests"]["bad_signature_rejected"]["passed"] is False
    assert out["tests"]["wrong_issuer_rejected"]["passed"] is True


@pytest.mark.parametrize(
    ("fetch", "error"),
    [
        (_discovery(issuer="https://evil.test/issuer"), "discovery names issuer"),
        (_discovery(kids=("other",)), "does not hold the token's key"),
    ],
)
def test_oidc_discovery_must_match_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fetch: Any, error: str
) -> None:
    """Discovery must name the token's issuer and a JWKS holding its signing key."""
    code, out, _ = _oidc(monkeypatch, capsys, lambda sent, token: sent == token, fetch)

    assert code == 1
    assert error in out["tests"]["discovery_and_jwks_reachable"]["error"]
    assert out["tests"]["valid_token_accepted"]["passed"] is True


def test_oidc_unreachable_issuer_fails_only_discovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable issuer fails discovery but the API probes still run."""

    def unreachable(url: str, timeout: int = 30) -> dict[str, Any]:
        raise OSError("connection refused")

    code, out, _ = _oidc(monkeypatch, capsys, lambda sent, token: sent == token, unreachable)

    assert code == 1
    assert "connection refused" in out["tests"]["discovery_and_jwks_reachable"]["error"]
    assert out["tests"]["expired_token_rejected"]["passed"] is True


def test_oidc_rejects_a_token_that_is_not_a_jws(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An opaque token cannot be probed; every subtest fails with the reason."""

    def prepare(module: Any) -> None:
        monkeypatch.setattr(module, "fetch_json", _discovery())

    code, out, _ = run(monkeypatch, capsys, "security/oidc_user_auth_test.py", {}, prepare=prepare)

    assert code == 1
    assert "compact JWS" in out["error"]
    assert not _check(OidcUserAuthCheck, out)._passed


# ── SEC04-01 / SEC04-02 least privilege ───────────────────────────────


def _lp_routes(iam: FakeIam, **overrides: Route) -> dict[str, Route]:
    """Return routes for the least-privilege probe; writes are denied (403) to account tokens."""

    def denied_to_accounts(_b: Any, _q: Any, token: str | None) -> dict[str, Any]:
        if iam.account_for(token):
            raise HttpError(403)
        raise AssertionError("the admin never calls the probe endpoints")

    base = f"/projects/{PROJECT}"
    return {
        **iam.routes(OTHER_PROJECT),
        "POST /projects": {"project": {"id": OTHER_PROJECT, "tenantId": TENANT}},
        f"POST {base}/compute/bms/bm.00000000000000000000000000/power-on": WithToken(denied_to_accounts),
        f"POST {base}/storage/fs": WithToken(denied_to_accounts),
        f"POST {base}/network/vpcs": WithToken(denied_to_accounts),
        **overrides,
    }


def test_least_privilege_passes_when_the_viewer_role_is_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Denied before the grant, allowed on A after it, denied on B and on every write."""
    iam = FakeIam()
    code, out, api = run(monkeypatch, capsys, "security/least_privilege_test.py", _lp_routes(iam))

    assert code == 0, out
    assert out["test_identity"] == "service-account.1" and out["allowed_resource"] == PROJECT
    assert out["created_project_ids"] == [OTHER_PROJECT]
    assert iam.roles[("service-account.1", PROJECT)] == ["VIEWER"]
    assert ("service-account.1", OTHER_PROJECT) not in iam.roles
    # The create probes carry an empty name, so they cannot create anything even if allowed.
    creates = {path: body for method, path, body, _ in api.calls if path.endswith(("/network/vpcs", "/storage/fs"))}
    assert creates == {
        f"/projects/{PROJECT}/network/vpcs": {"name": ""},
        f"/projects/{PROJECT}/storage/fs": {"name": ""},
    }
    assert _check(LeastPrivilegePolicyCheck, out)._passed
    assert _check(MinimalRoleEnforcementCheck, out)._passed


def test_least_privilege_fails_a_write_that_passes_authorization(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A VIEWER whose VPC create reaches validation (400) is not denied: SEC04-02 fails."""
    routes = _lp_routes(FakeIam(), **{f"POST /projects/{PROJECT}/network/vpcs": 400})

    code, out, _ = run(monkeypatch, capsys, "security/least_privilege_test.py", routes)

    assert code == 1
    assert "HTTP 400, expected 403" in out["tests"]["out_of_scope_network_denied"]["error"]
    assert out["tests"]["out_of_scope_compute_denied"]["passed"] is True
    assert not _check(MinimalRoleEnforcementCheck, out)._passed
    assert _check(LeastPrivilegePolicyCheck, out)._passed


def test_least_privilege_fails_when_access_does_not_follow_the_grant(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Project A readable without any grant, or project B readable too, fails SEC04-01."""
    iam = FakeIam()
    routes = _lp_routes(iam, **{f"GET /projects/{PROJECT}": {"project": {"id": PROJECT, "tenantId": TENANT}}})
    routes[f"GET /projects/{OTHER_PROJECT}"] = {"project": {"id": OTHER_PROJECT, "tenantId": TENANT}}

    code, out, _ = run(monkeypatch, capsys, "security/least_privilege_test.py", routes)

    assert code == 1
    assert "HTTP 200 before the VIEWER grant" in out["tests"]["policy_dimensions_user_based"]["error"]
    assert "200 on project.B" in out["tests"]["policy_dimensions_resource_based"]["error"]
    assert not _check(LeastPrivilegePolicyCheck, out)._passed


def test_least_privilege_records_the_account_when_login_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scoped account is recorded as soon as it exists, so teardown removes it after a failed login."""
    routes = _lp_routes(FakeIam(), **{"POST /auth/token": 401})

    code, out, _ = run(monkeypatch, capsys, "security/least_privilege_test.py", routes, ["--retries", "1"])

    assert code == 1
    assert out["created_service_account_ids"] == ["service-account.1"]
    assert render("security", "security", "teardown", {"least_privilege_test": out})[0] == (
        "--service-account-ids=service-account.1"
    )


def test_least_privilege_records_the_project_when_the_account_create_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Project B is recorded before the account is created, so teardown removes it."""
    routes = _lp_routes(FakeIam(), **{"POST /service-accounts": 403})

    code, out, _ = run(monkeypatch, capsys, "security/least_privilege_test.py", routes)

    assert code == 1
    assert out["created_project_ids"] == [OTHER_PROJECT] and out["created_service_account_ids"] == []
    assert all(t["passed"] is False for t in out["tests"].values())
    assert render("security", "security", "teardown", {"least_privilege_test": out})[1] == "--project-ids=project.B"


# ── SEC08 audit ───────────────────────────────────────────────────────


def _event(**fields: Any) -> dict[str, Any]:
    """Return an audit event."""
    return {
        "id": "audit.1",
        "state": "COMPLETED",
        "actorId": "service-account.admin",
        "source": "firebird-api",
        **fields,
    }


def _audit_routes(iam: FakeIam, *, retained: bool = True, logged: bool = True) -> dict[str, Route]:
    """Return audit routes: the retention window, recent events, and the account's CREATE event."""

    def audit(_b: Any, query: dict[str, list[str]]) -> dict[str, Any]:
        now = datetime.now(UTC)
        if "targetId" in query:
            sa_id = query["targetId"][0]
            if not logged or sa_id not in iam.accounts:
                return {"items": []}
            return {
                "items": [
                    _event(
                        targetKind="IAM",
                        targetId=sa_id,
                        operationAction="CREATE",
                        ts=now.strftime("%Y-%m-%dT%H:%M:%S.123Z"),
                    )
                ]
            }
        if "toTs" in query:
            old = (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"items": [_event(ts=old)] if retained else []}
        return {"items": [_event(ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"))] if logged else []}

    return {**iam.routes(), "GET /audit": audit}


def test_audit_reports_retention_and_fails_only_the_unsupported_entry_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retention passes; the entry check fails on source IP, user agent, and region alone."""
    iam = FakeIam()
    code, out, _ = run(monkeypatch, capsys, "security/audit_logging_test.py", _audit_routes(iam))

    assert code == 0, out
    assert out["created_service_account_ids"] == ["service-account.1"]
    assert out["retention_days_observed"] == 45
    unsupported = {k for k, t in out["tests"].items() if t.get("supported") is False}
    assert unsupported == {"audit_log_source_ip_present", "audit_log_user_agent_matches", "audit_log_region_matches"}
    assert all(out["tests"][k]["passed"] is False and "not supported" in out["tests"][k]["error"] for k in unsupported)
    assert all(t["passed"] for k, t in out["tests"].items() if k not in unsupported)

    assert _check(AuditLogRetentionCheck, out)._passed
    entry = _check(AuditLogEntryCheck, out)
    assert not entry._passed
    assert entry._error.count("not supported") == 3


@pytest.mark.parametrize(
    ("retained", "logged", "failing"),
    [
        (False, True, {"audit_log_retention_at_least_30_days"}),
        (True, False, {"audit_log_entry_found", "audit_log_trail_logging_enabled"}),
    ],
)
def test_audit_fails_missing_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], retained: bool, logged: bool, failing: set[str]
) -> None:
    """No event older than 30 days, or no event for the call, fails the matching subtests."""
    routes = _audit_routes(FakeIam(), retained=retained, logged=logged)
    code, out, _ = run(monkeypatch, capsys, "security/audit_logging_test.py", routes, ["--audit-timeout", "0"])

    assert code == 1
    failed = {k for k, t in out["tests"].items() if not t["passed"] and t.get("supported") is not False}
    assert failing <= failed
    if not retained:
        assert not _check(AuditLogRetentionCheck, out)._passed


@pytest.mark.parametrize("status", [404, 501])
def test_audit_skips_before_creating_anything_when_not_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    """A disabled audit API is a structured skip, taken before any account exists."""
    code, out, api = run(monkeypatch, capsys, "security/audit_logging_test.py", {"GET /audit": status})

    assert code == 0
    assert out["skipped"] is True and out["created_service_account_ids"] == []
    assert api.paths("POST") == []


# ── SEC13-02 insecure protocols ───────────────────────────────────────


def _alert() -> bytes:
    """Return a TLS handshake_failure alert record."""
    return b"\x15\x03\x01\x00\x02\x02\x28"


def _server_hello(version: int) -> bytes:
    """Return a ServerHello record choosing ``version``."""
    body = b"\x02\x00\x00\x26" + version.to_bytes(2, "big") + b"\x00" * 34
    return b"\x16" + version.to_bytes(2, "big") + len(body).to_bytes(2, "big") + body


class _FakeSocket:
    """In-memory socket: replies to a ClientHello by its version, or to an HTTP request."""

    def __init__(self, respond: Any, address: tuple[str, int]) -> None:
        """Answer through ``respond(address, sent)``."""
        self.respond, self.address, self.reply = respond, address, io.BytesIO()

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def settimeout(self, _t: float) -> None:
        """No-op."""

    def sendall(self, data: bytes) -> None:
        """Compute the reply to what the client sent."""
        self.reply = io.BytesIO(self.respond(self.address, data))

    def recv(self, n: int) -> bytes:
        """Return the next reply bytes."""
        return self.reply.read(n)


def _tls_server(accepted: tuple[int, ...] = (0x0303,), http: bool = False, silent: bool = False) -> Any:
    """Return a ``respond`` for a server accepting ``accepted`` TLS versions (and HTTP on 80 if ``http``)."""

    def respond(address: tuple[str, int], sent: bytes) -> bytes:
        if silent:
            raise TimeoutError()
        if address[1] == 80:
            if http:
                return b"HTTP/1.1 301 Moved Permanently\r\n"
            raise ConnectionRefusedError()
        version = int.from_bytes(sent[1:3], "big")
        return _server_hello(version) if version in accepted else _alert()

    return respond


def _insecure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    respond: Any,
    api_base: str = "https://api.test",
) -> tuple[int, dict[str, Any], list[tuple[str, int]]]:
    """Run the SEC13-02 probe against an in-memory server; return (code, output, addresses)."""
    addresses: list[tuple[str, int]] = []

    def create_connection(address: tuple[str, int], timeout: float | None = None) -> _FakeSocket:
        addresses.append(address)
        return _FakeSocket(respond, address)

    def prepare(_module: Any) -> None:
        monkeypatch.setenv("FIREBIRD_API_BASE", api_base)
        monkeypatch.delenv("EDGE_ENDPOINTS", raising=False)
        monkeypatch.setattr(socket, "create_connection", create_connection)

    code, out, _ = run(monkeypatch, capsys, "security/insecure_protocols_test.py", {}, prepare=prepare)
    return code, out, addresses


def test_insecure_protocols_pass_when_legacy_tls_and_http_are_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Alerts for SSLv3/TLS1.0/1.1, TLS1.2 answered, port 80 closed: SEC13-02 passes."""
    code, out, addresses = _insecure(monkeypatch, capsys, _tls_server())

    assert code == 0, out
    assert set(addresses) == {("api.test", 443), ("api.test", 80)}
    assert out["endpoints_tested"] == 1
    assert _check(InsecureProtocolsCheck, out)._passed


@pytest.mark.parametrize(
    ("respond", "failing"),
    [
        (_tls_server(accepted=(0x0301, 0x0303)), "tlsv1_0_disabled"),
        (_tls_server(accepted=(0x0300, 0x0303)), "sslv3_disabled"),
        (_tls_server(http=True), "plain_http_disabled"),
    ],
)
def test_insecure_protocols_fail_an_accepted_legacy_protocol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], respond: Any, failing: str
) -> None:
    """A ServerHello at a legacy version, or an HTTP banner on :80, fails that test."""
    code, out, _ = _insecure(monkeypatch, capsys, respond)

    assert code == 1
    assert out["tests"][failing]["passed"] is False
    assert not _check(InsecureProtocolsCheck, out)._passed


def test_insecure_protocols_fail_an_unreachable_endpoint_instead_of_passing_on_silence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Timeouts look like refusals to the shared prober; the TLS1.2 control makes them inconclusive."""
    code, out, _ = _insecure(monkeypatch, capsys, _tls_server(silent=True))

    assert code == 1
    assert out["tests"]["sslv3_disabled"]["passed"] is True  # the shared prober alone would pass this
    assert out["tests"]["endpoint_reachable"]["passed"] is False
    assert "inconclusive" in out["error"]
    assert not _check(InsecureProtocolsCheck, out)._passed


def test_insecure_protocols_probe_the_api_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An API base with an explicit port is probed on that port; plain HTTP stays on 80."""
    _, _, addresses = _insecure(monkeypatch, capsys, _tls_server(), api_base="https://api.test:8443/")

    assert set(addresses) == {("api.test", 8443), ("api.test", 80)}


# ── CAP04-01 capacity grouping ────────────────────────────────────────


def _grouping(output: dict[str, Any]) -> Any:
    """Run CapacityReservationGroupingCheck with the suite's min_resources (1)."""
    check = CapacityReservationGroupingCheck(config={"step_output": output, "min_resources": 1})
    check.run()
    return check


@pytest.mark.parametrize(
    ("pool", "passes"),
    [
        ([{"id": "bm.1", "tenantId": TENANT}, {"id": "bm.2", "tenantId": TENANT}], True),
        ([{"id": "bm.1", "tenantId": TENANT}, {"id": "bm.2", "tenantId": "tenant.other"}], False),
        ([], False),
    ],
)
def test_capacity_grouping_pins_the_pool_to_the_tenant(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pool: list[dict[str, Any]], passes: bool
) -> None:
    """The tenant pool is the reservation; a foreign or empty pool fails CAP04-01."""
    routes = {**FakeIam().routes(), "GET /compute/bms": {"items": pool}}

    code, out, _ = run(monkeypatch, capsys, "security/capacity_reservation_grouping.py", routes)

    assert code == 0
    assert out["reservation_id"] == out["account_id"] == TENANT
    assert [(r["account_id"], r["pinned"]) for r in out["resources"]] == [
        (bm["tenantId"], bm["tenantId"] == TENANT) for bm in pool
    ]
    assert out["pinned"] is out["isolation_enforced"] is passes
    # The pool is server-filtered by tenant: isolation is the API's filter, not observed.
    assert out["cross_tenant_observable"] is False
    assert _grouping(out)._passed is passes


# ── User-Agent on every outbound request ──────────────────────────────


class _Response(io.BytesIO):
    """A urlopen result: a readable body usable as a context manager."""


def _capture_urlopen(monkeypatch: pytest.MonkeyPatch, holder: Any, body: bytes) -> list[Any]:
    """Replace ``holder.urlopen`` with a stand-in that records each Request and answers ``body``."""
    seen: list[Any] = []

    def urlopen(request: Any, timeout: float = 0) -> _Response:
        seen.append(request)
        return _Response(body)

    monkeypatch.setattr(holder, "urlopen", urlopen)
    return seen


def test_oidc_discovery_fetch_names_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some identity-provider edges answer 403 to Python-urllib's default User-Agent, so the client names itself."""
    module = load("security/oidc_user_auth_test.py")
    seen = _capture_urlopen(monkeypatch, module, b'{"issuer": "https://auth.example"}')

    assert module.fetch_json("https://auth.example/.well-known/openid-configuration") == {
        "issuer": "https://auth.example"
    }
    assert seen[0].get_header("User-agent") == module._fb.USER_AGENT
    assert "urllib" not in module._fb.USER_AGENT.lower()


def test_api_client_names_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every API request carries the provider's User-Agent."""
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    module = load("security/oidc_user_auth_test.py")
    seen = _capture_urlopen(monkeypatch, module._fb, b'{"ok": true}')

    assert module._fb.FirebirdClient()._send("GET", "/api/v1/users/me", None, token="t") == {"ok": True}
    assert seen[0].get_header("User-agent") == module._fb.USER_AGENT
