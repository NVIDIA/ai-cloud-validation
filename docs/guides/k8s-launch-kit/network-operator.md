<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Network Operator connectivity validation through Kubernetes Launch Kit

## Scope

The Network Operator suite performs one validation operation:

```text
l8k validate --user-config <complete-config> --deployment-files <rendered-directory>
```

It reports the connectivity matrix produced by Launch Kit. After that command
is attempted, an always-run linked finalizer invokes:

```text
l8k sosreport --output-dir <artifact-directory>/sosreport
```

The diagnostic command runs whether validation passes, returns an error, or
produces a failing connectivity matrix. It is evidence collection, not a
second catalog test. The suite does not install
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
  -> validation step
     -> adapter.py
        -> l8k validate --user-config ... --deployment-files ... --output json
        -> retained argv, stdout, stderr, exit code, duration, and HTML report
  -> Network Operator suite YAML
     -> nine catalog tests, one per native Launch Kit JUnit suite
        -> one subtest for every case in that suite
  -> linked finalizer, after the catalog assertions
     -> adapter.py
        -> l8k sosreport --output-dir .../evidence/sosreport
        -> retained diagnostic directory, stdout, stderr, exit code, and duration
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

The `l8k` installation must also make the upstream
`kubectl-netop_sosreport` helper available to `l8k sosreport`. Validate this
once with a direct `l8k sosreport --output-dir <temporary-directory>` call. If
Launch Kit reports that the script is missing, install the helper below the
same installation prefix at `share/l8k/scripts/kubectl-netop_sosreport` before
running the suite.

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
labels. For example, disabling Launch Kit GPUDirect validation makes Launch Kit
emit a skipped `K8sEastWestNetworkDMABufBandwidth-<fabric>` case, and that
catalog test skips with Launch Kit's reason.

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

The `launch_kit_sosreport` finalizer has a 30-minute orchestration watchdog.
Unlike connectivity validation, the current Launch Kit sosreport command does
not calculate its own total deadline. A timeout or sosreport command error is
reported as a separate `test-teardown` orchestration failure; it does not
replace the connectivity test result.

## Results and errors

Use a Launch Kit binary supporting `validate --junit-path`
(NVIDIA/k8s-launch-kit#288). Launch Kit writes a `network/validation` suite of
deployment-state cases and one `K8sEastWestNetwork<Family>-<fabric>` suite per
connectivity family for the configured fabric. The suite wires one catalog
test per native suite, using the same `report_subtest` mechanism as
Kubernetes conformance tests:

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

The sosreport finalizer runs after these assertions. If sosreport itself fails,
the connectivity result remains intact and the overall orchestration reports
the diagnostic-collection failure separately.

## Evidence

The adapter writes:

```text
_output/k8s-launch-kit/network-operator/
  work/
  evidence/
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
    sosreport/
      ... files produced by the Network Operator sosreport helper ...
```

`command.json` records the resolved argv, exit code, and duration. `stdout.txt`
contains Launch Kit's complete JSON stream, including static validation,
connectivity, and report-path documents; `stderr.log` retains CLI progress and
diagnostics. The adapter uses the emitted `reportPath` as the authoritative
source, copies the HTML file to
`evidence/k8s-launch-kit-validation-report.html`, and registers the copied path
as the `validation_report` artifact. The original report remains at the path
written by Launch Kit, normally below the supplied deployment directory. A
report emitted for a failed connectivity matrix is copied in the same way. If
Launch Kit advertises a report that cannot be read, the provider returns an
evidence-retention error instead of silently reusing an older report.

The native JUnit report is registered unmodified as the `validation_junit`
artifact. A stale report is removed before each validation;
a missing or malformed report is an evidence error even when the process exits
successfully. Reports emitted by failing runs are retained and imported too.
The main merged JUnit file is uploaded through the existing reporting service;
the separate native XML and HTML files remain local evidence artifacts.

The sosreport command currently streams human-readable output even when the
global `--output` flag is available. The adapter therefore preserves that
stream in `commands/sosreport/stdout.txt` and emits its own normal structured
step envelope; it does not attempt to reinterpret the diagnostic contents.

## PRD boundary

This integration covers reportable Launch Kit connectivity validation. It
deliberately treats topology discovery, manifest generation, installation,
deployment health preparation, profile selection, and restoration as external
prerequisites. Tests that intentionally mutate Network Operator state require a
separate transaction and restoration design before they can be added.
