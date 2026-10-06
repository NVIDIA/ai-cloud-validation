<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Network Operator connectivity validation through Kubernetes Launch Kit

## Scope

Network Operator validation is the `network_operator` group of the Kubernetes
suite (`isvctl/configs/suites/k8s.yaml`), not a suite of its own. It performs one
validation operation:

```text
l8k validate --user-config <complete-config> --deployment-files <rendered-directory>
```

It reports the connectivity matrix produced by Launch Kit, then collects
diagnostics after every validate that started, whatever its outcome:

```text
l8k sosreport --output-dir <artifact-directory>/sosreport
```

The diagnostic command runs whether validation passes, returns an error, or
produces a failing connectivity matrix. It is evidence collection, not a
catalog test: the `LaunchKitSosreport` check reports it, and the catalog
excludes that check. The validation does not install
or verify the `l8k` binary, discover topology, generate manifests, deploy
Network Operator, run a separate Kubernetes preflight, or clean cluster state.

Those activities are prerequisites. Before starting the validation, the ISV must
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

The `l8k` installation must also make the upstream
`kubectl-netop_sosreport` helper available to `l8k sosreport`. Validate this
once with a direct `l8k sosreport --output-dir <temporary-directory>` call. If
Launch Kit reports that the script is missing, install the helper below the
same installation prefix at `share/l8k/scripts/kubectl-netop_sosreport` before
running the validation.

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

Paths accept `~`, but absolute paths are preferable in automation. The runner
resolves both paths, verifies that `user_config` is a file and
`deployment_files` is a directory, and passes the resolved paths to Launch Kit.
It does not copy, merge, parse, or modify either input.

`l8k` must be on `PATH`. It runs with the isvctl process environment, so the
cluster is selected the same way as for every other Kubernetes check (for
example `KUBECONFIG`).

Launch Kit owns every setting inside the complete config, including the
selected profile, validation mode, enabled checks, GPUDirect behavior,
bandwidth thresholds, per-operation timeouts, routing, IP pools, and resource
names. AI Cloud Validation stores no copies of those defaults.

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

Likewise, the validation does not infer a fabric or deployment mode from labels.
Run it once for the exact cluster state described by the supplied files. To
validate another topology, provision that topology and run again with its
config and deployment directory.

## Timeouts

`l8k validate` runs without an isvctl deadline. Launch Kit calculates and logs
its connectivity-matrix budget by default, or honors the timeout configured by
the user, so an independent watchdog cannot terminate a valid large matrix
before Launch Kit's bounded checks finish. An enclosing CI job may still impose
an overall job timeout.

`l8k sosreport` has a 30-minute limit, because the current Launch Kit sosreport
command does not calculate its own total deadline.

## Results and errors

Use a Launch Kit binary supporting `validate --junit-path`
(NVIDIA/k8s-launch-kit#288). Launch Kit writes a `network/validation` suite of
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

Sosreport runs right after validate, before any result is read, whatever
validate's outcome. `LaunchKitSosreport` reports it: a failed collection fails
that check and therefore the test phase, while every connectivity and
deployment result stays intact. When validate could not start, sosreport is not
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

`command.json` records the resolved argv, exit code, and duration. `stdout.txt`
contains Launch Kit's complete JSON stream, including static validation,
connectivity, and report-path documents; `stderr.log` retains CLI progress and
diagnostics. The runner uses the emitted `reportPath` as the authoritative
source and copies the HTML file to `k8s-launch-kit-validation-report.html`. The
original report remains at the path written by Launch Kit, normally below the
supplied deployment directory. A report emitted for a failed connectivity
matrix is copied in the same way. If Launch Kit advertises a report that cannot
be read, the run reports an evidence-retention error instead of silently
reusing an older report.

The native JUnit report is read unmodified. A stale report is removed before
each validation; a missing or malformed report is an evidence error even when
the process exits successfully. Reports emitted by failing runs are retained
and imported too. The main merged JUnit file is uploaded through the existing
reporting service; the separate native XML and HTML files remain local
evidence artifacts.

The Network Operator sosreport helper collects into `sosreport/`, archives it
as `sosreport.tar.gz` beside it, and removes the directory. A stale archive is
removed before each collection. If the helper fails before archiving, the
partial `sosreport/` directory is kept instead. The sosreport command streams
human-readable output even when the global `--output` flag is available. The
runner preserves that stream in `commands/sosreport/stdout.txt` and does not
attempt to reinterpret the diagnostic contents.

## Rules for changes

- Do not add discover, generate, deploy, clean, preflight, or other lifecycle
  operations; they are prerequisites owned by Launch Kit and the ISV.
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

## PRD boundary

This integration covers reportable Launch Kit connectivity validation. It
deliberately treats topology discovery, manifest generation, installation,
deployment health preparation, profile selection, and restoration as external
prerequisites. Tests that intentionally mutate Network Operator state require a
separate transaction and restoration design before they can be added.
