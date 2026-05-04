# Configuration Guide

This guide covers the configuration file format and options for ISV NCP Validation Suite.

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
| `suites/k8s.yaml` | Standard Kubernetes cluster |
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
  platform: network
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
    steps: [...]                              # Steps grouped by phase
```

| Field | Required | Description |
| ----- | -------- | ----------- |
| `phases` | No | Ordered list of phases (default: `["setup", "test", "teardown"]`) |
| `steps` | Yes | List of step configurations |
| `skip` | No | Skip this entire platform |

**Important:** If a step's `phase` is not in the `phases` list, an error is raised.

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
  output_schema: vpc
```

| Field | Required | Description |
| ----- | -------- | ----------- |
| `name` | Yes | Unique step identifier (used for output references) |
| `phase` | No | Phase this step belongs to (default: `setup`) |
| `command` | Yes | Script/command to execute |
| `args` | No | Arguments (supports Jinja2 templates) |
| `timeout` | No | Timeout in seconds (default: 300) |
| `env` | No | Environment variables |
| `skip` | No | Skip this step |
| `continue_on_failure` | No | Continue even if this step fails |
| `output_schema` | No | Schema name for output validation |

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

For the full list of validations with descriptions and platform markers, see [isvtest package docs](../packages/isvtest.md#available-validations).

Below is a summary by category.

### Generic (`validations/generic.py`)

| Validation | Description |
| ---------- | ----------- |
| `StepSuccessCheck` | Check step completed successfully |
| `FieldExistsCheck` | Check required fields exist in output |
| `FieldValueCheck` | Check field has expected value (eq, gt, gte, lt, lte, contains, min/max) |
| `CrudOperationsCheck` | Check all CRUD operations passed |
| `SchemaValidation` | Validate output matches JSON schema |

### Instance (`validations/instance.py`)

| Validation | Description |
| ---------- | ----------- |
| `InstanceCreatedCheck` | Check instance was created |
| `InstanceStateCheck` | Check instance is in expected state |
| `InstanceListCheck` | Check instance list from VPC |
| `InstanceTagCheck` | Check instance tags are present |
| `InstanceStopCheck` | Check instance stopped successfully |
| `InstanceStartCheck` | Check stopped instance started successfully |
| `InstanceRebootCheck` | Check instance rebooted successfully |
| `InstancePowerCycleCheck` | Check instance recovered from power-cycle |
| `StableIdentifierCheck` | Check instance ID is stable across lifecycle events |
| `SerialConsoleCheck` | Check serial console access |
| `TopologyPlacementCheck` | Check topology-based placement support |

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
| `GpuStressCheck` | GPU stress test via SSH |
| `NcclCheck` | NCCL AllReduce test via SSH |
| `TrainingCheck` | DDP training workload via SSH |
| `NvlinkCheck` | NVLink topology and status via SSH |
| `InfiniBandCheck` | InfiniBand interface status via SSH |
| `EthernetCheck` | Ethernet interfaces and connectivity via SSH |

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
    platforms: []   # Deselect all tests with these platform markers
    markers: []     # Deselect all tests with these markers
    tests: []       # Deselect specific tests by name
    files: []       # Deselect all tests in these files
```

### Exclusion Types

| Key | Behavior | Bypassed by `-k` / `-m`? |
| --- | -------- | ------------------------ |
| `platforms` | Removes tests whose markers include the listed platform (e.g., `bare_metal`, `kubernetes`) | No - always applied |
| `markers` | Removes tests whose markers include any of the listed values (e.g., `workload`, `slow`) | Yes - explicit `-k` or `-m` overrides |
| `tests` | Removes tests matching by exact name, prefix, or parametrized ID (e.g., `K8sNcclWorkload`, `K8sNimHelmWorkload-3b`) | No - always applied |
| `files` | Removes tests whose source file matches (e.g., `test_host.py`) | No - always applied |

### Examples

Skip all workload and slow tests (the most common use case):

```yaml
tests:
  exclude:
    markers:
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

### Interaction with `-k` and `-m`

When you pass explicit pytest selectors via `--`:

```bash
isvctl test run -f config.yaml -- -k "K8sNcclWorkload"
isvctl test run -f config.yaml -- -m "workload"
```

**Marker exclusions are bypassed**, allowing you to explicitly run tests that would normally be excluded. Platform, test name, and file exclusions still apply.

## Test Markers

Filter tests using pytest markers:

```bash
# Run only specific tests
isvctl test run -f config.yaml -- -k "vpc_crud"

# Run by marker
isvctl test run -f config.yaml -- -m kubernetes
```

Available markers: `bare_metal`, `vm`, `kubernetes`, `slurm`, `gpu`, `network`, `ssh`, `security`, `iam`, `workload`, `slow`

## Related Documentation

- [Getting Started](../getting-started.md) - Installation and first steps
- [Local Development](local-development.md) - Running tests locally
