<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Network Operator validation through Kubernetes Launch Kit

## Status and safety boundary

This integration ships a generic Kubernetes Launch Kit provider and a
Network Operator configuration that invoke real `l8k` and `kubectl` binaries.
It also adds catalog checks, requirement traceability, and mock-backed unit
tests. The mocks are not reachable from product configuration: tests load the
production YAML and inject test-owned executables in memory.

The production workflow reaches a Kubernetes cluster and mutates it during
`l8k deploy`. Each `l8k clean` step is declared in the teardown phase and linked
to its deployment. After every attempted deployment, the orchestrator runs that
cleanup directly after the use case's validations and reports a distinct
`<use-case>-teardown` result. Launch Kit deletes Network Operator custom
resources, waits for their finalizers, and uninstalls the Helm release last.
Cleanup is destructive and covers the complete resolved operator namespace plus
Launch Kit's known cluster-scoped Network Operator resources. Use a dedicated
qualification cluster.

`l8k clean` removes the test deployment but does not snapshot and restore
pre-existing state, so the provider still does not fully satisfy ENT-REQ-010.
A green unit test proves the AI Cloud Validation integration and reporting
path; it is not certification evidence.

The RoCE SR-IOV use case has also been exercised end to end on a two-node
Ubuntu 24.04 single-rail cluster. Launch Kit completed discovery, generation,
deployment, validation, and cleanup. The multi-rail member was reported as
skipped because the discovered topology contained one rail; the remaining
members and the parent use case passed. The other five use cases retain their
mock-backed integration status until they are qualified on applicable live
clusters.

## Framework architecture

AI Cloud Validation separates command execution from result interpretation:

```text
provider configuration
  -> ordered isvctl steps
       -> setup: l8k installation verification
       -> test prerequisite: repeat l8k version/schema verification
       -> one named phase per Launch Kit use case
            -> kubectl prerequisite probes
            -> l8k discover
            -> l8k generate
            -> l8k deploy
            -> l8k validate
            -> generic adapter.py transport
            -> phase validations
            -> linked teardown: l8k clean (when deploy was attempted)
       -> raw JSON transport envelopes stored as step outputs
  -> suite wiring
       -> globally reusable PRD validation classes
       -> one CompositeCheck-backed test per concrete use case
            -> member and probe-level pytest subtests
            -> catalog pass/fail/skip
  -> JUnit, run logs, catalog, and optional AI Cloud Labs reporting
```

The main files are:

| Layer | File |
|---|---|
| Generic provider | `isvctl/configs/providers/k8s-launch-kit/config/provider.yaml` |
| Production Network Operator provider | `isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml` |
| CLI transport | `isvctl/configs/providers/k8s-launch-kit/scripts/adapter.py` |
| Individual PRD check wiring | `isvctl/configs/suites/k8s-launch-kit/network-operator.yaml` |
| End-to-end use-case wiring | `isvctl/configs/suites/k8s-launch-kit/network-operator-use-cases.yaml` |
| Result interpretation | `isvtest/src/isvtest/validations/k8s_launch_kit/checks.py` |
| Provider unit tests | `isvctl/tests/providers/k8s_launch_kit/test_provider.py` |
| Executable test doubles and data | `isvctl/tests/providers/k8s_launch_kit/fixtures/` |
| Result-check unit tests | `isvtest/tests/k8s_launch_kit/test_checks.py` |
| PRD source | `docs/requirements/network-operator-readiness-requirements.yaml` |
| Traceability | `docs/requirements/test-requirements-matrix.yaml` |

Both suite files are plain suites with Kubernetes `requires` metadata. The
provider supplies a `network_operator` command target. This keeps the catalog reusable
while capability selection (`--capability kubernetes`) prevents the steps and
checks from running in VM, bare-metal, or Slurm contexts. Provider and suite
files are intentionally separate: a live overlay imports the provider plus the
individual suite or a program-specific use-case suite.

## Reusable framework changes

The productization audit kept only behavior that is useful outside Launch Kit:

| Change | Abstraction owner | Reuse contract |
|---|---|---|
| Named custom test phases | `isvctl.orchestrator` | Any configured phase other than setup/teardown is selected by `--phase test`, executes in declared order, and retains its configured name in results. |
| `continue_after_failure` | `isvctl.config.PlatformCommands` and orchestrator | Independent test-case phases may continue collecting evidence after a failure without changing the failed final verdict. |
| `finalizer_for` | `isvctl.config.StepConfig` and orchestrator | A cleanup step declared in teardown runs directly after its target phase validations when the mutating process started, gets a separate teardown result, and can also run through explicit teardown-only recovery. |
| Selection-aware step gate and failure ownership | `isvctl.config.StepConfig` and orchestrator | `requires_selected_validations` skips a lifecycle step unless its named validation remains selected after release, capability, label, and suite-exclusion filtering. A failed selected step becomes a `step_failed` JUnit error on the owning validation instead of a later missing-output skip. |
| Nested composite probes and member skips | `isvtest.core.CompositeCheck` | A composed member's own subtests are forwarded as `MemberName/probe-name`. A member-level `pytest.skip` is reported as a skipped member while the composite continues and evaluates its remaining members. `CompositeCheck` itself predates this integration. |
| Structured subtest summary | `isvtest.core.ResolvedEntry` and the `isvctl` renderer | Any validation with subtests gets passed/failed/skipped counts; successful terminal lines are concise while failures keep diagnostics. |
| Filtered-summary visibility | `isvctl` renderer | `tests.settings.show_skipped_tests: false` omits terminal phases containing only selection-filtered validations while retaining them in structured results and JUnit. |
| JUnit counter reconciliation | `isvtest.testing.subtests` | After subtest testcase injection, suite counters are derived from serialized testcase nodes so pytest's pre-counted subtests are not counted twice. |
| Project-PRD rendering | `scripts/requirements_source_to_md.py` | A uniquely named requirement source can set `format: project-prd`; future PRDs reuse the renderer without adding source-specific code. |
| Process-tree timeout enforcement | `isvctl.orchestrator` | On POSIX, timed-out steps terminate their complete process group, so a wrapper cannot leave its provider CLI running after orchestration has moved on. |

These APIs are documented for other suites in the
[configuration guide](../configuration.md) and
[`isvtest` package guide](../../packages/isvtest.md). Launch Kit's transport,
output schema, checks, and mocks remain provider/domain-specific and are not
framework features.

## Provider API: the actual Launch Kit workflow

The provider does not expose a different Network Operator abstraction. Its
steps follow the CLI workflow directly:

1. `launch_kit_prepare` — optionally install, then verify Launch Kit;
2. `launch_kit_verify` — verify again in the test phase, because a test-only run
   can skip setup;
3. `launch_kit_kubernetes_preflight` — prove the Kubernetes prerequisite;
4. `launch_kit_discover` — invoke `l8k discover`;
5. `launch_kit_generate` — invoke `l8k generate`;
6. `launch_kit_deploy` — invoke `l8k deploy`;
7. `launch_kit_validate` — invoke `l8k validate`;
8. `launch_kit_clean` — declare `l8k clean` in teardown, linked to the deploy
   step and executed immediately after that use case's phase validations.

The public context is deliberately small:

| Key | Meaning |
|---|---|
| `executable` | Existing `l8k` command or path |
| `installation.mode` | `verify` (default) or explicitly requested `install` |
| `installation.version` | Optional release/tag passed to the official installer path |
| `installation.installer_ref` | Full 40-character Git commit SHA for the official installer; required in install mode |
| `installation.installer_sha256` | Trusted SHA-256 of the installer at `installer_ref`; required in install mode |
| `installation.prefix` | Optional install prefix |
| `user_config` | Optional absolute path to a complete Launch Kit configuration outside the retained working directory; staged transiently for every selected use case |
| `kubectl_command` | Optional kubectl-compatible argv prefix, mainly for controlled environments/tests |
| `working_dir` | Directory in which l8k resolves relative inputs and writes outputs |
| `artifact_dir` | Directory for provider command evidence |
| `environment` | String environment entries forwarded to installation, verification, l8k workflows, and the matching Kubernetes preflight |
| `<command>.arguments` | Raw string argv for `discover`, `generate`, `deploy`, `validate`, or `clean` |

There are no AI Cloud Validation defaults for namespace, node selector,
Network Operator release, driver mode, rails, resource names, IP pools, GPU
count, validation mode/checks, RDMA tuning, or l8k timeouts. A user may pass any
flag supported by the installed Launch Kit release; omitted values are resolved
by Launch Kit. The provider adds only `--output json` to workflow commands so it
can preserve structured evidence. A user-supplied non-JSON output selection is
rejected.

The `timeout` fields on provider steps are outer isvctl watchdogs. They bound a
hung child process; they are not Launch Kit configuration defaults and are not
forwarded to l8k. On POSIX, timeout handling terminates the adapter and its l8k
child process group before returning the failed step. Launch Kit-specific
timeout flags remain raw user arguments.

Launch Kit `validate` steps intentionally set this outer watchdog to `null`.
The installed l8k release calculates and logs a bounded timeout from its matrix
plan, or honors the user's explicit `--connectivity-timeout` argument. Other
workflow steps retain finite isvctl watchdogs, and an enclosing CI job may
still impose its own deadline.

The configured executable is invoked directly. A path ending in `.py` receives
no special treatment; executable test doubles must use a shebang and executable
file mode. This prevents test-only behavior from becoming part of the live
provider API.

### Complete user configuration

Set `context.k8s_launch_kit.user_config` when a site needs values that are not
exposed as Launch Kit CLI flags. The file must be a complete Launch Kit
configuration; AI Cloud Validation does not merge partial YAML fragments or
interpret its fields. Use an absolute path available on the machine executing
the suite.

For each selected use case, the adapter copies the source to
`<working_dir>/user-config.yaml` with mode `0600`, then invokes discovery with:

```text
l8k discover <use-case arguments> \
  --user-config <working_dir>/user-config.yaml \
  --save-cluster-config <working_dir>/cluster-config.yaml \
  --output json
```

The source is not modified. The staged copy is deleted as soon as discovery
exits, whether it succeeds or fails; retained evidence contains only the input
path, byte size, and SHA-256 provenance. The source must therefore live outside
the retained provider working directory. Discovery refreshes hardware and
applies the use-case's explicit fabric/deployment selectors, while Launch Kit
preserves the settings from the staged input according to its own precedence
rules. The resolved `cluster-config.yaml` is then consumed through Launch Kit's
normal default paths by generate, deploy, validate, and clean. When
`user_config` is set, raw discovery arguments must not also contain
`--user-config` or `--save-cluster-config`.

### Example live overlay

Keep site and program choices in a user-owned overlay instead of adding them to
the generic provider:

```yaml
import:
  - isvctl/configs/providers/k8s-launch-kit/config/provider.yaml
  - isvctl/configs/suites/k8s-launch-kit/network-operator.yaml

context:
  k8s_launch_kit:
    executable: /opt/nvidia/bin/l8k
    user_config: /secure/partner-cluster-config.yaml
    working_dir: /var/tmp/aicv-launch-kit/work
    artifact_dir: /var/tmp/aicv-launch-kit/evidence
    discover:
      arguments:
        - --kubeconfig
        - /secure/partner.kubeconfig
        - --fabric
        - ethernet
        - --deployment-type
        - sriov
    generate:
      arguments: []
    deploy:
      arguments: [--kubeconfig, /secure/partner.kubeconfig]
    validate:
      arguments: [--kubeconfig, /secure/partner.kubeconfig]
    clean:
      arguments: [--kubeconfig, /secure/partner.kubeconfig]
```

These values are explicit inputs for that run. The complete user config is
copied into the isolated working directory only for discovery, then removed;
its values are not copied into evidence or treated as AI Cloud Validation
defaults.

## Download, install, and verification

`installation.mode: verify` resolves the configured executable from an explicit
path or `PATH`, then requires both commands to succeed and emit one JSON object:

```text
l8k version --output json
l8k schema
```

When `installation.version` is non-empty, verification also requires the
reported `version` field to match it exactly. The test-phase verification
repeats this check, so `--phase test` cannot bypass a configured version pin.
The schema must advertise `discover`, `generate`, `deploy`, `validate`, and
`clean`; a pre-clean Launch Kit binary is rejected before cluster mutation.

`installation.mode: install` is opt-in. The provider downloads
`scripts/install.sh` from the official NVIDIA Kubernetes Launch Kit repository
at the full Git commit in `installation.installer_ref`. It requires a trusted
`installation.installer_sha256`, records the source and actual digest, and
fails before writing or executing the script unless they match. Mutable refs,
tags, and abbreviated commits are rejected. The upstream script remains
responsible for release/archive selection, checksum verification, and
installing the binary and profiles. The provider then performs the same version
and schema verification against the binary at `<prefix>/bin/l8k` (or
`/usr/local/bin/l8k` when no prefix is supplied), rather than accepting an older
binary found elsewhere on `PATH`. Network access and permissions required by
the upstream installer remain operator prerequisites.

The installed schema is captured as evidence. This makes version/capability
drift diagnosable without teaching AI Cloud Validation Launch Kit's flag
defaults.

Keep `verify` as the default. Production automation that enables `install`
must obtain the immutable installer commit and its SHA-256 through a trusted
release channel; computing a digest from the same download is not trust.

An overlay opts into installation explicitly:

```yaml
context:
  k8s_launch_kit:
    executable: /opt/nvidia/bin/l8k
    installation:
      mode: install
      version: v0.1.0
      installer_ref: "<full 40-character Git commit SHA>"
      installer_sha256: "<trusted 64-character SHA-256>"
      prefix: /opt/nvidia
```

With `prefix` omitted, the upstream installer uses its own installation
default. With `version` omitted, it resolves its own latest supported release;
`installer_ref` and `installer_sha256` remain mandatory in install mode.

## Mandatory Kubernetes prerequisite

Every normal test use case verifies the cluster before `discover`, `generate`,
`deploy`, or `validate` can run. Its linked `clean` then acts on the same
working directory and cluster selection. The preflight:

1. scans all five raw argument arrays for `--kubeconfig` and
   `--kubeconfig=<path>`;
2. fails if different workflow commands select different kubeconfigs;
3. forwards the same `environment` mapping used for l8k and otherwise uses
   kubectl's normal environment/default resolution when no kubeconfig is
   explicit;
4. runs `kubectl version -o json` and requires
   `serverVersion.gitVersion`;
5. runs `kubectl get nodes -o json` and requires at least one node and at least
   one node with `Ready=True`.

Each probe stores argv, stdout, and stderr and becomes a pytest subtest. Normal
steps within a use case are sequential and stop on failure. Therefore a failed
preflight, discovery, or generation prevents deployment and does not activate
destructive cleanup. Once deploy is attempted, `clean` runs after the phase
validations as a linked teardown regardless of the deploy, validate, or
validation verdict. A missing executable or unresolved command template is not
an attempt and cannot activate destructive cleanup. The grouped production
configuration's use cases are independent custom phases, so
`continue_after_failure` allows the next use case to start only when teardown
succeeded. The final run still records the failed case and a failed overall
verdict; a cleanup failure blocks later cases because cluster state is unknown.
An explicit `--phase teardown` run invokes the selected cleanup steps without a
prior in-memory deployment attempt or a fresh preflight, providing an
idempotent recovery command even when test prerequisites no longer pass. The
cleanup command still performs Launch Kit's own API access and safety checks.

This is intentionally a minimum prerequisite, not a replacement for Launch
Kit's topology, RBAC, device, fabric, or deployment preflight logic.

## Transport and Launch Kit output fidelity

Every provider action emits one JSON transport envelope with fields such as:

```json
{
  "success": true,
  "platform": "kubernetes",
  "operation": "deploy",
  "executable": "/opt/nvidia/bin/l8k",
  "argv": ["/opt/nvidia/bin/l8k", "deploy", "...", "--output", "json"],
  "working_directory": "/var/tmp/aicv-launch-kit/work",
  "exit_code": 0,
  "documents": [],
  "artifacts": {
    "command": ".../command.json",
    "stdout": ".../stdout.txt",
    "stderr": ".../stderr.log"
  }
}
```

`documents` are unmodified JSON objects parsed from Launch Kit stdout. The unit
fixture is pinned to the machine-output shapes and validation profile contract
audited on Launch Kit `main` at commit `db32e4b98170`:

| Command | Current machine stdout |
|---|---|
| `discover` | One `ui.JSONResult` containing the resolved profile |
| `generate` | One `ui.JSONResult` containing generated file paths |
| successful standalone `deploy` | Empty stdout; progress remains on stderr |
| `validate` | Three concatenated objects: static validation, connectivity matrix, and report path |
| `clean` | One `ui.JSONResult` containing namespace, custom-resource deletion count, Helm removal status, and Helm-retention choice |

The transport parses concatenated JSON without renaming PascalCase Launch Kit
fields such as `PingResults`, `Family`, `ObservedOK`, and `BandwidthGbps`.
GPUDirect DMA-BUF rows remain in that same matrix with
`Family: gpudirect_dmabuf` and endpoint GPU indices/PCI addresses. Semantic
knowledge stays in the pytest validations, not the provider. The envelope also
records the absolute command working directory so evidence checks can resolve
relative paths emitted by Launch Kit without rewriting the documents.

## Production use cases and mock-backed unit tests

The production `config/network-operator.yaml` imports the generic provider and
the use-case suite, then replaces the generic single-workflow command list with
six named phases. It inherits `executable: l8k` and an empty
`kubectl_command`, which means normal `kubectl` resolution from `PATH`. The YAML
contains only the fabric/deployment flags that define each test identity.
Launch Kit owns the default `./cluster-config.yaml` and `./deployment` paths
across `discover -> generate -> deploy -> validate -> clean`; the grouped
`generate`, `deploy`, `validate`, and `clean` argument arrays are therefore
empty. Each use case runs in an isolated working directory, so these defaults
cannot collide.

Provider unit tests load this same YAML, replace the two executable settings in
memory, and run it against `mock_l8k.py` and `mock_kubectl.py`. The test double
produces a discovered configuration, generated manifests, static resource
results, connectivity results, an HTML report, and the current Launch Kit
cleanup summary. Representative resolved values in mock output belong to the
test fixture's Launch Kit side; they are not provider defaults.

The fixture supports these explicit profiles:

| Fabric | Deployment | Secondary network kind |
|---|---|---|
| Ethernet/RoCE | `sriov` | `SriovNetwork` |
| InfiniBand | `sriov` | `SriovIBNetwork` |
| Ethernet/RoCE | `rdma_shared` | `MacvlanNetwork` |
| InfiniBand | `rdma_shared` | `IPoIBNetwork` |
| Ethernet/RoCE | `host_device` | `HostDeviceNetwork` |
| InfiniBand | `host_device` | `HostDeviceNetwork` |

One production invocation runs all six profiles in the table, in order. Every
profile has explicit raw discover arguments and its own working/evidence
directory, so one case cannot reuse another case's generated output. Its
linked `clean` teardown removes the deployed Network Operator resources before
the next test phase. The ordered result names make that boundary explicit:

```text
setup
launch-kit-verification
roce-sriov
roce-sriov-teardown
infiniband-sriov
infiniband-sriov-teardown
roce-rdma-shared
roce-rdma-shared-teardown
infiniband-rdma-shared
infiniband-rdma-shared-teardown
roce-host-device
roce-host-device-teardown
infiniband-host-device
infiniband-host-device-teardown
```

Run the production workflow against the active Kubernetes context:

```bash
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run \
  -f isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml \
  --capability kubernetes --no-upload -- -v
```

This command invokes real `l8k`, real `kubectl`, destructive `l8k deploy`, and
destructive `l8k clean`. Confirm the active kubeconfig and use a dedicated
qualification cluster. If an interrupted run left a selected deployment behind,
invoke its teardown without rerunning the test:

```bash
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run \
  -f isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml \
  --phase teardown --capability kubernetes \
  --label ethernet --label sriov --no-upload
```

This example runs only `launch_kit_roce_sriov_clean`. Omitting labels runs all
six idempotent cleanup entries in best-effort mode. Confirm the target cluster
before using either form. To validate the integration without a cluster, run
the automated tests instead:

```bash
uv run pytest \
  isvctl/tests/providers/k8s_launch_kit \
  isvtest/tests/k8s_launch_kit -q
```

The final orchestration summary has one test result and one teardown result per
use case. The verbose pytest section still contains every member/probe result:

```text
[PASS] SETUP                    : launch_kit_prepare: passed
[PASS] LAUNCH-KIT-VERIFICATION  : launch_kit_verify: passed
[PASS] ROCE-SRIOV               : ...
  [network_operator_roce_sriov] EastWestNetworkRoceSriovCheck: PASSED - 122 subtests passed
[PASS] ROCE-SRIOV-TEARDOWN      : launch_kit_roce_sriov_clean: passed
[PASS] INFINIBAND-SRIOV         : ...
  [network_operator_infiniband_sriov] EastWestNetworkInfiniBandSriovCheck: PASSED - 122 subtests passed
[PASS] INFINIBAND-SRIOV-TEARDOWN: launch_kit_infiniband_sriov_clean: passed
[PASS] ROCE-RDMA-SHARED         : ...
  [network_operator_roce_rdma_shared] EastWestNetworkRoceRdmaSharedCheck: PASSED - 117 subtests passed
[PASS] ROCE-RDMA-SHARED-TEARDOWN: launch_kit_roce_rdma_shared_clean: passed
[PASS] INFINIBAND-RDMA-SHARED   : ...
  [network_operator_infiniband_rdma_shared] EastWestNetworkInfiniBandRdmaSharedCheck: PASSED - 117 subtests passed
[PASS] INFINIBAND-RDMA-SHARED-TEARDOWN: launch_kit_infiniband_rdma_shared_clean: passed
[PASS] ROCE-HOST-DEVICE         : ...
  [network_operator_roce_host_device] EastWestNetworkRoceHostDeviceCheck: PASSED - 117 subtests passed
[PASS] ROCE-HOST-DEVICE-TEARDOWN: launch_kit_roce_host_device_clean: passed
[PASS] INFINIBAND-HOST-DEVICE   : ...
  [network_operator_infiniband_host_device] EastWestNetworkInfiniBandHostDeviceCheck: PASSED - 117 subtests passed
[PASS] INFINIBAND-HOST-DEVICE-TEARDOWN: launch_kit_infiniband_host_device_clean: passed
[PASS] All phases completed successfully
```

The launch-kit checks remain unreleased, so the environment variable is
required for development runs. Do not add them directly to
`released_tests.json`; the repository's release process owns that file.

The six use-case phases are listed under `continue_after_failure`. This is a
generic isvctl feature for independent test cases: a failed case does not hide
later cases after its linked teardown succeeds, but it still makes the final
run fail. A teardown failure blocks later cases. Setup is deliberately not
listed because all cases depend on the prepared and verified executable.

## Selectable checks, use cases, and error reporting

The suite contains one prerequisite check and fourteen currently supported
PRD-facing checks:

| Test ID | Check | Evidence |
|---|---|---|
| N/A | Kubernetes prerequisite | API version, node inventory, Ready nodes |
| K8S42-01 | Deployment health | version match, summary, every manifest row |
| K8S42-02 | SR-IOV readiness | policy plus profile-specific SR-IOV network |
| K8S42-03 | RDMA connectivity | every `rping` matrix row |
| K8S42-04 | RoCE | exact Ethernet profile network kind |
| K8S42-05 | InfiniBand | exact InfiniBand profile network kind |
| K8S42-06 | Host-device | `HostDeviceNetwork` for an applicable profile |
| K8S42-07 | GPUDirect RDMA | every `gpudirect_dmabuf` row, endpoint GPUs, PCI addresses, bandwidth, and Launch Kit threshold |
| K8S42-08 | Topology discovery | successful discover and resolved profile |
| K8S42-09 | Secondary network/IPAM | `IPPool`, exact network kind, test DaemonSet rollout |
| K8S42-10 | RDMA Shared | `MacvlanNetwork` or `IPoIBNetwork`, as selected |
| K8S42-11 | ICMP | every source-bound ICMP matrix row |
| K8S42-12 | RDMA bandwidth | every `ib_write_bw` row and Launch Kit threshold |
| K8S42-13 | Multi-rail | same-rail and cross-rail matrix coverage; skipped when the matrix contains only one distinct rail |
| K8S42-15 | Evidence | command files and Launch Kit HTML report |

These check classes are registered once and reused by each applicable use case.
The individual suite can still run them directly against one selected Launch
Kit profile; in that mode, profile-specific checks call `pytest.skip` when the
profile is not applicable. `LaunchKitMultirailCheck` also skips when Launch Kit
returns connectivity rows for only one distinct rail. `LaunchKitGpuDirectRdmaCheck`
skips when Launch Kit emits no `gpudirect_dmabuf` family because discovery
disabled `validation.gpuDirect` or `ib_write_bw` was not selected. If GPUDirect
is enabled but topology or execution fails, Launch Kit emits failed rows and
the check fails with their diagnostics. Inside a grouped use-case composite,
an inapplicable member is reported as skipped while the other checks continue;
it does not fail or skip the parent use case.

Full state restoration (`ENT-REQ-010`) remains deferred and has no test ID.
Launch Kit cleanup removes the test deployment rather than capturing and
restoring pre-existing state; full coverage still requires a snapshot,
restore, and semantic verification workflow.

The grouped suite exposes six separate catalog tests. Each composite lists only
the global checks applicable to its fabric and deployment type:

| Test ID | Use-case test | Fabric | Deployment-specific check |
|---|---|---|---|
| K8S42-16 | `EastWestNetworkRoceSriovCheck` | Ethernet/RoCE | SR-IOV readiness |
| K8S42-17 | `EastWestNetworkInfiniBandSriovCheck` | InfiniBand | SR-IOV readiness |
| K8S42-18 | `EastWestNetworkRoceRdmaSharedCheck` | Ethernet/RoCE | RDMA Shared |
| K8S42-19 | `EastWestNetworkInfiniBandRdmaSharedCheck` | InfiniBand/IPoIB | RDMA Shared |
| K8S42-20 | `EastWestNetworkRoceHostDeviceCheck` | Ethernet/RoCE | host-device |
| K8S42-21 | `EastWestNetworkInfiniBandHostDeviceCheck` | InfiniBand | host-device |

This is why the grouped output has no unrelated fabric/deployment skips in
the middle of a use case.

Every resource and connectivity row is a named pytest subtest. A connectivity
name includes family, source/destination, and source/destination rails, for
example:

```text
rping/worker-a->worker-b/rail-0->rail-1
ib_write_bw/worker-b->worker-a/rail-1->rail-1
gpudirect_dmabuf/worker-a->worker-b/rail-0->rail-0
```

Messages preserve expectation, observed outcome, stderr, and bandwidth/minimum
values. GPUDirect messages additionally preserve source/destination GPU indices
and PCI addresses when Launch Kit reports them. The validation reports every
row before aggregating failures, so a user does not need repeated runs to
discover the next failed pair.

`CompositeCheck` was already the framework mechanism behind `compose:`. This
integration extends it to forward a member's own probes as
`MemberName/probe-name`, preserving the detailed pytest and JUnit diagnostics.
It also preserves member-level applicability: when a member calls
`pytest.skip`, that member is emitted as a skipped subtest and the composite
continues. The parent can therefore pass when all of its applicable members
pass.
The shared isvctl renderer now abbreviates any successful parent that reported
subtests, for example `PASSED - 122 subtests passed`. Failed and errored parents
keep the original actionable message. There is no Launch Kit-specific or YAML
`compact_output` option.

Individual PRD-check selection is available from a run configuration that
imports the generic provider and `network-operator.yaml`, such as the live
overlay shown earlier:

```bash
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run \
  -f /path/to/my-launch-kit-overlay.yaml \
  --capability kubernetes --no-upload -- \
  -v -k LaunchKitRdmaConnectivityCheck
```

The grouped provider imports the use-case suite, so its selectable pytest identities are the
six `EastWestNetwork*Check` parents. For example, use
`-k EastWestNetworkRoceSriovCheck` to interpret only that parent. Composite member
names are detailed subtests, not separately selectable pytest identities in
that configuration.

The grouped suite can be selected with `--label network_operator` or the more
specific `--label network_operator_use_cases`. Fabric labels divide it into
three Ethernet/RoCE and three InfiniBand workflows:

```bash
# All six use cases (default)
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run \
  -f isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml \
  --capability kubernetes --no-upload -- -v

# Ethernet/RoCE only
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run \
  -f isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml \
  --capability kubernetes --label ethernet --no-upload -- -v

# InfiniBand only
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run \
  -f isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml \
  --capability kubernetes --label infiniband --no-upload -- -v
```

The `roce` label remains available as a more specific alias for the Ethernet
subset. Each use-case step declares its owning composite through
`requires_selected_validations`, so the other fabric's preflight, discover,
generate, deploy, validate, and clean commands do not execute. With no fabric
label, all six validations are selected and all workflows run.

Deployment-mode labels select the corresponding Ethernet/RoCE and InfiniBand
pair:

| Selection | Workflows |
|---|---|
| `--label sriov` | RoCE SR-IOV and InfiniBand SR-IOV |
| `--label rdma_shared` | RoCE RDMA Shared and InfiniBand RDMA Shared/IPoIB |
| `--label host_device` | RoCE host-device and InfiniBand host-device |
| `--label gpudirect` | All six workflows; the GPUDirect member runs or skips from Launch Kit output in each |

Labels compose with all-match semantics. For example,
`--label ethernet --label sriov` runs only
`EastWestNetworkRoceSriovCheck`, while
`--label infiniband --label rdma_shared` runs only
`EastWestNetworkInfiniBandRdmaSharedCheck`.

The `era` and `ncp` labels are present for future program policy.
Required-versus-optional program profiles are not yet typed metadata. Pytest
`-k`/`-m` selection is still applied after lifecycle steps and therefore does
not prune workflows; use `--label` for mutating suite selection.

## Evidence and reporting

The provider records these files per action or command:

- exact argv, exit code, and duration in `command.json`;
- raw stdout, including Launch Kit JSON, in `stdout.txt`;
- raw stderr in `stderr.log`;
- immutable installer source, expected/actual SHA-256, and verification result
  when installation is requested;
- version and schema responses;
- Kubernetes preflight output;
- safe `user_config` provenance (path, size, and SHA-256), when configured, and
  Launch Kit's resolved `cluster-config.yaml`; the raw staged input is removed;
- generated manifests, validation report, and other files written beneath the
  configured working directory.

The evidence validation checks command artifact paths and the report path
emitted by Launch Kit. JUnit, run logs, and catalog status flow through existing
AI Cloud Validation reporting. AI Cloud Labs does not currently upload the
arbitrary evidence directory as a binary attachment set; that remains an
explicit gap.

In the grouped production run, shared installation/version/schema evidence is stored once under
`_output/k8s-launch-kit/network-operator/shared-evidence`. Each use case then stores its command
evidence and Launch Kit files below
`_output/k8s-launch-kit/network-operator/use-cases/<use-case>/`. These files are validated and
referenced locally; they are not yet registered as Labs binary attachments.

The generic provider does not recursively delete a working or evidence
directory, because those paths are user-controlled and may contain retained
evidence. Use run-scoped directories when historical separation is required.
Current-step command files are overwritten, and a failed prerequisite cannot
reuse stale files as a passing validation because downstream steps have no
current output and are skipped.

## Cluster prerequisites

| Area | Required state |
|---|---|
| All live checks | Reachable Kubernetes API, authorization to list nodes, non-empty cluster, at least one Ready node, consistent kubeconfig across workflow commands |
| Discovery/generation | Permissions required by Launch Kit discovery; supported Kubernetes and Launch Kit versions; access to required profiles/config inputs |
| Deployment health | Helm/Kubernetes mutation and read permissions, image registry access, supported Network Operator release |
| SR-IOV | Supported NVIDIA NICs and VFs, SR-IOV components, Multus, secondary-network CRDs, and applicable IPAM |
| RDMA Shared | RDMA Shared device-plugin resource on enough workers; Macvlan for RoCE or IPoIB for InfiniBand |
| RoCE | Configured Ethernet fabric, valid GIDs, and required lossless QoS/PFC/ECN outside Kubernetes |
| InfiniBand | Active fabric and subnet manager, valid P_Keys, IB VFs or shared devices, and IPoIB when selected |
| Host-device in VMs | Supported Ethernet or InfiniBand devices passed through to worker VMs and allocatable through host-device networking |
| Connectivity/bandwidth | At least two applicable schedulable workers, test image availability, secondary addresses, RDMA device mapping, `ping`, `rping`, and `ib_write_bw` support |
| Multi-rail | At least two distinct rails in Launch Kit connectivity output; the multi-rail check is skipped on a single-rail topology |
| Cleanup | Permission to list CRDs and list/get/delete Network Operator custom resources cluster-wide, process their finalizers, and manage the `network-operator` Helm release in the resolved namespace |
| GPUDirect RDMA | At least two GPU workers, allocatable `validation.gpuDirect.gpuResourceType` on every targeted worker, compatible CUDA/DMA-BUF and GPU/NIC topology, unambiguous per-rail `connectedGPU` mappings, the full-runtime DOCA validation image, and its pull Secret in every validation namespace. Discovery enables the check only when every worker can satisfy the topology-derived GPU request. |
| State restoration | Cleanup removes the test deployment; preserving and restoring pre-existing state still requires a future Launch Kit transaction API |

## PRD coverage and remaining gaps

| Requirement | Current result |
|---|---|
| ENT-REQ-000/001 | Generic provider boundary and normal isvctl/pytest workflow exist; long-term Network Operator ownership is organizational. |
| ENT-REQ-002 | Reuses Launch Kit discover, manifest readiness, ICMP, `rping`, host-memory `ib_write_bw`, and GPUDirect DMA-BUF output. |
| ENT-REQ-003 | Individual PRD checks and six grouped end-to-end use-case tests exist; independent phases continue to collect all case results. Fabric labels prune unselected lifecycle commands; typed Enterprise/NCP required/optional profiles remain a gap. |
| ENT-REQ-004 | Users pass raw supported l8k arguments; Launch Kit owns flags and defaults. |
| ENT-REQ-005/006 | Production wiring covers SR-IOV and RDMA Shared resources plus ICMP, `rping`, and bandwidth for RoCE and InfiniBand; unit fixtures exercise every path, and RoCE SR-IOV has been exercised on a live single-rail cluster. The other fabric/deployment profiles still require live qualification. Current l8k output has no dedicated pod RDMA-device inventory result, so an explicit device-availability contract remains. |
| ENT-REQ-007 | Production wiring and unit fixtures cover both host-device fabrics; live worker-VM qualification remains. |
| ENT-REQ-008 | `K8S42-07` consumes Launch Kit's `gpudirect_dmabuf` matrix, including endpoint GPU topology, bandwidth, threshold, and errors. The integration and mocks are qualified; representative live GPUDirect hardware qualification remains. |
| ENT-REQ-009 | Version, summary, manifest rows, IPAM/network kinds, and test workload readiness are interpreted. |
| ENT-REQ-010 | Partially addressed: teardown-linked `l8k clean` runs after every attempted deployment, including failure, and waits for CR finalizers before Helm uninstall. Full coverage remains deferred because cleanup does not capture and restore pre-test state. |
| ENT-REQ-011/012 | Catalog YAML, IDs, labels, Kubernetes dependencies, descriptions, traceability, and prerequisite documentation exist; typed owner metadata remains a catalog gap. |
| ENT-REQ-013 | Pass/fail/skip, subtests, logs, JUnit, catalog, and local evidence integrate; Labs binary attachment upload remains a gap. |

The unit tests are cluster-free because they inject test executables and give
every use case an isolated temporary directory. The grouped product YAML is not
cluster-free: it runs six mutation workflows sequentially on one live cluster,
with `l8k clean` between attempted deployments. Use a dedicated qualification
cluster because cleanup intentionally removes the complete Network Operator
deployment boundary and does not restore any installation that existed before
the run.

## Recommended framework and Launch Kit improvements

1. Extend lifecycle pruning to structured selectors beyond labels. Mutating
   steps can declare `requires_selected_validations`, but raw pytest `-k`/`-m`
   expressions remain intentionally downstream of lifecycle execution.
2. Add typed catalog fields for owner, dependencies, applicability, and
   `profiles.{era,ncp}.requirement` rather than encoding all policy in labels.
3. Add a redacted attachment manifest with path, media type, checksum, size,
   retention, and upload status. Explicitly exclude kubeconfigs, Secrets,
   tokens, and private-registry credentials.
4. Extend Launch Kit cleanup with an idempotent snapshot/restore/verify workflow with a
   semantic post-restore diff. AI Cloud Validation should orchestrate that API,
   not reimplement Network Operator state handling.
5. Replace the unversioned three-document validate stream with one versioned
   envelope containing verdict, checks, warnings, artifacts, and report path,
   while keeping a compatibility parser for older releases.
6. Add explicit executed/disabled result-family metadata to the versioned
   validate envelope so consumers need not infer disabled GPUDirect from an
   absent `gpudirect_dmabuf` family.
7. Add nested progress or resumable command events so Labs can display long
   deploy/validate execution and retry validation without repeating deployment.

## Production exit criteria

1. qualify the generic provider against a released Launch Kit binary and a real
   representative cluster;
2. agree on and version the Launch Kit machine-output compatibility contract;
3. publish supported immutable installer refs and SHA-256 values through a
   trusted release channel;
4. implement and failure-inject Launch Kit-owned state restoration;
5. add secure evidence attachment upload;
6. define Enterprise and NCP required/optional subsets;
7. live-qualify GPUDirect DMA-BUF across representative GPU/NIC topologies;
8. qualify every required fabric/deployment profile on representative hardware;
9. qualify sequential use cases and failure-inject the linked `l8k clean` teardown on
   representative clusters;
10. release the checks through the normal repository release process.
