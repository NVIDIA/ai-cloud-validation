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

"""Tests for test_plan_yaml_to_adoc.py: live status icons and the bare-#N invariant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "test_plan_yaml_to_adoc", Path(__file__).resolve().parent.parent / "test_plan_yaml_to_adoc.py"
)
assert _spec and _spec.loader
adoc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adoc)


def _plan(github_issues: list[str]) -> dict[str, Any]:
    return {
        "domains": [
            {"components": [{"capabilities": [{"tests": [{"test_id": "X-1", "github_issues": github_issues}]}]}]}
        ]
    }


def test_fmt_gh_issues_renders_state_from_live_states() -> None:
    """Icons come from the live ``states`` arg, not from the YAML."""
    assert "check-circle" in adoc.fmt_gh_issues_adoc(["#40"], {40: "closed"})
    assert "exclamation-circle" in adoc.fmt_gh_issues_adoc(["#40"], {40: "open"})


def test_fmt_gh_issues_unknown_state_renders_plain_link() -> None:
    """When state is unknown (offline render), emit a plain link with no icon."""
    out = adoc.fmt_gh_issues_adoc(["#40"], {})
    assert "issues/40[#40]" in out
    assert "icon:" not in out


def test_fetch_issue_states_falls_back_when_gh_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make plan` must still work offline: gh failure yields an empty mapping."""

    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(adoc.subprocess, "run", boom)
    assert adoc.fetch_issue_states({1, 2}) == {}


def test_validate_rejects_stored_issue_state() -> None:
    """Storing open/closed in the YAML is rejected - state is derived at render time."""
    with pytest.raises(SystemExit):
        adoc.validate_test_plan(_plan(["#40 (closed)"]))


def test_validate_accepts_bare_issue_reference() -> None:
    """A bare '#N' reference is valid."""
    adoc.validate_test_plan(_plan(["#40"]))
