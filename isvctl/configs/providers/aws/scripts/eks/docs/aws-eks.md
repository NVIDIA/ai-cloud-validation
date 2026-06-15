# AWS EKS GPU Cluster - AI Cloud Validation Guide

This guide provides a complete walkthrough for ISV partners to provision AWS infrastructure, run NVIDIA ISV validation tests, and clean up resources.

## Overview

The ISV validation workflow consists of three phases:

1. **Setup** - Provision EKS cluster with GPU nodes using Terraform
2. **Test** - Run validation tests (GPU checks, workloads, benchmarks)
3. **Teardown** - Destroy infrastructure to avoid ongoing costs

The `isvctl` tool orchestrates all three phases automatically.

## Prerequisites

### Required Tools

```bash
# AWS CLI (v2)
aws --version

# Terraform (v1.5+)
terraform --version

# kubectl (or set KUBECTL env var to use an alternative CLI, e.g., "oc")
kubectl version --client

# curl (used to auto-detect your public IP for the EKS API allowlist)
curl --version

# Helm (v3)
helm version

# uv (Python package manager)
uv --version
```

### AWS Credentials

Configure AWS credentials with permissions to create EKS clusters, VPCs, and IAM roles:

```bash
# Option 1: AWS CLI configuration
aws configure

# Option 2: Environment variables (long-term credentials)
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-2

# Option 3: Environment variables (STS temporary credentials)
# Use this when using assumed roles, SSO, or MFA-protected access
export AWS_ACCESS_KEY_ID=ASIA...          # Temporary access key (starts with ASIA)
export AWS_SECRET_ACCESS_KEY=...          # Temporary secret key
export AWS_SESSION_TOKEN=...              # Session token (required for STS)
export AWS_REGION=us-west-2

# Option 4: IAM instance role (recommended for CI/CD on EC2/EKS)
# Credentials are automatically provided by the instance metadata service
```

### NGC API Key

Required for NIM inference workloads:

```bash
export NGC_API_KEY=nvapi-XXXXX
```

Get your API key from [NGC](https://ngc.nvidia.com/setup/api-key).

---

## Quick Start (Recommended)

The fastest way to run the complete validation:

```bash
# Clone and install
git clone <repository-url>
cd ai-cloud-validation
uv sync

# Run full validation (setup -> test -> teardown)
# This runs all three phases: setup provisions/queries cluster, test runs validations
NGC_API_KEY=nvapi-XXXXX \
  uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml
```

> **Note**: By default, teardown runs automatically to clean up resources. Set `AWS_SKIP_TEARDOWN=true` to preserve resources after testing. The setup phase is idempotent - if the cluster already exists, it will skip provisioning and just generate inventory. See [Teardown](#phase-3-teardown) for details.

---

## Step-by-Step Workflow

### Phase 1: Setup (Provision Infrastructure)

The setup phase uses Terraform to provision:

- VPC with public/private subnets and NAT Gateway
- EKS cluster (Kubernetes 1.32)
- GPU node group (g5.2xlarge by default)
- System node group (m5.large)
- NVIDIA GPU Operator
- EFS storage for NIM model cache
- gp3 as default StorageClass
- Required IAM roles and security groups

#### Why Setup is Always Required

The setup phase serves two purposes:

1. **Provision infrastructure** (if cluster doesn't exist)
2. **Generate inventory** (cluster info needed by tests)

Even if your cluster already exists, you must run setup to generate the inventory data that tests require. The setup script is smart - it detects existing clusters and skips Terraform provisioning.

#### Run Setup Only

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml --phase setup
```

#### Customize Infrastructure

Override Terraform variables via environment:

```bash
# Custom region and instance type
TF_VAR_region=us-east-1 \
TF_VAR_gpu_node_instance_types='["p4d.24xlarge"]' \
TF_VAR_gpu_node_desired_size=2 \
  uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml --phase setup
```

By default, setup auto-detects the caller's public IPv4 address and passes
`TF_VAR_cluster_endpoint_public_access_cidrs='["<detected-ip>/32"]'` to Terraform.
Set `TF_VAR_cluster_endpoint_public_access_cidrs` yourself when running from a
known VPN, proxy, or CI egress range.

Or create a custom config file:

```yaml
# my-aws-config.yaml
commands:
  kubernetes:
    setup:
      command: "isvctl/configs/providers/aws/scripts/eks/setup.sh"
      timeout: 1800
      env:
        TF_AUTO_APPROVE: "true"
        TF_VAR_region: "us-east-1"
        TF_VAR_gpu_node_instance_types: '["p4d.24xlarge"]'
        TF_VAR_gpu_node_desired_size: "2"
        TF_VAR_cluster_endpoint_public_access_cidrs: '["YOUR.IP.ADDRESS/32"]'
```

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml -f my-aws-config.yaml --phase setup
```

#### Available Terraform Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TF_VAR_region` | us-west-2 | AWS region |
| `TF_VAR_cluster_name_prefix` | isv-gpu | Cluster name prefix |
| `TF_VAR_environment` | dev | Environment suffix (cluster name = prefix-env) |
| `TF_VAR_kubernetes_version` | 1.32 | EKS Kubernetes version |
| `TF_VAR_gpu_node_instance_types` | ["g5.2xlarge"] | GPU instance types (JSON array) |
| `TF_VAR_gpu_node_desired_size` | 1 | Number of GPU nodes |
| `TF_VAR_system_node_instance_types` | ["m5.large"] | System node instance types |
| `TF_VAR_system_node_desired_size` | 2 | Number of system nodes |
| `TF_VAR_cluster_endpoint_public_access_cidrs` | Auto-detected caller IPv4 `/32` | EKS API allowlist; override for VPN/proxy/CI egress ranges |
| `TF_VAR_enable_efs` | true | Enable EFS for NIM cache |

#### Supported GPU Instance Types

| Instance Type | GPUs | GPU Type | GPU Memory | Use Case |
|---------------|------|----------|------------|----------|
| g4dn.xlarge | 1 | T4 | 16GB | Development, small models |
| g5.xlarge | 1 | A10G | 24GB | Development, medium models |
| g5.48xlarge | 8 | A10G | 24GB | Multi-GPU workloads |
| p4d.24xlarge | 8 | A100 40GB | 40GB | Large models, MIG support |
| p5.48xlarge | 8 | H100 80GB | 80GB | LLM inference, MIG support |

> **Note**: MIG (Multi-Instance GPU) is only supported on A100 and H100 GPUs.

---

### Phase 2: Test (Run Validations)

The test phase runs validation checks and workloads.

> **Important**: The test phase requires inventory data from the setup phase. You must always run setup before test, even if your cluster already exists. The setup phase is idempotent - it will detect the existing cluster and skip provisioning.

#### Run All Phases (Recommended)

```bash
# Runs: setup -> test -> teardown
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml

# To preserve resources, skip teardown:
AWS_SKIP_TEARDOWN=true uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml
```

#### Run Phases Separately

If you need to run phases individually:

```bash
# First run setup (generates inventory, skips provisioning if cluster exists)
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml --phase setup

# Then run tests (uses inventory from previous setup)
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml --phase test
```

> **Note**: The `--phase` option only accepts one value at a time. To run setup and test together without teardown, use `AWS_SKIP_TEARDOWN=true` and omit `--phase`.

#### Validation Categories

**Basic Validations** (default):

- Node count and readiness
- GPU driver version
- GPU Operator status
- nvidia-smi accessibility
- GPU capacity verification

**Workloads** (excluded by default):

- NCCL communication tests
- GPU stress tests
- NIM inference benchmarks

#### Run Specific Tests

```bash
# Run only node checks (runs all phases, but filters tests)
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml -- -k "Node"

# Run GPU operator checks
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml -- -k "GpuOperator"

# Include workloads (long-running)
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml -- -m "workload"

# Verbose output
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml -- -v -s
```

#### AWS VPC Network Tests (Separate Config)

VPC network tests run independently without requiring an EKS cluster:

```bash
# Run all AWS network tests
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml

# Run VPC CRUD test only
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml -- -k "AwsVpcCrud"

# Run subnet/connectivity tests with existing VPC
AWS_VPC_ID=vpc-xxxxx uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml
```

#### Test Output

Results are saved to:

- Console output with pass/fail status
- JUnit XML report (for CI integration)
- JSON inventory file

---

### Phase 3: Teardown

> **Important**: Teardown [runs by default](../../../../../../../docs/guides/external-validation-guide.md#running-validations), even after test failures. Set `AWS_SKIP_TEARDOWN=true` to preserve resources.

#### Check Current Resources

```bash
# View Terraform state
cd isvctl/configs/providers/aws/scripts/eks/terraform
terraform state list

# Check running costs
aws ce get-cost-and-usage \
  --time-period Start=$(date -d '1 day ago' +%Y-%m-%d),End=$(date +%Y-%m-%d) \
  --granularity DAILY \
  --metrics BlendedCost
```

#### Destroy Infrastructure

```bash
# Option 1: Via isvctl (recommended) - teardown runs by default
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml --phase teardown

# Option 2: Direct Terraform
cd isvctl/configs/providers/aws/scripts/eks/terraform
terraform destroy
```

When teardown is skipped (via `AWS_SKIP_TEARDOWN=true`), you'll see:

```text
========================================
  TEARDOWN SKIPPED - Resources Preserved
========================================

AWS infrastructure was NOT destroyed.
Your EKS cluster and resources are still running.

To destroy resources, run without AWS_SKIP_TEARDOWN:
  uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml --phase teardown
```

---

## Complete Example: End-to-End Validation

Here's a complete example running all phases:

```bash
#!/bin/bash
set -e

# Configuration
export NGC_API_KEY=nvapi-XXXXX
export TF_VAR_region=us-west-2
export TF_VAR_gpu_node_instance_types='["g5.2xlarge"]'
export TF_VAR_gpu_node_desired_size=1

# Run all phases: setup -> test -> teardown
# Setup detects existing cluster and skips provisioning if it exists
# Teardown runs by default (set AWS_SKIP_TEARDOWN=true to preserve resources)
echo "=== RUNNING VALIDATION (setup -> test -> teardown) ==="
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml

echo "=== VALIDATION COMPLETE ==="
```

---

## Troubleshooting

### Setup Issues

#### EKS API Access Denied

If you can't connect to the cluster:

```bash
# Check the public IP your shell is using
curl https://checkip.amazonaws.com

# Update allowlist
TF_VAR_cluster_endpoint_public_access_cidrs='["YOUR.IP/32"]' \
  terraform apply
```

Clusters created with older versions of this config may still have
`0.0.0.0/0` in the EKS API allowlist. Re-apply Terraform with a restricted
CIDR, or run the EKS teardown phase and create the cluster again.

### Test Failures

#### No GPU Nodes Found

```bash
# Check node labels
kubectl get nodes -l nvidia.com/gpu.present=true

# Check GPU Operator pods
kubectl get pods -n gpu-operator

# View GPU Operator logs
kubectl logs -n gpu-operator -l app=gpu-operator
```

#### MIG Test Failures

MIG (Multi-Instance GPU) requires A100 or H100 GPUs. For g5/g4 instances:

```yaml
# The eks.yaml config already excludes MIG tests for A10G/T4 GPUs
# If you see MIG failures, ensure you're using the correct config
```

#### NIM Workload Timeouts

```bash
# Check NGC API key
echo $NGC_API_KEY

# Check NIM pod status
kubectl get pods -l app.kubernetes.io/name=nim-llm

# View NIM logs
kubectl logs -l app.kubernetes.io/name=nim-llm
```

## IAM Permissions

### Basic EKS Access

For running basic Kubernetes validations, you need:

```json
{
  "Effect": "Allow",
  "Action": [
    "eks:DescribeCluster",
    "eks:ListClusters",
    "eks:ListNodegroups",
    "eks:DescribeNodegroup"
  ],
  "Resource": "*"
}
```

### VPC Network Tests

For running AWS VPC network validation tests (`-m network`), additional permissions are required:

```json
{
  "Effect": "Allow",
  "Action": [
    "ec2:CreateVpc",
    "ec2:DeleteVpc",
    "ec2:DescribeVpcs",
    "ec2:ModifyVpcAttribute",
    "ec2:CreateSubnet",
    "ec2:DeleteSubnet",
    "ec2:DescribeSubnets",
    "ec2:CreateSecurityGroup",
    "ec2:DeleteSecurityGroup",
    "ec2:AuthorizeSecurityGroupIngress",
    "ec2:AuthorizeSecurityGroupEgress",
    "ec2:DescribeSecurityGroups",
    "ec2:RunInstances",
    "ec2:TerminateInstances",
    "ec2:DescribeInstances",
    "ec2:CreateTags",
    "ec2:DescribeRouteTables",
    "ec2:DescribeNetworkAcls",
    "ec2:DescribeVpcPeeringConnections",
    "ec2:DescribeImages",
    "ec2:DescribeNatGateways",
    "ec2:DescribeVpcAttribute"
  ],
  "Resource": "*"
}
```

> **Note**: VPC network tests create and delete temporary AWS resources (VPCs, Security Groups, EC2 instances). Ensure your IAM policy allows these operations and consider using resource tags for cost tracking.

---

## Cost & Cleanup

> **Warning**: These tests create AWS resources (EKS clusters, EC2 node groups,
> VPCs, IAM roles) that incur costs. Resources are automatically cleaned up
> during the teardown phase, but if teardown fails or is skipped, you must
> manually delete them to avoid ongoing charges.

```bash
# Check for running EKS clusters
aws eks list-clusters --query 'clusters' --output table

# Delete orphaned cluster
aws eks delete-cluster --name isv-test-cluster
```

## Related Documentation

- [Terraform Module README](../terraform/README.md) - Detailed Terraform configuration
- [Configuration Guide](../../../../../../../docs/guides/configuration.md) - Config file options
- [Remote Deployment](../../../../../../../docs/guides/remote-deployment.md) - Deploy to remote clusters
