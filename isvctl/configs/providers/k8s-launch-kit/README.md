<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Kubernetes Launch Kit provider internals

## Layout

| Path | Purpose |
|---|---|
| `config/network-operator.yaml` | Connectivity validation plus always-run diagnostics for an ISV-provisioned Network Operator deployment |
| `scripts/adapter.py` | Thin process and JSON evidence transport |

Executable mocks and pinned scenarios are test-only and live under
`isvctl/tests/providers/k8s_launch_kit/fixtures/`. Product configuration must
never reference them.

## Network Operator provider

`config/network-operator.yaml` defines one test step that feeds every catalog test:

```text
l8k validate --user-config <file> --deployment-files <directory> --output json
```

The validate step sets `continue_on_failure: true`, so the next step always runs:

```text
l8k sosreport --output-dir <artifact-dir>/sosreport
```

Sosreport runs after both successful and failed validation commands; the catalog
tests still report the validation result. It is diagnostic evidence, not another
catalog test. Its failure fails the test phase.

The cluster, Network Operator deployment, complete Launch Kit config, rendered
deployment directory, and installed `l8k` binary are prerequisites. There are
no AI Cloud Validation use-case workflows and no discover, generate, deploy,
clean, verification, or separate preflight steps.

The adapter resolves and validates the two input paths without copying or
parsing them. It rejects raw `--user-config` or `--deployment-files` arguments
when the dedicated inputs are used. Launch Kit remains responsible for fabric,
deployment type, enabled checks, GPUDirect applicability, thresholds, runtime
budgets, and every other value in its config.

Launch Kit must support `validate --junit-path` (NVIDIA/k8s-launch-kit#288).
The adapter writes that report to `launch-kit-junit.xml` in the evidence
directory and does not modify it. Nine catalog tests, all bound to the one
validate step, each report one native suite with its cases as subtests:

| Catalog test | Native suite |
|---|---|
| `K8sNetworkOperatorDeployment` | `network/validation` (release, components, Helm values, manifests, topology presets) |
| `K8sEastWestNetwork<Family>-<fabric>` | the suite of the same name; families `ICMPPing`, `RDMAPing`, `IBWriteBandwidth`, `DMABufBandwidth`; fabrics `ethernet`, `infiniband` |

Launch Kit only emits suites for the configured fabric, so the other fabric's
four tests skip with "Cluster fabric is not configured for this fabric type".
A family suite holding only a skipped case (check disabled) skips with Launch
Kit's reason. `K8sNetworkOperatorDeployment` also fails when `l8k validate`
failed and no native case explains it, so a failed run is never all green.
Results flow into isvctl's standard `--junitxml` report (default
`_output/junit-validation.xml`) for existing merge, remote download, and upload
automation.

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
  other lifecycle steps to `config/network-operator.yaml`.
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
