<!--
SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Contributing to AI Cloud Validation Suite

Thank you for your interest in contributing! This project is a **Python monorepo** with three interdependent packages (`isvctl`, `isvtest`, `isvreporter`) managed as a [uv](https://docs.astral.sh/uv/) workspace. It orchestrates GPU cluster validation across Kubernetes, Slurm, and bare-metal environments, so even small changes can have cross-package effects. Please read through this guide before opening a pull request.

## Table of Contents

- [Issues Management](#issues-management)
- [Find Something to Work On](#find-something-to-work-on)
- [About This Codebase](#about-this-codebase)
- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development](#development)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Review Process](#review-process)
- [Developer Certificate of Origin (DCO)](#developer-certificate-of-origin-dco)
- [Releasing](#releasing)

## Issues Management

- Read the `README.md` to understand the project.
- Check existing issues to avoid duplicates.
- Browse [Discussions](https://github.com/NVIDIA/ai-cloud-validation/discussions) for questions.
- Review the [security policy](SECURITY.md) for security-related contributions.

Ways to contribute:

- 🐛 Report bugs via GitHub issues
- 💡 Suggest features through feature requests
- 📝 Improve documentation
- 🧪 Add tests to increase coverage
- 🔧 Fix issues with code contributions
- 💬 Help others in discussions

### Reporting Issues

When reporting issues:

- Use the issue templates when available.
- Provide clear reproduction steps.
- Include environment details (OS, Kubernetes version, etc.).
- Add relevant logs or error messages.
- Search existing issues first to avoid duplicates.

## Find Something to Work On

- Browse [`good first issue`](https://github.com/NVIDIA/ai-cloud-validation/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) for beginner-friendly tasks, or [`help wanted`](https://github.com/NVIDIA/ai-cloud-validation/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22) for slightly larger ones.
- **Claiming an issue:** comment on it to let others know you're taking it, so effort isn't duplicated.
- Not sure where to start? Adding a provider stub via the [my-isv scaffold](isvctl/configs/providers/my-isv/scripts/README.md) with `make demo-test` is a self-contained first contribution that needs no real cloud hardware.

## About This Codebase

AI Cloud Validation suite is a monorepo with three packages:

| Package | Purpose |
|---------|---------|
| **isvctl** | CLI controller - orchestrates setup, test, and teardown phases via step-based configs |
| **isvtest** | Validation engine - pytest-based framework with dynamic test discovery |
| **isvreporter** | Results reporter - uploads test results to the ISV Lab Service API |

Changes often span packages. For example, adding a new validation involves `isvtest` (test class), `isvctl` (config schema / stubs), and possibly `isvreporter` (result format). Please consider cross-package impact when contributing.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Be respectful and inclusive in all interactions, help maintain a welcoming environment, and focus on constructive feedback in reviews. Please report unacceptable behavior to GitHub_Conduct@nvidia.com.

## Getting Started

### Prerequisites

- Linux (Ubuntu) or WSL2
- Git
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)

### Clone and Setup

```bash
git clone https://github.com/NVIDIA/ai-cloud-validation.git
cd ai-cloud-validation
uv sync
uvx pre-commit install
```

## Development

### Common Tasks

```bash
make help          # Show all available targets
make test          # Run tests for all packages
make lint          # Run linting on all packages
make format        # Format code on all packages
make pre-commit    # Run pre-commit on all packages
make build         # Build all packages
make clean         # Clean build artifacts
make plan          # Render docs/test-plan.yaml to AsciiDoc and interactive HTML
```

### Per-Package Development

```bash
cd isvtest  # or isvctl, isvreporter
uv sync
uvx pre-commit run -a
uv run pytest -m unit
uv build
```

### Running Tools

```bash
uv run isvctl --help
uv run isvtest --help
uv run isvreporter --help
```

### Code Quality

We use `ruff` for linting and formatting, and `pyright` for type checking. All code must include type annotations and docstrings (PEP 257).

```bash
uvx ruff check --fix    # Lint
uvx ruff format          # Format
uvx pyright              # Type check
```

Pre-commit hooks run automatically on commit. To run manually:

```bash
uvx pre-commit run -a
```

## Testing

All CI checks must pass before a PR can be merged.

### Unit Tests

```bash
# All packages
make test

# Specific package
uv --directory=isvtest run pytest tests/ -v
uv --directory=isvctl run pytest -v
uv --directory=isvreporter run pytest -v
```

### Integration Tests

Integration tests require access to a real cluster:

```bash
uv run isvctl test run -f isvctl/configs/suites/k8s.yaml
uv run isvctl test run -f isvctl/configs/providers/microk8s.yaml
uv run isvctl test run -f isvctl/configs/providers/minikube.yaml
uv run isvctl test run -f isvctl/configs/providers/k3s.yaml
```

See the [Local Development Guide](docs/guides/local-development.md) for MicroK8s, Minikube, and k3s setup.

## Pull Request Process

1. **Fork** the [upstream repository](https://github.com/NVIDIA/ai-cloud-validation) and create a branch from `main`.
2. **Make your changes** following the coding guidelines above.
3. **Run the full check suite** before opening the PR:

   ```bash
   make test && make lint
   ```

4. **Sign off all commits** (see [DCO](#developer-certificate-of-origin-dco) below).
5. **Open a pull request** with a clear description of what changed and why.

### PR Guidelines

- Provide a clear description of the problem and solution.
- Reference any related issues.
- Keep pull requests focused on a single change.
- Ensure all CI checks pass before requesting review.
- Be responsive to feedback and code review comments.
- Assign reviewer as `NCP ISV Lab Maintainer` - at least one engineer will review the PR.

## Review Process

Maintainers review PRs on a best-effort basis. Reviews are collaborative — suggested
changes are meant to help your contribution land, not to criticize. Once approved, a
maintainer will merge your PR. If you haven't heard back, feel free to ping the PR.

[PLACEHOLDER: team to confirm timeline] — e.g., an initial response within N business days.

## Developer Certificate of Origin (DCO)

This project requires the [Developer Certificate of Origin](https://developercertificate.org/) (DCO) process for all contributions. The DCO is a lightweight way for contributors to certify that they wrote or otherwise have the right to submit the code they are contributing.

### Signing Your Commits

Add a `Signed-off-by` line to every commit using the `-s` flag:

```bash
git commit -s -m "Your commit message"
```

This appends a line like:

```
Signed-off-by: Your Name <your@email.com>
```

**Tip:** Create a Git alias to always sign off:

```bash
git config --global alias.ci 'commit -s'
# Now use: git ci -m "Your commit message"
```

### Signing Off Multiple Commits

```bash
git rebase --signoff origin/main
```

### DCO Enforcement

All pull requests are automatically checked for DCO compliance via the DCO bot. Pull requests with unsigned commits cannot be merged until all commits are properly signed off.

### Full DCO Text

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## Releasing

### Versioning Policy

The project follows [semver](https://semver.org/) but is still pre-1.0
(experimental), and that frames how we bump:

- **Major** (`X.Y.Z` -> `(X+1).0.0`) - reserved for graduation out of the
  experimental `0.x` line. Do not bump until the project is no longer
  declared experimental.
- **Minor** (`X.Y.Z` -> `X.(Y+1).0`) - milestone releases: a coherent set of
  new features, a new domain coming online, or a behavior change worth
  flagging to downstream consumers.
- **Patch** (`X.Y.Z` -> `X.Y.(Z+1)`) - a decent batch of fixes/features/chores
  that has accumulated on `main` and is worth cutting, or an urgent fix that
  needs to ship on its own. An urgent fix for an older minor is cut from a
  maintenance branch instead of `main` (see [Out-of-Band
  Releases](#out-of-band-releases)).

Every checkout runs all validations wired by its configuration. A release tag
therefore freezes the complete test set in that commit, while `main` remains
the developer workflow and runs everything currently present there.

### Version Bumping

All packages share a single version. To bump:

```bash
make bump-patch             # 0.4.2 -> 0.4.3
make bump-minor             # 0.4.2 -> 0.5.0
make bump-major             # 0.4.2 -> 1.0.0
make bump VERSION=1.2.3     # Explicit version
```

The script updates all `pyproject.toml` files and runs `uv lock`.
[`CHANGELOG.md`](CHANGELOG.md) is populated separately by
`make changelog-fill` after the release tag exists (see the next section).

### Changelog

[`CHANGELOG.md`](CHANGELOG.md) is the canonical, per-tag changelog (Keep a
Changelog format), populated by `make changelog-fill` rather than by PR
authors. After cutting a release tag (typically via `make bump-*`), run:

```bash
make changelog-fill                # auto-detect (codex -> claude -> cursor-agent)
make changelog-fill CLI=codex      # explicit codex
make changelog-fill CLI=claude     # explicit claude
make changelog-fill CLI=cursor     # explicit cursor-agent
```

The chosen LLM CLI inspects `git log` and fetches PR details to generate
the new section, then edits `CHANGELOG.md` in place. Review the diff and
tidy any awkward wording before committing. The prompt lives in
[`scripts/changelog-prompt.md`](scripts/changelog-prompt.md) and the
dispatch logic in [`scripts/changelog-fill.sh`](scripts/changelog-fill.sh);
either can be tweaked without changing the Makefile.

For per-milestone stakeholder overviews (e.g. quarterly summaries),
`scripts/generate_release_notes.py` fetches issues and PRs attached to a
GitHub milestone — that is a separate tool with a different purpose.

### Creating a Release Tag

After bumping, open a PR, review, and merge. Then the repo maintainers will create a tag:

1. Go to **Actions** > **Create version tag** in GitHub
2. Enter the version (e.g. `1.0.0`, without leading `v`)
3. The workflow verifies all package versions, creates `v1.0.0`, then builds the
   catalog from that tagged commit and publishes it to staging
4. After production approval, the catalog is published to production

The workflow tags whichever branch it is dispatched from, and only `main` or a
`releases/**` branch is accepted. It will not re-cut an existing tag - tags are
never deleted, so a mistake means burning that version - and it refuses to tag a
commit that CI has not passed on. Since merges are squashed, that commit is a new
one no pull request tested, so wait for its CI to finish before dispatching.

### Out-of-Band Releases

To patch an older minor without shipping everything that has landed on `main`
since, cut a maintenance branch named `releases/<minor>.x` from the tag being
patched, and tag from there.

```bash
git switch -c releases/<minor>.x v<tag>     # once per minor, from the tag
git switch -c <topic> releases/<minor>.x    # then cherry-pick and bump
git cherry-pick -x <sha>
make bump-patch
```

Open the PR against the release branch, then dispatch **Create version tag**
with that branch selected.

Conventions:

- Fixes land on `main` first and are cherry-picked onto the release branch,
  never the reverse.
- The release branch is not merged back. Forward-port only the CHANGELOG
  section, not the version bump.
- `make bump-patch` derives the version from the nearest ancestor tag, so it
  increments the release branch's own line.
- Workflows run from the branch they are on, so a branch cut from an older tag
  carries that tag's CI config. Add `releases/**` to the `push` triggers in
  `.github/workflows/ci.yaml` on the branch, or its tip is never tested.
- Confirm the version is free first - a bump can reach `main` without a tag
  ever being cut, which burns the number.

## Project Structure

```text
ai-cloud-validation/
├── isvctl/           # Controller package
│   ├── configs/      # Config files and stub scripts
│   ├── src/isvctl/   # Source code
│   └── tests/        # Unit tests
├── isvtest/          # Validation framework
│   ├── src/isvtest/  # Source code
│   └── tests/        # Unit tests
├── isvreporter/      # Reporter package
│   ├── src/isvreporter/
│   └── tests/
└── docs/             # Documentation
```

## Related Documentation

- [Getting Started](docs/getting-started.md) - Installation and usage
- [Configuration](docs/guides/configuration.md) - Config file format and options
- [Local Development](docs/guides/local-development.md) - MicroK8s, Minikube, and k3s setup for local testing

## License

By contributing to this project, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
