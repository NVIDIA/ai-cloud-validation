# ISV Test - NVIDIA ISV Lab Validation Framework

> **Note:** For cluster validation, use **`isvctl`** - the unified controller tool.
> `isvtest` is the internal validation engine used by `isvctl`.
>
> ```bash
> isvctl test run -f isvctl/configs/suites/k8s.yaml
> ```

A validation framework for NVIDIA ISV Lab environments supporting bare-metal servers, virtual machines, Kubernetes clusters, and Slurm HPC systems.

## Quick Start

```bash
# Install
uv sync

# Use via isvctl (recommended)
isvctl test run -f isvctl/configs/suites/k8s.yaml
isvctl test run -f isvctl/configs/providers/my-isv/config/vm.yaml
isvctl test run -f isvctl/configs/providers/my-isv/config/bare_metal.yaml
```

## Architecture

```text
isvtest/src/isvtest/
├── config/              # Configuration loading
├── core/                # Framework core
│   ├── validation.py    # BaseValidation class
│   ├── workload.py      # BaseWorkloadCheck class
│   ├── runners.py       # Command runners
│   ├── discovery.py     # Test discovery
│   ├── ssh.py           # SSH client utilities
│   ├── k8s.py           # Kubernetes helpers
│   ├── slurm.py         # Slurm helpers
│   └── nvidia.py        # NVIDIA driver/GPU helpers
├── validations/         # Platform-agnostic validation checks
│   ├── generic.py       # Field checks, schema validation, step success
│   ├── instance.py      # Instance lifecycle (stop/start/reboot/power-cycle)
│   ├── network.py       # VPC, subnet, security group, DNS, peering
│   ├── host.py          # SSH-based host checks (GPU, driver, OS, NCCL, NVLink)
│   ├── nim.py           # NIM container health/inference/models
│   ├── cluster.py       # Kubernetes cluster validations
│   ├── iam.py           # Access key and tenant validations
│   ├── bm_*.py          # Legacy bare metal validations
│   ├── k8s_*.py         # Legacy Kubernetes validations
│   └── slurm_*.py       # Legacy Slurm validations
├── workloads/           # Workload-based tests (longer running)
│   ├── k8s_*.py         # K8s workloads (NCCL, stress, NIM)
│   ├── slurm_*.py       # Slurm workloads (NCCL, stress, sbatch)
│   └── reframe_*.py     # ReFrame tests
├── catalog.py           # Test catalog generation
└── main.py              # CLI entry point
```

## Available Validations

Validations are platform-agnostic checks that inspect JSON step output. They are organized by category and referenced by class name in YAML test configs. Each validation has a `description` and public labels that indicate which platforms and capabilities it applies to.

### Generic (`validations/generic.py`)

Utility checks that work with any step output.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `StepSuccessCheck` | all | Check step completed successfully |
| `FieldExistsCheck` | all | Check required fields exist in output |
| `FieldValueCheck` | all | Check field has expected value |
| `CrudOperationsCheck` | all | Check all CRUD operations passed |
| `SchemaValidation` | all | Validate output matches JSON schema |

### Instance (`validations/instance.py`)

Instance lifecycle validations for VMs and bare metal.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `InstanceCreatedCheck` | vm | Check instance was created |
| `InstanceStateCheck` | vm, bare_metal | Check instance is in expected state |
| `InstanceListCheck` | vm, bare_metal | Check instance list from VPC |
| `InstanceTagCheck` | vm, bare_metal | Check instance tags are present |
| `InstanceStopCheck` | vm, bare_metal | Check instance stopped successfully |
| `InstanceStartCheck` | vm, bare_metal | Check stopped instance started successfully |
| `InstanceRebootCheck` | vm, bare_metal | Check instance rebooted successfully |
| `InstancePowerCycleCheck` | bare_metal | Check instance recovered from power-cycle |
| `StableIdentifierCheck` | vm, bare_metal | Check instance ID is stable across lifecycle events |
| `SerialConsoleCheck` | vm, bare_metal | Check serial console access |
| `TopologyPlacementCheck` | bare_metal | Check topology-based placement support |

### Network (`validations/network.py`)

VPC, subnet, security group, DNS, and connectivity checks.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `BackendSwitchFabricCheck` | network | Check backend switch fabric IDs |
| `NetworkProvisionedCheck` | network | Check network was provisioned |
| `VpcCrudCheck` | network | Check VPC CRUD operations |
| `SubnetConfigCheck` | network | Check subnet configuration |
| `VpcIsolationCheck` | network, security | Check VPC isolation |
| `VpcIpConfigCheck` | network | Check VPC IP configuration |
| `VpcPeeringCheck` | network | Check VPC peering |
| `SgCrudCheck` | network, security | Check security group CRUD operations |
| `SecurityBlockingCheck` | network, security | Check security blocking rules |
| `FloatingIpCheck` | network | Check floating IP switch |
| `LocalizedDnsCheck` | network | Check localized DNS |
| `ByoipCheck` | network | Check BYOIP support |
| `StablePrivateIpCheck` | network | Check private IP stability |
| `NetworkConnectivityCheck` | network | Check network connectivity |
| `NvlinkDomainCheck` | network | Check NVLink domain ID |
| `TrafficFlowCheck` | network | Check traffic flow |
| `DhcpIpManagementCheck` | network, ssh | Check DHCP/IP management via SSH |

### Security (`validations/security.py`)

Infrastructure security hardening checks for management-plane exposure and BMC protocol posture.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `BmcTenantIsolationCheck` | security, network | Check BMC/IPMI/Redfish are unreachable from tenant networks |
| `BmcProtocolSecurityCheck` | security, network | Check CNP10-01: IPMI disabled; Redfish over TLS with AAA |
| `ApiEndpointIsolationCheck` | security, network | Check management/API endpoints are not publicly accessible |

### Host (`validations/host.py`)

SSH-based host validations for GPU, driver, OS, networking, and workloads.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `ConnectivityCheck` | vm, bare_metal | Validates SSH connectivity |
| `OsCheck` | vm, bare_metal | Validates OS via SSH |
| `CpuInfoCheck` | vm, bare_metal | Validates CPU, NUMA topology, and PCI configuration |
| `VcpuPinningCheck` | vm | Validates vCPU pinning and NUMA affinity |
| `PciBusCheck` | vm | Validates PCI bus configuration for GPU devices |
| `HostSoftwareCheck` | vm, bare_metal | Validates kernel, libvirt, SBIOS, and NVIDIA drivers |
| `GpuCheck` | vm, bare_metal | Validates GPU via SSH |
| `DriverCheck` | vm, bare_metal | Validates kernel and NVIDIA drivers |
| `ContainerRuntimeCheck` | vm, bare_metal | Tests container runtime and NVIDIA Docker support |
| `CloudInitCheck` | vm, bare_metal | Validates cloud-init completed and metadata service is reachable. Supports `metadata_headers` (custom HTTP headers for non-AWS providers) and optional `metadata_url` override |
| `GpuStressCheck` | bare_metal | GPU stress test via SSH |
| `NcclCheck` | bare_metal | NCCL AllReduce test via SSH |
| `TrainingCheck` | bare_metal | DDP training workload via SSH |
| `NvlinkCheck` | bare_metal | NVLink topology and status via SSH |
| `InfiniBandCheck` | bare_metal | InfiniBand interface status via SSH |
| `EthernetCheck` | bare_metal | Ethernet interfaces and connectivity via SSH |

### NIM (`validations/nim.py`)

NIM container validations via SSH.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `NimHealthCheck` | vm, bare_metal | Validates NIM health endpoint |
| `NimModelCheck` | vm, bare_metal | Validates NIM model listing |
| `NimInferenceCheck` | vm, bare_metal | Validates NIM inference via chat completions |

### Cluster (`validations/cluster.py`)

Kubernetes cluster validations.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `ClusterHealthCheck` | kubernetes | Check cluster is healthy |
| `NodeCountCheck` | kubernetes | Check cluster node count matches expected |
| `GpuOperatorInstalledCheck` | kubernetes | Check GPU operator installation |
| `PerformanceCheck` | workload | Check workload performance meets requirements |

### IAM (`validations/iam.py`)

Access key and tenant validations.

| Validation | Platforms | Description |
| ---------- | --------- | ----------- |
| `AccessKeyCreatedCheck` | iam | Check access key was created |
| `AccessKeyAuthenticatedCheck` | iam | Check access key can authenticate |
| `AccessKeyDisabledCheck` | iam | Check access key was disabled |
| `AccessKeyRejectedCheck` | iam | Check disabled key is rejected |
| `TenantCreatedCheck` | iam | Check tenant was created |
| `TenantListedCheck` | iam | Check tenant appears in list |
| `TenantInfoCheck` | iam | Check tenant info retrieved |

## Workloads

Workloads are longer-running tests that deploy containers or run multi-node jobs.

| Workload | Platform | Description |
| -------- | -------- | ----------- |
| `K8sNcclWorkload` | kubernetes | Single-node NCCL AllReduce validation |
| `K8sNcclMultiNodeWorkload` | kubernetes | Multi-node NCCL AllReduce via MPIJob |
| `K8sGpuStressWorkload` | kubernetes | GPU stress test |
| `K8sNimHelmWorkload` | kubernetes | NIM Helm deployment + GenAI-Perf KPIs |
| `K8sNimInferenceWorkload` | kubernetes | NIM inference validation |
| `SlurmNcclMultiNodeWorkload` | slurm | Multi-node NCCL AllReduce via Slurm |
| `SlurmGpuStressWorkload` | slurm | GPU stress test across Slurm partition |
| `SlurmSbatchWorkload` | slurm | Run arbitrary sbatch script |

### Workload Prerequisites

| Workload | Requirement | Notes |
| -------- | ----------- | ----- |
| `K8sNcclMultiNodeWorkload` | [Kubeflow MPI Operator](https://github.com/kubeflow/mpi-operator) | Provides the `MPIJob` CRD (`kubeflow.org/v2beta1`) |
| `K8sNcclMultiNodeWorkload` | NVIDIA DRA driver (optional) | MNNVL/IMEX channels for full NVLink bandwidth. `use_compute_domain: auto\|true\|false` |
| `K8sNimHelmWorkload` | `NGC_API_KEY` env var | Required to pull NIM models from NGC |

## Configuration Format

See [Configuration Guide](../guides/configuration.md) for full details.

Validations are referenced in YAML test configs by class name under `tests.validations`. Each validation group is bound to a step and lists one or more checks:

```yaml
version: "1.0"

commands:
  vm:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: launch_instance
        phase: setup
        command: "python3 ../providers/my-isv/scripts/vm/launch_instance.py"
        timeout: 600

      - name: stop_instance
        phase: test
        command: "python3 ../providers/my-isv/scripts/vm/stop_instance.py"
        timeout: 600

tests:
  platform: vm
  validations:
    setup_checks:
      step: launch_instance
      checks:
        InstanceStateCheck:
          expected_state: "running"

    stop_checks:
      step: stop_instance
      checks:
        InstanceStopCheck: {}

    start_checks:
      step: start_instance
      checks:
        InstanceStartCheck: {}
        StableIdentifierCheck:
          reference_id: "{{steps.launch_instance.instance_id}}"
```

Canonical test configs live in `isvctl/configs/suites/` (vm.yaml, bare_metal.yaml, network.yaml, etc.). Provider-specific configs in `isvctl/configs/providers/<provider>/` import the canonical config and override commands with platform stubs.

## Test Labels

Filter tests using labels:

- `bare_metal`, `vm`, `kubernetes`, `slurm` - Platform-specific
- `gpu`, `network`, `ssh`, `security`, `iam` - Component-specific
- `workload` - Workload-based tests (longer running)
- `slow` - Tests that take longer than 5 minutes

```bash
isvtest test --config tests.yaml --label gpu --label network
```

**Note:** By default, `workload` and `slow` labels are excluded. Use `--label workload` or another explicit selector to run them. Each label is mirrored as a pytest mark, so `-- -m "not slow"` and `-- -k "not Workload"` still filter pytest natively.

## Development

```bash
# Run unit tests
uv --directory=isvtest run pytest tests/ -v

# Lint
uvx pre-commit run -a
```

## Related Documentation

- [Configuration Guide](../guides/configuration.md)
- [Local Development with MicroK8s](../guides/local-development.md)

## License

See [LICENSE](../../LICENSE) for license information.
