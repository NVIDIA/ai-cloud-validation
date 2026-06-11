# AWS EKS GPU Cluster Terraform Module

This Terraform module deploys an AWS EKS cluster configured for GPU workloads and ISV Lab validation testing.

## Features

- **EKS Cluster** with configurable Kubernetes version
- **GPU Node Group** with NVIDIA GPU instances (g4dn, g5, p4d, p5)
- **System Node Group** for non-GPU workloads
- **Upstream Kubernetes Cluster Autoscaler** installed via Helm with IRSA
- **NVIDIA GPU Operator** installed via Helm
- **EFS Storage** for ReadWriteMany PVCs (NIM model cache)
- **gp3 StorageClass** as default for EBS volumes
- **VPC** with public/private subnets and NAT Gateway
- **IRSA** for EBS CSI, EFS CSI, and Cluster Autoscaler

## Prerequisites

- [Terraform](https://terraform.io) >= 1.5.0
- [AWS CLI](https://aws.amazon.com/cli/) configured with appropriate credentials
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/) (optional, for manual chart management)

### AWS Permissions

Your AWS credentials need permissions for:

- EKS cluster management
- EC2 instances and Auto Scaling Groups
- VPC, subnets, security groups
- IAM roles and policies
- EFS file systems (if enabled)

## Quick Start

```bash
# 1. Navigate to terraform directory
cd isvctl/configs/providers/aws/scripts/eks/terraform

# 2. Copy and customize configuration
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars as needed

# 3. Initialize Terraform
terraform init

# 4. Review the plan
terraform plan

# 5. Apply the configuration
terraform apply

# 6. Configure kubectl
aws eks update-kubeconfig --name $(terraform output -raw cluster_name) --region $(terraform output -raw region)

# 7. Verify cluster
kubectl get nodes
kubectl get pods -n gpu-operator

# 8. Run ISV Lab tests
export AWS_REGION=$(terraform output -raw region)
export EKS_CLUSTER_NAME=$(terraform output -raw cluster_name)
export NGC_API_KEY=nvapi-XXXXX  # Your NGC API key

cd ../../../../../../../..  # Back to repo root
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml
```

## Configuration

### GPU Instance Types

| Instance Type | GPUs | GPU Type | VRAM | Best For |
|---------------|------|----------|------|----------|
| `g4dn.xlarge` | 1 | T4 | 16GB | Development, small models |
| `g5.xlarge` | 1 | A10G | 24GB | Development, medium models |
| `g5.12xlarge` | 4 | A10G | 24GB | Multi-GPU workloads |
| `g5.48xlarge` | 8 | A10G | 24GB | Large multi-GPU workloads |
| `p4d.24xlarge` | 8 | A100 40GB | 40GB | Training, large inference |
| `p5.48xlarge` | 8 | H100 80GB | 80GB | LLM inference, training |

### Example Configurations

**Development (minimal cost):**

```hcl
gpu_node_instance_types = ["g5.2xlarge"]
gpu_node_desired_size   = 1
single_nat_gateway      = true
enable_efs              = false  # Use node-local storage
```

**Testing (balanced):**

```hcl
gpu_node_instance_types = ["g5.2xlarge"]
gpu_node_desired_size   = 2
single_nat_gateway      = true
enable_efs              = true
```

**Production (high availability):**

```hcl
gpu_node_instance_types = ["p4d.24xlarge"]
gpu_node_desired_size   = 4
single_nat_gateway      = false  # HA NAT
enable_efs              = true
gpu_node_taints         = true   # Isolate GPU workloads
mig_strategy            = "single"  # Enable MIG for A100/H100 GPUs
```

### MIG (Multi-Instance GPU) Configuration

MIG allows partitioning supported NVIDIA GPUs (A100, H100) into smaller instances. The `mig_strategy` variable controls how the GPU Operator handles MIG:

| Strategy | Description | Use Case |
|----------|-------------|----------|
| `none` | MIG disabled (default) | A10G/g5, T4/g4dn, or when full GPU is needed |
| `single` | Single MIG strategy | A100/H100 when partitioning GPUs |
| `mixed` | Mixed MIG strategy | Advanced: different MIG configs per node |

**Important:** MIG is only supported on A100 and H100 GPUs. For A10G (g5 instances) and T4 (g4dn instances), keep the default `mig_strategy = "none"`.

```hcl
# For A10G/g5 clusters (default - no MIG)
mig_strategy = "none"

# For A100/H100 clusters with MIG enabled
mig_strategy = "single"
```

### Cluster Autoscaler

The module installs the upstream Kubernetes Cluster Autoscaler chart by default so the EKS provider satisfies the
`K8sClusterAutoscalerCheck` integration validation. It is configured for AWS auto-discovery, uses IRSA for AWS API
permissions, and disables scale-down by default to keep validation runs low-risk.

```hcl
install_cluster_autoscaler       = true
cluster_autoscaler_chart_version = ""  # Empty uses the upstream chart repository default
cluster_autoscaler_image_tag     = ""  # Empty derives v<kubernetes_version>.0
```

## Outputs

After `terraform apply`, useful outputs include:

```bash
# Get cluster name
terraform output cluster_name

# Get kubectl configuration command
terraform output configure_kubectl

# Get environment exports for ISV Lab tools
terraform output environment_exports

# Get EFS file system ID (if enabled)
terraform output efs_file_system_id
```

## Cleanup

To destroy all resources:

```bash
terraform destroy
```

**Warning:** This will delete:

- EKS cluster and all workloads
- VPC and networking components
- EFS file system and data
- All associated IAM roles

## Troubleshooting

### GPU Operator pods not running

```bash
# Check GPU operator status
kubectl get pods -n gpu-operator

# Check for node issues
kubectl describe nodes -l nvidia.com/gpu.workload=true

# View GPU operator logs
kubectl logs -n gpu-operator -l app=gpu-operator
```

### EFS mount failures

```bash
# Verify EFS file system exists
aws efs describe-file-systems --file-system-id $(terraform output -raw efs_file_system_id)

# Check EFS mount targets
aws efs describe-mount-targets --file-system-id $(terraform output -raw efs_file_system_id)

# Verify security group allows NFS (port 2049)
```

### Node group scaling issues

```bash
# Check Auto Scaling Group events
aws autoscaling describe-scaling-activities --auto-scaling-group-name <asg-name>

# Check for capacity issues
kubectl describe nodes | grep -A5 "Conditions"
```

## State Management

This module uses local state by default (`terraform.tfstate` in the current directory).

For team environments, consider migrating to remote state:

```hcl
# In main.tf, replace the local backend with:
terraform {
  backend "s3" {
    bucket = "your-terraform-state-bucket"
    key    = "isv-lab-cluster/terraform.tfstate"
    region = "us-west-2"
  }
}
```

## Cost Estimation

Approximate monthly costs (us-west-2, as of 2024):

| Component | Configuration | Est. Cost/Month |
|-----------|---------------|-----------------|
| EKS Control Plane | 1 cluster | $73 |
| System Nodes | 2x m5.large | $140 |
| GPU Nodes (g5.2xlarge) | 1 node | $1,000 |
| GPU Nodes (p4d.24xlarge) | 1 node | $24,000 |
| NAT Gateway | 1 gateway | $45 + data |
| EFS | 100GB | $30 |

**Note:** GPU instances are expensive. Use `gpu_node_min_size = 0` and scale up only when testing.

## Related Documentation

- [AWS EKS Validation Guide](../docs/aws-eks.md)
- [ISV NCP Validation Suite Getting Started](../../../../../../../docs/getting-started.md)
- [NVIDIA GPU Operator Documentation](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/)
