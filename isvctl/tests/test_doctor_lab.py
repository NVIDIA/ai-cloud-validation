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

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from typer.testing import CliRunner

from isvctl.cli.doctor import app
from isvctl.doctor.checks import lab as lab_checks
from isvctl.doctor.result import Status

runner = CliRunner()


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _http_error(url: str, code: int) -> HTTPError:
    return HTTPError(url, code, "err", None, None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ISV_SERVICE_ENDPOINT", "https://isv.example")
    monkeypatch.setenv("ISV_SSA_ISSUER", "https://ssa.example")
    monkeypatch.setenv("ISV_CLIENT_ID", "id")
    monkeypatch.setenv("ISV_CLIENT_SECRET", "s3cret")


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, handler: Callable[[Request], Any]) -> list[str]:
    seen: list[str] = []

    def fake(request: Request, timeout: int = 0) -> Any:
        seen.append(request.full_url)
        return handler(request)

    monkeypatch.setattr(lab_checks, "urlopen", fake)
    return seen


def _statuses(lab_id: int = 37) -> list[Status]:
    return [r.status for r in lab_checks.check_lab(lab_id).results]


def test_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: Request) -> _Resp:
        return _Resp({"access_token": "t"} if req.full_url.endswith("/token") else {"data": {}})

    seen = _patch_urlopen(monkeypatch, handler)
    assert _statuses() == [Status.OK, Status.OK]
    assert seen[-1] == "https://isv.example/v1/labs/37"


def test_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ISV_CLIENT_SECRET")
    report = lab_checks.check_lab(37)
    assert [r.status for r in report.results] == [Status.FAIL, Status.SKIP, Status.SKIP]
    assert "ISV_CLIENT_SECRET" in report.results[0].message


def test_bad_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: Request) -> _Resp:
        raise _http_error(req.full_url, 401)

    _patch_urlopen(monkeypatch, handler)
    assert _statuses() == [Status.FAIL, Status.SKIP]


@pytest.mark.parametrize(("code", "text"), [(404, "not found"), (403, "forbidden"), (401, "rejected")])
def test_lab_errors(monkeypatch: pytest.MonkeyPatch, code: int, text: str) -> None:
    def handler(req: Request) -> _Resp:
        if req.full_url.endswith("/token"):
            return _Resp({"access_token": "t"})
        raise _http_error(req.full_url, code)

    _patch_urlopen(monkeypatch, handler)
    results = lab_checks.check_lab(37).results
    assert results[1].status == Status.FAIL
    assert text in results[1].message


def test_secret_never_in_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: Request) -> _Resp:
        raise _http_error(req.full_url, 401)

    _patch_urlopen(monkeypatch, handler)
    result = runner.invoke(app, ["--check", "lab", "--lab-id", "37", "--no-user-config", "--json"])
    assert result.exit_code == 1
    assert "s3cret" not in result.output


def test_cli_check_lab_requires_lab_id() -> None:
    result = runner.invoke(app, ["--check", "lab", "--no-user-config"])
    assert result.exit_code == 2
