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

"""Tests for released test manifest helpers."""

import json
from pathlib import Path

import pytest

from isvtest.release_manifest import (
    INCLUDE_UNRELEASED_ENV,
    include_unreleased_tests_enabled,
    load_release_manifest,
    load_released_test_filter,
    load_released_tests,
    write_release_manifest,
)


def test_write_release_manifest_sorts_and_deduplicates(tmp_path: Path) -> None:
    """The generated manifest should be deterministic."""
    manifest_path = tmp_path / "released_tests.json"

    write_release_manifest("1.2.3", ["ZCheck", "ACheck", "ZCheck"], manifest_path)

    data = json.loads(manifest_path.read_text())
    assert data == {"version": "1.2.3", "tests": ["ACheck", "ZCheck"]}


def test_load_released_tests_returns_set(tmp_path: Path) -> None:
    """Released names are exposed as a set for fast membership checks."""
    manifest_path = tmp_path / "released_tests.json"
    manifest_path.write_text(json.dumps({"version": "1.2.3", "tests": ["ACheck"]}))

    assert load_released_tests(manifest_path) == {"ACheck"}


def test_load_released_test_filter_returns_none_when_unreleased_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev escape hatch disables release-manifest filtering."""
    manifest_path = tmp_path / "released_tests.json"
    manifest_path.write_text(json.dumps({"version": "1.2.3", "tests": ["ACheck"]}))
    monkeypatch.setenv(INCLUDE_UNRELEASED_ENV, "1")

    assert load_released_test_filter(manifest_path) is None


def test_include_unreleased_tests_enabled_accepts_common_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Truth-like environment values enable unreleased validations."""
    for value in ["1", "true", "TRUE", "yes", "on"]:
        monkeypatch.setenv(INCLUDE_UNRELEASED_ENV, value)
        assert include_unreleased_tests_enabled()


def test_include_unreleased_tests_enabled_rejects_falsey_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset or false-like environment values keep released-only behavior."""
    monkeypatch.delenv(INCLUDE_UNRELEASED_ENV, raising=False)
    assert not include_unreleased_tests_enabled()

    monkeypatch.setenv(INCLUDE_UNRELEASED_ENV, "0")
    assert not include_unreleased_tests_enabled()


def test_load_release_manifest_rejects_bad_shape(tmp_path: Path) -> None:
    """Malformed manifests should fail loudly."""
    manifest_path = tmp_path / "released_tests.json"
    manifest_path.write_text(json.dumps({"version": "1.2.3", "tests": [123]}))

    with pytest.raises(ValueError, match="'tests'"):
        load_release_manifest(manifest_path)
