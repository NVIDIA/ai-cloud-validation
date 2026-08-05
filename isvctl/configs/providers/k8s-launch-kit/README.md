<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Kubernetes Launch Kit provider internals

This directory owns the implementation behind
`config/provider.yaml`. It is provider-specific code, not a cross-provider
helper.

## Layout

| Path | Purpose |
|---|---|
| `config/provider.yaml` | Generic single-workflow provider using real `l8k` and `kubectl` by default |
| `config/network-operator.yaml` | Production six-use-case Network Operator workflow |
| `scripts/adapter.py` | Transport for install/verify, Kubernetes preflight, and one `l8k` workflow command |

Test doubles and pinned scenario data intentionally live outside the shipped
provider under `isvctl/tests/providers/k8s_launch_kit/fixtures/`. The provider
tests load the production YAML and inject those paths in memory.

The adapter must remain thin. It accepts raw argument arrays for `discover`,
`generate`, `deploy`, `validate`, and `clean`, appends `--output json`, executes the
configured `l8k` executable, and preserves the CLI's JSON documents without
renaming or interpreting fields. The one file-level input is `user_config`, a
path to a complete Launch Kit configuration. Before discovery, the adapter
copies it to the workflow as a mode-`0600` `user-config.yaml`, explicitly writes
the discovered result to `cluster-config.yaml`, and removes the staged input as
soon as discovery exits. The original is never modified, and evidence retains
only its path, size, and SHA-256 provenance rather than its potentially
sensitive contents. Launch Kit still owns the file schema, domain flags, and
defaults. Semantic assertions belong in `isvtest.validations.k8s_launch_kit`.

Launch Kit `validate` steps use `timeout: null` so the CLI owns its deadline.
l8k calculates and logs a bounded matrix budget by default and honors a user's
explicit `--connectivity-timeout`. The remaining workflow steps keep finite
isvctl watchdogs. Other providers may also use `timeout: null`, but only when
their child command has its own bounded timeout.

The grouped Network Operator workflow is validation-only. ISVs install and
configure Network Operator before running it. Each selected use case executes
`preflight -> discover -> generate -> validate`; it never invokes `l8k deploy`
or `l8k clean`, so AI Cloud Validation cannot replace or delete the ISV-managed
installation. The generic `provider.yaml` deliberately retains deploy and
clean as public Launch Kit operations for other consumers.

The grouped workflow passes only its fabric and deployment identity during
discovery. With no `user_config`, Launch Kit resolves the default
`./cluster-config.yaml` and `./deployment` paths throughout the validation
workflow. With `user_config`, every selected use case stages an independent
copy, and the adapter owns `--user-config` plus `--save-cluster-config` for
discovery. Each transient copy is deleted after its discovery command. Do not
repeat either flag in the raw discovery argument array or place the source
inside the retained provider working directory.

Each workflow envelope records the absolute working directory while retaining
Launch Kit's JSON documents unchanged. Validations use that metadata to resolve
relative `generatedFiles` paths emitted by the CLI.

Install mode accepts only an immutable full Git commit for the official
`scripts/install.sh` plus a caller-supplied SHA-256, verifies that digest before
writing or executing the script, delegates archive selection and checksum
handling to Launch Kit, then verifies the binary at the install prefix.
When the user pins `installation.version`, both setup and test-phase
verification require `l8k version --output json` to report that exact version.
The captured schema must advertise all five generic provider commands,
including `deploy` and `clean`. This verifies that the installation satisfies
the generic provider contract even though the Network Operator suite invokes
only discover, generate, and validate.

The preflight accepts the non-empty subset of Launch Kit commands used by the
calling workflow. It uses their explicit kubeconfig and forwarded environment,
rejects conflicting `--kubeconfig` arguments, and requires Kubernetes API
access plus at least one Ready node before validation starts.

The same string-only environment mapping is also passed to the installer and
version/schema verification, so proxy and executable runtime settings do not
change between setup and test phases.

The production adapter executes `executable` directly. There is no Python-file
special case: a test double must be an executable with a valid shebang, just
like any other CLI implementation. This keeps mock behavior out of the public
provider contract.

See the [integration guide](../../../../docs/guides/k8s-launch-kit/network-operator.md)
for configuration, use cases, evidence, prerequisites, and current production
gaps.
