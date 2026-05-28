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

"""Tests for check_md_links.py."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "check_md_links", Path(__file__).resolve().parent.parent / "check_md_links.py"
)
assert _spec and _spec.loader
check_md_links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_md_links)


def test_find_markdown_files_returns_only_git_tracked_markdown(tmp_path: Path) -> None:
    """Untracked local notes should not make the repo-wide pre-commit hook fail."""
    tracked = tmp_path / "tracked.md"
    tracked.write_text("[ok](target.md)\n")
    (tmp_path / "scratch.md").write_text("[broken](missing.md)\n")

    result = subprocess.CompletedProcess(
        args=["git", "ls-files", "*.md"],
        returncode=0,
        stdout="tracked.md\n",
        stderr="",
    )
    with patch.object(check_md_links.subprocess, "run", return_value=result) as run:
        assert check_md_links.find_markdown_files(tmp_path) == [tracked]

    run.assert_called_once_with(
        ["git", "ls-files", "*.md"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )


def test_check_links_ignores_markdown_links_inside_fenced_code(tmp_path: Path) -> None:
    """Examples in fenced code blocks are code, not document links."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "\n".join(
            [
                "```markdown",
                "[example](missing.md)",
                "```",
                "[real link](target.md)",
            ]
        )
    )
    (tmp_path / "target.md").write_text("# Target\n")

    assert check_md_links.check_links(tmp_path, [doc]) == []


def test_check_links_requires_closing_fence_line_to_be_only_fence_chars(tmp_path: Path) -> None:
    """Fence-prefixed example lines with info strings do not close the code block."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "\n".join(
            [
                "````markdown",
                "[example](missing.md)",
                "````python",
                "[still example](still-missing.md)",
                "````",
                "[real link](target.md)",
            ]
        )
    )
    (tmp_path / "target.md").write_text("# Target\n")

    assert check_md_links.check_links(tmp_path, [doc]) == []
