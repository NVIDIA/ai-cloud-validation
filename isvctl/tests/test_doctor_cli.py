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

"""Unit tests for the doctor CLI subcommand."""

import json
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from typer.testing import CliRunner

from isvctl.cli.doctor import app
from isvctl.config.env_catalog import EnvVar
from isvctl.doctor.checks import env as env_checks
from isvctl.doctor.checks import tools as tools_checks
from isvctl.doctor.result import Status, worst

runner = CliRunner()


class _JsonResponse:
    """Minimal context-manager response for urllib-based doctor tests."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def all_tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub shutil.which so every probed binary "exists" and skip version probes."""

    def fake_which(name: str) -> str:
        return f"/fake/bin/{name}"

    def fake_probe(executable: str, args: tuple[str, ...]) -> str:
        return f"{executable} 1.2.3"

    monkeypatch.setattr(tools_checks.shutil, "which", fake_which)
    monkeypatch.setattr(tools_checks, "_probe_version", fake_probe)


@pytest.fixture
def all_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every env var the doctor knows about so the env category is all-OK."""
    for var in env_checks.ENV_VARS:
        monkeypatch.setenv(var.name, "x")


@pytest.fixture
def all_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no known env var is set (independent of the host environment)."""
    for var in env_checks.ENV_VARS:
        monkeypatch.delenv(var.name, raising=False)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_doctor_help() -> None:
    """The doctor command should expose its help text."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Pre-flight diagnostics" in result.output


def test_doctor_unknown_check_value() -> None:
    """Unknown --check values should fail with Typer's usage exit code."""
    result = runner.invoke(app, ["--check", "bogus"])
    assert result.exit_code == 2
    assert "unknown --check value" in result.output


def test_doctor_all_pass_exits_zero(all_tools_present: None, all_env_set: None) -> None:
    """A clean doctor run should exit 0 and print the success summary."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "All checks passed." in result.output


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_doctor_missing_required_tool_fails(monkeypatch: pytest.MonkeyPatch, all_env_set: None) -> None:
    """A missing required base tool (uv) must produce exit 1."""

    def fake_which(name: str) -> str | None:
        return None if name == "uv" else f"/fake/bin/{name}"

    monkeypatch.setattr(tools_checks.shutil, "which", fake_which)
    monkeypatch.setattr(tools_checks, "_probe_version", lambda exe, args: f"{exe} x")

    result = runner.invoke(app, ["--check", "tools"])
    assert result.exit_code == 1
    assert "uv" in result.output
    assert "not found in PATH" in result.output


def test_doctor_provider_escalates_optional_tool(monkeypatch: pytest.MonkeyPatch, all_env_set: None) -> None:
    """--provider aws should turn missing terraform from WARN to FAIL."""

    def fake_which(name: str) -> str | None:
        return None if name == "terraform" else f"/fake/bin/{name}"

    monkeypatch.setattr(tools_checks.shutil, "which", fake_which)
    monkeypatch.setattr(tools_checks, "_probe_version", lambda exe, args: f"{exe} x")

    # Without --provider, only WARN → exit 0.
    result = runner.invoke(app, ["--check", "tools"])
    assert result.exit_code == 0
    # With --provider aws, terraform becomes required → exit 1.
    result = runner.invoke(app, ["--check", "tools", "--provider", "aws"])
    assert result.exit_code == 1


def test_doctor_aws_provider_no_credentials_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, all_tools_present: None, all_env_unset: None
) -> None:
    """--provider aws fails when no credential source (env, profile, or files) exists."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Point home at an empty dir so ~/.aws/{credentials,config} are absent.
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(app, ["--check", "env", "--provider", "aws"])
    assert result.exit_code == 1
    assert "AWS credentials" in result.output


def test_doctor_aws_provider_accepts_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, all_tools_present: None, all_env_unset: None
) -> None:
    """AWS_PROFILE (no static keys) must satisfy the credential check — no false FAIL."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AWS_PROFILE", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    result = runner.invoke(app, ["--check", "env", "--provider", "aws"])
    assert result.exit_code == 0, result.output
    assert "AWS credentials" in result.output


def test_doctor_nico_provider_accepts_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
    all_tools_present: None,
    all_env_unset: None,
) -> None:
    """--provider nico should accept the local bearer-token workflow."""
    secret = "nico-secret-token"
    monkeypatch.setenv("NICO_API_BASE", "http://127.0.0.1:8080/v2/org")
    monkeypatch.setenv("NICO_ORGANIZATION", "test-org")
    monkeypatch.setenv("NICO_SITE_ID", "site-1")
    monkeypatch.setenv("NICO_BEARER_TOKEN", secret)
    monkeypatch.setattr(env_checks, "_probe_nico_api", lambda org, site_id, api_base, token: True)

    result = runner.invoke(app, ["--check", "env", "--provider", "nico"])

    assert result.exit_code == 0, result.output
    assert "NICo auth" in result.output
    assert "bearer token configured" in result.output
    assert "NICo API" in result.output
    assert secret not in result.output


def test_doctor_nico_provider_accepts_oidc_credentials(
    monkeypatch: pytest.MonkeyPatch,
    all_tools_present: None,
    all_env_unset: None,
) -> None:
    """--provider nico should accept non-interactive OIDC client credentials."""
    secret = "nico-client-secret"
    monkeypatch.setenv("NICO_API_BASE", "http://127.0.0.1:8080/v2/org")
    monkeypatch.setenv("NICO_ORGANIZATION", "test-org")
    monkeypatch.setenv("NICO_SITE_ID", "site-1")
    monkeypatch.setenv("NICO_SSA_ISSUER", "https://issuer.example")
    monkeypatch.setenv("NICO_CLIENT_ID", "client-id")
    monkeypatch.setenv("NICO_CLIENT_SECRET", secret)
    monkeypatch.setattr(
        env_checks, "_resolve_nico_doctor_token", lambda: ("oidc-token", "OIDC client_credentials configured")
    )
    monkeypatch.setattr(env_checks, "_probe_nico_api", lambda org, site_id, api_base, token: True)

    result = runner.invoke(app, ["--check", "env", "--provider", "nico"])

    assert result.exit_code == 0, result.output
    assert "NICo auth" in result.output
    assert "OIDC client_credentials configured" in result.output
    assert "NICo API" in result.output
    assert secret not in result.output


def test_doctor_nico_provider_requires_auth(
    monkeypatch: pytest.MonkeyPatch,
    all_tools_present: None,
    all_env_unset: None,
) -> None:
    """--provider nico should fail before a run when neither auth path is configured."""
    monkeypatch.setenv("NICO_API_BASE", "http://127.0.0.1:8080/v2/org")
    monkeypatch.setenv("NICO_ORGANIZATION", "test-org")
    monkeypatch.setenv("NICO_SITE_ID", "site-1")

    result = runner.invoke(app, ["--check", "env", "--provider", "nico"])

    assert result.exit_code == 1
    assert "NICo auth" in result.output
    assert "NICO_BEARER_TOKEN" in result.output


def test_doctor_nico_provider_requires_api_base(
    monkeypatch: pytest.MonkeyPatch,
    all_tools_present: None,
    all_env_unset: None,
) -> None:
    """--provider nico should require callers to choose the NICo API base."""
    monkeypatch.setenv("NICO_ORGANIZATION", "test-org")
    monkeypatch.setenv("NICO_SITE_ID", "site-1")
    monkeypatch.setenv("NICO_BEARER_TOKEN", "nico-secret-token")

    result = runner.invoke(app, ["--check", "env", "--provider", "nico"])

    assert result.exit_code == 1
    assert "NICO_API_BASE" in result.output
    assert "unset (required)" in result.output
    assert "export NICO_API_BASE" in result.output
    assert "NICo API" in result.output
    assert "skipped until NICo config and auth are complete" in result.output


def test_doctor_nico_provider_labels_oidc_request_failure_as_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
    all_tools_present: None,
    all_env_unset: None,
) -> None:
    """A rejected token request is configured auth that failed, not missing config."""
    monkeypatch.setenv("NICO_API_BASE", "http://127.0.0.1:8080/v2/org")
    monkeypatch.setenv("NICO_ORGANIZATION", "test-org")
    monkeypatch.setenv("NICO_SITE_ID", "site-1")
    monkeypatch.setenv("NICO_SSA_ISSUER", "https://issuer.example")
    monkeypatch.setenv("NICO_CLIENT_ID", "client-id")
    monkeypatch.setenv("NICO_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        env_checks,
        "_resolve_nico_doctor_token",
        lambda: (_ for _ in ()).throw(RuntimeError("OIDC token request failed with HTTP 400: invalid_scope")),
    )

    result = runner.invoke(app, ["--check", "env", "--provider", "nico"])

    assert result.exit_code == 1
    assert "NICo auth" in result.output
    assert "auth failed" in result.output
    assert "token request failed" in result.output
    assert "NICo auth:           not configured" not in result.output


def test_doctor_nico_auth_reads_ssa_issuer_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor should resolve OIDC client_credentials from NICO_SSA_ISSUER."""
    seen: dict[str, str] = {}

    def fake_request_token(*, issuer_url: str, client_id: str, client_secret: str, scope: str) -> str:
        seen.update(
            {
                "issuer_url": issuer_url,
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            }
        )
        return "oidc-token"

    monkeypatch.delenv("NICO_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("NICO_SSA_ISSUER", "https://issuer.example")
    monkeypatch.setenv("NICO_CLIENT_ID", "client-id")
    monkeypatch.setenv("NICO_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("NICO_OIDC_SCOPE", "read:nico")
    monkeypatch.setattr(env_checks, "_request_nico_oidc_token", fake_request_token)

    token, label = env_checks._resolve_nico_doctor_token()

    assert token == "oidc-token"
    assert label == "OIDC client_credentials configured"
    assert seen == {
        "issuer_url": "https://issuer.example",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scope": "read:nico",
    }


def test_nico_oidc_token_request_reports_http_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issuer error bodies should be included because they usually explain HTTP 400."""
    body = b'{"error":"invalid_scope","error_description":"scope is not allowed"}'

    def fake_urlopen(request: Any, timeout: int):
        _ = timeout
        if request.full_url.endswith("/.well-known/openid-configuration"):
            return _JsonResponse({"token_endpoint": "https://issuer.example/oauth/token"})
        raise HTTPError(request.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(body))

    monkeypatch.setattr(env_checks, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="invalid_scope"):
        env_checks._request_nico_oidc_token(
            issuer_url="https://issuer.example",
            client_id="client-id",
            client_secret="client-secret",
            scope="bad-scope",
        )


def test_probe_nico_api_defaults_to_carbide_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor should probe legacy /carbide/ site paths by default."""
    seen: dict[str, str] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request, timeout: int = 10):
        seen["url"] = request.full_url
        return _Response()

    monkeypatch.delenv("NICO_API_NAME", raising=False)
    monkeypatch.setattr(env_checks, "urlopen", fake_urlopen)

    assert env_checks._probe_nico_api("ncx", "site-1", "http://127.0.0.1:8080/v2/org", "tok") is True
    assert seen["url"] == "http://127.0.0.1:8080/v2/org/ncx/carbide/site/site-1"


def test_probe_nico_api_honors_nico_api_name_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor should probe /nico/ site paths when NICO_API_NAME is set."""
    seen: dict[str, str] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request, timeout: int = 10):
        seen["url"] = request.full_url
        return _Response()

    monkeypatch.setenv("NICO_API_NAME", "nico")
    monkeypatch.setattr(env_checks, "urlopen", fake_urlopen)

    assert env_checks._probe_nico_api("ncx", "site-1", "http://127.0.0.1:8080/v2/org", "tok") is True
    assert seen["url"] == "http://127.0.0.1:8080/v2/org/ncx/nico/site/site-1"


def test_doctor_strict_flips_warnings_to_failure(all_tools_present: None, all_env_unset: None) -> None:
    """Without --strict, recommended-but-missing env vars only WARN."""
    result = runner.invoke(app, ["--check", "env"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--check", "env", "--strict"])
    assert result.exit_code == 1


def test_doctor_required_env_var_reports_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing required env vars should say required, not optional."""
    name = "ISVCTL_REQUIRED_FOR_TEST"
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        env_checks,
        "ENV_VARS",
        (
            EnvVar(
                name=name,
                group="Test",
                requirement=env_checks.Requirement.REQUIRED,
                hint="set it",
            ),
        ),
    )

    report = env_checks.check_env()

    assert report.results[0].status == Status.FAIL
    assert report.results[0].message == "unset (required)"


# ---------------------------------------------------------------------------
# Persisted user config integration
# ---------------------------------------------------------------------------


def test_doctor_applies_persisted_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """doctor should see vars persisted by `isvctl configure`, not just exports."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("NICO_API_BASE", raising=False)
    cfg_dir = tmp_path / "isvctl"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yml").write_text("nico:\n  api_base: https://nico.example.com\n")

    result = runner.invoke(app, ["--check", "env", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    env_results = {r["name"]: r for cat in payload["categories"] for r in cat["results"]}
    assert env_results["NICO_API_BASE"]["status"] == "OK"


def test_doctor_no_user_config_ignores_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--no-user-config should ignore persisted config files."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("NICO_API_BASE", raising=False)
    cfg_dir = tmp_path / "isvctl"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yml").write_text("nico:\n  api_base: https://nico.example.com\n")

    result = runner.invoke(app, ["--check", "env", "--json", "--no-user-config"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    env_results = {r["name"]: r for cat in payload["categories"] for r in cat["results"]}
    assert env_results["NICO_API_BASE"]["status"] != "OK"


# ---------------------------------------------------------------------------
# Category filtering
# ---------------------------------------------------------------------------


def test_doctor_check_filter_runs_only_requested(all_tools_present: None, all_env_set: None) -> None:
    """--check should render only the selected category."""
    result = runner.invoke(app, ["--check", "env"])
    assert result.exit_code == 0
    assert "1 category checked" in result.output
    # The tools and config categories should not have been rendered.
    assert "[✓] tools" not in result.output
    assert "[✓] config" not in result.output


# ---------------------------------------------------------------------------
# JSON output contract
# ---------------------------------------------------------------------------


def test_doctor_json_shape(all_tools_present: None, all_env_set: None) -> None:
    """JSON output should expose the stable top-level doctor contract."""
    result = runner.invoke(app, ["--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["overall_status"] == "OK"
    assert {c["name"] for c in payload["categories"]} == {"tools", "env", "config"}
    summary = payload["summary"]
    assert set(summary) == {"ok", "warn", "fail", "skip"}
    assert sum(summary.values()) > 0


def test_doctor_json_does_not_leak_env_values(monkeypatch: pytest.MonkeyPatch, all_tools_present: None) -> None:
    """Env values are sensitive — JSON output must only report set/unset state."""
    secret = "super-secret-token-1234567890"
    monkeypatch.setenv("ISV_CLIENT_SECRET", secret)
    monkeypatch.setenv("NGC_API_KEY", secret)

    result = runner.invoke(app, ["--check", "env", "--json"])
    assert result.exit_code == 0
    assert secret not in result.output


def test_doctor_json_detail_gated_by_verbose(all_tools_present: None, all_env_set: None) -> None:
    """Tool paths (in `detail`) must only appear in JSON when --verbose is set."""
    plain = runner.invoke(app, ["--check", "tools", "--json"])
    assert plain.exit_code == 0, plain.output
    for result in json.loads(plain.output)["categories"][0]["results"]:
        assert result["detail"] is None

    verbose = runner.invoke(app, ["--check", "tools", "--json", "--verbose"])
    assert verbose.exit_code == 0, verbose.output
    details = [r["detail"] for r in json.loads(verbose.output)["categories"][0]["results"]]
    assert any(d and "/fake/bin/" in d for d in details)


# ---------------------------------------------------------------------------
# Config validation path
# ---------------------------------------------------------------------------


def test_doctor_validates_real_provider_config(all_tools_present: None, all_env_set: None) -> None:
    """The shipped aws reference config must parse cleanly under the merger + schema."""
    repo_root = Path(__file__).resolve().parents[2]
    cfg = repo_root / "isvctl" / "configs" / "providers" / "aws" / "config" / "control-plane.yaml"
    assert cfg.exists(), f"fixture config missing: {cfg}"

    result = runner.invoke(app, ["--check", "config", "-f", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "merged and validated" in result.output


def test_doctor_reports_invalid_yaml(tmp_path: Path, all_tools_present: None, all_env_set: None) -> None:
    """A broken YAML payload should produce a FAIL row and exit 1."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("commands: [\n  not-closed\n")

    result = runner.invoke(app, ["--check", "config", "-f", str(bad)])
    assert result.exit_code == 1
    assert "merge failed" in result.output or "schema validation failed" in result.output


def test_doctor_unknown_provider_directory_fails(all_tools_present: None, all_env_set: None) -> None:
    """--provider <name> with no on-disk scripts/ directory must surface as FAIL."""
    result = runner.invoke(app, ["--check", "config", "--provider", "no-such-provider"])
    assert result.exit_code == 1
    assert "no-such-provider" in result.output


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_status_worst_priority() -> None:
    """The aggregate status priority should prefer blocking failures."""
    assert worst([Status.OK, Status.WARN, Status.SKIP]) == Status.WARN
    assert worst([Status.OK, Status.FAIL, Status.WARN]) == Status.FAIL
    assert worst([Status.SKIP, Status.OK]) == Status.OK
    assert worst([]) == Status.SKIP


def test_check_tools_returns_category_report() -> None:
    """Smoke: with shutil.which fully patched, every tool resolves OK."""
    with (
        patch.object(tools_checks.shutil, "which", lambda name: f"/fake/{name}"),
        patch.object(tools_checks, "_probe_version", lambda exe, args: "1.0"),
    ):
        report = tools_checks.check_tools()
    assert report.name == "tools"
    assert report.worst_status == Status.OK
    assert all(r.status == Status.OK for r in report.results)


def test_check_tools_reports_kubectl_client_git_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """kubectl structured output should render the actual client version."""

    def fake_run(*args, **kwargs):
        return tools_checks.subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='clientVersion:\n  gitVersion: "v1.36.0"\nkustomizeVersion: v5.8.1\n',
            stderr="",
        )

    monkeypatch.setattr(tools_checks.shutil, "which", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(tools_checks, "_kubectl_command", lambda: ["kubectl"])
    monkeypatch.setattr(tools_checks.subprocess, "run", fake_run)

    report = tools_checks.check_tools()
    kubectl = next(result for result in report.results if result.name == "kubectl")

    assert kubectl.status == Status.OK
    assert kubectl.message == "v1.36.0"
    assert kubectl.detail is not None
    assert "version: v1.36.0" in kubectl.detail
