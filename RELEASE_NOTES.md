<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Release Notes

User-visible changes per release. Format inspired by
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## How to update this file

- During your PR, add a one-line bullet under `## [Unreleased]` in the matching
  subsection. Reference the PR/issue in parentheses, e.g. `(#425)`.
- `make bump-*` rolls `## [Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD` and
  inserts a fresh `## [Unreleased]` placeholder. After bumping, re-read the
  new version section and tidy wording before opening the release PR.
- Empty subsections may be deleted; only `## [Unreleased]` and the version
  headings are required.

Suggested subsections, in this order:

- **Added** — new validations, providers, CLI commands, config options.
- **Changed** — behavior or output changes downstream consumers may notice.
- **Fixed** — bug fixes worth calling out.
- **Removed** — removed or deprecated functionality.
- **Security** — security-impacting fixes.
- **Internal** — refactors, docs, tests, CI, and other non-user-facing changes.

> For per-milestone stakeholder overviews (e.g. quarterly summaries), see
> `scripts/generate_release_notes.py`, which queries GitHub for issues/PRs
> attached to a milestone. The file you are reading now is the canonical,
> manually-curated per-tag changelog.

## [Unreleased]

### Added

### Changed

### Fixed

## [0.7.0] - 2026-05-21

### Added

- Virtual device hardening validation (`CNP01-17 M1-P1`) (#413).
- Per-host status log validation (`BMAAS-XX-07 M1-P1`) (#407).
- BIOS baseline validation (`ATTEST-XX-02`) (#410).
- Tenant isolation validation (`SEC11-01`) (#392).
- Short-lived credentials validation (`SEC02-01 M5-P0`) (#390).
- Key-management validations (`SEC09-01/02/03 M5-P1`) (#394).
- SDN logging validations (`SDN09-01/02/03 M5-P1`) (#395).
- Additional security checks (`SEC04`, `SEC08`) (#398).
- `isvctl catalog list` command for inspecting the validation catalog (#397).
- Test plan YAML and AsciiDoc generation script, wired into `make plan` (#402).

### Changed

- Validations are now gated by `isvtest/src/isvtest/released_tests.json`; new
  checks are no-ops in client configs until they appear in the manifest at
  release time (#391). Set `ISVTEST_INCLUDE_UNRELEASED=1` to opt in locally.
- Unified validation resolution pipeline for deterministic phase ordering (#405).
- K8s validations and helpers now parse structured JSON instead of text
  output, giving stricter contracts (#415).
- `K8sApiNetworkAclCheck` no longer depends on the CAPI provider (#406).
- Failed step output now surfaces useful stderr instead of swallowing it (#399).

### Fixed

- AWS VM validation regressions (#424).
- Skip Jinja re-rendering of pre-resolved configs to avoid double-evaluation (#408).
- Restrict the EKS API endpoint allowlist (#400).
- Scope K8s node counts to the configured test pool (#396).
- Surface SSH diagnostics during verbose deploy runs (#403).

### Internal

- Migrate GitHub issue templates to YAML forms (#416).
- Add Cursor Cloud–specific instructions to `AGENTS.md` (#422).
- Use `tmp_path` fixture for script-writing tests (#401).
- Colocate `bump-version` tests with the script (#393).
