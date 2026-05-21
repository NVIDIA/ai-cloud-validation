<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Changelog backfill prompt

This prompt is invoked verbatim by `make changelog-fill` (via
`scripts/changelog-fill.sh`, which dispatches to `cursor-agent`, `codex`,
or `claude`). Edit it to tune output style/grouping; the target picks up
changes automatically.

---

You are filling in missing per-tag sections in `CHANGELOG.md` for the
NVIDIA ISV NCP Validation Suite repository.

## Goal

For every git tag of the form `vX.Y.Z` that is **not** already documented
in `CHANGELOG.md`, add a complete `## [X.Y.Z] - YYYY-MM-DD` section, in
descending version order, between the existing `## [Unreleased]` block and
the next-newest documented version. Do not modify the file header, the
"How to update this file" block, the `## [Unreleased]` section, or any
version section that already has content.

## Steps

1. Read `CHANGELOG.md` and list every `## [X.Y.Z]` heading already present.
2. Run `git tag --sort=-v:refname` to list all release tags. Any tag of the
   form `vX.Y.Z` whose version is **not** already a heading in the file is
   missing.
3. For each missing tag, in chronological order (oldest first):
   - Find the previous tag with `git tag --sort=v:refname` and list its
     commits with
     `git log --pretty='%H %s' <prev_tag>..<tag>`.
   - Each commit subject ends with the PR number in parentheses, e.g.
     `(#425)`. Fetch the PR for richer context from
     `https://github.com/NVIDIA/ISV-NCP-Validation-Suite/pull/<N>` (use the
     `gh pr view <N>` CLI if available, otherwise an HTTP fetch). If the PR
     is inaccessible, fall back to reading the commit itself with
     `git show <hash>`.
   - For each PR, write a professional-grade description (max 2-3
     sentences) that helps consumers of the repo understand what changed
     and why. Avoid implementation jargon when a behavior description is
     clearer.
4. Pick the section date from the tag's commit date:
   `git log -1 --format=%ad --date=short <tag>`.

## Format

- Match the style of the existing `## [0.7.0]` section as the canonical
  example.
- Group bullets by intent using these subsections, in this order
  (omit any that are empty):
  - `### Added` — new validations, providers, CLI commands, config options
    (`feat:` commits that introduce something new).
  - `### Changed` — behavior or output changes downstream consumers may
    notice (`feat:` or `refactor:` commits that alter existing behavior).
  - `### Fixed` — bug fixes worth calling out (`fix:` commits).
  - `### Removed` — removed or deprecated functionality.
  - `### Security` — security-impacting fixes.
  - `### Internal` — refactors, docs, tests, CI, and other non-user-facing
    changes (`refactor:`, `docs:`, `test:`, `chore:` commits).
- End every bullet with `(#N)` so it auto-links on GitHub.
- Skip the version-bump commit itself
  (`chore: update package versions to X.Y.Z`).
- Omit purely cosmetic chores (SPDX-header updates, lint-only changes,
  dependency lock-file refreshes) unless they have user-visible impact.

## When done

Edit `CHANGELOG.md` in place and print a one-line summary of which tags
were added, e.g. `Added 3 sections: 0.6.7, 0.6.8, 0.7.0`. The maintainer
will review the diff before committing.
