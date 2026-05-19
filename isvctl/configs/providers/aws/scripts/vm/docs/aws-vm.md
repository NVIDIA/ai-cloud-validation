# AWS VM (VM-as-a-Service) Validation Guide

Validates AWS EC2 VM-as-a-Service capabilities using the ISV validation framework.

## Overview

The AWS VM validation tests verify:

1. **Instance Lifecycle** - Provisioning, state verification, instance listing, tagging
2. **SSH Access** - Remote connectivity via SSH, cloud-init completion
3. **Host OS Validation** - OS image, kernel, libvirt/QEMU, SBIOS, NVIDIA drivers
4. **GPU Tests** - GPU visibility, stress workloads
5. **vCPU Pinning** - vCPU count, NUMA topology, CPU-GPU locality
6. **PCI Bus Config** - PCIe link speed/width, IOMMU groups, BAR memory
7. **Stop/Start Resilience** - Instance stop, start, SSH/GPU re-validation
8. **Reboot Resilience** - Instance reboot, recovery, full host OS persistence
9. **Serial Console** - Console output accessibility
10. **NIM Inference** - NIM container deployment, health, model listing, inference (optional)

## Architecture

Scripts perform cloud/SSH operations and output JSON. Validations assert on the JSON.

```text
Config (YAML) -> Script (boto3/paramiko) -> JSON output -> Validations (assertions)
```

### Steps

| # | Step | Phase | Script | Description |
|---|------|-------|--------|-------------|
| 1 | `launch_instance` | setup | `providers/aws/scripts/vm/launch_instance.py` | Provision EC2 GPU instance |
| 2 | `list_instances` | test | `providers/aws/scripts/vm/list_instances.py` | List instances in VPC |
| 3 | `verify_tags` | test | `providers/aws/scripts/vm/describe_tags.py` | Verify user-defined tags on instance |
| 4 | `stop_instance` | test | `providers/aws/scripts/vm/stop_instance.py` | Stop VM, verify stopped state |
| 5 | `start_instance` | test | `providers/aws/scripts/vm/start_instance.py` | Start stopped VM, verify recovery + SSH |
| 6 | `reboot_instance` | test | `providers/aws/scripts/vm/reboot_instance.py` | Reboot and validate recovery |
| 7 | `serial_console` | test | `providers/aws/scripts/vm/serial_console.py` | Retrieve serial console output |
| 8 | `deploy_nim` | test | `providers/shared/deploy_nim.py` | Deploy NIM container via SSH (skipped if no NGC key) |
| 9 | `teardown_nim` | teardown | `providers/shared/teardown_nim.py` | Stop NIM container |
| 10 | `teardown` | teardown | `providers/aws/scripts/vm/teardown.py` | Terminate instance, delete key pair + SG |

Only `launch_instance` is in the **setup** phase. If any test step fails, teardown still runs
to prevent resource leaks. NIM steps are shared and reusable across VMaaS and BMaaS.

### Validations

| Validation | Step | Description |
|------------|------|-------------|
| `InstanceStateCheck` | launch_instance, reboot_instance | Verify instance is running |
| `InstanceListCheck` | list_instances | Verify instances in VPC, target found |
| `InstanceTagCheck` | verify_tags | Verify required tags (Name, CreatedBy) |
| `ConnectivityCheck` | launch_instance, start_instance, reboot_instance | SSH connectivity and command execution |
| `OsCheck` | launch_instance, start_instance, reboot_instance | Verify OS type |
| `CloudInitCheck` | launch_instance | Cloud-init completed successfully |
| `GpuCheck` | launch_instance, start_instance, reboot_instance | GPU visibility via nvidia-smi |
| `VcpuPinningCheck` | launch_instance, reboot_instance | vCPU count, NUMA topology, CPU-GPU locality |
| `PciBusCheck` | launch_instance, reboot_instance | PCI GPU enumeration, PCIe link, IOMMU, BAR memory |
| `HostSoftwareCheck` | launch_instance, reboot_instance | Kernel, libvirt/QEMU, SBIOS, NVIDIA drivers |
| `InstanceStopCheck` | stop_instance | Stop API call, state transitions to stopped |
| `InstanceStartCheck` | start_instance | Start API call, state recovery to running |
| `SerialConsoleCheck` | serial_console | Serial console output available and accessible |
| `InstanceRebootCheck` | reboot_instance | Reboot API call, state recovery, SSH, uptime reset |
| `NimHealthCheck` | deploy_nim | NIM `/v1/health/ready` (skipped if no NGC key) |
| `NimModelCheck` | deploy_nim | NIM `/v1/models` returns expected model |
| `NimInferenceCheck` | deploy_nim | Chat completion request and response validation |
| `StepSuccessCheck` | teardown | Teardown completed successfully |

## Prerequisites

- AWS CLI v2, Python 3.12+ with boto3/paramiko, uv package manager
- AWS credentials with EC2 permissions (RunInstances, TerminateInstances, CreateKeyPair, etc.)
- `NGC_API_KEY` environment variable (optional, for NIM tests)

```bash
aws --version
uv run python -c "import boto3, paramiko; print('OK')"
```

## Quick Start

```bash
# 1. Install
cd ISV-NCP-Validation-Suite && uv sync

# 2. Configure credentials
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-2
export NGC_API_KEY=...  # optional, for NIM tests

# 3. Run all tests
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml

# 4. Run specific tests
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml -- -k "Ssh"
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml -- -k "reboot"
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml -- -k "Nim"
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml -- -m "gpu"
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml -- -m "not workload"
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml -v -- -v -s  # verbose
```

## Configuration

Full config: [`isvctl/configs/providers/aws/config/vm.yaml`](../../../config/vm.yaml)

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `region` | string | `us-west-2` | AWS region |
| `instance_type` | string | `g5.xlarge` | GPU instance type |

### Instance Type to GPU Count

| Instance Type | GPUs | GPU Type |
|---------------|------|----------|
| g5.xlarge | 1 | A10G |
| g5.2xlarge | 1 | A10G |
| g5.12xlarge | 4 | A10G |
| g5.48xlarge | 8 | A10G |
| g4dn.xlarge | 1 | T4 |
| p4d.24xlarge | 8 | A100 |

### Test Duration

| Phase | Duration | Description |
|-------|----------|-------------|
| Launch Instance | 3-5 min | Create key, SG, launch EC2, wait for running |
| SSH + GPU + Host OS | ~2 min | All SSH-based validations (including cloud-init) |
| Verify Tags | ~5s | Check instance tags |
| Stop + Start | 3-5 min | Stop VM, verify stopped, start VM, re-validate SSH/GPU |
| Reboot + Revalidation | 3-7 min | Reboot via API, re-run SSH/GPU/host OS checks |
| Serial Console | ~5s | Retrieve console output |
| NIM Deploy | 5-20 min | Pull image, start container, wait for health (first run) |
| NIM Validation | ~30s | Health, models, inference checks |
| Teardown | ~1 min | Terminate instance, delete resources |
| **Total** | **20-40 min** | Full test cycle (with NIM) |

---

## Validation Details

Each validation SSHs into the host and runs subtests. All checks report
subtest-level results so you can pinpoint exactly what passed or failed.

### VcpuPinningCheck

Validates vCPU provisioning, online status, and NUMA topology.

| Subtest | What it checks |
|---------|---------------|
| `vcpu_count` | vCPU count matches `expected_vcpus` (if set) |
| `vcpu_online` | All vCPUs are online |
| `cpu_affinity` | CPU affinity mask of PID 1 spans all vCPUs |
| `numa_topology` | All NUMA nodes have CPUs assigned |
| `gpu{N}_numa` | GPU PCI device NUMA node (GPU-CPU locality) |

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `expected_vcpus` | int | *(auto-detect)* | Fail if vCPU count doesn't match |

### PciBusCheck

Validates PCI bus configuration for GPU passthrough.

| Subtest | What it checks |
|---------|---------------|
| `pci_gpu_count` | NVIDIA GPU devices on PCI bus match expected count |
| `pcie_link_gpu{N}` | PCIe generation and link width (current/max) |
| `iommu_groups` | IOMMU group assignment for GPU devices |
| `gpu{N}_bar_mem` | GPU BAR memory region |
| `acs` | PCI Access Control Services status |

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `expected_gpus` | int | `1` | Expected number of GPU PCI devices |
| `expected_link_width` | string | *(none)* | Expected PCIe link width, e.g. `"x16"` |

### HostSoftwareCheck

Validates the full software stack: kernel, libvirt/QEMU, SBIOS, NVIDIA drivers.

| Subtest | What it checks |
|---------|---------------|
| `kernel_version` | `uname -r` |
| `kernel_modules` | GPU/virt modules: `nvidia`, `kvm`, `vfio`, `vhost` |
| `libvirt`, `qemu`, `kvm` | Virtualization stack |
| `bios_vendor`, `bios_version`, `bios_date` | System BIOS via `dmidecode` |
| `tpm_present`, `tpm_version` | TPM device and major version via `/sys/class/tpm/tpm0` |
| `nvidia_driver`, `cuda_version` | NVIDIA driver and CUDA versions |

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `expected_kernel` | string | *(none)* | Kernel version substring to enforce |
| `expected_driver_version` | string | *(none)* | NVIDIA driver version substring |
| `expected_libvirt_version` | string | *(none)* | libvirt version substring |
| `expected_bios_vendor` | string | *(none)* | BIOS vendor name |
| `bios_baselines` | mapping | *(none)* | Approved BIOS minimums keyed by `system_vendor|product_name` |
| `tpm_baselines` | mapping | *(none)* | Approved minimum TPM major version keyed by `system_vendor\|product_name` (SEC22-02) |

When no `expected_*` parameter or `*_baselines` policy is set, the check **reports**
the value without failing. To build a BIOS or TPM policy, run once in report-only
mode to capture `system_vendor`, `system_product`, `bios_version`, and
`tpm_version`, then configure the approved baseline:

```yaml
HostSoftwareCheck:
  bios_baselines:
    "Dell Inc.|PowerEdge R760xa":
      min_version: "2.4.8"
  tpm_baselines:
    "Dell Inc.|PowerEdge R760xa":
      min_version: "2"
```

### InstanceRebootCheck

Validates that an EC2 instance rebooted successfully and fully recovered.

| Check | Fails if |
|-------|----------|
| `reboot_initiated` | Reboot API call did not succeed |
| `state` | Instance is not `"running"` after reboot |
| `ssh_ready` | SSH connectivity not restored |
| `uptime_seconds` | Uptime exceeds `max_uptime` (reboot didn't happen) |

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_uptime` | int | `600` | Max uptime in seconds to consider reboot confirmed |

---

## Script Outputs

### launch_instance.py

```json
{
  "success": true,
  "instance_id": "i-0abc123def456",
  "instance_type": "g5.xlarge",
  "public_ip": "54.1.2.3",
  "private_ip": "172.31.1.5",
  "state": "running",
  "key_file": "/tmp/isv-test-key.pem",
  "vpc_id": "vpc-0abc123",
  "ssh_user": "ubuntu"
}
```

### list_instances.py

```json
{
  "success": true,
  "platform": "vm",
  "instances": [{"instance_id": "i-0abc123", "state": "running", "vpc_id": "vpc-0abc123"}],
  "count": 1,
  "found_target": true,
  "target_instance": "i-0abc123"
}
```

### describe_tags.py

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "i-0abc123def456",
  "tags": {"Name": "isv-test-gpu", "CreatedBy": "isvtest"},
  "tag_count": 2
}
```

### stop_instance.py

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "i-0abc123def456",
  "state": "stopped",
  "stop_initiated": true
}
```

### start_instance.py

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "i-0abc123def456",
  "state": "running",
  "public_ip": "54.1.2.3",
  "key_file": "/tmp/isv-test-key.pem",
  "start_initiated": true,
  "ssh_ready": true
}
```

### serial_console.py

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "i-0abc123def456",
  "console_available": true,
  "serial_access_enabled": true,
  "output_length": 4096
}
```

### reboot_instance.py

```json
{
  "success": true,
  "platform": "vm",
  "instance_id": "i-0abc123def456",
  "state": "running",
  "reboot_initiated": true,
  "ssh_ready": true,
  "uptime_seconds": 45.2,
  "reboot_confirmed": true
}
```

### deploy_nim.py

```json
{
  "success": true,
  "platform": "vm",
  "skipped": false,
  "container_id": "ea20e03203fb",
  "model": "meta/llama-3.2-1b-instruct",
  "endpoint": "http://localhost:8000",
  "port": 8000,
  "health_ready": true,
  "host": "54.1.2.3",
  "key_file": "/tmp/isv-test-key.pem"
}
```

When `NGC_API_KEY` is not set, returns `{"success": true, "skipped": true}`.

### teardown_nim.py

```json
{"success": true, "platform": "vm", "container_removed": true}
```

### teardown.py

```json
{
  "success": true,
  "platform": "vm",
  "resources_destroyed": true,
  "deleted": {"instances": ["i-0abc123"], "security_groups": ["sg-0abc123"], "key_pairs": ["isv-test-key"]}
}
```

---

## Troubleshooting

### Instance Not Starting

```bash
aws service-quotas list-service-quotas --service-code ec2 --region us-west-2
```

### SSH Connection Failed

```bash
aws ec2 describe-instances --instance-ids i-xxx --query 'Reservations[*].Instances[*].PublicIpAddress'
ssh -i /tmp/isv-test-key.pem ubuntu@<public-ip>
```

### Host OS Check Failed

```bash
ssh -i /tmp/isv-test-key.pem ubuntu@<public-ip>
nproc && cat /sys/devices/system/cpu/online     # vCPU
lspci -d 10de: -nn && nvidia-smi -q -d PCIE     # PCI
uname -r && nvidia-smi                           # kernel + driver
sudo dmidecode -s bios-vendor                    # SBIOS
```

### NIM Deployment Failed

```bash
# Check container status
ssh -i /tmp/isv-test-key.pem ubuntu@<ip> "docker ps -a; docker logs isv-nim 2>&1 | tail -20"

# Check disk space (NIM images are 8-15GB)
ssh -i /tmp/isv-test-key.pem ubuntu@<ip> "df -h / && docker system df"

# Test manually
ssh -i /tmp/isv-test-key.pem ubuntu@<ip> "curl -sf http://localhost:8000/v1/models"
```

### Cleanup Failed Resources

```bash
aws ec2 describe-instances --filters "Name=tag:CreatedBy,Values=isvtest" \
  --query 'Reservations[*].Instances[*].[InstanceId,State.Name]'
aws ec2 terminate-instances --instance-ids i-xxx
```

---

## Cost & Cleanup

> **Warning**: These tests create AWS resources (EC2 instances, security groups,
> key pairs) that incur costs. Resources are automatically cleaned up during the
> teardown phase, but if teardown fails or is skipped, you must manually delete
> them to avoid ongoing charges.

```bash
# Find instances tagged by isvtest
aws ec2 describe-instances \
  --filters "Name=tag:CreatedBy,Values=isvtest" "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[*].Instances[*].[InstanceId,InstanceType,State.Name]' --output table

# Terminate orphaned instances
aws ec2 terminate-instances --instance-ids i-xxx

# Find and delete orphaned security groups
aws ec2 describe-security-groups --filters "Name=group-name,Values=isv-test-*" \
  --query 'SecurityGroups[*].[GroupId,GroupName]' --output table

# Delete orphaned key pairs
aws ec2 delete-key-pair --key-name isv-test-key
```

## Related Documentation

- [AWS EKS Validation Guide](../../eks/docs/aws-eks.md) - Kubernetes cluster tests
- [AWS Network Validation Guide](../../network/docs/aws-network.md) - VPC and network tests
- [Configuration Guide](../../../../../../../docs/guides/configuration.md) - Config file options
- [isvctl Documentation](../../../../../../../docs/packages/isvctl.md) - CLI reference
