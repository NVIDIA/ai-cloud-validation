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

"""Tests for `isvctl doctor --lab-id` (ISV Lab Service credential/lab check)."""

import io
import json
from collections.abc import Callable
from http.client import BadStatusLine
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request

import pytest
from isvreporter import auth
from typer.testing import CliRunner

from isvctl.cli.doctor import app
from isvctl.doctor.checks import lab as lab_checks
from isvctl.doctor.result import Status

runner = CliRunner()


def _json(payload: Any) -> io.BytesIO:
    """A urlopen-compatible response carrying ``payload`` as JSON."""
    return io.BytesIO(json.dumps(payload).encode())


def _http_error(req: Request, code: int) -> HTTPError:
    """An HTTPError for ``req`` with status ``code``."""
    return HTTPError(req.full_url, code, "err", None, None)  # type: ignore[arg-type]


def _reject_401(req: Request) -> io.BytesIO:
    """Handler that rejects every request with HTTP 401."""
    raise _http_error(req, 401)


def _token_then(lab_handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
    """Grant a token, then answer the lab request with ``lab_handler``."""
    return lambda req: _json({"access_token": "t"}) if req.full_url.endswith("/token") else lab_handler(req)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a complete, valid ISV Lab Service config."""
    monkeypatch.setenv("ISV_SERVICE_ENDPOINT", "https://isv.example")
    monkeypatch.setenv("ISV_SSA_ISSUER", "https://ssa.example")
    monkeypatch.setenv("ISV_CLIENT_ID", "id")
    monkeypatch.setenv("ISV_CLIENT_SECRET", "s3cret")


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, handler: Callable[[Request], Any]) -> list[Request]:
    """Route both the token and the lab request through ``handler``; return the requests seen."""
    seen: list[Request] = []

    def fake(request: Request, timeout: float = 0) -> Any:
        """Record ``request`` and delegate to ``handler``."""
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(auth, "urlopen", fake)
    monkeypatch.setattr(lab_checks, "urlopen", fake)
    return seen


def _statuses() -> list[Status]:
    """Run the lab check for lab 37 and return the row statuses."""
    return [r.status for r in lab_checks.check_lab(37).results]


def test_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid credentials and an accessible lab give two OK rows and well-formed requests."""
    seen = _patch_urlopen(monkeypatch, _token_then(lambda req: _json({"data": {}})))
    assert _statuses() == [Status.OK, Status.OK]

    token_req, lab_req = seen
    assert token_req.data is not None
    form = parse_qs(token_req.data.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["scope"] == ["create-isv-lab-test-run update-isv-lab-test-run"]
    assert (token_req.get_header("Authorization") or "").startswith("Basic ")
    assert lab_req.full_url == "https://isv.example/v1/labs/37"
    assert lab_req.get_header("Authorization") == "Bearer t"
    # Credentials must not be copied onto redirects (urllib only copies ``headers``).
    assert "Authorization" not in token_req.headers
    assert "Authorization" not in lab_req.headers


@pytest.mark.parametrize("value", ["host.example", "http://host.example", "https://[::1"])
@pytest.mark.parametrize("var", ["ISV_SSA_ISSUER", "ISV_SERVICE_ENDPOINT"])
def test_non_https_url_is_config_fail(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    """Scheme-less, plain-http and malformed URLs fail the config row before any request."""
    monkeypatch.setenv(var, value)
    seen = _patch_urlopen(monkeypatch, _reject_401)
    report = lab_checks.check_lab(37)
    assert [r.status for r in report.results] == [Status.FAIL, Status.SKIP, Status.SKIP]
    assert f"not an https URL: {var}" in report.results[0].message
    assert seen == []


def test_loopback_http_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain http to a loopback host is allowed for local test servers."""
    monkeypatch.setenv("ISV_SSA_ISSUER", "http://127.0.0.1:8080")
    monkeypatch.setenv("ISV_SERVICE_ENDPOINT", "http://localhost:8081")
    _patch_urlopen(monkeypatch, _token_then(lambda req: _json({"data": {}})))
    assert _statuses() == [Status.OK, Status.OK]


def _timeout(req: Request) -> io.BytesIO:
    """Handler that simulates a socket timeout."""
    raise TimeoutError("timed out")


def _bad_status_line(req: Request) -> io.BytesIO:
    """Handler that simulates a malformed HTTP status line (not an OSError)."""
    raise BadStatusLine("garbage")


@pytest.mark.parametrize(
    "handler",
    [_timeout, _bad_status_line, lambda req: _json([]), lambda req: _json({"access_token": 123})],
    ids=["timeout", "bad-status-line", "non-object-json", "non-string-token"],
)
def test_token_failures_are_fail(monkeypatch: pytest.MonkeyPatch, handler: Callable[[Request], Any]) -> None:
    """Timeouts and malformed token responses fail the token row and skip the lab row."""
    _patch_urlopen(monkeypatch, handler)
    assert _statuses() == [Status.FAIL, Status.SKIP]


def test_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset variable fails the config row, names it, and skips the rest."""
    monkeypatch.delenv("ISV_CLIENT_SECRET")
    report = lab_checks.check_lab(37)
    assert [r.status for r in report.results] == [Status.FAIL, Status.SKIP, Status.SKIP]
    assert "ISV_CLIENT_SECRET" in report.results[0].message


def test_bad_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected token request fails the token row and skips the lab row."""
    _patch_urlopen(monkeypatch, _reject_401)
    assert _statuses() == [Status.FAIL, Status.SKIP]


@pytest.mark.parametrize(("code", "text"), [(404, "not found"), (403, "forbidden"), (401, "rejected")])
def test_lab_errors(monkeypatch: pytest.MonkeyPatch, code: int, text: str) -> None:
    """Lab HTTP errors map to a FAIL row with a specific message."""

    def lab_error(req: Request) -> io.BytesIO:
        """Reject the lab request with ``code``."""
        raise _http_error(req, code)

    _patch_urlopen(monkeypatch, _token_then(lab_error))
    results = lab_checks.check_lab(37).results
    assert results[1].status == Status.FAIL
    assert text in results[1].message


@pytest.mark.parametrize("lab_handler", [_timeout, _bad_status_line], ids=["timeout", "bad-status-line"])
def test_lab_unreachable(monkeypatch: pytest.MonkeyPatch, lab_handler: Callable[[Request], Any]) -> None:
    """Socket errors and malformed responses on the lab request fail the lab row."""
    _patch_urlopen(monkeypatch, _token_then(lab_handler))
    assert _statuses() == [Status.OK, Status.FAIL]


def test_secret_never_in_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client secret never appears in CLI output."""
    _patch_urlopen(monkeypatch, _reject_401)
    result = runner.invoke(app, ["--check", "env", "--lab-id", "37", "--no-user-config", "--json"])
    assert result.exit_code == 1
    assert "s3cret" not in result.output


def test_lab_id_alone_runs_only_lab(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--lab-id` narrows the run to the lab check; `--check` adds categories back."""
    _patch_urlopen(monkeypatch, _token_then(lambda req: _json({})))

    alone = json.loads(runner.invoke(app, ["--lab-id", "37", "--no-user-config", "--json"]).output)
    assert [c["name"] for c in alone["categories"]] == ["lab"]

    with_env = json.loads(runner.invoke(app, ["--lab-id", "37", "--check", "env", "--no-user-config", "--json"]).output)
    assert [c["name"] for c in with_env["categories"]] == ["env", "lab"]


def test_skipped_rows_hidden_unless_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows blocked by an earlier failure only show with -v; the summary says how to list them."""
    _patch_urlopen(monkeypatch, _reject_401)

    quiet = runner.invoke(app, ["--lab-id", "37", "--no-user-config"], env={"COLUMNS": "200"}).output
    assert "skipped: no token" not in quiet
    assert "1 fail, 1 skip (-v to list skipped)" in quiet
    assert "0 ok" not in quiet

    verbose = runner.invoke(app, ["--lab-id", "37", "--no-user-config", "-v"], env={"COLUMNS": "200"}).output
    assert "skipped: no token" in verbose
    assert "-v to list skipped" not in verbose
