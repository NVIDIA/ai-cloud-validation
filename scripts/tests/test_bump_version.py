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

"""Tests for bump-version.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load bump-version.py as a module (filename has a hyphen so normal import won't work)
_spec = importlib.util.spec_from_file_location(
    "bump_version", Path(__file__).resolve().parent.parent / "bump-version.py"
)
assert _spec and _spec.loader
bump_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_version)


# ---------------------------------------------------------------------------
# _resolve_version
# ---------------------------------------------------------------------------
class TestResolveVersion:
    """Tests for bump keyword resolution."""

    @pytest.mark.parametrize(
        ("arg", "current", "expected"),
        [
            ("patch", "0.1.0", "0.1.1"),
            ("fix", "0.1.0", "0.1.1"),
            ("patch", "1.2.9", "1.2.10"),
            ("minor", "0.1.5", "0.2.0"),
            ("feat", "0.1.5", "0.2.0"),
            ("minor", "1.9.3", "1.10.0"),
            ("major", "0.1.0", "1.0.0"),
            ("major", "2.3.4", "3.0.0"),
        ],
    )
    def test_bump_keywords(self, arg: str, current: str, expected: str) -> None:
        assert bump_version._resolve_version(arg, current) == expected

    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ("1.2.3", "1.2.3"),
            ("v1.2.3", "1.2.3"),
            ("0.0.1", "0.0.1"),
        ],
    )
    def test_explicit_version(self, arg: str, expected: str) -> None:
        assert bump_version._resolve_version(arg, "0.0.0") == expected

    def test_invalid_version_exits(self) -> None:
        with pytest.raises(SystemExit):
            bump_version._resolve_version("abc", "0.1.0")


# ---------------------------------------------------------------------------
# bump + check (using temp pyproject files)
# ---------------------------------------------------------------------------
class TestBumpAndCheck:
    """Tests for bump() and check() against real files."""

    PYPROJECT_TEMPLATE = textwrap.dedent("""\
        [project]
        name = "test-pkg"
        version = "{version}"
    """)

    @pytest.fixture()
    def fake_pyprojects(self, tmp_path: Path) -> list[Path]:
        """Create 4 fake pyproject.toml files mirroring the real layout."""
        paths = []
        for name in ["root", "a", "b", "c"]:
            p = tmp_path / name / "pyproject.toml"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self.PYPROJECT_TEMPLATE.format(version="0.1.0"))
            paths.append(p)
        return paths

    @pytest.fixture()
    def fake_release_manifest(self, tmp_path: Path) -> Path:
        """Create a fake released-test manifest under the patched repo root."""
        path = tmp_path / "isvtest" / "src" / "isvtest" / "released_tests.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": "0.1.0", "tests": ["ExistingCheck"]}))
        return path

    def test_bump_updates_all_files(self, fake_pyprojects: list[Path], tmp_path: Path) -> None:
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
        ):
            bump_version.bump("1.0.0")

        for p in fake_pyprojects:
            assert 'version = "1.0.0"' in p.read_text()

    def test_bump_skips_already_matching(
        self, fake_pyprojects: list[Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
        ):
            bump_version.bump("0.1.0")

        out = capsys.readouterr().out
        assert "already 0.1.0" in out

    def test_check_passes_when_matching(
        self,
        fake_pyprojects: list[Path],
        fake_release_manifest: Path,
        tmp_path: Path,
    ) -> None:
        """Verify check() succeeds when project files and manifest versions match."""
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
        ):
            bump_version.check("0.1.0")

    def test_check_does_not_import_isvtest_package(
        self,
        fake_pyprojects: list[Path],
        fake_release_manifest: Path,
        tmp_path: Path,
    ) -> None:
        """The tag workflow runs --check on a checkout without installing isvtest."""
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
            patch.dict(sys.modules, {"isvtest": None, "isvtest.release_manifest": None}),
        ):
            bump_version.check("0.1.0")

    def test_check_fails_on_mismatch(
        self,
        fake_pyprojects: list[Path],
        fake_release_manifest: Path,
        tmp_path: Path,
    ) -> None:
        """Ensure check() exits when project and manifest versions do not match."""
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
        ):
            with pytest.raises(SystemExit):
                bump_version.check("9.9.9")

    def test_check_fails_on_manifest_mismatch(
        self,
        fake_pyprojects: list[Path],
        fake_release_manifest: Path,
        tmp_path: Path,
    ) -> None:
        """Ensure check() fails when the release manifest version mismatches."""
        fake_release_manifest.write_text(json.dumps({"version": "9.9.9", "tests": []}))
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
        ):
            with pytest.raises(SystemExit):
                bump_version.check("0.1.0")

    def test_check_fails_when_manifest_is_not_a_dict(
        self,
        fake_pyprojects: list[Path],
        fake_release_manifest: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A non-object manifest should fail cleanly, not raise AttributeError."""
        fake_release_manifest.write_text(json.dumps(["ExistingCheck"]))
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
        ):
            with pytest.raises(SystemExit):
                bump_version.check("0.1.0")

        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "JSON object" in err

    def test_check_fails_when_manifest_is_unreadable(
        self,
        fake_pyprojects: list[Path],
        fake_release_manifest: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A missing manifest is reported as ERROR (not MISMATCH)."""
        fake_release_manifest.unlink()
        with (
            patch.object(bump_version, "PYPROJECT_FILES", fake_pyprojects),
            patch.object(bump_version, "REPO_ROOT", tmp_path),
        ):
            with pytest.raises(SystemExit):
                bump_version.check("0.1.0")

        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "unreadable" in err

    def test_check_rejects_invalid_semver(self) -> None:
        with pytest.raises(SystemExit):
            bump_version.check("not-a-version")

    def test_refresh_released_tests_writes_current_catalog(self, tmp_path: Path) -> None:
        """Refreshing releases every catalog entry under the target version."""
        with (
            patch.object(bump_version, "REPO_ROOT", tmp_path),
            patch("isvtest.catalog.build_catalog", return_value=[{"name": "ZCheck"}, {"name": "ACheck"}]) as build,
            patch("isvtest.release_manifest.write_release_manifest") as write_manifest,
        ):
            bump_version.refresh_released_tests("1.2.3")

        build.assert_called_once_with(released_only=False)
        write_manifest.assert_called_once_with(
            "1.2.3",
            ["ACheck", "ZCheck"],
            manifest_path=tmp_path / "isvtest" / "src" / "isvtest" / "released_tests.json",
        )


# ---------------------------------------------------------------------------
# _git_tag_version
# ---------------------------------------------------------------------------
class TestGitTagVersion:
    """Tests for git tag lookup."""

    def test_returns_version_on_success(self) -> None:
        mock_result = MagicMock(returncode=0, stdout="v1.2.3\n")
        with patch.object(bump_version.subprocess, "run", return_value=mock_result):
            assert bump_version._git_tag_version() == "1.2.3"

    def test_returns_none_when_no_tags(self) -> None:
        mock_result = MagicMock(returncode=128, stdout="")
        with patch.object(bump_version.subprocess, "run", return_value=mock_result):
            assert bump_version._git_tag_version() is None

    def test_returns_none_when_git_missing(self) -> None:
        with patch.object(bump_version.subprocess, "run", side_effect=FileNotFoundError):
            assert bump_version._git_tag_version() is None


# ---------------------------------------------------------------------------
# _base_version
# ---------------------------------------------------------------------------
class TestBaseVersion:
    """Tests for base version resolution (git tag vs pyproject.toml)."""

    def test_prefers_git_tag_over_pyproject(self) -> None:
        with (
            patch.object(bump_version, "_git_tag_version", return_value="0.4.2"),
            patch.object(bump_version, "_pyproject_version", return_value="0.1.0"),
        ):
            version, source = bump_version._base_version()
            assert version == "0.4.2"
            assert source == "git tag"

    def test_falls_back_to_pyproject_when_no_tags(self) -> None:
        with (
            patch.object(bump_version, "_git_tag_version", return_value=None),
            patch.object(bump_version, "_pyproject_version", return_value="0.1.0"),
        ):
            version, source = bump_version._base_version()
            assert version == "0.1.0"
            assert source == "pyproject.toml"

    def test_falls_back_to_pyproject_when_tag_not_semver(self) -> None:
        with (
            patch.object(bump_version, "_git_tag_version", return_value="not-semver"),
            patch.object(bump_version, "_pyproject_version", return_value="0.1.0"),
        ):
            version, source = bump_version._base_version()
            assert version == "0.1.0"
            assert source == "pyproject.toml"


# ---------------------------------------------------------------------------
# _confirm warnings
# ---------------------------------------------------------------------------
class TestConfirmWarnings:
    """Tests for skip/downgrade/major warnings in _confirm."""

    def _collect_warnings(self, base: str, new: str) -> list[str]:
        """Run the warning-detection logic from _confirm and return the warnings list."""
        base_parts = bump_version._parse_core(base)
        new_parts = bump_version._parse_core(new)
        warnings: list[str] = []
        if new_parts < base_parts:
            warnings.append(f"DOWNGRADE from {base}")
        if new_parts[0] != base_parts[0]:
            warnings.append("MAJOR version change")
        if new_parts > base_parts:
            if new_parts[0] > base_parts[0] + 1:
                warnings.append(f"skipping major versions ({base_parts[0]} -> {new_parts[0]})")
            elif new_parts[0] == base_parts[0] and new_parts[1] > base_parts[1] + 1:
                warnings.append(f"skipping minor versions ({base_parts[1]} -> {new_parts[1]})")
            elif new_parts[:2] == base_parts[:2] and new_parts[2] > base_parts[2] + 1:
                warnings.append(f"skipping patch versions ({base_parts[2]} -> {new_parts[2]})")
            if new_parts[0] != base_parts[0] and (new_parts[1] != 0 or new_parts[2] != 0):
                warnings.append(f"major bump should reset to {new_parts[0]}.0.0")
            elif new_parts[1] != base_parts[1] and new_parts[2] != 0:
                warnings.append(f"minor bump should reset to {new_parts[0]}.{new_parts[1]}.0")
        return warnings

    def test_no_warnings_for_normal_patch(self) -> None:
        assert self._collect_warnings("0.4.2", "0.4.3") == []

    def test_no_warnings_for_normal_minor(self) -> None:
        assert self._collect_warnings("0.4.2", "0.5.0") == []

    def test_no_warnings_for_normal_major(self) -> None:
        w = self._collect_warnings("0.4.2", "1.0.0")
        assert "MAJOR version change" in w
        assert not any("skipping" in x for x in w)

    def test_warns_on_skipped_patch(self) -> None:
        w = self._collect_warnings("0.4.2", "0.4.4")
        assert any("skipping patch" in x for x in w)

    def test_warns_on_skipped_minor(self) -> None:
        w = self._collect_warnings("0.4.2", "0.6.0")
        assert any("skipping minor" in x for x in w)

    def test_warns_on_skipped_major(self) -> None:
        w = self._collect_warnings("1.0.0", "3.0.0")
        assert any("skipping major" in x for x in w)

    def test_warns_on_downgrade(self) -> None:
        w = self._collect_warnings("1.0.0", "0.9.0")
        assert any("DOWNGRADE" in x for x in w)

    def test_warns_on_minor_bump_with_nonzero_patch(self) -> None:
        w = self._collect_warnings("0.4.2", "0.5.2")
        assert any("should reset to 0.5.0" in x for x in w)

    def test_warns_on_major_bump_with_nonzero_minor(self) -> None:
        w = self._collect_warnings("0.4.2", "1.1.0")
        assert any("should reset to 1.0.0" in x for x in w)

    def test_warns_on_major_bump_with_nonzero_patch(self) -> None:
        w = self._collect_warnings("0.4.2", "1.0.1")
        assert any("should reset to 1.0.0" in x for x in w)

    def test_no_reset_warning_for_clean_minor(self) -> None:
        w = self._collect_warnings("0.4.2", "0.5.0")
        assert not any("should reset" in x for x in w)

    def test_no_reset_warning_for_clean_major(self) -> None:
        w = self._collect_warnings("0.4.2", "1.0.0")
        assert not any("should reset" in x for x in w)
