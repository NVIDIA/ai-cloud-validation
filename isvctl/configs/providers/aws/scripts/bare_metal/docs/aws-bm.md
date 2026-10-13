# AWS Bare Metal (BMaaS) Validation Guide

Validates AWS EC2 bare-metal GPU instances using the AI Cloud Validation framework.

## Overview

The AWS BM validation tests verify:

1. **Instance Lifecycle** - Provisioning, state verification, instance listing
2. **Tag Validation** - Instance tag verification (Name, CreatedBy)
3. **Topology Placement** - Placement group support (cluster strategy)
4. **Serial Console** - Console output accessibility
5. **Image Registry** - OS image verification, install config validation (cross-domain)
6. **SSH Access** - Remote connectivity via SSH, cloud-init completion
7. **Host OS Validation** - Kernel, BIOS, NVIDIA drivers
8. **GPU Tests** - GPU visibility, stress, NCCL, training, NVLink
9. **Networking** - InfiniBand, Ethernet connectivity
10. **Stop/Start Resilience** - Power off, power on, SSH/GPU re-validation, stable instance ID
11. **Reboot Resilience** - Instance reboot, recovery, full host OS persistence, stable instance ID
12. **Power-Cycle Resilience** - Force stop + start, recovery, SSH/GPU re-validation, stable instance ID
13. **Reinstall** - OS reinstall from stock image (skipped by default)
14. **NIM Inference** - NIM container deployment, health, model listing, inference
15. **Sanitization** - Resource cleanup verification after teardown

## Prerequisites

### Required Tools

- AWS CLI v2 (`aws`)
- Python 3.12+ with boto3, paramiko
- uv package manager

### AWS Credentials

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-2
```

### Required IAM Permissions

Same as VM validation. See [AWS VM Guide](../../vm/docs/aws-vm.md) for the full IAM policy.

## Quick Start

```bash
# Run all tests (provisions g4dn.metal by default)
uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml

# Verbose output
uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml -- -v -s

# Run only SSH tests
uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml -- -k "Ssh"

# Run only reboot validations
uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml -- -k "reboot"
```

## Steps

| # | Step | Phase | Script | Description |
|---|------|-------|--------|-------------|
| 1 | `launch_instance` | setup | `providers/aws/scripts/bare_metal/launch_instance.py` | Provision bare-metal GPU instance |
| 2 | `list_instances` | test | `providers/aws/scripts/vm/list_instances.py` | List instances in VPC (reuses VM script) |
| 3 | `verify_tags` | test | `providers/aws/scripts/bare_metal/describe_tags.py` | Verify instance tags (Name, CreatedBy) |
| 4 | `topology_placement` | test | `providers/aws/scripts/bare_metal/topology_placement.py` | Validate placement group support |
| 5 | `serial_console` | test | `providers/aws/scripts/bare_metal/serial_console.py` | Retrieve serial console output; retention proof requires an external log archive |
| 6 | `verify_image` | test | `providers/aws/scripts/image-registry/verify_image_installed.py` | Verify OS image installed on BM |
| 7 | `stop_instance` | test | `providers/aws/scripts/bare_metal/stop_instance.py` | Power off node, verify stopped state |
| 8 | `start_instance` | test | `providers/aws/scripts/bare_metal/start_instance.py` | Power on node, verify recovery |
| 9 | `reboot_instance` | test | `providers/aws/scripts/bare_metal/reboot_instance.py` | Reboot instance, validate recovery |
| 10 | `power_cycle_instance` | test | `providers/aws/scripts/bare_metal/power_cycle_instance.py` | Force stop + start, validate recovery |
| 11 | `describe_instance` | test | `providers/aws/scripts/bare_metal/describe_instance.py` | Describe post-power-cycle state + SSH info |
| 12 | `host_status_log` | test | `providers/aws/scripts/bare_metal/host_status_log.py` | Capture recent host status events |
| 13 | `reinstall_instance` | test | `providers/aws/scripts/bare_metal/reinstall_instance.py` | Reinstall OS (skip: true by default) |
| 14 | `deploy_nim` | test | `scripts/nim/deploy_nim.py` | Deploy NIM container via SSH |
| 15 | `teardown_nim` | teardown | `scripts/nim/teardown_nim.py` | Stop NIM container |
| 16 | `teardown` | teardown | `providers/aws/scripts/bare_metal/teardown.py` | Terminate instance, delete resources |
| 17 | `verify_teardown` | teardown | `providers/aws/scripts/bare_metal/verify_terminated.py` | Confirm instance terminated + SG deleted |

Step 6 (`verify_image`) cross-references the image-registry domain: it is how AWS proves
BOOT01-03, since its image-registry run launches a VM rather than a metal host. Step 13
(`reinstall_instance`) is skipped by default because root volume replacement is slow on
AWS metal (~30-45 min).

## Validations

`SerialConsoleRetentionCheck` requires evidence from a historical serial console
log archive. The AWS config excludes that check because EC2 `GetConsoleOutput`
does not prove one-month retention by itself; the canonical
`BmSerialConsoleCheck` composite remains intact. Add an external archive
integration before re-enabling the retention check for AWS.

| Validation Group | Check | Step | Description |
|------------------|-------|------|-------------|
| `setup_checks` | `BmHostRunningCheck` | launch_instance | Instance is running |
| `list_instances` | `BmHostListedCheck` | list_instances | Target instance found in VPC |
| `tag_checks` | `BmHostTaggedCheck` | verify_tags | Instance has required tags (Name, CreatedBy) |
| `topology_placement` | `BmTopologyPlacementCheck` | topology_placement | Placement group CRUD operations |
| `serial_console` | `BmSerialConsoleCheck` | serial_console | Console output available |
| `cloud_init` | `BmCloudInitCheck` | launch_instance | Cloud-init completed |
| `image_installed` | `BmHostRunsExpectedImageCheck` | verify_image | Running host reports the configured image |
| `instance_info` | `BmHostStateReportedCheck` | describe_instance | Post-start state is running |
| `ssh` | `BmHostReadyCheck` | describe_instance | SSH works, OS is ubuntu |
| `gpu` | `BmGpusPresentCheck` | describe_instance | GPU visibility (8 GPUs) |
| `host_os` | `BmVcpuPinningCheck`, `BmPciBusCheck`, `BmHostSoftwareCheck` | describe_instance | vCPU pinning, PCI bus, kernel/drivers/BIOS |
| `host_status_logs` | `BmHostStatusLogCheck` | host_status_log | Recent host status log sources are present |
| `gpu_stress` | `BmGpuStressCheck` | describe_instance | PyTorch matrix multiply on all 8 GPUs |
| `nccl` | `BmNcclCheck` | describe_instance | NCCL AllReduce (NVLink/NVSwitch) |
| `training` | `BmTrainingCheck` | describe_instance | DDP training workload (50 steps) |
| `nvlink` | `BmNvlinkCheck` | describe_instance | NVLink topology and bandwidth |
| `infiniband` | `BmInfiniBandCheck` | describe_instance | InfiniBand device presence |
| `ethernet` | `BmEthernetCheck` | describe_instance | Network connectivity (ping 8.8.8.8) |
| `stop_checks` | `BmHostStoppedCheck` | stop_instance | Power-off confirmed |
| `start_checks` | `BmHostStartedCheck`, `BmIdentifierStableAfterStartCheck` | start_instance | Power-on confirmed, instance ID stable |
| `start_ssh` | `BmHostReadyAfterStartCheck` | start_instance | SSH works after start |
| `start_gpu` | `BmGpusPresentAfterStartCheck` | start_instance | GPUs visible after start (8 GPUs) |
| `reboot_checks` | `BmHostRebootedCheck`, `BmIdentifierStableAfterRebootCheck` | reboot_instance | Reboot confirmed, instance ID stable |
| `reboot_state` | `BmHostRunningAfterRebootCheck` | reboot_instance | Instance running after reboot |
| `reboot_ssh` | `BmHostReadyAfterRebootCheck` | reboot_instance | SSH works after reboot |
| `reboot_gpu` | `BmGpusPresentAfterRebootCheck` | reboot_instance | GPUs visible after reboot (8 GPUs) |
| `reboot_host_os` | `HostSoftwareCheck` | reboot_instance | Host OS persisted after reboot |
| `power_cycle_checks` | `BmHostPowerCycledCheck`, `BmIdentifierStableAfterPowerCycleCheck` | power_cycle_instance | Power-cycle recovery, instance ID stable |
| `power_cycle_state` | `BmHostRunningAfterPowerCycleCheck` | power_cycle_instance | Instance running after power-cycle |
| `power_cycle_ssh` | `BmHostReadyAfterPowerCycleCheck` | power_cycle_instance | SSH works after power-cycle |
| `power_cycle_gpu` | `BmGpusPresentAfterPowerCycleCheck` | power_cycle_instance | GPUs visible after power-cycle |
| `reinstall_state` | `BmHostRunningAfterReinstallCheck`, `BmIdentifierStableAfterReinstallCheck` | reinstall_instance | Running with a stable ID after reinstall (if enabled) |
| `reinstall_ssh` | `BmHostReadyAfterReinstallCheck` | reinstall_instance | SSH works after reinstall |
| `reinstall_gpu` | `BmGpusPresentAfterReinstallCheck` | reinstall_instance | GPUs visible after reinstall |
| `nim_inference` | `BmNimInferenceReadyCheck` | deploy_nim | NIM is healthy, exposes its model, and answers inference |
| `nim_teardown` | `BmNimDeploymentDeletedCheck` | teardown_nim | NIM container removed |
| `teardown_checks` | `BmResourcesDeletedCheck` | teardown | Instance terminated |
| `teardown_verification` | `BmHostTerminatedCheck` | verify_teardown | Instance, SG, and key pair confirmed deleted |

## Dev Workflow (Instance Reuse)

Bare-metal instances take ~3 min to provision and ~20 min to terminate.
For fast iteration during development, you can keep the instance alive
between runs.

```bash
# Run 1: launch + test, keep instance alive
AWS_BM_SKIP_TEARDOWN=true uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml -- -v -s

# Run 2+: reuse existing instance (get instance ID from run 1 output)
AWS_BM_INSTANCE_ID=i-xxx AWS_BM_KEY_FILE=/tmp/isv-bm-test-key.pem \
  AWS_BM_SKIP_TEARDOWN=true uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml -- -v -s

# Final: teardown (unset skip flag)
AWS_BM_INSTANCE_ID=i-xxx AWS_BM_KEY_FILE=/tmp/isv-bm-test-key.pem \
  uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml -- -v -s
```

## Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `region` | string | `us-west-2` | AWS region |
| `instance_type` | string | `g4dn.metal` | Bare-metal instance type |

### Available Bare-Metal GPU Instance Types

| Instance Type | GPUs | GPU Type | vCPUs | Memory |
|---------------|------|----------|-------|--------|
| g4dn.metal | 8 | T4 | 96 | 384 GiB |
| g5g.metal | 2 | T4g | 64 | 128 GiB |

## Test Duration

| Phase | Duration | Description |
|-------|----------|-------------|
| Launch Instance | 3-5 min | Create key, SG, launch bare-metal EC2, wait for running |
| List Instances | ~5s | Verify instance visible in VPC |
| Topology Placement | ~10s | Placement group CRUD |
| Serial Console | ~5s | Retrieve console output |
| Image Verify | ~10s | Cross-check image registry (verify_image) |
| SSH/GPU/Host OS | ~1 min | SSH, GPU, kernel, drivers, cloud-init |
| GPU Stress/NCCL/Training | 2-5 min | All GPU workload validations |
| NVLink/IB/Ethernet | ~30s | Interconnect and network checks |
| Stop + Start | 5-15 min | Power off, wait, power on, re-validate SSH/GPU |
| Reboot Instance | 10-20 min | Reboot via API, wait for status checks, SSH |
| NIM Deploy | 5-15 min | Pull + start NIM container |
| NIM Validation | ~30s | Health, models, inference |
| Teardown | 15-25 min | Terminate bare-metal instance, delete resources |
| Verify Teardown | ~5s | Confirm instance terminated, SG/key deleted |
| **Total** | **45-90 min** | Full test cycle |

## Cost & Cleanup

> **Warning**: These tests create AWS resources (EC2 bare-metal instances,
> security groups, key pairs) that incur costs. Bare-metal instances are
> significantly more expensive than regular VMs. Resources are automatically
> cleaned up during the teardown phase, but if teardown fails or is skipped,
> you must manually delete them to avoid ongoing charges.

### Checking for Orphaned Resources

```bash
# Find instances tagged by isvtest
aws ec2 describe-instances \
  --filters "Name=tag:CreatedBy,Values=isvtest" "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[*].Instances[*].[InstanceId,InstanceType,State.Name,Tags[?Key==`Name`].Value|[0]]' \
  --output table

# Terminate orphaned instances
aws ec2 terminate-instances --instance-ids i-xxx

# Find orphaned security groups
aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=isv-bm-*" \
  --query 'SecurityGroups[*].[GroupId,GroupName]' --output table

# Delete orphaned security groups
aws ec2 delete-security-group --group-id sg-xxx

# Delete orphaned key pairs
aws ec2 describe-key-pairs --filters "Name=key-name,Values=isv-bm-*" --output table
aws ec2 delete-key-pair --key-name isv-bm-test-key
```

## Related Documentation

- [AWS VM Validation Guide](../../vm/docs/aws-vm.md) - VM-as-a-Service tests
- [AWS Image Registry Validation Guide](../../image-registry/docs/aws-image-registry.md) - Image import tests
- [AWS EKS Validation Guide](../../eks/docs/aws-eks.md) - Kubernetes cluster tests
- [AWS Network Validation Guide](../../network/docs/aws-network.md) - VPC and network tests
- [Configuration Guide](../../../../../../../docs/guides/configuration.md) - Config file options
- [isvctl Documentation](../../../../../../../docs/packages/isvctl.md) - CLI reference
