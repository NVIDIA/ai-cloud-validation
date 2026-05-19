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
"""Add Apache-2.0 SPDX license headers to all NVIDIA-authored source files.

Handles Python, Shell, YAML, and Terraform files.
Preserves shebangs, migrates legacy proprietary headers in place, and skips
files that already carry the correct Apache-2.0 header.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HEADER_LINES = [
    "# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.",
    "# SPDX-License-Identifier: Apache-2.0",
    "#",
    '# Licensed under the Apache License, Version 2.0 (the "License");',
    "# you may not use this file except in compliance with the License.",
    "# You may obtain a copy of the License at",
    "#",
    "# http://www.apache.org/licenses/LICENSE-2.0",
    "#",
    "# Unless required by applicable law or agreed to in writing, software",
    '# distributed under the License is distributed on an "AS IS" BASIS,',
    "# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
    "# See the License for the specific language governing permissions and",
    "# limitations under the License.",
]

HEADER_TEXT = "\n".join(HEADER_LINES) + "\n"

# Legacy proprietary header that pre-dates the Apache-2.0 relicense. The year
# is wildcarded so all historical variants are migrated, and the proprietary
# boilerplate paragraph is optional so partial/orphan blocks are also caught.
LEGACY_PROPRIETARY_RE = re.compile(
    r"(?m)^# SPDX-FileCopyrightText: Copyright \(c\) \d{4} NVIDIA CORPORATION & AFFILIATES\. All rights reserved\.\n"
    r"# SPDX-License-Identifier: LicenseRef-NvidiaProprietary\n"
    r"(?:\n"
    r"# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual\n"
    r"# property and proprietary rights in and to this material, related\n"
    r"# documentation and any modifications thereto\. Any use, reproduction,\n"
    r"# disclosure or distribution of this material and related documentation\n"
    r"# without an express license agreement from NVIDIA CORPORATION or\n"
    r"# its affiliates is strictly prohibited\.\n"
    r")?"
    r"\n?"
)

# Anchored to line start so the constant strings below don't false-positive
# on this script's own source.
LEGACY_MARKER_RE = re.compile(r"^# SPDX-License-Identifier: LicenseRef-NvidiaProprietary", re.MULTILINE)
APACHE_MARKER = "SPDX-License-Identifier: Apache-2.0"

SKIP_PATHS = {
    ".pre-commit-config.yaml",
    ".coderabbit.yaml",
    ".trivyignore.yaml",
}


def _git_ls_files() -> list[str]:
    """List all tracked + untracked-but-not-ignored files via git."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("git is required to enumerate repository files") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git ls-files failed: {exc.stderr.strip() or exc}") from exc

    return [line for line in result.stdout.splitlines() if line]


def find_files() -> list[Path]:
    """Find all NVIDIA-authored source files that need SPDX headers.

    Uses ``git ls-files`` so that .gitignore patterns (e.g. .terraform/)
    are automatically respected.
    """
    files: list[Path] = []

    for rel_str in _git_ls_files():
        rel = Path(rel_str)

        if rel.as_posix() in SKIP_PATHS:
            continue

        if rel.parts and rel.parts[0] == ".github":
            continue

        ext = rel.suffix

        if ext in (".py", ".sh", ".tf", ".yaml", ".yml"):
            files.append(REPO_ROOT / rel)

    return sorted(files)


def has_apache_header(content: str) -> bool:
    """Return True if the file already carries the Apache-2.0 SPDX header."""
    return APACHE_MARKER in content


def has_legacy_header(content: str) -> bool:
    """Return True if the file still carries the pre-relicense proprietary header."""
    return LEGACY_MARKER_RE.search(content) is not None


def _strip_legacy_header(content: str) -> tuple[str, bool]:
    """Remove the legacy proprietary header block. Returns (new_content, removed)."""
    new_content, n = LEGACY_PROPRIETARY_RE.subn("", content, count=1)
    return new_content, n > 0


def _insert_header(content: str) -> str:
    """Insert the Apache-2.0 header, preserving a shebang line if present."""
    if not content.strip():
        return HEADER_TEXT

    lines = content.split("\n")
    if lines and lines[0].startswith("#!"):
        return lines[0] + "\n" + HEADER_TEXT + "\n" + "\n".join(lines[1:])
    return HEADER_TEXT + "\n" + content


def add_header(filepath: Path) -> bool:
    """Apply the Apache-2.0 SPDX header. Returns True if the file was modified."""
    content = filepath.read_text(encoding="utf-8")

    content, migrated = _strip_legacy_header(content)

    if has_apache_header(content):
        if migrated:
            filepath.write_text(content, encoding="utf-8")
            return True
        return False

    new_content = _insert_header(content)
    filepath.write_text(new_content, encoding="utf-8")
    return True


def check_headers(files: list[Path]) -> int:
    """Check files for missing/legacy SPDX headers. Returns count of offenders."""
    offenders = 0
    for fpath in files:
        rel = fpath.relative_to(REPO_ROOT)
        try:
            content = fpath.read_text(encoding="utf-8")
            if has_legacy_header(content):
                offenders += 1
                print(f"  ! {rel} - legacy proprietary header (must be migrated to Apache-2.0)")
            elif not has_apache_header(content):
                offenders += 1
                print(f"  ! {rel} - missing Apache-2.0 SPDX header")
        except Exception as e:
            offenders += 1
            print(f"  ! {rel} ERROR: {e}", file=sys.stderr)
    return offenders


def main() -> int:
    """Add SPDX headers to all source files, or check with --check."""
    parser = argparse.ArgumentParser(description="Manage Apache-2.0 SPDX license headers.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for missing/legacy headers without modifying files (exit 1 if any are found).",
    )
    args = parser.parse_args()

    files = find_files()
    print(f"Found {len(files)} source files to check\n")

    if args.check:
        offenders = check_headers(files)
        if offenders:
            print(f"\n{offenders} file(s) need attention. Run 'make update-spdx-headers' to fix.")
            return 1
        print("\nAll files carry the Apache-2.0 SPDX header.")
        return 0

    modified = 0
    skipped = 0
    errors = 0

    for fpath in files:
        rel = fpath.relative_to(REPO_ROOT)
        try:
            if add_header(fpath):
                modified += 1
                print(f"  + {rel}")
            else:
                skipped += 1
                print(f"  . {rel} (already has Apache-2.0 header)")
        except Exception as e:
            errors += 1
            print(f"  ! {rel} ERROR: {e}", file=sys.stderr)

    print(f"\nDone: {modified} modified, {skipped} skipped, {errors} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
