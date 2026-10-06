<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Network Operator connectivity validation through Kubernetes Launch Kit

## Scope

Network Operator validation is the `network_operator` group of the Kubernetes
suite (`isvctl/configs/suites/k8s.yaml`), not a suite of its own. It requires
[Kubernetes Launch Kit](https://github.com/NVIDIA/k8s-launch-kit), the `l8k`
CLI, and runs one validation:

```text
l8k validate --user-config <complete-config> --deployment-files <rendered-directory>
```

After every validate that started, whatever its outcome, it collects
diagnostics with `l8k sosreport`. The `LaunchKitSosreport` check reports that
collection; it is not a catalog test.

The Launch Kit config decides the topology, the fabric, and which connectivity
families run. AI Cloud Validation does not duplicate that model: each catalog
test reports what Launch Kit ran.

## Prerequisites

The validation does not install `l8k`, discover topology, generate manifests,
deploy Network Operator, or clean cluster state. Before running it, provide:

- a reachable cluster with Network Operator and the networking profile in the
  expected state;
- a complete Launch Kit config and the deployment files rendered from it;
- a Launch Kit release supporting `validate --junit-path`
  ([NVIDIA/k8s-launch-kit#288](https://github.com/NVIDIA/k8s-launch-kit/pull/288)),
  with `l8k` on `PATH`;
- the `kubectl-netop_sosreport` helper under the same installation prefix, at
  `share/l8k/scripts/kubectl-netop_sosreport`. Check it once with
  `l8k sosreport --output-dir <temporary-directory>`.

## Architecture

```text
suites/k8s.yaml, network_operator group (no step)
  -> first check: run_launch_kit()
     -> l8k validate --junit-path ... --user-config ... --deployment-files ... --output json
        -> retained argv, stdout, stderr, exit code, duration, JUnit, and HTML report
     -> l8k sosreport --output-dir _output/k8s-launch-kit/sosreport
        -> retained sosreport.tar.gz, stdout, stderr, exit code, and duration
  -> the other checks reuse that run
  -> nine catalog tests, one per native Launch Kit JUnit suite
     -> one subtest for every case in that suite
  -> LaunchKitSosreport (catalog-excluded): passes or fails on sosreport alone
  -> console and JUnit results
```

The relevant files are:

| Layer | File |
|---|---|
| Catalog wiring and inputs | `network_operator` group and `tests.settings.k8s_launch_kit` in `isvctl/configs/suites/k8s.yaml` |
| Launch Kit invocation and evidence | `isvtest/src/isvtest/validations/k8s_launch_kit/runner.py` |
| Result interpretation | `isvtest/src/isvtest/validations/k8s_launch_kit/checks.py` |
| Runner and check unit tests, mock `l8k` | `isvtest/tests/k8s_launch_kit/` |
| End-to-end run of the Kubernetes suite | `isvctl/tests/test_k8s_launch_kit.py` |

## Inputs

The Kubernetes suite exposes two settings under `tests.settings.k8s_launch_kit`:

| Key | Meaning |
|---|---|
| `user_config` | Complete Launch Kit cluster configuration |
| `deployment_files` | Existing rendered deployment directory validated by Launch Kit |

Both default to empty, which skips every `network_operator` check without
running Launch Kit, so a Kubernetes run that does not target Network Operator is
unaffected.

A check that cannot start skips with the reason instead of failing: only one
input set, a `user_config` that is not a file, a `deployment_files` that is not
a directory, or no `l8k` executable. Once `l8k validate` has started, any
problem it reports is a failure.

Paths may use `~`. The runner passes them to Launch Kit resolved and does not
copy, parse, or modify either input.

`l8k` runs with the isvctl process environment, so the cluster is selected the
same way as for every other Kubernetes check (for example `KUBECONFIG`).

## Running the validation

From the repository root, on a machine that reaches the cluster:

```bash
uv run isvctl test run -f isvctl/configs/suites/k8s.yaml \
  --phase test --label network_operator \
  --set 'tests.settings.k8s_launch_kit.user_config=/absolute/path/cluster-config.yaml' \
  --set 'tests.settings.k8s_launch_kit.deployment_files=/absolute/path/deployment' \
  --no-upload -- -v
```

`--label network_operator` limits the run to these checks; drop it to run them
with the rest of the Kubernetes suite. A provider config that imports
`k8s.yaml` takes the same `--set` values.

Omit `--no-upload` when the run should use the configured AI Cloud Labs upload
path.

## Selecting connectivity checks

Selection happens in the Launch Kit config, not with AI Cloud Validation
labels. For example, disabling Launch Kit GPUDirect validation makes Launch Kit
emit a skipped `K8sEastWestNetworkDMABufBandwidth-<fabric>` case, and that
catalog test skips with Launch Kit's reason.

To validate another topology, provision it and run again with its config and
deployment directory.

## Timeouts

`l8k validate` runs without an isvctl deadline. Launch Kit calculates and logs
its connectivity-matrix budget by default, or honors the timeout configured by
the user, so an independent watchdog cannot terminate a valid large matrix
before Launch Kit's bounded checks finish. An enclosing CI job may still impose
an overall job timeout.

`l8k sosreport` has a 30-minute limit, because the current Launch Kit sosreport
command does not calculate its own total deadline.

## Results and errors

Launch Kit writes a `network/validation` suite of
deployment-state cases and one `K8sEastWestNetwork<Family>-<fabric>` suite per
connectivity family for the configured fabric. The `network_operator` group wires
one catalog test per native suite:

| Catalog test | Result |
|---|---|
| `K8sNetworkOperatorDeployment` | `network/validation` cases: release, component versions, Helm values, stray resources, each manifest, topology presets |
| `K8sEastWestNetwork{ICMPPing,RDMAPing,IBWriteBandwidth,DMABufBandwidth}-{ethernet,infiniband}` | that family's probes on that fabric |

Native names, durations, failures, skips, and diagnostic evidence are kept as
subtests. A family test whose suite is absent skips with
`Cluster fabric is not configured for this fabric type: <fabric>`, so an
Ethernet cluster reports four skipped `-infiniband` tests and vice versa.
Fabric comes from the native suite names; user configuration is not parsed.
A missing or malformed report fails every test. `K8sNetworkOperatorDeployment`
also fails when `l8k validate` failed and no native case explains it, so a
failed command is never reported as all green.

The standard `isvctl --junitxml` output (default `_output/junit-validation.xml`)
contains one testcase per catalog test, named after the catalog entry (for
example `K8sEastWestNetworkICMPPing-ethernet`), with native cases as
`<catalog-test>::<native-case-name>` subtests. Existing phase merging, remote
report download, and `isvreporter` upload therefore report against the static
catalog.

A failed sosreport fails `LaunchKitSosreport`, and so the test phase, without
changing any catalog result. When validate could not start, sosreport is not
attempted and `LaunchKitSosreport` skips with the same reason.

## Evidence

The runner writes, relative to the isvctl working directory:

```text
_output/k8s-launch-kit/
  work/
  k8s-launch-kit-validation-report.html
  launch-kit-junit.xml
  commands/validate/
    command.json
    stdout.txt
    stderr.log
  commands/sosreport/
    command.json
    stdout.txt
    stderr.log
  sosreport.tar.gz
```

`command.json` records the argv, exit code, and duration of each command;
`stdout.txt` and `stderr.log` hold its output. The HTML report Launch Kit
advertises (`reportPath`) is copied to `k8s-launch-kit-validation-report.html`;
an advertised report that cannot be read is an error.

Stale JUnit, HTML, and sosreport files are removed before each run, so results
always come from this run. Only the merged `isvctl` JUnit is uploaded; the files
above stay local.

The sosreport helper archives its output as `sosreport.tar.gz`. If it fails
before archiving, the partial `sosreport/` directory is kept instead.

## Rules for changes

- Do not add discover, generate, deploy, clean, preflight, or other lifecycle
  operations; they are prerequisites owned by Launch Kit and the ISV. Tests that
  change Network Operator state need a separate restore design first.
- Do not model or duplicate Launch Kit flags, schema, or defaults, and never
  parse the user config (infer fabric from native JUnit suite names).
- A missing prerequisite (an input or `l8k`) skips. Once `l8k validate` has
  started, missing or malformed JUnit, no executed connectivity cases, or a
  failed command must fail, never pass vacuously.
- Do not invent results or reinterpret Launch Kit's verdict.
- `l8k clean` is the only supported deletion path; never reproduce Launch Kit
  cleanup with kubectl.
- The PRD source is
  `docs/requirements/network-operator-readiness-requirements.yaml`; its
  traceability edges live in `docs/requirements/test-requirements-matrix.yaml`.
  Regenerate committed views with `make plan`.
