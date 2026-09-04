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

"""Tests for version module."""

import subprocess
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest

from isvreporter.version import (
    BUILD_REF_ENV,
    _repository_root,
    build_is_release,
    build_ref,
    describe_checkout,
    get_version,
    parse_build_ref,
)


@pytest.fixture(autouse=True)
def _uncached_describe() -> Iterator[None]:
    """Drop the describe cache so each test starts from a cold lookup.

    The result is cached for the life of the process, and these tests run
    inside a checkout that would otherwise answer every one of them.
    """
    describe_checkout.cache_clear()
    yield
    describe_checkout.cache_clear()


def _describe(output: str) -> subprocess.CompletedProcess[str]:
    """Return a successful git-describe process result for *output*."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{output}\n", stderr="")


class TestGetVersion:
    """The base version is the installed metadata and nothing else."""

    def test_returns_metadata_version(self) -> None:
        with patch("isvreporter.version.version", return_value="1.2.3") as mock:
            assert get_version("isvreporter") == "1.2.3"
            mock.assert_called_once_with("isvreporter")

    def test_returns_dev_when_package_not_found(self) -> None:
        with patch("isvreporter.version.version", side_effect=PackageNotFoundError("nope")):
            assert get_version("nonexistent") == "dev"

    def test_the_checkout_never_changes_the_reported_version(self) -> None:
        """The regression this module was rewritten for.

        An earlier attempt folded the commit distance into the version itself
        (``0.9.0.post3+g08339c7``). Build provenance is a separate fact and
        travels in its own field; the version stays a plain release number that
        every consumer may read as one, whatever the tree is doing.
        """
        with patch("isvreporter.version._repository_root", return_value=Path("/repo")):
            with patch("subprocess.run", return_value=_describe("v0.9.0-3-g08339c7")):
                with patch("isvreporter.version.version", return_value="0.9.0"):
                    assert get_version("isvtest") == "0.9.0"

    def test_git_is_never_consulted_for_a_version(self) -> None:
        """Partners run air-gapped from copied trees; this path must not need git."""
        with patch("isvreporter.version.version", return_value="0.9.0"):
            with patch("subprocess.run", side_effect=AssertionError("git must not run")):
                assert get_version("isvtest") == "0.9.0"


class TestDescribeCheckout:
    """The checkout is reported verbatim, or not at all."""

    def test_reports_the_describe_output_unchanged(self) -> None:
        """The recipient gets the observation, not this module's reading of it."""
        with patch("isvreporter.version._repository_root", return_value=Path("/repo")):
            with patch("subprocess.run", return_value=_describe("v0.9.0-3-g08339c7")):
                assert describe_checkout() == "v0.9.0-3-g08339c7"

    def test_a_clean_tag_still_reports_its_distance(self) -> None:
        """--long keeps one shape to parse rather than two."""
        with patch("isvreporter.version._repository_root", return_value=Path("/repo")):
            with patch("subprocess.run", return_value=_describe("v0.10.0-0-g6634373")):
                assert describe_checkout() == "v0.10.0-0-g6634373"

    def test_no_checkout_is_the_ordinary_answer(self) -> None:
        """A wheel install, a copied tree, an air-gapped cluster."""
        with patch("isvreporter.version._repository_root", return_value=None):
            assert describe_checkout() is None

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("git not installed"),
            subprocess.CalledProcessError(128, "git"),
            subprocess.TimeoutExpired("git", 5),
        ],
    )
    def test_a_failed_lookup_reports_nothing_rather_than_breaking_the_run(self, failure: Exception) -> None:
        """No git, no tags, or a hung call must never fail a reporting call."""
        with patch("isvreporter.version._repository_root", return_value=Path("/repo")):
            with patch("subprocess.run", side_effect=failure):
                assert describe_checkout() is None

    def test_unparseable_output_reports_nothing(self) -> None:
        """A tag scheme this workspace does not use is not provenance."""
        with patch("isvreporter.version._repository_root", return_value=Path("/repo")):
            with patch("subprocess.run", return_value=_describe("some-other-scheme")):
                assert describe_checkout() is None

    def test_the_checkout_is_described_once_per_process(self) -> None:
        """Every package's lookup would otherwise spawn its own git."""
        with patch("isvreporter.version._repository_root", return_value=Path("/repo")):
            with patch("subprocess.run", return_value=_describe("v0.9.0-3-g08339c7")) as mock:
                describe_checkout()
                describe_checkout()
                describe_checkout()
                assert mock.call_count == 1


class TestBuildRef:
    """Where the build came from, when anything can say."""

    def test_the_environment_wins_over_the_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pipeline shipping into an air-gap knows what the copy cannot."""
        monkeypatch.setenv(BUILD_REF_ENV, "v0.9.0-0-gdeadbee")
        with patch("isvreporter.version.describe_checkout", side_effect=AssertionError("not consulted")):
            assert build_ref() == "v0.9.0-0-gdeadbee"

    def test_a_blank_variable_falls_through_to_the_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BUILD_REF_ENV, "   ")
        with patch("isvreporter.version.describe_checkout", return_value="v1.0.0-2-gabc1234"):
            assert build_ref() == "v1.0.0-2-gabc1234"

    def test_an_over_long_value_is_truncated_to_the_column(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Truncated rather than dropped: the leading part still identifies it."""
        monkeypatch.setenv(BUILD_REF_ENV, "x" * 500)
        assert build_ref() == "x" * 128

    def test_nothing_to_go_on_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(BUILD_REF_ENV, raising=False)
        with patch("isvreporter.version.describe_checkout", return_value=None):
            assert build_ref() is None


class TestParseBuildRef:
    """Splitting a reference, and declining to split what is not one."""

    def test_splits_a_description(self) -> None:
        assert parse_build_ref("v0.9.0-3-g08339c7") == ("0.9.0", 3, "08339c7", False)

    def test_marks_a_dirty_tree(self) -> None:
        assert parse_build_ref("v0.9.0-0-g6634373-dirty") == ("0.9.0", 0, "6634373", True)

    @pytest.mark.parametrize("ref", [None, "", "release-42", "built by hand on tuesday"])
    def test_anything_else_yields_no_detail(self, ref: str | None) -> None:
        """Operator-supplied free text is accepted upstream but carries no detail."""
        assert parse_build_ref(ref) is None

    def test_oversized_distance_yields_no_detail(self) -> None:
        assert parse_build_ref(f"v0.9.0-{'9' * 5000}-g08339c7") is None


class TestBuildIsRelease:
    """True, False, and - the common case in the field - None."""

    def test_a_clean_tagged_commit_is_the_release(self) -> None:
        assert build_is_release("0.9.0", "v0.9.0-0-g6634373") is True

    def test_commits_past_the_tag_are_not(self) -> None:
        """The lab-42 case: 0.9.0 plus three commits is not 0.9.0."""
        assert build_is_release("0.9.0", "v0.9.0-3-g08339c7") is False

    def test_a_dirty_tree_is_not_the_release_even_on_the_tag(self) -> None:
        assert build_is_release("0.9.0", "v0.9.0-0-g6634373-dirty") is False

    def test_a_tag_disagreeing_with_the_metadata_is_not_the_release(self) -> None:
        """A stale install: the tree moved, the installed packages did not."""
        assert build_is_release("0.8.0", "v0.9.0-0-g6634373") is False

    @pytest.mark.parametrize("ref", [None, "built by hand on tuesday"])
    def test_nothing_to_go_on_is_neither_answer(self, ref: str | None) -> None:
        """None must never be read as False; that is what the digest is for."""
        assert build_is_release("0.9.0", ref) is None

    def test_an_absent_reference_is_not_silently_rediscovered(self) -> None:
        """Running inside a checkout must not answer for a caller that has none.

        The predicate takes the reference it is given; looking one up here would
        make the result depend on where the process happens to be running, which
        is the one thing this design refuses to rely on.
        """
        with patch("subprocess.run", side_effect=AssertionError("git must not run")):
            assert build_is_release("0.9.0", None) is None


class TestRepositoryRoot:
    """Which trees count as this workspace's own checkout."""

    @pytest.mark.parametrize(
        "install_path",
        [
            # A wheel in a virtualenv created inside the other repository.
            ".venv/lib/python3.12/site-packages/isvreporter/version.py",
            # `--target`, which lands anywhere and carries no segment to recognise.
            "libs/isvreporter/version.py",
        ],
    )
    def test_an_installed_copy_claims_no_checkout(self, tmp_path: Path, install_path: str) -> None:
        """A partner's tags must not be reported as ours.

        Installing into an environment under another repository is ordinary, and
        that repository's `v*` tag would describe just as cleanly as ours. The
        foreign repository is built for real, so the walk has something to find
        and the test fails if the module stops checking what it found.
        """
        foreign = tmp_path / "their-repo"
        (foreign / ".git").mkdir(parents=True)
        module = foreign / install_path
        module.parent.mkdir(parents=True)
        module.write_text("")

        with patch("isvreporter.version.__file__", str(module)):
            assert _repository_root() is None

    def test_a_source_tree_is_the_checkout(self, tmp_path: Path) -> None:
        """Including an editable install, whose files stay in the tree."""
        root = tmp_path / "client"
        package = root / "isvreporter" / "src" / "isvreporter"
        package.mkdir(parents=True)
        # The worktree spelling: .git as a file rather than a directory.
        (root / ".git").write_text("gitdir: /elsewhere\n")
        module = package / "version.py"
        module.write_text("")

        with patch("isvreporter.version.__file__", str(module)):
            assert _repository_root() == root.resolve()
