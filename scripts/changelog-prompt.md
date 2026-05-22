<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Changelog backfill prompt

This prompt is invoked verbatim by `make changelog-fill` (via
`scripts/changelog-fill.sh`, which dispatches to `codex`, `claude`, or
`cursor-agent`). Edit it to tune output style/grouping; the target picks
up changes automatically.

---

You are filling in missing per-tag sections in `CHANGELOG.md` for the
NVIDIA ISV NCP Validation Suite repository.

## Goal

For every release version that is not yet documented in `CHANGELOG.md`,
add a complete `## [X.Y.Z] - YYYY-MM-DD` section, in descending version
order, immediately above the first existing `## [X.Y.Z]` heading (or at
the end of the file if no version sections exist yet). A version is
considered a release if either:

1. There is a git tag of the form `vX.Y.Z`, OR
2. The `version` field of the root `pyproject.toml` is newer than every
   git tag — this is a **pending release** that has been bumped but is
   not yet tagged (typically run as part of `make bump-*`).

Do not modify the file header, the "How to update this file" block, or
any version section that already has content.

## Steps

1. Read `CHANGELOG.md` and list every `## [X.Y.Z]` heading already present.
2. Run `git tag --sort=-v:refname` to list all release tags. Any tag of the
   form `vX.Y.Z` whose version is **not** already a heading in the file is
   missing. Also read the root `pyproject.toml` — if its `version = "X.Y.Z"`
   is newer than every git tag and not already a heading, treat it as a
   pending release.
3. For each missing version, in chronological order (oldest first):
   - For a **tagged release**, the commit range is `<prev_tag>..<tag>`
     (`git log --pretty='%H %s' <prev_tag>..<tag>`).
   - For a **pending release**, the commit range is `<latest_tag>..HEAD`
     (`git log --pretty='%H %s' <latest_tag>..HEAD`). Skip the bump
     commit itself (`chore: update package versions to X.Y.Z`).
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
4. Pick the section date:
   - For a **tagged release**: the tag's commit date,
     `git log -1 --format=%ad --date=short <tag>`.
   - For a **pending release**: today's date (UTC or local, your choice).

## Format

Group bullets by intent using these subsections, in this order (omit any
that are empty):

- `### Added` — new validations, providers, CLI commands, config options
  (`feat:` commits that introduce something new).
- `### Changed` — behavior or output changes downstream consumers may
  notice (`feat:` or `refactor:` commits that alter existing behavior).
- `### Fixed` — bug fixes worth calling out (`fix:` commits).
- `### Removed` — removed or deprecated functionality.
- `### Internal` — refactors, docs, tests, CI, and other non-user-facing
  changes (`refactor:`, `docs:`, `test:`, `chore:` commits).

### Bullet style by section

For **Added / Changed / Fixed / Removed**, use a two-line
bullet with a bold title, a linked PR reference, and a 1-2 sentence
description indented two spaces under the title:

```
- **Concise title summarizing the change** ([#N](https://github.com/NVIDIA/ISV-NCP-Validation-Suite/pull/N))
  One to two sentences explaining what changed and why. Describe the
  user-visible behavior — not the implementation — and reference the
  relevant validation ID, CLI flag, config key, or provider when useful.
```

For **Internal**, use a terse one-line form with a linked PR ref (no
bold title, no description paragraph):

```
- Brief description ([#N](https://github.com/NVIDIA/ISV-NCP-Validation-Suite/pull/N)).
```

### Roll-up entries

When several PRs are clearly part of one initiative (e.g. a sweeping
refactor or a multi-PR feature series), collapse them into a single
bullet with all PR refs inline and one shared description:

```
- **Common theme across the series** ([#A](https://github.com/NVIDIA/ISV-NCP-Validation-Suite/pull/A), [#B](https://github.com/NVIDIA/ISV-NCP-Validation-Suite/pull/B), [#C](https://github.com/NVIDIA/ISV-NCP-Validation-Suite/pull/C))
  One description that covers the whole series. Prefer this over three
  near-identical separate bullets.
```

### Always

- Skip the version-bump commit itself
  (`chore: update package versions to X.Y.Z`).
- Omit purely cosmetic chores (SPDX-header updates, lint-only changes,
  dependency lock-file refreshes) unless they have user-visible impact.
- If a commit has no PR (initial commit, direct push), omit the
  parenthetical entirely rather than inventing one.

## When done

Edit `CHANGELOG.md` in place and print a one-line summary of which tags
were added, e.g. `Added 3 sections: 0.6.7, 0.6.8, 0.7.0`. The maintainer
will review the diff before committing.
