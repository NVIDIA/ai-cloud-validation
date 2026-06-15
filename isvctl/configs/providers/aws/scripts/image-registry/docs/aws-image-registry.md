# AWS ISO/VMDK Import Validation Guide

This guide provides a complete walkthrough for validating AWS VM Import capabilities using the AI Cloud Validation framework. These tests verify the ability to import external disk images (VMDK) as AMIs and validate GPU functionality on the resulting instances.

## Overview

The AWS ISO/VMDK import validation tests verify:

1. **upload_image** - Download VMDK, upload to S3, import as AMI
2. **crud_image** - Get, list, copy, delete AMI lifecycle
3. **launch_instance** - Launch GPU instance from imported AMI
4. **crud_install_config** - EC2 Launch Template CRUD lifecycle
5. **teardown** - Clean up all resources (instance, AMI, S3, IAM roles)

**Key Features:**

- All steps are **SELF-CONTAINED** - they create their own S3 buckets, IAM roles, and clean up after
- **No pre-existing infrastructure required** - just AWS credentials
- Supports **local VMDK files** to skip download (faster iteration)
- **SSH validation** for GPU checks via paramiko
- **Step-based architecture** - scripts handle AWS operations, validations are platform-agnostic

## Architecture

### Step-Based Architecture

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  Scripts (Platform-Specific - boto3)                                     │
│  ┌──────────────────┐ ┌──────────────────┐ ┌─────────────────────────┐   │
│  │  upload_image.py │ │  crud_image.py   │ │  crud_install_config.py │   │
│  │ - Download VMDK  │ │ - Get AMI        │ │ - Create template       │   │
│  │ - Upload to S3   │ │ - List AMIs      │ │ - Read template         │   │
│  │ - Import as AMI  │ │ - Copy AMI       │ │ - Update template       │   │
│  │                  │ │ - Delete copy    │ │ - Delete template       │   │
│  └──────────────────┘ └──────────────────┘ └─────────────────────────┘   │
│  ┌──────────────────┐ ┌──────────────────┐                               │
│  │launch_instance.py│ │   teardown.py    │                               │
│  │ - Create keypair │ │ - Terminate EC2  │                               │
│  │ - Create SG      │ │ - Delete AMI     │                               │
│  │ - Launch EC2     │ │ - Delete bucket  │                               │
│  │                  │ │ - Cleanup IAM    │                               │
│  └──────────────────┘ └──────────────────┘                               │
└──────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Validations (Platform-Agnostic)                                         │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────────┐  │
│  │ StepSuccessCheck │ │ CrudOperations   │ │ ConnectivityCheck        │  │
│  │ FieldExistsCheck │ │     Check        │ │ OsCheck, GpuCheck        │  │
│  │ InstanceState    │ │                  │ │                          │  │
│  │     Check        │ │                  │ │                          │  │
│  └──────────────────┘ └──────────────────┘ └──────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

### Test Flow

```text
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  1. upload_image (SETUP phase)                                     │
│     Download VMDK ─▶ Create S3 Bucket ─▶ Upload ─▶ Import AMI      │
│     Output: {image_id, storage_bucket, disk_ids}                   │
│     Validations: StepSuccessCheck, FieldExistsCheck                │
└────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  2. crud_image (TEST phase)                                        │
│     Get AMI ─▶ List AMIs ─▶ Copy AMI ─▶ Delete copy                │
│     Output: {image_id, operations: {get, list, create, delete}}    │
│     Validations: StepSuccessCheck, CrudOperationsCheck             │
└────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  3. launch_instance (TEST phase)                                   │
│     Create Key Pair ─▶ Create SG ─▶ Launch from imported AMI       │
│     Output: {instance_id, public_ip, key_path}                     │
│     Validations: InstanceStateCheck, ConnectivityCheck,            │
│                  OsCheck, GpuCheck                                 │
└────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  4. crud_install_config (TEST phase)                               │
│     Create template ─▶ Read ─▶ Update ─▶ Delete                    │
│     Output: {config_id, config_name, operations: {create, read,    │
│              update, delete}}                                      │
│     Validations: StepSuccessCheck, FieldExistsCheck                │
└────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  5. teardown (TEARDOWN phase)                                      │
│     Terminate Instance ─▶ Delete AMI ─▶ Delete Snapshots           │
│     Delete Bucket ─▶ Delete Key Pair ─▶ Delete SG ─▶ Delete IAM    │
│     Validations: StepSuccessCheck                                  │
└────────────────────────────────────────────────────────────────────┘
```

> **Note**: The canonical image-registry config also defines `install_image_bm` and
> `install_config_bm` steps for bare-metal provisioning. On AWS, these are implemented
> in the [bare_metal.yaml](../../../config/bare_metal.yaml) config instead
> (as `verify_image` and `verify_config` steps). They are auto-skipped here since
> this config doesn't define those steps.

## Prerequisites

### Required Tools

```bash
# AWS CLI (v2)
aws --version

# Python with boto3 and requests (installed via uv sync)
uv run python -c "import boto3, requests, paramiko; print('OK')"

# uv (Python package manager)
uv --version
```

### AWS Credentials

Configure AWS credentials with required permissions:

```bash
# Option 1: AWS CLI configuration
aws configure

# Option 2: Environment variables
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-2

# Option 3: IAM instance role (recommended for CI/CD on EC2)
# Credentials are automatically provided by the instance metadata service
```

### Required IAM Permissions

The AWS credentials must have these permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:ImportImage",
        "ec2:DescribeImportImageTasks",
        "ec2:CancelImportTask",
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:DescribeInstances",
        "ec2:DescribeImages",
        "ec2:DeregisterImage",
        "ec2:DeleteSnapshot",
        "ec2:DescribeSnapshots",
        "ec2:CreateKeyPair",
        "ec2:DeleteKeyPair",
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeInstanceTypeOfferings",
        "ec2:CreateTags",
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:CreateInstanceProfile",
        "iam:DeleteInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile",
        "iam:PassRole"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Quick Start

```bash
# Clone and install
git clone <repository-url>
cd ai-cloud-validation
uv sync

# Run AWS ISO import validation
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml
```

### Test Duration Summary

| Phase | Duration | Description |
|-------|----------|-------------|
| Download VMDK | 2-5 min | ~700MB from Ubuntu cloud |
| Upload to S3 | 2-5 min | Depends on network speed |
| VM Import | 15-30 min | AWS import_image processing |
| CRUD Image | 2-5 min | AMI get/list/copy/delete lifecycle |
| Launch Instance | 5-8 min | Instance + status checks |
| GPU Validation | 1-2 min | SSH + nvidia-smi |
| CRUD Install Config | ~30s | Launch Template CRUD |
| Cleanup | 1-2 min | Delete all resources |
| **Total** | **28-55 min** | Full test cycle |

---

## Configuration

### image-registry.yaml Structure

The AWS provider config imports the canonical image-registry test suite and overrides commands with boto3 scripts:

```yaml
import:
  - ../../../../suites/image-registry.yaml

version: "1.0"

commands:
  image_registry:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: upload_image          # Setup: Download VMDK, upload to S3, import as AMI
        phase: setup
        command: "python3 ../upload_image.py"
        args: ["--image-url", "{{image_url}}", "--image-format", "{{image_format}}", "--region", "{{region}}"]
        timeout: 3600

      - name: crud_image            # Test: AMI get/list/copy/delete lifecycle
        phase: test
        command: "python3 ../crud_image.py"
        args: ["--image-id", "{{steps.upload_image.image_id}}", "--region", "{{region}}"]
        timeout: 600

      - name: launch_instance       # Test: Launch GPU instance from imported AMI
        phase: test
        command: "python3 ../launch_instance.py"
        args: ["--ami-id", "{{steps.upload_image.image_id}}", "--instance-type", "{{instance_type}}", "--region", "{{region}}"]
        timeout: 600

      - name: crud_install_config   # Test: EC2 Launch Template CRUD
        phase: test
        command: "python3 ../crud_install_config.py"
        args: ["--region", "{{region}}"]
        timeout: 120

      - name: teardown              # Teardown: Clean up all resources
        phase: teardown
        command: "python3 ../teardown.py"
        # ... instance, AMI, snapshots, bucket, key, SG, IAM cleanup
        timeout: 1800

tests:
  cluster_name: "aws-image-registry-validation"
  settings:
    region: "us-west-2"
    image_url: "https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.vmdk"
    image_format: "vmdk"
    instance_type: "g4dn.xlarge"
```

See [`providers/aws/config/image-registry.yaml`](../../../config/image-registry.yaml) for the full config with all arguments and timeouts.

### Settings Reference

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `region` | string | `us-west-2` | AWS region |
| `image_url` | string | Ubuntu 24.04 | URL to download VMDK |
| `image_format` | string | `vmdk` | Image format (vmdk, vhd, ova, raw) |
| `instance_type` | string | `g4dn.xlarge` | GPU instance type |
| `teardown_flag` | string | `` | Set to `--skip-destroy` to skip cleanup |

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region for tests | `us-west-2` |
| `AWS_ACCESS_KEY_ID` | AWS access key | From AWS config |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | From AWS config |
| `AWS_ISO_SKIP_TEARDOWN` | Skip teardown if `true` | `false` |

---

## Running Tests

### Run Full ISO Import Test

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml
```

### Skip Teardown (for debugging)

```bash
AWS_ISO_SKIP_TEARDOWN=true uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml
```

### Run in Different Region

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml \
  --set tests.settings.region=us-east-1
```

### Use Different Instance Type

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml \
  --set tests.settings.instance_type=g5.xlarge
```

### Verbose Output

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml -v
```

---

## Validations

### Validations by Step

| Validation Group | Checks | Step |
|------------------|--------|------|
| `image_upload` | `StepSuccessCheck`, `FieldExistsCheck` (image_id, storage_bucket, disk_ids) | upload_image |
| `image_crud` | `StepSuccessCheck`, `FieldExistsCheck`, `CrudOperationsCheck` (get, list, create, delete) | crud_image |
| `vm_from_image` | `StepSuccessCheck`, `FieldExistsCheck`, `InstanceStateCheck` (running) | launch_instance |
| `vm_ssh` | `ConnectivityCheck`, `OsCheck` (ubuntu) | launch_instance |
| `install_config_crud` | `StepSuccessCheck`, `FieldExistsCheck` (config_id, config_name, operations) | crud_install_config |
| `teardown_checks` | `StepSuccessCheck` | teardown |

The canonical config also defines `bm_from_image` and `bm_from_config` validation groups
for bare-metal provisioning steps. These are auto-skipped in this config since the
`install_image_bm` and `install_config_bm` steps are not defined here (they live in
[`bare_metal.yaml`](../../../config/bare_metal.yaml) instead).

---

## Cost & Cleanup

> **Warning**: These tests create AWS resources (S3 buckets, EC2 instances, AMIs,
> EBS snapshots, security groups) that incur costs. Resources are automatically
> cleaned up during the teardown phase, but if teardown fails or is skipped,
> you must manually delete them to avoid ongoing charges.

Tests automatically clean up all resources, even on failure:

| Resource | Cleanup Action |
|----------|----------------|
| EC2 Instance | `terminate_instances()` |
| AMI | `deregister_image()` |
| EBS Snapshots | `delete_snapshot()` |
| S3 Objects | `delete_object()` |
| S3 Bucket | `delete_bucket()` |
| EC2 Key Pair | `delete_key_pair()` |
| Security Group | `delete_security_group()` |
| IAM Instance Profile | `delete_instance_profile()` |
| IAM Role | `delete_role()` |
| vmimport Role | Policy updated (not deleted) |

### Manual Cleanup

If cleanup fails, manually delete resources:

```bash
# Find orphaned resources
aws ec2 describe-instances --filters "Name=tag:CreatedBy,Values=isvtest"
aws ec2 describe-images --owners self
aws s3 ls | grep isv-iso

# Manual cleanup
aws ec2 terminate-instances --instance-ids i-xxxxx
aws ec2 deregister-image --image-id ami-xxxxx
aws s3 rb s3://isv-iso-import-xxxxx --force
```

---

## Troubleshooting

### "Import task failed"

Check the import task status:

```bash
aws ec2 describe-import-image-tasks --import-task-ids import-ami-xxxxx
```

Common causes:

- VMDK format not supported (must be streamOptimized or flat)
- vmimport role missing permissions
- S3 bucket in different region

### "InsufficientInstanceCapacity"

No capacity for the instance type in the AZ. Try a different region or instance type:

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml \
  --set tests.settings.instance_type=g5.xlarge
```

### "SSH connection failed"

The instance may not have a public IP or security group is misconfigured:

```bash
# Check instance
aws ec2 describe-instances --instance-ids i-xxxxx

# Check security group
aws ec2 describe-security-groups --group-ids sg-xxxxx
```

### "nvidia-smi not found"

The imported AMI may not have NVIDIA drivers pre-installed. Consider:

1. Using an AMI with pre-installed NVIDIA drivers
2. Adding a step to install drivers after launch

---

## Supported Image Formats

| Format | Extension | Notes |
|--------|-----------|-------|
| VMDK | `.vmdk` | VMware virtual disk (recommended) |
| VHD | `.vhd` | Hyper-V virtual disk |
| OVA | `.ova` | Open Virtual Appliance |
| RAW | `.raw` | Raw disk image |

**Note**: QCOW2 is not directly supported by AWS VM Import. Convert to RAW first:

```bash
qemu-img convert -f qcow2 -O raw image.qcow2 image.raw
```

---

## Related Documentation

- [AWS VM Validation Guide](../../vm/docs/aws-vm.md) - EC2 GPU instance tests
- [AWS Network Validation Guide](../../network/docs/aws-network.md) - VPC and network tests
- [Configuration Guide](../../../../../../../docs/guides/configuration.md) - Config file options
- [isvctl Documentation](../../../../../../../docs/packages/isvctl.md) - CLI reference
