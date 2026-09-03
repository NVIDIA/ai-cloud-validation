<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Network Operator validation through Kubernetes Launch Kit

## Safety and ownership boundary

The production Network Operator suite validates an installation that the ISV
has already deployed and configured. Its workflow is:

```text
verify l8k
  -> verify Kubernetes access and at least one Ready node
  -> l8k discover
  -> l8k generate
  -> l8k validate
  -> AI Cloud Validation checks and reports
```

It does **not** invoke `l8k deploy` or `l8k clean`. AI Cloud Validation therefore
does not install, replace, reconfigure, or remove the ISV-managed Network
Operator deployment. A validation or orchestration failure also cannot activate
a cleanup finalizer.

Discovery may label nodes and validation creates Launch Kit's temporary test
workloads. Those operations remain part of Launch Kit itself. The important
ownership boundary is that this suite never applies the generated Network
Operator deployment manifests and never removes the existing installation.

The generic provider still exposes `discover`, `generate`, `deploy`, `validate`,
and `clean`. That API mirrors the complete Launch Kit CLI and remains available
to other suites that explicitly own deployment lifecycle. The validation-only
behavior is defined by `config/network-operator.yaml`, not by removing features
from the generic provider.

## Architecture

AI Cloud Validation separates command execution from result interpretation:

```text
provider configuration
  -> ordered isvctl phases and steps
       -> adapter.py executes real l8k and kubectl
       -> raw command envelopes become step outputs and evidence
  -> suite configuration
       -> binds one use-case check to each validate step
       -> supplies discover/generate/preflight outputs as context
  -> isvtest validation classes
       -> interpret unmodified l8k JSON
       -> report member and probe-level subtests
  -> console, JUnit, retained artifacts, and optional Labs upload
```

The main files are:

| Layer | File |
|---|---|
| Generic provider | `isvctl/configs/providers/k8s-launch-kit/config/provider.yaml` |
| Network Operator validation workflow | `isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml` |
| CLI transport | `isvctl/configs/providers/k8s-launch-kit/scripts/adapter.py` |
| Individual PRD checks and concrete use cases | `isvctl/configs/suites/k8s-launch-kit/network-operator.yaml` |
| Result interpretation | `isvtest/src/isvtest/validations/k8s_launch_kit/checks.py` |
| Provider tests and mock CLI | `isvctl/tests/providers/k8s_launch_kit/` |
| Result-check tests | `isvtest/tests/k8s_launch_kit/test_checks.py` |
| PRD and traceability | `docs/requirements/` |

The provider and suite files are intentionally separate. The provider owns
process execution and evidence. The single frontend-visible `network_operator`
suite owns catalog identity, selection, and the mapping from step outputs to
reusable validation checks. It contains both individually selectable PRD checks
and the six grouped use cases.

## Network Operator workflow

For every selected use case, the production provider executes these steps in
order:

1. `launch_kit_<use_case>_preflight` checks Kubernetes access;
2. `launch_kit_<use_case>_discover` runs `l8k discover` with that use case's
   fabric and deployment type;
3. `launch_kit_<use_case>_generate` runs `l8k generate` to reconstruct the
   expected manifests used by Launch Kit validation;
4. `launch_kit_<use_case>_validate` runs `l8k validate` against the already
   deployed cluster state.

Generation is intentional even though deployment is external. Launch Kit
validation compares the live installation with the desired resources generated
for the selected topology. The generated files are evidence; this suite does
not apply them.

The six concrete use cases are:

| Test | Fabric labels | Deployment label |
|---|---|---|
| `EastWestNetworkRoceSriovCheck` | `ethernet`, `roce` | `sriov` |
| `EastWestNetworkInfiniBandSriovCheck` | `infiniband` | `sriov` |
| `EastWestNetworkRoceRdmaSharedCheck` | `ethernet`, `roce` | `rdma_shared` |
| `EastWestNetworkInfiniBandRdmaSharedCheck` | `infiniband` | `rdma_shared` |
| `EastWestNetworkRoceHostDeviceCheck` | `ethernet`, `roce` | `host_device` |
| `EastWestNetworkInfiniBandHostDeviceCheck` | `infiniband` | `host_device` |

All applicable use cases run by default. Labels can select a fabric, a
deployment mode, or their intersection. `continue_after_failure` lets later
independent use cases collect evidence after an earlier use case fails; the
overall run still fails.

## Provider API

The adapter accepts raw argument arrays instead of reproducing Launch Kit's
domain configuration:

| Key | Meaning |
|---|---|
| `executable` | Existing `l8k` command or absolute path |
| `installation.mode` | `verify` by default, or explicit `install` |
| `installation.version` | Optional exact Launch Kit version |
| `installation.installer_ref` | Immutable installer commit used in install mode |
| `installation.installer_sha256` | Trusted installer digest used in install mode |
| `installation.prefix` | Optional installation prefix |
| `user_config` | Optional path to a complete Launch Kit configuration |
| `kubectl_command` | Optional kubectl-compatible argv prefix |
| `working_dir` | Per-workflow Launch Kit working directory |
| `artifact_dir` | Per-workflow evidence directory |
| `environment` | String environment entries forwarded to all commands |
| `<command>.arguments` | Raw arguments for that Launch Kit command |

AI Cloud Validation defines no defaults for namespaces, node selectors,
Network Operator versions, driver modes, rails, resource names, IP pools, GPU
counts, validation modes/checks, bandwidth thresholds, or Launch Kit timeouts.
Omitted values are resolved by the installed Launch Kit release. The adapter
adds only `--output json` and rejects a conflicting user output mode.

The generic configuration includes argument arrays for all five Launch Kit
workflow commands. The Network Operator configuration includes only `discover`,
`generate`, and `validate`, because those are the commands it actually invokes.

### Complete user configuration

Set `context.k8s_launch_kit.user_config` when a cluster needs settings that are
not exposed as CLI flags. This must be a complete Launch Kit configuration;
the provider does not merge partial YAML.

For each selected use case, the adapter:

1. copies the source to `<working_dir>/user-config.yaml` with mode `0600`;
2. adds `--user-config <staged-path>` and
   `--save-cluster-config <working_dir>/cluster-config.yaml` to discovery;
3. removes the staged copy as soon as discovery exits;
4. records only source path, size, and SHA-256 provenance in evidence.

The source file is never modified or retained as an uploaded artifact. Do not
put it inside a retained working or evidence directory, and do not repeat the
provider-owned `--user-config` or `--save-cluster-config` flags in discovery
arguments.

Example overlay:

```yaml
import:
  - /path/to/ai-cloud-validation/isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml

context:
  k8s_launch_kit:
    user_config: /secure/path/cluster-config.yaml
    environment:
      KUBECONFIG: /secure/path/kubeconfig.yaml
```

The supplied configuration must describe the desired use case consistently
with the selected labels. The use-case discovery flags select the authoritative
fabric and deployment type.

## Installation verification and Kubernetes prerequisite

`installation.mode: verify` resolves the configured executable and captures:

- `l8k version --output json`;
- `l8k schema --output json`.

The generic provider contract requires the schema to advertise all five
workflow commands, including deploy and clean. This does not cause the Network
Operator suite to call those commands. An optional exact version is checked in
both setup and test-phase verification, so `--phase test` is safe when setup is
skipped.

Install mode requires an immutable full Git commit for the official installer
and a caller-supplied SHA-256. The adapter verifies the downloaded installer
before executing it, then verifies the installed binary.

The preflight helper accepts the non-empty subset of Launch Kit commands used
by its caller. It extracts `--kubeconfig` from those command arguments,
rejects inconsistent kubeconfigs, and runs kubectl probes for API access and at
least one Ready node. If no command argument selects a kubeconfig, kubectl and
l8k inherit the same forwarded `KUBECONFIG` environment or normal client
defaults.

## Timeouts

Provider step timeouts are outer isvctl watchdogs. Discovery and generation
have finite watchdogs. Network Operator validate steps use `timeout: null`, so
isvctl does not preempt a valid large connectivity matrix. Launch Kit computes
and logs its bounded validation budget by default, or uses a user-supplied
Launch Kit timeout argument.

Other providers may use `timeout: null` only when the child tool owns a bounded
deadline. An enclosing CI job can still impose a total job timeout.

## Result and error reporting

The adapter preserves Launch Kit JSON documents without renaming fields and
records the exact argv, cwd, stdout, stderr, exit code, and timing for every
command. A non-zero process result remains attached to its step even when the
CLI emitted partial JSON.

The composite use-case check converts Launch Kit resource and connectivity rows
into pytest subtests. Names identify the member check and the individual probe,
so failures point to a concrete resource, rail, source/destination pair, or
bandwidth result. A member that is inapplicable may skip without skipping the
whole use case; failed members fail the parent.

If preflight, discovery, or generation fails before validate produces output,
the owning use-case validation is emitted as a `step_failed` error in JUnit. A
normal Launch Kit validation failure is emitted as a failed testcase with its
probe diagnostics. Neither path invokes deploy or cleanup.

`LaunchKitEvidenceCaptureCheck` accepts both lifecycle-owning and
validation-only workflows. Verify, preflight, discover, generate, and validate
evidence is required. Deploy evidence is checked only when a suite actually
provides `deploy_output`.

## Running the suite

Prerequisites:

- the selected kubeconfig reaches a Kubernetes cluster with at least one Ready
  worker node;
- Network Operator and the resources required for the selected profile are
  already deployed and reconciled;
- the installed `l8k` release supports JSON version, schema, discovery,
  generation, and validation output;
- the execution identity can discover topology, create validation workloads,
  exec into them, inspect events/resources, and collect logs;
- the cluster satisfies Launch Kit prerequisites for the selected SR-IOV,
  RDMA Shared, host-device, RoCE, InfiniBand, and GPUDirect checks.

Run all use cases:

```bash
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run \
  -f isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml \
  --capability kubernetes --no-upload -- -v
```

Select a subset by adding labels after `--`:

```bash
# Ethernet/RoCE only
... --capability kubernetes --no-upload -- -v --label ethernet

# All SR-IOV profiles
... --capability kubernetes --no-upload -- -v --label sriov

# RoCE SR-IOV only
... --capability kubernetes --no-upload -- -v \
  --label ethernet --label sriov
```

Do not use `--phase teardown` for this Network Operator configuration; it has
no teardown phase because deployment lifecycle belongs to the ISV.

## Evidence and upload

Each use case writes to separate working and evidence directories under:

```text
_output/k8s-launch-kit/network-operator/use-cases/<use-case>/
  work/
    cluster-config.yaml
    deployment/
    k8s-launch-kit-validation-report.html (location may vary by l8k release)
  evidence/
    kubernetes-preflight/
    commands/discover/
    commands/generate/
    commands/validate/
```

Shared setup and verification evidence is stored under
`_output/k8s-launch-kit/network-operator/shared-evidence/`. Generated manifests,
resource status, events, connectivity and bandwidth documents, stdout/stderr,
and the HTML report are registered in provider step outputs and retained
locally. Current AI Cloud Labs upload sends JUnit, the combined run log, and
catalog metadata; it does not yet upload these evidence files as attachments.

User configuration contents are intentionally excluded; only provenance is
retained.

## PRD coverage and remaining work

The suite provides selectable and grouped Network Operator checks for RoCE and
InfiniBand across SR-IOV, RDMA Shared, and host-device profiles. It reuses Launch
Kit topology, manifest readiness, ICMP, rping, RDMA bandwidth, multi-rail, and
GPUDirect validation output. Catalog metadata and requirement mappings identify
ownership, labels, dependencies, applicability, and prerequisites.

The validation-only boundary changes the interpretation of ENT-REQ-010: this
integration does not modify the Network Operator deployment, so it has no
pre-test operator state to restore. If a future test intentionally changes
operator state, that test needs a separate, explicit Launch Kit transaction or
snapshot/restore contract before it can be added here.

Live qualification is still required for every supported hardware/fabric/
deployment combination. Unit fixtures prove integration and reporting behavior,
not partner certification. Additional Launch Kit improvements worth considering
are stable machine-readable result schemas, explicit artifact manifests, and a
first-class API for validating an existing deployment without regenerating
desired manifests when the site already has an authoritative complete config.
