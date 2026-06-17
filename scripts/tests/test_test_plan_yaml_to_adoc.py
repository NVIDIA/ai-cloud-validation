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

"""Tests for test_plan_yaml_to_adoc.py and the bare-#N github_issues invariant."""

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
    """Build a minimal test-plan dict containing one test's ``github_issues`` wiring."""
    return {
        "domains": [
            {"components": [{"capabilities": [{"tests": [{"test_id": "X-1", "github_issues": github_issues}]}]}]}
        ]
    }


def test_fmt_gh_issues_renders_plain_links() -> None:
    """Issue references render as plain GitHub links (no Asciidoctor icon macros)."""
    out = adoc.fmt_gh_issues_adoc(["#40", "#41"])
    assert out == (
        "https://github.com/NVIDIA/ai-cloud-validation/issues/40[#40] +\n"
        "https://github.com/NVIDIA/ai-cloud-validation/issues/41[#41]"
    )
    assert "icon:" not in out


def test_fmt_gh_issues_does_not_linkify_non_bare_references() -> None:
    """Issue references with suffix text are rendered as plain text."""
    assert adoc.fmt_gh_issues_adoc(["#40 extra"]) == "#40 extra"


def test_validate_rejects_non_bare_issue_reference() -> None:
    """Only bare '#N' issue references are valid in the YAML."""
    with pytest.raises(SystemExit):
        adoc.validate_test_plan(_plan(["#40 extra"]))


def test_validate_accepts_bare_issue_reference() -> None:
    """A bare '#N' reference is valid."""
    adoc.validate_test_plan(_plan(["#40"]))
