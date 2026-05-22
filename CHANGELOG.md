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

## [0.6.8] - 2026-04-30

### Added

- Serial console retention validation (`CNP06-02 M5-P0`) (#384).
- Customer-managed key validation (`SEC09-04 M5-P0`) (#387).
- Network fabric topology metadata validations (`NET01-01`, `NET02-02 M5-P0`) (#386).
- BMC bastion access validation (`SEC12-03 M5-P0`) (#385).
- BMC protocol security validation (`CNP10-01 M5-P0`) (#383).
- OIDC user authentication validation (`SEC01-01 M5-P0`) (#379).
- MFA enforcement validation (`SEC07-01 M5-P0`) (#378).
- VM console RBAC validation (`CNP01-16 M5-P0`) (#381).
- Service-level security group scoping validation (`SDN02-09 M5-P0`) (#380).
- BMC management network validation (`SEC12-01 M5-P0`) (#377).

### Fixed

- OIDC user authentication now skips cleanly when no issuer, audience, target, or token is configured (#389).

### Internal

- Refreshed agent guidance and moved generic rules into a Cursor rule file (#376).

## [0.6.7] - 2026-04-24

### Added

- CSI storage validation suite covering storage classes, quota APIs, tenant-scoped credentials, and dynamic/static provisioning (`K8S23 M5-P0`) (#372).
- Security validation domain plus workload, node, and subnet security group scoping checks (`M5-P0`) (#370).
- Kubernetes API endpoint network ACL validation (`K8S15-01 M5-P0`) (#373).
- Node-pool CRUD lifecycle validation for create, scale, and destroy flows (`K8S06 M5-P0`) (#371).

### Fixed

- Removed false-positive template warnings for future-phase steps, deselected validations, CAPI namespace defaults, and CSI inventory drift (#374).
- Hardened the AWS reference implementation around reboot proof, verified resource reuse, transient cleanup, public IP polling, and key-name sanitization (#369).
- Teardown steps now skip with a clear message when required `{{steps.X.Y}}` references are unavailable (#235).

## [0.6.6] - 2026-04-22

### Added

- CNCF Kubernetes conformance validation through a direct upstream e2e Pod (`K8S01`) (#234).
- Dual-stack node and NetworkPolicy enforcement validations (`K8S22`) (#318).
- `my-isv` living example, demo-mode end-to-end smoke tests, validation-only suite contracts, and a contract drift guard (#245).
- Kubernetes control-plane log retrieval validation (`K8S20`) (#317).
- `auto_assign_ip_mode` on `VpcIpConfigCheck` for non-AWS public-IP models (#236).
- OIDC issuer endpoint validation (`K8S18`) (#240).
- Kubernetes API server metrics availability validation (`K8S07`) (#239).
- `KUBECTL` environment override for kubectl-compatible CLI prefixes (#229).
- `metadata_headers` configuration for `CloudInitCheck` so non-AWS metadata services can be probed correctly (#194).

### Changed

- Reorganized provider configuration into `suites/` and `providers/<name>/{config,scripts}/` for clearer ownership boundaries (#367).
- VM and bare-metal lifecycle suites now share a post-lifecycle describe step so host checks validate the recovered system state (#238).

### Fixed

- Orchestration now deduplicates repeated validation names, writes artifacts to the root `_output/` directory, and emits Kubernetes control-plane namespace inventory (#357).
- `DriverCheck` falls back to `/usr/local/cuda/bin/nvcc` when CUDA is installed in NVIDIA's standard location but not on `PATH` (#237).
- Slurm validations skip unavailable partitions consistently and deploy forwarding now preserves quoted pytest args (#232).
- EKS opts into the cluster output schema explicitly instead of applying that schema to every generic `setup` step (#231).
- Missing Jinja step data is surfaced as a warning, and the EKS setup step now feeds real inventory into Kubernetes validations (#191).
- Teardown now runs after setup validation failures, continues best-effort after individual teardown failures, and supports standalone teardown runs (#193).

### Internal

- Centralized kubectl shell command construction for Kubernetes validations and workloads (#241).
- Documented recent validation, teardown, template-warning, and environment-variable changes across the user guides (#230).
- Updated the SPDX header script to use `git ls-files` so ignored and generated files are not scanned (#195).

## [0.6.5] - 2026-04-13

### Fixed

- Excluded superseded cluster checks from the uploaded catalog so Kubernetes coverage reflects the current direct-query checks (#189).
- `K8sNimHelmWorkload-3b` now requests GPUs per node instead of total cluster GPUs, avoiding unschedulable pods on multi-node clusters (#188).

### Internal

- Documented `tests.exclude` configuration for platforms, markers, tests, and files (#190).
- Updated stale validation references and step-based examples in the documentation (#186).

## [0.6.4] - 2026-04-10

### Added

- Stable identifier validation across VM and bare-metal lifecycle events (`CNP08`) (#183).
- Bare-metal tag validation (`CNP05`) (#180).
- Security group CRUD lifecycle validation (`SDN03`) (#179).

## [0.6.3] - 2026-04-07

### Added

- Minikube and k3s provider support with shared Kubernetes setup stubs (#173).
- Bare-metal power-cycle validation plus SSH timeout and log-sanitization fixes for long-running host checks (#174).

### Fixed

- Removed AWS-specific defaults from canonical test configs so provider-agnostic suites do not imply AWS regions or instance types (#172).
- Removed AWS-specific defaults from canonical stubs and consolidated duplicated SSH wait helpers (#175).
- Excluded example and utility validations from generated catalogs (#176).

## [0.6.2] - 2026-04-01

### Changed

- Renamed SSH-prefixed validation classes to user-facing host names and aligned marker-derived platform tagging in the catalog (#169).

### Security

- Disabled Trivy usage while the upstream image was under active supply-chain compromise (#157).
- Added an SPDX header pre-commit check and disabled local Trivy targets until a trusted image is available (#159).

### Internal

- Improved the README landing page with project purpose and AWS quick-start guidance (#167).
- Expanded AWS domain guides and added provider/stub index READMEs (#166).
- Added community health files and open-source readiness documentation (#161).
- Updated CONTRIBUTING guidance (#165).

## [0.6.1] - 2026-03-24

### Internal

- Fixed broken documentation links, added a test suites README, and added a link-check hook (#155).

## [0.6.0] - 2026-03-24

### Added

- `isvctl catalog` subcommand for building, saving, and uploading the validation catalog (#130).
- VM stop/start lifecycle validations (#138).
- Bare-metal stop/start validations and a clearer `bare_metal` naming convention (#141).
- VM tag and cloud-init validations, plus an AWS VM reuse workflow for development (#142).
- Platform IP management validations for DHCP behavior and VPC IP configuration (#143).
- SDN controller validations for BYOIP, stable private IPs, floating IPs, localized DNS, and VPC peering (#144).
- VM serial console access validation (#147).
- Custom OS image CRUD validation for the image registry suite (#150).
- Bare-metal topology placement, serial console, and cloud-init validations (#151).

### Changed

- Configs now support an `import:` directive and dict-based validation overrides for provider-specific composition (#137).
- Validation markers were normalized for more consistent filtering and catalog output (#153).

### Security

- Bumped transitive dependencies to resolve CVEs in `cryptography`, `pyasn1`, and `requests` (#152).
- Added CI security scanning jobs for TruffleHog, CodeQL, and Trivy (#146).

## [0.5.1] - 2026-03-09

### Fixed

- Locked `urllib3` to a non-vulnerable version (#125).
- Completed the missing `isvtest` version update (#126).

### Internal

- Added DCO sign-off guidance to the contributing documentation (#124).

## [0.5.0] - 2026-03-06

### Added

- Test catalog upload support and enhanced test discovery for coverage reporting in the ISV Lab Service (#103).

## [0.4.4] - 2026-03-05

### Added

- Multi-node NCCL workload support for Kubernetes with MPIJob execution and shared NCCL result parsing (#97).
- Centralized redaction utilities for CLI args, dictionaries, environment variables, free text, and JUnit XML (#100).

### Changed

- Test discovery now includes workloads and can show config-specific validation instances (#98).

### Internal

- Reorganized dependency groups and package requirements (#101).
- Enhanced documentation commands in `isvctl` (#96).
- Added environment selection to the tag workflow (#95).

## [0.4.3] - 2026-02-26

### Changed

- Standardized NIM workload configuration on `NGC_API_KEY` with legacy fallback handling and clearer validation warnings (#89).

### Fixed

- Kubernetes NCCL and GPU workload manifests now include appropriate runtime class, tolerations, and timeout configuration (#93).

### Internal

- Simplified package versioning around `pyproject.toml` as the source of truth (#92).
- Updated CODEOWNERS for the NCP ISV Lab ownership model (#91).

## [0.4.2] - 2026-02-24

### Fixed

- Updated the Kubernetes NCCL workload to use the NVIDIA HPC Benchmarks image and added bandwidth threshold configuration (#90).

## [0.4.1] - 2026-02-24

### Fixed

- NIM inference manifests now read `NGC_API_KEY` instead of the legacy `NGC_NIM_API_KEY` name (#88).

## [0.4.0] - 2026-02-24

### Added

- Bare-metal workload and network validations for GPU stress, NCCL, training, NVLink, InfiniBand, Ethernet, and skipped reinstall wiring (#87).

### Fixed

- AWS EKS teardown now runs by default unless `AWS_SKIP_TEARDOWN=true`, reducing accidental resource leaks (#86).

## [0.3.0] - 2026-02-22

### Added

- AWS bare-metal validation suite with launch, describe, reboot, reinstall, teardown, and termination checks (#83).
- Provider-agnostic validation templates for IAM, network, VM, bare metal, EKS, control plane, and image import workflows (#84).
- Image Registry naming, install-config CRUD, bare-metal image verification, and dry-run deployability checks (#85).
- VM instance listing, reusable NIM validation, and phase fixes that keep teardown in the lifecycle (#81).

### Internal

- Added a unit testing job to CI (#80).

## [0.2.1] - 2026-02-17

### Fixed

- Slurm NCCL workloads now use MPI-capable execution with runtime auto-detection and configurable environment variables (#79).

### Internal

- Added initial GitHub CI workflow files (#72).
- Switched CI to the default runner image (#73).
- Updated CI configuration follow-ups (#76).

## [0.2.0] - 2026-02-11

### Internal

- Added troubleshooting guidance for test runs stuck in `STARTED`, including cleanup and split-flow recovery steps (#4).

## [0.1.0] - 2025-10-28

### Added

- Initial release of the validation suite monorepo with `isvctl`, `isvtest`, `isvreporter`, AWS reference configs, provider stubs, schemas, and documentation.
