<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Kubernetes Launch Kit provider internals

## Layout

| Path | Purpose |
|---|---|
| `config/provider.yaml` | Generic provider mirroring the full Launch Kit workflow |
| `config/network-operator.yaml` | Connectivity validation plus always-run diagnostics for an ISV-provisioned Network Operator deployment |
| `scripts/adapter.py` | Thin process and JSON evidence transport |

Executable mocks and pinned scenarios are test-only and live under
`isvctl/tests/providers/k8s_launch_kit/fixtures/`. Product configuration must
never reference them.

## Generic provider

`config/provider.yaml` exposes install/verify, Kubernetes preflight, discover,
generate, deploy, validate, and clean for consumers that own the complete
Launch Kit lifecycle. Its workflow configuration is raw argument arrays. It
does not reproduce Launch Kit's domain schema or defaults.

Discovery optionally stages a complete `user_config`, writes the resolved
`cluster-config.yaml`, and deletes the staged copy after the command. Validate
uses `timeout: null` because Launch Kit calculates a bounded connectivity budget
or honors its user-supplied timeout. The other generic steps retain finite
outer watchdogs.

## Network Operator provider

`config/network-operator.yaml` intentionally does not import the generic
provider. It defines one catalog-owning test step:

```text
l8k validate --user-config <file> --deployment-files <directory> --output json
```

After validation is attempted, a linked finalizer always executes:

```text
l8k sosreport --output-dir <artifact-dir>/sosreport
```

Sosreport runs after both successful and failed validation commands and after
the connectivity assertion. It is diagnostic evidence, not another catalog
test. Its failure is reported as a separate teardown result.

The cluster, Network Operator deployment, complete Launch Kit config, rendered
deployment directory, and installed `l8k` binary are prerequisites. There are
no AI Cloud Validation use-case workflows and no discover, generate, deploy,
clean, verification, or separate preflight steps.

The adapter resolves and validates the two input paths without copying or
parsing them. It rejects raw `--user-config` or `--deployment-files` arguments
when the dedicated inputs are used. Launch Kit remains responsible for fabric,
deployment type, enabled checks, GPUDirect applicability, thresholds, runtime
budgets, and every other value in its config.

The one catalog validation, `LaunchKitConnectivityCheck`, converts every
emitted `connectivity.PingResults` row into a subtest. It does not expect a
fixed family list: a disabled family is absent, while a newly emitted family is
reported automatically.

## Adapter contract

For every structured workflow invocation, `adapter.py`:

1. resolves the configured executable;
2. adds `--output json` unless the caller already selected JSON;
3. executes exactly one Launch Kit command;
4. preserves stdout, stderr, argv, exit code, and duration;
5. parses concatenated JSON objects without renaming their fields;
6. copies the HTML file advertised by a validation `reportPath` into the
   provider evidence directory;
7. returns one provider envelope containing the raw documents and artifact paths.

For `sosreport`, the adapter does not force JSON because the current Launch Kit
command streams text output. It defaults `--output-dir` to the provider evidence
directory, records that directory as an artifact, and still returns the same
structured provider envelope. The installed Launch Kit must make its
`kubectl-netop_sosreport` helper available under the Launch Kit installation
prefix.

Semantic assertions belong in
`isvtest.validations.k8s_launch_kit`, not the transport.

See the [Network Operator integration guide](../../../../docs/guides/k8s-launch-kit/network-operator.md)
for prerequisites, invocation, output, and evidence layout.
