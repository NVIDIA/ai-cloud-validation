<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Network Operator connectivity validation through Kubernetes Launch Kit

## Scope

The Network Operator suite performs one operation:

```text
l8k validate --user-config <complete-config> --deployment-files <rendered-directory>
```

It reports the connectivity matrix produced by Launch Kit. It does not install
or verify the `l8k` binary, discover topology, generate manifests, deploy
Network Operator, run a separate Kubernetes preflight, or clean cluster state.

Those activities are prerequisites. Before starting the suite, the ISV must
provide a reachable Kubernetes cluster, bring Network Operator and the desired
networking profile into the expected state, create a complete Launch Kit
configuration, and retain the corresponding rendered deployment files.

There are no separate AI Cloud Validation tests for RoCE, InfiniBand, SR-IOV,
RDMA Shared, host-device, ICMP, rping, bandwidth, or GPUDirect. The supplied
Launch Kit configuration determines the topology and enabled validation
families. This avoids duplicating Launch Kit's configuration and applicability
model in AI Cloud Validation.

## Architecture

```text
Network Operator provider YAML
  -> one isvctl test step
     -> adapter.py
        -> l8k validate --user-config ... --deployment-files ... --output json
        -> retained argv, stdout, stderr, exit code, and duration
  -> Network Operator suite YAML
     -> LaunchKitConnectivityCheck
        -> one subtest for every Launch Kit connectivity row
  -> console and JUnit results
```

The relevant files are:

| Layer | File |
|---|---|
| Production entrypoint | `isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml` |
| CLI transport | `isvctl/configs/providers/k8s-launch-kit/scripts/adapter.py` |
| Catalog wiring | `isvctl/configs/suites/k8s-launch-kit/network-operator.yaml` |
| Result interpretation | `isvtest/src/isvtest/validations/k8s_launch_kit/checks.py` |
| Mock-backed provider tests | `isvctl/tests/providers/k8s_launch_kit/` |
| Result-check unit tests | `isvtest/tests/k8s_launch_kit/` |

The generic provider in
`isvctl/configs/providers/k8s-launch-kit/config/provider.yaml` still mirrors the
complete Launch Kit lifecycle for other consumers. The Network Operator
entrypoint does not import it, so none of those lifecycle steps are inherited.

## Inputs

The Network Operator provider exposes only these settings:

| Key | Required | Meaning |
|---|---:|---|
| `executable` | no | `l8k` command or absolute executable path; default is `l8k` |
| `user_config` | yes | Complete Launch Kit cluster configuration |
| `deployment_files` | yes | Existing rendered deployment directory validated by Launch Kit |
| `working_dir` | no | Provider process working directory |
| `artifact_dir` | no | Directory for command evidence |
| `environment` | no | String environment entries forwarded to Launch Kit, such as `KUBECONFIG` |

Paths accept `~`, but absolute paths are preferable in automation. The adapter
resolves both paths, verifies that `user_config` is a file and
`deployment_files` is a directory, and passes the resolved paths to Launch Kit.
It does not copy, merge, parse, or modify either input.

Launch Kit owns every setting inside the complete config, including the
selected profile, validation mode, enabled checks, GPUDirect behavior,
bandwidth thresholds, per-operation timeouts, routing, IP pools, and resource
names. AI Cloud Validation stores no copies of those defaults.

Do not also put `--user-config` or `--deployment-files` in a raw Launch Kit
argument list. The adapter rejects duplicate path sources rather than allowing
ambiguous last-value behavior.

## Running the suite

From the repository root:

```bash
uv run isvctl test run \
  -f isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml \
  --capability kubernetes \
  --set 'context.k8s_launch_kit.user_config=/absolute/path/cluster-config.yaml' \
  --set 'context.k8s_launch_kit.deployment_files=/absolute/path/deployment' \
  --no-upload -- -v
```

To use a kubeconfig that is not selected by the normal client environment, add:

```text
--set 'context.k8s_launch_kit.environment={"KUBECONFIG":"/absolute/path/kubeconfig.yaml"}'
```

Omit `--no-upload` when the run should use the configured AI Cloud Labs upload
path.

## Selecting connectivity checks

Selection happens in the Launch Kit config, not with AI Cloud Validation
labels. For example, disabling Launch Kit GPUDirect validation means no
`gpudirect_dmabuf` rows are emitted. The wrapper then reports the remaining
rows only; it does not create a skipped or failed GPUDirect placeholder.

Likewise, the suite does not infer a fabric or deployment mode from labels.
Run it once for the exact cluster state described by the supplied files. To
validate another topology, provision that topology and invoke the same suite
with its config and deployment directory.

## Timeouts

The `launch_kit_validate` step has `timeout: null`. Launch Kit calculates and
logs its connectivity-matrix budget by default, or honors the timeout configured
by the user. This prevents an independent isvctl watchdog from terminating a
valid large matrix before Launch Kit's bounded checks finish. An enclosing CI
job may still impose an overall job timeout.

## Results and errors

`LaunchKitConnectivityCheck` finds the `connectivity.PingResults` array in the
unmodified JSON stream. Each emitted row becomes a named subtest:

```text
<family>/<source-node>-><destination-node>/<source-rail>-><destination-rail>
```

Failure messages preserve Launch Kit's expectation, observed result, bandwidth
and minimum when present, endpoint GPU information when present, stderr, and
structured error text. Explicit future `Family` values are forwarded without
requiring an AI Cloud Validation catalog update. Older numeric `Kind` values
remain supported as a compatibility fallback.

The check fails when any emitted row has `OK != true`, when no connectivity
matrix is present, or when the matrix contains no results. A command that fails
before producing connectivity output retains its provider error in the
validation and JUnit output.

## Evidence

The adapter writes:

```text
_output/k8s-launch-kit/network-operator/
  work/
  evidence/
    commands/validate/
      command.json
      stdout.txt
      stderr.log
```

`command.json` records the resolved argv, exit code, and duration. `stdout.txt`
contains Launch Kit's complete JSON stream, including static validation,
connectivity, and report-path documents; `stderr.log` retains CLI progress and
diagnostics. The adapter also registers these paths in the provider step output.
The Launch Kit HTML report remains at the `reportPath` emitted by Launch Kit,
normally below the supplied deployment directory.

## PRD boundary

This integration covers reportable Launch Kit connectivity validation. It
deliberately treats topology discovery, manifest generation, installation,
deployment health preparation, profile selection, and restoration as external
prerequisites. Tests that intentionally mutate Network Operator state require a
separate transaction and restoration design before they can be added.
