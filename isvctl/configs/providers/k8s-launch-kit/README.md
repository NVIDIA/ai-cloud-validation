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

The one catalog validation, `LaunchKitConnectivityCheck`, imports native JUnit
cases through the same subtest reporting path as Kubernetes conformance tests.
Native disabled-check skips are preserved. Four skipped suites are added for
the opposing fabric, with the reason that the cluster fabric is not configured
for that fabric type. Fabric is inferred from native suite names.

Launch Kit must support `validate --junit-path` (NVIDIA/k8s-launch-kit#288).
The adapter retains `launch-kit-junit.raw.xml` and `launch-kit-junit.xml` in the
evidence directory. The latter includes opposing-fabric skips. All cases flow
into isvctl's standard `--junitxml` report (default `_output/junit-validation.xml`)
for existing merge, remote download, and upload automation.

## Adapter contract

For every structured workflow invocation, `adapter.py`:

1. resolves the configured executable;
2. adds `--output json` unless the caller already selected JSON, and a provider-owned
   `--junit-path` for validation;
3. executes exactly one Launch Kit command;
4. preserves stdout, stderr, argv, exit code, and duration as files under
   `<artifact_dir>/commands/<command>/`;
5. parses concatenated JSON objects without renaming their fields;
6. copies the HTML file advertised by a validation `reportPath` into the
   provider evidence directory;
7. returns a minimal envelope: `success`, `platform`, `operation`, `artifacts`
   (evidence paths), and `error` on failure. Raw Launch Kit output stays in the
   evidence files, not in step JSON.

For `sosreport`, the adapter does not force JSON because the current Launch Kit
command streams text output. It defaults `--output-dir` to the provider evidence
directory, records that directory as an artifact, and still returns the same
structured provider envelope. The installed Launch Kit must make its
`kubectl-netop_sosreport` helper available under the Launch Kit installation
prefix.

Semantic assertions belong in
`isvtest.validations.k8s_launch_kit`, not the transport.

## Rules for changes

- Do not add prepare, verify, preflight, discover, generate, deploy, clean, or
  other finalizer steps to `config/network-operator.yaml`.
- Workflow settings stay raw argument arrays. Do not model or duplicate Launch
  Kit flags, schema, or defaults, and never parse the user config (infer fabric
  from native JUnit suite names).
- Missing or malformed JUnit, no executed connectivity cases, or a failed
  command must fail, never pass vacuously.
- Do not invent results or reinterpret Launch Kit's verdict.
- `l8k clean` is the only supported deletion path; never reproduce Launch Kit
  cleanup with kubectl.

## Tests and traceability

- Provider tests in `isvctl/tests/providers/k8s_launch_kit/` load the
  production YAML and inject the test-owned executables from `fixtures/`.
- Result interpretation tests live in `isvtest/tests/k8s_launch_kit/`.
- The PRD source is
  `docs/requirements/network-operator-readiness-requirements.yaml`; its
  traceability edges live in `docs/requirements/test-requirements-matrix.yaml`.
  Regenerate committed views with `make plan`.

See the [Network Operator integration guide](../../../../docs/guides/k8s-launch-kit/network-operator.md)
for prerequisites, invocation, output, and evidence layout.
