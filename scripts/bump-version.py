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

"""Bump or verify the version in all workspace pyproject.toml files.

Bump keywords increment from the latest git tag (the last actual release),
falling back to pyproject.toml if no tags exist.

Usage:
    python scripts/bump-version.py patch          # v0.4.2 -> 0.4.3  (alias: fix)
    python scripts/bump-version.py minor          # v0.4.2 -> 0.5.0  (alias: feat)
    python scripts/bump-version.py major          # v0.4.2 -> 1.0.0
    python scripts/bump-version.py 1.2.3          # explicit version
    python scripts/bump-version.py --check 1.2.3  # verify they already match (for CI)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PYPROJECT_FILES = [
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "isvctl" / "pyproject.toml",
    REPO_ROOT / "isvtest" / "pyproject.toml",
    REPO_ROOT / "isvreporter" / "pyproject.toml",
]

CHANGELOG_FILL_SCRIPT = REPO_ROOT / "scripts" / "changelog-fill.sh"

VERSION_RE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)
# Official semver.org regex (https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string)
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

BUMP_ALIASES = {"fix": "patch", "feat": "minor"}


def _release_manifest_path() -> Path:
    """Return the committed released-test manifest path."""
    return REPO_ROOT / "isvtest" / "src" / "isvtest" / "released_tests.json"


def _load_release_manifest_version(manifest_path: Path) -> str:
    """Load and validate the released-test manifest version without package imports."""
    try:
        data = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid released test manifest JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("released test manifest must be a JSON object")

    version = data.get("version")
    tests = data.get("tests")
    if not isinstance(version, str) or not version:
        raise ValueError("released test manifest requires non-empty string field 'version'")
    if not isinstance(tests, list) or not all(isinstance(name, str) and name for name in tests):
        raise ValueError("released test manifest requires 'tests' to be a list of non-empty strings")

    return version


def _parse_core(version: str) -> list[int]:
    """Extract [major, minor, patch] integers from a semver string."""
    m = SEMVER_RE.match(version)
    if not m:
        return [int(x) for x in version.split(".")[:3]]
    return [int(m.group("major")), int(m.group("minor")), int(m.group("patch"))]


def _pyproject_version() -> str:
    """Read the current version from the root pyproject.toml."""
    text = PYPROJECT_FILES[0].read_text()
    match = VERSION_RE.search(text)
    if not match:
        print("error: no version field found in root pyproject.toml", file=sys.stderr)
        sys.exit(1)
    return match.group(2)


def _git_tag_version() -> str | None:
    """Get the latest version from git tags."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=REPO_ROOT,
        )
        if result.returncode == 0:
            return result.stdout.strip().lstrip("v")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _base_version() -> tuple[str, str]:
    """Determine the base version to bump from and its source.

    Prefers the latest git tag over pyproject.toml since the tag represents
    the last actual release (pyproject.toml may be stale).
    """
    tag = _git_tag_version()
    pyproject = _pyproject_version()

    if tag and SEMVER_RE.match(tag):
        return tag, "git tag"
    return pyproject, "pyproject.toml"


def _resolve_version(arg: str, base: str) -> str:
    """Resolve a bump keyword or explicit version string.

    For bump keywords (patch/fix, minor/feat, major), increments from *base*.
    For explicit versions, validates and returns as-is.
    """
    kind = BUMP_ALIASES.get(arg, arg)
    parts = [int(x) for x in base.split(".")]

    if kind == "patch":
        parts[2] += 1
    elif kind == "minor":
        parts[1] += 1
        parts[2] = 0
    elif kind == "major":
        parts[0] += 1
        parts[1] = 0
        parts[2] = 0
    else:
        version = arg.lstrip("v")
        if not SEMVER_RE.match(version):
            print(
                f"error: '{arg}' is not valid semver or bump keyword (patch/fix, minor/feat, major)",
                file=sys.stderr,
            )
            sys.exit(1)
        return version

    return ".".join(str(x) for x in parts)


def _confirm(base: str, base_source: str, new: str) -> None:
    """Show the planned change and ask for confirmation."""
    base_parts = _parse_core(base)
    new_parts = _parse_core(new)

    yellow = "\033[33m" if sys.stderr.isatty() else ""
    reset = "\033[0m" if sys.stderr.isatty() else ""

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

        # Non-zero trailing digits on major/minor bump (e.g. 0.4.2 -> 0.5.2 should be 0.5.0)
        if new_parts[0] != base_parts[0] and (new_parts[1] != 0 or new_parts[2] != 0):
            warnings.append(f"major bump should reset to {new_parts[0]}.0.0")
        elif new_parts[1] != base_parts[1] and new_parts[2] != 0:
            warnings.append(f"minor bump should reset to {new_parts[0]}.{new_parts[1]}.0")

    if new == base:
        print(f"Already at {base}, nothing to do.")
        sys.exit(0)

    print(f"  current:  {base} ({base_source})")
    print(f"  new:      {new}")

    if warnings:
        print(f"\n  {yellow}WARNING: {', '.join(warnings)}{reset}", file=sys.stderr)

    answer = input("\nProceed? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(1)


def bump(new_version: str) -> None:
    """Update version in all pyproject.toml files."""
    for path in PYPROJECT_FILES:
        text = path.read_text()
        match = VERSION_RE.search(text)
        if not match:
            print(
                f"warning: no version field found in {path.relative_to(REPO_ROOT)}",
                file=sys.stderr,
            )
            continue

        old_version = match.group(2)
        if old_version == new_version:
            print(f"  {path.relative_to(REPO_ROOT)}: already {new_version}")
            continue

        updated = VERSION_RE.sub(rf"\g<1>{new_version}\3", text, count=1)
        path.write_text(updated)
        print(f"  {path.relative_to(REPO_ROOT)}: {old_version} -> {new_version}")


def run_changelog_fill() -> None:
    """Invoke scripts/changelog-fill.sh to backfill the new version's CHANGELOG section.

    Best-effort: warns instead of failing the bump if changelog-fill exits
    non-zero (e.g. no LLM CLI installed). The maintainer can re-run
    `make changelog-fill` manually later.
    """
    if not CHANGELOG_FILL_SCRIPT.exists():
        print(
            f"  warning: {CHANGELOG_FILL_SCRIPT.relative_to(REPO_ROOT)} not found; skipping changelog fill",
            file=sys.stderr,
        )
        return

    print("\nRunning changelog-fill (this can take a few minutes)...")
    result = subprocess.run(["bash", str(CHANGELOG_FILL_SCRIPT)], cwd=REPO_ROOT, check=False)
    if result.returncode != 0:
        print(
            f"  warning: changelog-fill exited {result.returncode}; "
            "run `make changelog-fill` manually after fixing the issue",
            file=sys.stderr,
        )


def refresh_released_tests(new_version: str) -> None:
    """Refresh the released-test manifest from the current catalog."""
    try:
        from isvtest.catalog import build_catalog
        from isvtest.release_manifest import write_release_manifest
    except ImportError as exc:
        print(f"error: cannot refresh released tests: {exc}", file=sys.stderr)
        sys.exit(1)

    catalog = build_catalog(released_only=False)
    test_names = sorted(entry["name"] for entry in catalog)
    manifest_path = _release_manifest_path()
    write_release_manifest(new_version, test_names, manifest_path=manifest_path)
    print(f"  {manifest_path.relative_to(REPO_ROOT)}: {len(test_names)} released tests")


def check(expected: str) -> None:
    """Verify all pyproject.toml files already have the expected version."""
    if not SEMVER_RE.match(expected):
        print(f"error: '{expected}' is not valid semver (expected X.Y.Z)", file=sys.stderr)
        sys.exit(1)

    ok = True
    for path in PYPROJECT_FILES:
        text = path.read_text()
        match = VERSION_RE.search(text)
        actual = match.group(2) if match else "<not found>"
        rel = path.relative_to(REPO_ROOT)
        if actual != expected:
            print(f"  MISMATCH {rel}: {actual} (expected {expected})", file=sys.stderr)
            ok = False
        else:
            print(f"  ok {rel}: {actual}")

    manifest_path = _release_manifest_path()
    rel = manifest_path.relative_to(REPO_ROOT)
    try:
        manifest_version = _load_release_manifest_version(manifest_path)
    except (OSError, ValueError) as exc:
        print(f"  ERROR {rel}: unreadable ({exc})", file=sys.stderr)
        ok = False
    else:
        if manifest_version != expected:
            print(
                f"  MISMATCH {rel}: {manifest_version} (expected {expected})",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"  ok {rel}: {manifest_version}")

    if not ok:
        print(
            f"\nerror: run 'python scripts/bump-version.py {expected}' and merge a PR first.",
            file=sys.stderr,
        )
        sys.exit(1)
    print("\nAll versions match.")


def main() -> None:
    """Entry point."""
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        expected = sys.argv[2].lstrip("v")
        print(f"Checking for {expected}:")
        check(expected)
        return

    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        print(f"usage: {sys.argv[0]} [--check] <version>", file=sys.stderr)
        print("  version: X.Y.Z | patch | minor | major | fix | feat", file=sys.stderr)
        sys.exit(1)

    arg = sys.argv[1]
    base, base_source = _base_version()
    new_version = _resolve_version(arg, base)
    _confirm(base, base_source, new_version)

    print(f"\nBumping to {new_version}:")
    bump(new_version)
    refresh_released_tests(new_version)

    print("\nRunning uv lock...")
    subprocess.run(["uv", "lock"], cwd=REPO_ROOT, check=True)

    run_changelog_fill()

    print("\nDone. Review with 'git diff', then commit and open a PR.")


if __name__ == "__main__":
    main()
