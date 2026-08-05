# Configuration Guide

This guide covers the configuration file format and options for AI Cloud Validation suite.

## Overview

Configuration files define what tests to run and how to run them. They use YAML format with:

- **Step-based execution** - Scripts perform operations and output JSON
- **Schema validation** - Output is validated against auto-detected or explicit schemas
- **Advanced validations** - Field checks, state verification, cross-step comparisons
- **Phase ordering** - Define custom phases that execute in order
- **Centralized validations** - All validations in `tests.validations` section
- **Template variables** - Reference step outputs and settings via Jinja2

## Architecture

```text
┌──────────────────────────────────────────────────────────────────┐
│                     Step-Based Execution                         │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Config (YAML)          Scripts (Any Language)    Validations    │
│  ┌──────────────┐      ┌──────────────────┐     ┌────────────┐   │
│  │ phases: [...]│      │ provision.py     │     │ Check JSON │   │
│  │ steps:       │─────▶│ create_vpc.py    │────▶│ output for │   │
│  │   - name: x  │      │ launch_vm.sh     │     │ success    │   │
│  │     phase    │      │ check_api.py     │     │            │   │
│  │     command  │      └──────────────────┘     └────────────┘   │
│  └──────────────┘              │                      │          │
│                                │                      │          │
│                                ▼                      ▼          │
│                         JSON Output              Pass/Fail       │
│                         {"success": true,        assertions      │
│                          "vpc_id": "vpc-xxx"}                    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Benefits:**

- **Language-agnostic** - Scripts can be Python, Bash, Go, etc.
- **Simple validations** - Just check JSON output fields
- **Reusable scripts** - Same script works across configs
- **Easy debugging** - Run scripts manually, inspect JSON

## Example Configs

Pre-built configs are provided in `isvctl/configs/`:

| Config | Description |
| ------ | ----------- |
| `providers/my-isv/config/*.yaml` | [my-isv scaffold](../../isvctl/configs/providers/my-isv/scripts/README.md) - source template for `isvctl provider scaffold <provider-name>` (runs end-to-end under `ISVCTL_DEMO_MODE=1`) |
| `providers/aws/config/control-plane.yaml` | AWS API health, access key lifecycle, tenant management |
| `providers/aws/config/network.yaml` | AWS VPC network validation (6 test suites) |
| `providers/aws/config/vm.yaml` | AWS EC2 GPU instance tests |
| `providers/aws/config/iam.yaml` | AWS IAM user lifecycle |
| `providers/aws/config/eks.yaml` | AWS EKS with GPU nodes |
| `providers/k8s-launch-kit/config/provider.yaml` | Generic Kubernetes Launch Kit workflow |
| `providers/k8s-launch-kit/config/network-operator.yaml` | Six Network Operator Launch Kit use cases |
| `suites/k8s.yaml` | Standard Kubernetes cluster |
| `suites/k8s-launch-kit/*.yaml` | Launch Kit-specific Network Operator catalog wiring |
| `suites/slurm.yaml` | Slurm HPC cluster |

## Basic Usage

```bash
# Generate a provider scaffold
isvctl provider scaffold acme

# Run a config
isvctl test run -f isvctl/configs/providers/aws/config/control-plane.yaml

# Merge multiple configs (later files override earlier ones)
isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml -f my-overrides.yaml

# Verbose output (shows script output on failure)
isvctl test run -f config.yaml -v

# Validate config without running
isvctl test run -f config.yaml --dry-run

# Pass pytest arguments after --
isvctl test run -f config.yaml -- -v -s -k "NodeCount"
```

## Config Structure

### Complete Example

```yaml
version: "1.0"

commands:
  network:
    # Phases execute in this order
    phases: ["setup", "test", "teardown"]

    steps:
      # Step 1: Create VPC (setup phase)
      - name: create_network
        phase: setup
        command: "python ./scripts/create_vpc.py"
        args:
          - "--name"
          - "test-vpc"
          - "--region"
          - "{{region}}"
        timeout: 300

      # Step 2: Run tests (test phase)
      - name: test_connectivity
        phase: test
        command: "python ./scripts/test_connectivity.py"
        args:
          - "--vpc-id"
          - "{{steps.create_network.network_id}}"
        timeout: 600

      # Step 3: Cleanup (teardown phase)
      - name: teardown
        phase: teardown
        command: "python ./scripts/teardown.py"
        args:
          - "--vpc-id"
          - "{{steps.create_network.network_id}}"
        timeout: 300

tests:
  cluster_name: "aws-network-test"

  settings:
    region: "us-west-2"

  # Centralized validations grouped by category
  validations:
    network:
      step: create_network
      checks:
        NetworkProvisionedCheck: {}

    connectivity:
      step: test_connectivity
      checks:
        StepSuccessCheck: {}

    teardown_checks:
      step: teardown
      checks:
        StepSuccessCheck: {}
```

### Platform Configuration

Each platform defines phases and steps:

```yaml
commands:
  network:
    phases: ["setup", "test", "teardown"]    # Execution order
    continue_after_failure: []                 # Optional independent test phases
    steps: [...]                              # Steps grouped by phase
```

| Field | Required | Description |
| ----- | -------- | ----------- |
| `phases` | No | Ordered list of phases (default: `["setup", "teardown"]`) |
| `continue_after_failure` | No | Phase names whose failure records a failed run but does not prevent later phases from running |
| `steps` | Yes | List of step configurations |
| `skip` | No | Skip this entire platform |

**Important:** If a step's `phase` is not in the `phases` list, an error is raised.

Phase names are not limited to `setup`, `test`, and `teardown`. Any other name
is a custom test phase: it runs in the declared order, appears under its own
name in the orchestration summary, and is selected by `--phase test`. A
validation bound to a step runs after that step's custom phase.

By default, a failed phase prevents later non-teardown phases from running. Use
`continue_after_failure` only when the named phases are independent test cases
and collecting every result in one invocation is more useful than stopping at
the first failure:

```yaml
commands:
  network_operator:
    phases: [setup, roce-sriov, infiniband-sriov, roce-host-device]
    continue_after_failure: [roce-sriov, infiniband-sriov, roce-host-device]
    steps:
      - name: prepare
        phase: setup
        command: ./prepare.sh
      - name: test_roce_sriov
        phase: roce-sriov
        command: ./run-use-case.sh
        args: [roce-sriov]
      - name: test_infiniband_sriov
        phase: infiniband-sriov
        command: ./run-use-case.sh
        args: [infiniband-sriov]
      - name: test_roce_host_device
        phase: roce-host-device
        command: ./run-use-case.sh
        args: [roce-host-device]
```

This setting changes continuation, not the verdict: if `roce-sriov` fails,
later listed use cases still run, but the final orchestration result remains
failed. Every continuation name must also appear once in `phases`;
configuration validation rejects unknown or duplicate names and forbids
`setup` and `teardown`. Do not list prerequisites shared by later phases or
phases that leave state on which later phases depend. Teardown retains its
existing `teardown_on_failure` behavior.

### Step Configuration

Each step defines a command to execute:

```yaml
- name: create_network
  phase: setup
  command: "python ./scripts/create_vpc.py"
  args: ["--region", "{{region}}"]
  timeout: 300
  env:
    AWS_PROFILE: "production"
  skip: false
  continue_on_failure: false
  finalizer_for: null
  output_schema: vpc
```

| Field | Required | Description |
| ----- | -------- | ----------- |
| `name` | Yes | Unique step identifier (used for output references) |
| `phase` | No | Phase this step belongs to (default: `setup`) |
| `command` | Yes | Script/command to execute |
| `args` | No | Arguments (supports Jinja2 templates) |
| `timeout` | No | Orchestration watchdog in seconds (default: 300); `null` disables it |
| `env` | No | Environment variables |
| `skip` | No | Skip this step |
| `continue_on_failure` | No | Continue even if this step fails |
| `finalizer_for` | No | Run as linked teardown after the named step's phase when that command was attempted |
| `output_schema` | No | Schema name for output validation |
| `requires` | No | Capability contexts this step runs in (see [Capabilities](#capabilities-and-requires)) |
| `requires_available_validations` | No | Validation names that must be available after release filtering |
| `requires_selected_validations` | No | Configured validation names that must remain selected after release, capability, label, and suite-exclusion filtering; failed steps become errors on these owning validations |

The timeout is an orchestration watchdog, not a provider-specific setting. Set
it to `null` only when the invoked tool owns a bounded deadline; isvctl will
then wait for the command to exit. On POSIX systems, isvctl starts each step in
a separate process group. When the
watchdog expires, it sends `SIGTERM` to the entire group, waits briefly, then
uses `SIGKILL` if needed. This prevents a wrapper's child CLI from continuing
to modify infrastructure after the wrapper step has been reported as timed
out. On non-POSIX systems, isvctl terminates the direct child process.

#### Linked teardown finalizers

Use `finalizer_for` when cleanup must run after the validations for one custom
test phase, including when the mutating step or a validation failed. Declare
cleanup in `phase: teardown`; the orchestrator executes it directly after its
target's test phase instead of waiting until every test case has finished:

```yaml
commands:
  network:
    phases: [setup, use-case-one, use-case-two, teardown]
    continue_after_failure: [use-case-one]
    steps:
      - name: deploy_fixture
        phase: use-case-one
        command: ./deploy.sh

      - name: clean_fixture
        phase: teardown
        command: ./clean.sh
        finalizer_for: deploy_fixture
```

The finalizer target must resolve to one unique step, precede the configured
`teardown` phase, and cannot itself be a finalizer. The finalizer must use the
same capability and validation-selection gates as its target. Configuration
validation rejects violations of these rules.

The orchestrator withholds linked teardown from normal phase execution, runs
the target phase validations, and then executes the eligible cleanup in
best-effort mode. The result is reported separately as
`<target-phase>-teardown`. This interleaving applies even to `--phase test`, so
multiple independent cases cannot leave deployments overlapping until the end
of the suite. A target activates cleanup only when its command process actually
started, whether it passed or failed. If an earlier prerequisite stopped the
phase, a template could not be rendered, or the executable could not be
started, cleanup is reported as skipped; this prevents deletion of pre-existing
state the current run never mutated.

An explicit `--phase teardown` run executes linked teardown steps without an
in-memory target attempt. This is the standalone recovery path for resources
left by an interrupted earlier run. When target test phases and teardown are
part of the same invocation, already-linked cleanup is not run again in the
final teardown position.

An ordinary use-case failure may still honor `continue_after_failure` after its
finalizers succeed. A failed finalizer always blocks later non-teardown phases,
because the fixture can no longer be assumed clean. Finalizer command output
and failure details are recorded in the teardown phase result. Keep finalizers
lifecycle-only rather than binding validations to their output, because target
phase validations intentionally run before cleanup. A same-phase finalizer is
still supported for compatibility, but a destructive provider cleanup should
normally be declared in `phase: teardown` so its lifecycle role and reporting
are explicit.

Finalizers are an orchestration guarantee, not a recovery service. An abrupt
isvctl process termination, host failure, or `SIGKILL` can prevent them from
running. Provider cleanup commands should therefore be idempotent and usable as
standalone recovery commands.

#### Gating a step with `requires`

A step is skipped automatically when **every** validation bound to it is
filtered out by the run's capability. That inference covers most steps, but it
cannot reach a step no validation binds to — typically a teardown step:

```yaml
# Runs only under --capability kubernetes. Without the gate, a core run would
# try to tear down a cluster it never created.
- name: teardown_cluster
  phase: teardown
  command: "./scripts/teardown_cluster.sh"
  requires: [kubernetes]
```

Rule of thumb: **if a step builds or destroys a fixture that only some contexts
need, give it an explicit `requires:`** — and give both halves of the fixture the
same one, so setup and teardown always move together. A step that survives the
gate must not reference a gated-off step's output; use `default(...)` if it
legitimately might be absent.

#### Gating Mutating Steps by Test Selection

Use `requires_selected_validations` when a lifecycle step exists only to serve
specific validation entries. This applies selection before the command runs,
so `--label` and `--exclude-label` do not execute an unrelated deployment and
then discard its result:

```yaml
commands:
  network:
    steps:
      - name: deploy_ethernet_fixture
        phase: ethernet
        command: ./deploy-ethernet.sh
        requires_selected_validations: [EthernetConnectivityCheck]

tests:
  validations:
    network:
      checks:
        EthernetConnectivityCheck:
          step: deploy_ethernet_fixture
          labels: [ethernet]
```

With no label filter, the validation is selected and the step runs. With
`--label ethernet`, it also runs; with `--label infiniband`, the step is
skipped before execution. Every listed validation must be configured and
selected. The gate also honors the release manifest, capability requirements,
`tests.exclude.tests`, and effective label exclusions.

The same list is the reporting ownership edge for the lifecycle step. If a
selected step fails before its validation can run, each listed validation is
reported as `error` with reason `step_failed`, including in JUnit. This prevents
an early deploy or setup failure from being misreported as a harmless
`step_no_output` skip merely because a later validation step was never reached.
The error message names the failed step and retains its redacted command
diagnostic.

`requires_available_validations` is narrower: it only prevents a step from
running when its named checks are absent from the release manifest. Retain it
for providers that only need release gating.

Pytest `-k` and `-m` expressions are evaluated inside pytest and therefore do
not drive `requires_selected_validations`. Use framework `--label` filtering
for lifecycle pruning in mutating suites.

Selection-filtered validations remain in the structured result and JUnit
report. With the default `tests.settings.show_skipped_tests: false`, terminal
output omits summary phases containing only those filtered validations. Set it
to `true` when the skipped selection decisions should be visible interactively.

### Validation Configuration

Validations are centralized in `tests.validations`, grouped by category. Each group binds to a step and lists checks as a dict:

```yaml
tests:
  validations:
    # Group name (any meaningful name)
    network:
      step: create_network       # Step whose JSON output is checked
      checks:
        NetworkProvisionedCheck: {}

    teardown_checks:
      step: teardown
      checks:
        StepSuccessCheck: {}
```

For Kubernetes/Slurm configs where validations don't bind to individual step outputs, the `step:` field is omitted:

```yaml
tests:
  validations:
    kubernetes:
      checks:
        K8sNodeCountCheck:
          count: "{{steps.setup.kubernetes.node_count}}"
```

**Validation Timing (`phase`):**

| Value | When it runs |
| ----- | ------------ |
| *(not set)* | After setup phase (default) |
| `teardown` | After teardown phase |
| `<phase>` | After the specified phase |

### Test Variants

A validation check can be run multiple times with different parameters by appending a **dash-separated suffix** to the class name. The dash (`-`) is the only accepted variant separator.

```yaml
validations:
  k8s_workloads:
    checks:
      K8sNimHelmWorkload-1b:
        model: "meta/llama-3.2-1b-instruct"
        gpu_count: 1
        timeout: 900
      K8sNimHelmWorkload-3b:
        model: "meta/llama-3.2-3b-instruct"
        gpu_count: 4
        timeout: 1800

  slurm:
    checks:
      SlurmPartition-cpu:
        partition_name: "cpu"
      SlurmPartition-gpu:
        partition_name: "gpu"
```

The part before the dash must match an existing validation class name (e.g., `K8sNimHelmWorkload`, `SlurmPartition`). The suffix after the dash is a label - it can be any descriptive string. Each variant runs as a separate test case with its own parameters and appears independently in test results and coverage.

**Rules:**

- Validation class names **cannot** contain dashes, so the first dash always marks the start of a variant suffix.
- The suffix is free-form: `K8sNimHelmWorkload-small`, `SlurmPartition-cpu`, `SlurmGpuAllocation-1gpu` are all valid.
- Each variant is a distinct test entry in coverage tracking.

## Capabilities and `requires`

An ISV declares which **capabilities** it supports. There are exactly four, and
they are mutually exclusive execution environments — you run on one at a time,
never a combination:

`vm` · `bare_metal` · `kubernetes` · `slurm`

Each has a **platform suite** (`suites/vm.yaml`, `suites/k8s.yaml`, ...) carrying
the checks you owe by declaring it. Its `tests.capability:` key names the
capability, and its checks declare no `requires:` — they all run.

Everything else is a **plain suite** (`storage`, `network`, `iam`, ...), named by
its filename. A plain suite mixes checks that need no particular infrastructure
with checks that presuppose some. Each check says which:

```yaml
requires: []                # core - runs in every context
requires: [kubernetes]      # runs only under --capability kubernetes
requires: [vm, bare_metal]  # any-match: either context satisfies it
```

`requires` is **any-match**, not a set to satisfy simultaneously: a check runs
when its list is empty, or when the run's capability appears in it. There is
deliberately no way to express "needs vm AND kubernetes" — no check needs it,
and the mutual exclusivity above means such a check could never run.

### What runs, and when

One rule, and it does not depend on how you named the config — `--suite`, `-f`,
and `--label` discovery behave identically:

> **A plain suite with no `--capability` runs its core checks.** Name a
> capability to add the checks gated on it.

```bash
isvctl test run --suite storage --capability kubernetes --phase test    # canonical suite, no provider lifecycle
isvctl test run --provider acme --suite storage                         # core only
isvctl test run --provider acme --suite storage --capability vm         # core + vm checks
isvctl test run --provider acme --suite kubernetes                      # the platform suite
```

Without `--provider`, `--suite` selects the canonical file in
`configs/suites/`. With `--provider`, it selects the provider config and its
lifecycle commands.

There is no "run everything" context: a plain suite always carries exactly one.
Passing a capability no check in the suite requires is allowed but warns, since
a flag that silently does nothing is usually a typo.

Two consequences worth internalising:

- **Nothing is mandatory.** A check is in scope only if you declared the suite
  that contains it. 100% is always relative to what you declared, so declaring
  a subset legitimately yields zero checks from the suites you left out.
- **A capability and a plain suite compose.** The 15 CSI checks in `storage`
  need `storage` *and* `kubernetes`. Declaring `kubernetes` alone runs the
  Kubernetes platform suite but no storage CSI checks.

Capability names and plain-suite names share one namespace, so a plain suite may
not be named after a capability. `catalog_document` and
`scripts/validate_suite_wiring.py` both reject the collision.

Suite discovery is recursive under `isvctl/configs/suites/`. A domain with
multiple related suites may therefore use a subdirectory such as
`suites/k8s-launch-kit/`; catalog generation, `--suite` resolution, doctor,
wiring validation, and test-plan coverage all discover the nested YAMLs. Suite
identity is still the YAML filename stem, so stems must remain unique across
the complete suite tree.

## Import and Override

Provider configs can import a canonical test suite and override command definitions while inheriting validations (unless explicitly overridden):

```yaml
# isvctl/configs/providers/my-isv/config/vm.yaml
import: ../../../suites/vm.yaml

commands:
  vm:
    steps:
      - name: launch_instance
        command: "python3 ../scripts/vm/launch_instance.py"
      - name: stop_instance
        command: "python3 ../scripts/vm/stop_instance.py"
      # ... list the full set of steps you need

tests:
  settings:
    region: "us-east-1"
    instance_type: "gpu.large"
```

The import path is relative to the importing file. The imported config provides the base step list, phases, and validations. Nested dictionaries (like `tests.settings`) are deep-merged, but list fields (like `commands.<platform>.steps`) are **replaced as a whole** - if you set `steps:` in the provider config, include the full desired list. See the [AWS reference implementation](../references/aws.md) for working examples.

## Template Variables

### Referencing Step Outputs

Use `{{steps.step_name.field}}` to reference previous step outputs:

```yaml
steps:
  - name: create_instance
    command: "python launch.py"
    # Output: {"instance_id": "i-xxx", "public_ip": "54.1.2.3"}

  - name: test_ssh
    command: "python test_ssh.py"
    args:
      - "--host"
      - "{{steps.create_instance.public_ip}}"
```

### Other Variables

| Variable | Description |
| -------- | ----------- |
| `{{setting_name}}` | From `tests.settings` |
| `{{env.VAR_NAME}}` | Environment variable |
| `{{steps.name.field}}` | Step output field |

> **Template warnings:** The orchestrator logs warnings when a template references a step that hasn't run or a field that doesn't exist in the step output. This catches typos, renames, and missing data that Jinja2's `ChainableUndefined` would otherwise silently absorb. Warnings are suppressed for steps in phases that were intentionally skipped (e.g., `--phase teardown`).

## Script Output and Schema Validation

Scripts must output valid JSON to stdout. The output is validated against schemas defined in `output_schemas.py`.

### Schema Auto-Detection

The schema is automatically detected from the step name using a mapping system:

| Step Name Pattern | Schema | Required Fields |
| ----------------- | ------ | --------------- |
| `setup`, `create_cluster`, `provision_cluster` | `cluster` | `success`, `platform`, `cluster_name`, `node_count` |
| `create_network`, `create_vpc` | `network` | `success`, `platform` |
| `launch_instance`, `create_vm` | `instance` | `success`, `platform`, `instance_id` |
| `run_workload`, `run_test` | `workload_result` | `success`, `platform`, `status` |
| `teardown`, `cleanup`, `destroy` | `teardown` | `success`, `platform` |
| `check_api`, `test_api` | `api_health` | `success`, `platform` |
| `create_access_key` | `access_key` | `success`, `platform`, `access_key_id` |
| `create_tenant` | `tenant` | `success`, `platform`, `tenant_name` |
| `vpc_crud`, `vpc_crud_test` | `vpc_crud` | `success`, `platform` |
| *(unrecognized)* | `generic` | `success`, `platform` |

### Common Required Fields

All schemas require these common fields:

```json
{
  "success": true,
  "platform": "network"
}
```

- `success`: Boolean indicating operation success
- `platform`: Platform type (e.g., `"network"`, `"vm"`, `"iam"`, `"control_plane"`)

### Explicit Schema Override

You can override auto-detection using the `output_schema` field:

```yaml
steps:
  - name: my_custom_step
    command: "python ./my_script.py"
    output_schema: cluster  # Force cluster schema validation
```

### Example Output by Schema Type

**Network schema (`create_network`, `create_vpc`):**

```json
{
  "success": true,
  "platform": "network",
  "network_id": "vpc-0123456789",
  "cidr": "10.0.0.0/16",
  "subnets": [
    {"subnet_id": "subnet-aaa", "cidr": "10.0.1.0/24", "availability_zone": "us-west-2a"}
  ],
  "region": "us-west-2"
}
```

**Cluster schema (`setup`):**

```json
{
  "success": true,
  "platform": "kubernetes",
  "cluster_name": "my-cluster",
  "node_count": 3,
  "endpoint": "https://cluster.example.com",
  "gpu_count": 8,
  "driver_version": "570.195.03"
}
```

**Teardown schema (`teardown`, `cleanup`):**

```json
{
  "success": true,
  "platform": "network",
  "resources_deleted": ["vpc-123", "subnet-456"],
  "message": "Cleanup completed"
}
```

**Exit codes:**

- `0` = Success
- Non-zero = Failure

### Python Script Example

```python
#!/usr/bin/env python3
"""Create VPC and output JSON."""

import argparse
import json
import sys
import boto3

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "network",
    }

    try:
        ec2 = boto3.client("ec2", region_name=args.region)
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")
        result["network_id"] = vpc["Vpc"]["VpcId"]
        result["success"] = True
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1

if __name__ == "__main__":
    sys.exit(main())
```

### Bash Script Example

```bash
#!/bin/bash
# Create VPC and output JSON

set -e

NAME="${1:-test-vpc}"
REGION="${AWS_REGION:-us-west-2}"

VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.0.0.0/16 \
  --region "$REGION" \
  --query 'Vpc.VpcId' \
  --output text)

cat <<EOF
{
  "success": true,
  "platform": "network",
  "network_id": "$VPC_ID",
  "region": "$REGION"
}
EOF
```

## Available Validations

For the full list of validations with descriptions and labels, see [isvtest package docs](../packages/isvtest.md#available-validations).

Below is a summary by category.

### Generic (`validations/generic.py`)

`StepSuccessCheck`, `FieldExistsCheck`, `FieldValueCheck`, and `CrudOperationsCheck`
are compose-only mechanisms. Reference them under a named test instead of wiring
their class names directly:

```yaml
checks:
  NetworkProvisionedCheck:
    description: "Check the network was provisioned"
    compose:
      - StepSuccessCheck
      - FieldExistsCheck:
          fields: ["network_id"]
```

`CompositeCheck` is the existing framework runner behind `compose:`; authors do
not register or invoke that class directly. The YAML key creates one catalog
test and runs every listed validation member. Each member is reported as a
subtest. If a member reports its own probes through `report_subtest()`, those
probes are retained with qualified names such as
`LaunchKitRdmaConnectivityCheck/rping/worker-a->worker-b/rail-0->rail-1`.
This avoids collisions between members and keeps the full probe tree in pytest
and JUnit output.

A member may call `pytest.skip` when it is not applicable to the current
environment. `CompositeCheck` records that member as a skipped subtest and
continues with the remaining members. The skip neither passes nor fails the
member, and the parent composite passes when every non-skipped member passes.
This is different from skipping the step output or the composite itself, both
of which skip the entire parent validation.

The orchestration summary automatically abbreviates a successful validation
that reported subtests:

```text
MyUseCase: PASSED - 12 subtests passed
```

If optional probes were skipped, the summary includes passed, failed, and
skipped counts. Failed and errored validations keep their original diagnostic
message instead of being abbreviated. There is no YAML presentation flag;
this behavior applies to composites and ordinary validation classes alike.
After subtest testcase nodes are injected into JUnit, the suite's tests,
failures, errors, and skipped counters are recalculated from those serialized
nodes so reports do not double-count pytest's pre-counted subtest events.

`SchemaValidation` remains directly wireable, but is catalog-excluded because
the step executor runs schema checks automatically.

| Validation | Description |
| ---------- | ----------- |
| `StepSuccessCheck` | Compose-only: check step completed successfully |
| `FieldExistsCheck` | Compose-only: check required fields exist in output |
| `FieldValueCheck` | Compose-only: check field has expected value (eq, gt, gte, lt, lte, contains, min/max) |
| `CrudOperationsCheck` | Compose-only: check all CRUD operations passed |
| `SchemaValidation` | Validate output matches JSON schema |

### Instance (`validations/instance.py`)

| Validation | Description |
| ---------- | ----------- |
| `VmInstanceIdReportedCheck` | Check instance was created |
| `InstanceStateCheck` | Check instance is in expected state |
| `InstanceListCheck` | Check instance list from VPC |
| `InstanceTagCheck` | Check instance tags are present |
| `InstanceStopCheck` | Check instance stopped successfully |
| `InstanceStartCheck` | Check stopped instance started successfully |
| `InstanceRebootCheck` | Check instance rebooted successfully |
| `InstancePowerCycleCheck` | Check instance recovered from power-cycle |
| `StableIdentifierCheck` | Check instance ID is stable across lifecycle events |
| `SerialConsoleCheck` | Check serial console access |
| `BmTopologyPlacementCheck` | Check topology-based placement support |

### Network (`validations/network.py`)

| Validation | Description |
| ---------- | ----------- |
| `BackendSwitchFabricCheck` | Check backend switch fabric IDs |
| `NetworkProvisionedCheck` | Check network was provisioned |
| `VpcCrudCheck` | Check VPC CRUD operations |
| `SubnetConfigCheck` | Check subnet configuration |
| `VpcIsolationCheck` | Check VPC isolation |
| `VpcIpConfigCheck` | Check VPC IP configuration |
| `VpcPeeringCheck` | Check VPC peering |
| `SgCrudCheck` | Check security group CRUD operations |
| `SecurityBlockingCheck` | Check security blocking rules |
| `FloatingIpCheck` | Check floating IP switch |
| `LocalizedDnsCheck` | Check localized DNS |
| `ByoipCheck` | Check BYOIP support |
| `StablePrivateIpCheck` | Check private IP stability |
| `NetworkConnectivityCheck` | Check network connectivity |
| `NvlinkDomainCheck` | Check NVLink domain ID |
| `TrafficFlowCheck` | Check traffic flow |
| `DhcpIpManagementCheck` | Check DHCP/IP management via SSH |

### Security (`validations/security.py`)

| Validation | Description |
| ---------- | ----------- |
| `BmcTenantIsolationCheck` | Check BMC/IPMI/Redfish are unreachable from tenant networks |
| `BmcProtocolSecurityCheck` | Check CNP10-01: IPMI disabled; Redfish over TLS with AAA |
| `ApiEndpointIsolationCheck` | Check management/API endpoints are not publicly accessible |

### Host (`validations/host.py`)

| Validation | Description |
| ---------- | ----------- |
| `ConnectivityCheck` | Validates SSH connectivity |
| `OsCheck` | Validates OS via SSH |
| `CpuInfoCheck` | Validates CPU, NUMA topology, and PCI configuration |
| `VcpuPinningCheck` | Validates vCPU pinning and NUMA affinity |
| `PciBusCheck` | Validates PCI bus configuration for GPU devices |
| `HostSoftwareCheck` | Validates kernel, libvirt, SBIOS, and NVIDIA drivers |
| `GpuCheck` | Validates GPU via SSH |
| `DriverCheck` | Validates kernel and NVIDIA drivers |
| `ContainerRuntimeCheck` | Tests container runtime and NVIDIA Docker support |
| `CloudInitCheck` | Validates cloud-init completed and metadata service is reachable (supports non-AWS providers; see [isvtest docs](../packages/isvtest.md#available-validations) for `metadata_headers` and `metadata_url` options) |
| `BmGpuStressCheck` | GPU stress test via SSH |
| `BmNcclCheck` | NCCL AllReduce test via SSH |
| `BmTrainingCheck` | DDP training workload via SSH |
| `BmNvlinkCheck` | NVLink topology and status via SSH |
| `BmInfiniBandCheck` | InfiniBand interface status via SSH |
| `BmEthernetCheck` | Ethernet interfaces and connectivity via SSH |

### NIM (`validations/nim.py`)

| Validation | Description |
| ---------- | ----------- |
| `NimHealthCheck` | Validates NIM health endpoint |
| `NimModelCheck` | Validates NIM model listing |
| `NimInferenceCheck` | Validates NIM inference via chat completions |

### Cluster (`validations/cluster.py`)

| Validation | Description |
| ---------- | ----------- |
| `ClusterHealthCheck` | Check cluster is healthy |
| `NodeCountCheck` | Check cluster node count matches expected |
| `GpuOperatorInstalledCheck` | Check GPU operator installation |
| `PerformanceCheck` | Check workload performance meets requirements |

### IAM (`validations/iam.py`)

| Validation | Description |
| ---------- | ----------- |
| `AccessKeyCreatedCheck` | Check access key was created |
| `AccessKeyAuthenticatedCheck` | Check access key can authenticate |
| `AccessKeyDisabledCheck` | Check access key was disabled |
| `AccessKeyRejectedCheck` | Check disabled key is rejected |
| `TenantCreatedCheck` | Check tenant was created |
| `TenantListedCheck` | Check tenant appears in list |
| `TenantInfoCheck` | Check tenant info retrieved |

### Kubernetes Conformance Modes

`K8sCncfConformanceCheck` (in `validations/k8s_conformance.py`) runs the upstream CNCF e2e suite in-cluster. The `mode` parameter selects which subset of tests runs:

| Mode | Description |
| ---- | ----------- |
| `certified-conformance` (default) | Full `[Conformance]` suite, serial. Required for CNCF certification; expect multi-hour runtime. |
| `non-disruptive-conformance` | `[Conformance]` minus `[Disruptive]` and `[Serial]` tests. Safe to run against clusters carrying other workloads. |
| `quick` | Single ConfigMap test. Smoke-tests the harness end-to-end without exercising real conformance coverage. |

## Excluding Tests

Use the `tests.exclude` section to deselect tests before they run. Excluded tests are removed from collection entirely (they do not appear as skipped or failed).

```yaml
tests:
  exclude:
    platforms: []   # Deselect all tests with these platform labels
    labels: []      # Deselect all tests with these labels
    tests: []       # Deselect specific tests by name
    files: []       # Deselect all tests in these files
```

### Exclusion Types

| Key | Behavior | Bypassed by explicit selectors? |
| --- | -------- | ------------------------------- |
| `platforms` | Removes tests whose labels include the listed platform (e.g., `bare_metal`, `kubernetes`) | No - always applied |
| `labels` | Removes tests whose labels include any of the listed values (e.g., `workload`, `slow`) | Yes - explicit `--label`, `-k`, or pytest `-m` overrides |
| `tests` | Removes tests matching by exact name, prefix, or parametrized ID (e.g., `K8sNcclWorkload`, `K8sNimHelmWorkload-3b`) | No - always applied |
| `files` | Removes tests whose source file matches (e.g., `test_host.py`) | No - always applied |

Labels are mirrored as pytest marks, so explicit selection with `--label`, `-- -k`, or `-- -m` bypasses `exclude.labels` for the same run.

### Examples

Skip all workload and slow tests (the most common use case):

```yaml
tests:
  exclude:
    labels:
      - workload
      - slow
```

Skip specific tests by name:

```yaml
tests:
  exclude:
    tests:
      - K8sNcclWorkload
      - K8sNimHelmWorkload-3b
```

### Override File

You can keep exclusions in a separate file and merge it on top of any config:

```bash
isvctl test run -f isvctl/configs/suites/k8s.yaml -f my-overrides.yaml
```

A template is provided in `isvctl/configs/overrides.yaml`. Note that `exclude` lists from later `-f` files **replace** earlier lists (they are not appended).

### Interaction with Explicit Selectors

When you pass explicit selectors:

```bash
isvctl test run -f config.yaml --label workload
isvctl test run -f config.yaml -- -k "K8sNcclWorkload"
isvctl test run -f config.yaml -- -m "workload"
```

**Label exclusions are bypassed**, allowing you to explicitly run tests that would normally be excluded. Platform, test name, and file exclusions still apply. Pytest `-m` remains available for advanced internal marker selection.

## Test Labels

Filter tests using labels:

```bash
# Run only specific tests
isvctl test run -f config.yaml -- -k "vpc_crud"

# Run by label
isvctl test run -f config.yaml --label kubernetes
```

Available labels: `bare_metal`, `vm`, `kubernetes`, `slurm`, `gpu`, `network`, `ssh`, `security`, `iam`, `workload`, `slow`

## Related Documentation

- [Getting Started](../getting-started.md) - Installation and first steps
- [Local Development](local-development.md) - Running tests locally
