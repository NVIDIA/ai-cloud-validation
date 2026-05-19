#!/usr/bin/env python3
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

"""Validate that relative links in markdown files point to existing targets.

Checks all [text](path) links in tracked .md files, resolving relative paths
from each file's directory.  External URLs (http/https/mailto) and anchor-only
links (#section) are skipped.

Exit codes:
    0  All links valid
    1  One or more broken links found
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")
SKIP_DIRS = {".git", ".venv", ".terraform", "node_modules", "__pycache__"}


def repo_root() -> Path:
    """Return the repository root via git."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def find_markdown_files(root: Path) -> list[Path]:
    """Find all tracked .md files, excluding generated directories."""
    files: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if any(part.startswith(".") for part in rel.parts) and rel.parts[0] != ".github":
            continue
        files.append(path)
    return files


def check_links(root: Path, md_files: list[Path]) -> list[tuple[Path, int, str]]:
    """Return list of (file, line_number, url) for broken relative links."""
    broken: list[tuple[Path, int, str]] = []
    for md in md_files:
        for lineno, line in enumerate(md.read_text(errors="replace").splitlines(), start=1):
            for url in LINK_RE.findall(line):
                if any(url.startswith(p) for p in SKIP_PREFIXES):
                    continue
                path_part = url.split("#")[0]
                if not path_part:
                    continue
                target = (md.parent / path_part).resolve()
                if not target.exists():
                    broken.append((md.relative_to(root), lineno, url))
    return broken


def main() -> int:
    root = repo_root()
    md_files = find_markdown_files(root)
    broken = check_links(root, md_files)

    if broken:
        print(f"Found {len(broken)} broken markdown link(s):\n")
        for path, lineno, url in broken:
            print(f"  {path}:{lineno}")
            print(f"    -> {url}\n")
        return 1

    print(f"All relative links OK ({len(md_files)} files checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
