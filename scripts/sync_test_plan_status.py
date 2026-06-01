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

"""Keep docs/test-plan.yaml issue annotations in sync with live GitHub state.

The plan references tracking issues as ``#123 (open)`` / ``#123 (closed)``.
Those annotations are *derived* data that rots whenever an issue is closed on
GitHub. This tool refreshes them from the live issue state so nobody has to
hand-maintain them.

Modes:

* ``--write`` - rewrite the annotations in place (surgical text edit; comments,
  ordering, and formatting are preserved).
* ``--check`` - CI drift guardrail: exit non-zero if the committed annotations
  disagree with live GitHub, printing the stale entries.

Requires the ``gh`` CLI authenticated against the repo. The text transform
itself is pure and unit-tested without network access.

Usage:
    python3 scripts/sync_test_plan_status.py --check
    python3 scripts/sync_test_plan_status.py --write
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = REPO_ROOT / "docs" / "test-plan.yaml"
GH_REPO = "NVIDIA/ISV-NCP-Validation-Suite"

# Matches a "#<num> (open|closed)" annotation, capturing the number and state.
ANNOTATION_RE = re.compile(r"#(\d+)(\s*\()(open|closed)(\))")
# Any "#<num>" reference, for collecting the issues to query.
ISSUE_REF_RE = re.compile(r"#(\d+)")


def collect_issue_numbers(text: str) -> set[int]:
    """Return every GitHub issue number referenced in the plan text."""
    return {int(n) for n in ISSUE_REF_RE.findall(text)}


def apply_states(text: str, states: dict[int, str]) -> tuple[str, list[tuple[int, str, str]]]:
    """Rewrite ``(open|closed)`` annotations to match ``states``.

    Returns the updated text and a list of ``(issue, old_state, new_state)``
    changes. Issues missing from ``states`` (e.g. could not be fetched) are
    left untouched.
    """
    changes: list[tuple[int, str, str]] = []

    def repl(m: re.Match[str]) -> str:
        num = int(m.group(1))
        old = m.group(3)
        new = states.get(num)
        if new and new != old:
            changes.append((num, old, new))
            return f"#{m.group(1)}{m.group(2)}{new}{m.group(4)}"
        return m.group(0)

    return ANNOTATION_RE.sub(repl, text), changes


def fetch_issue_states(numbers: set[int]) -> dict[int, str]:
    """Return ``{issue_number: "open"|"closed"}`` from live GitHub via ``gh``."""
    states: dict[int, str] = {}
    ordered = sorted(numbers)
    for i in range(0, len(ordered), 60):
        chunk = ordered[i : i + 60]
        aliases = " ".join(f"i{n}: issue(number:{n}){{number state}}" for n in chunk)
        owner, name = GH_REPO.split("/")
        query = f'query{{repository(owner:"{owner}",name:"{name}"){{{aliases}}}}}'
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"gh query failed: {result.stderr.strip()}")
        repo = json.loads(result.stdout).get("data", {}).get("repository", {})
        for node in repo.values():
            if node:
                states[node["number"]] = node["state"].lower()
    return states


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Fail if annotations are stale (CI guardrail).")
    mode.add_argument("--write", action="store_true", help="Rewrite annotations to match live GitHub.")
    args = parser.parse_args(argv)

    text = PLAN_PATH.read_text()
    states = fetch_issue_states(collect_issue_numbers(text))
    new_text, changes = apply_states(text, states)

    if not changes:
        print("OK: test-plan issue annotations match live GitHub.")
        return 0

    summary = "\n  ".join(f"#{n}: {old} -> {new}" for n, old, new in changes)
    if args.check:
        sys.stderr.write(f"Stale test-plan annotations ({len(changes)}); run `make sync-plan`:\n  {summary}\n")
        return 1

    PLAN_PATH.write_text(new_text)
    print(f"Updated {len(changes)} annotations:\n  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
