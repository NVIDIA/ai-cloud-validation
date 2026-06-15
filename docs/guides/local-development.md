# Local Development Guide

This guide covers running AI Cloud Validation suite locally for development and testing.

## Prerequisites

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone https://github.com/NVIDIA/ai-cloud-validation.git
cd ai-cloud-validation
uv sync
```

## Running Tests Locally

### AWS Tests

AWS tests require credentials:

```bash
# Option 1: Environment variables
export AWS_ACCESS_KEY_ID="your-key"
export AWS_SECRET_ACCESS_KEY="your-secret"
export AWS_REGION="us-west-2"

# Option 2: AWS CLI profile
aws configure

# Verify credentials
aws sts get-caller-identity
```

Run AWS validation tests:

```bash
# Control plane validation (API health, IAM, tenants)
uv run isvctl test run -f isvctl/configs/providers/aws/config/control-plane.yaml

# Network validation (VPC, subnets, security groups)
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml

# VM validation (EC2 instances)
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml

# IAM user lifecycle
uv run isvctl test run -f isvctl/configs/providers/aws/config/iam.yaml
```

### Kubernetes Tests

Three local Kubernetes providers are supported: MicroK8s, Minikube, and k3s.
Only one can hold the GPU at a time - stop the others before switching.

#### MicroK8s

```bash
# Install MicroK8s
sudo snap install microk8s --classic
sudo usermod -a -G microk8s $USER
newgrp microk8s

# Enable GPU support
microk8s enable dns storage gpu

# Verify
microk8s kubectl get nodes

# Run tests
uv run isvctl test run -f isvctl/configs/providers/microk8s.yaml
```

#### Minikube

```bash
# Install Minikube (https://minikube.sigs.k8s.io/docs/start/)
# Start with GPU passthrough (requires nvidia-container-toolkit)
minikube start --driver docker --container-runtime docker --gpus all

# Install GPU Operator (driver/toolkit/device-plugin provided by minikube)
helm install gpu-operator nvidia/gpu-operator \
  --namespace nvidia-gpu-operator --create-namespace \
  --set driver.enabled=false \
  --set toolkit.enabled=false \
  --set devicePlugin.enabled=false

# Run tests
uv run isvctl test run -f isvctl/configs/providers/minikube.yaml
```

#### k3s

```bash
# Install k3s
curl -sfL https://get.k3s.io | sh -

# Make kubeconfig readable
sudo chmod 644 /etc/rancher/k3s/k3s.yaml

# Install GPU Operator (driver/toolkit/device-plugin provided by host)
KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm install gpu-operator nvidia/gpu-operator \
  --namespace nvidia-gpu-operator --create-namespace \
  --set driver.enabled=false \
  --set toolkit.enabled=false \
  --set devicePlugin.enabled=false

# Run tests
KUBECONFIG=/etc/rancher/k3s/k3s.yaml uv run isvctl test run -f isvctl/configs/providers/k3s.yaml
```

## Useful Options

```bash
# Verbose output (shows script output on failure)
uv run isvctl test run -f config.yaml -v

# Dry run (validate config without executing)
uv run isvctl test run -f config.yaml --dry-run

# Run specific tests by name
uv run isvctl test run -f config.yaml -- -k "vpc_crud"

# Pass pytest arguments
uv run isvctl test run -f config.yaml -- -v -s --tb=short
```

## Creating Test Configs

### Minimal Config

```yaml
version: "1.0"

commands:
  network:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: my_test
        phase: test
        command: "python ./my_script.py"
        args: ["--region", "{{region}}"]
        timeout: 300

      - name: cleanup
        phase: teardown
        command: "python ./cleanup.py"
        timeout: 60

tests:
  platform: network
  cluster_name: "local-test"
  settings:
    region: "us-west-2"

  validations:
    my_checks:
      - StepSuccessCheck:
          step: my_test
```

### Testing Scripts Manually

Run scripts directly to debug:

```bash
# Run a script directly
python isvctl/configs/providers/aws/scripts/control-plane/check_api.py \
  --region us-west-2 \
  --services ec2,s3,iam,sts

# Output should be valid JSON with success and platform fields
```

## Debugging

### Config Validation

```bash
# Validate without running
uv run isvctl test run -f config.yaml --dry-run
```

### Step Failures

When a step fails:

1. Check the error message in the output
2. Run the script manually with the same arguments
3. Verify JSON output format

### Validation Failures

Validations check JSON output. Common issues:

- Missing required fields (`success`, `platform`)
- Field name mismatches
- Wrong data types

### Credential Issues

```bash
# AWS
aws sts get-caller-identity

# Kubernetes
kubectl cluster-info
```

## Unit Tests

Run the framework's own tests:

```bash
# All packages
make test

# Specific package
cd isvtest && uv run pytest -m unit

# With coverage
uv run pytest --cov=src
```

## Code Quality

```bash
# Linting
make lint

# Formatting
make format

# Pre-commit hooks
uvx pre-commit run -a
```

## Related Documentation

- [Configuration Guide](configuration.md) - Config file format and options
- [Getting Started](../getting-started.md) - Installation guide
