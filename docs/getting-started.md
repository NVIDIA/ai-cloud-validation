# Getting Started

This guide covers installation and basic usage of NVIDIA ISV NCP Validation Suite.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)

## Installation

### Local Development

Clone the repository and install dependencies:

```bash
git clone https://github.com/NVIDIA/ISV-NCP-Validation-Suite.git
cd ISV-NCP-Validation-Suite
uv sync
```

Verify installation:

```bash
uv run isvctl --help
```

## Quick Start

### Try it without cloud credentials (~10s)

The fastest way to see the framework run is the `my-isv` scaffold - a set of
copy-and-fill-in stubs with a demo-mode fallback that makes every validation
pass without touching a real cloud:

```bash
make demo-test
```

This runs all 6 my-isv provider configs end-to-end under `ISVCTL_DEMO_MODE=1`.
No credentials, no cloud resources created. Great sanity check after
install, and the starting point if you're adding a new platform --
see [`providers/my-isv/scripts/`](../isvctl/configs/providers/my-isv/scripts/).

Create a ready-to-edit scaffold for your own provider:

```bash
uv run isvctl provider scaffold acme
```

Provider implementations can live in private repositories and be run by path;
see the [my-isv scaffold README](../isvctl/configs/providers/my-isv/scripts/README.md#private-provider-repositories)
for the current workflow.

Then preview the generated VM flow without cloud access:

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run -f isvctl/configs/providers/acme/config/vm.yaml
```

### Running Validation Tests

**From source (development):**

```bash
# AWS control plane validation
uv run isvctl test run -f isvctl/configs/providers/aws/config/control-plane.yaml

# AWS network validation
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml

# Kubernetes cluster
uv run isvctl test run -f isvctl/configs/suites/k8s.yaml

# Local MicroK8s
uv run isvctl test run -f isvctl/configs/providers/microk8s.yaml

# Local Minikube
uv run isvctl test run -f isvctl/configs/providers/minikube.yaml

# Local k3s
uv run isvctl test run -f isvctl/configs/providers/k3s.yaml

# Slurm cluster
uv run isvctl test run -f isvctl/configs/suites/slurm.yaml
```

**From installed wheel:**

```bash
# Kubernetes
isvctl test run -f configs/suites/k8s.yaml

# Slurm (may require sudo for docker access)
sudo -E env "PATH=$PATH" isvctl test run -f configs/suites/slurm.yaml
```

> **Note:** Slurm tests using docker containers may require `sudo` if the Slurm user
> doesn't have docker group permissions. Use `sudo -E env "PATH=$PATH" isvctl ...`
> to preserve environment and PATH.

### Common Options

```bash
# Verbose output (shows script output on failure)
isvctl test run -f configs/suites/k8s.yaml -v

# Pass extra pytest args
isvctl test run -f configs/suites/k8s.yaml -- -v -s -k "NodeCount"

# Upload results to ISV Lab Service
isvctl test run -f configs/suites/k8s.yaml --lab-id 35

# With ISV software version metadata
isvctl test run -f configs/suites/k8s.yaml --lab-id 35 --isv-software-version "2.1.0-rc3"

# Dry run (validate config without executing)
isvctl test run -f configs/suites/k8s.yaml --dry-run
```

When you use `--lab-id`, the same process creates the test run (shown as STARTED in the portal) and, after all phases complete, updates it to SUCCESS or FAILED. **If the process is killed, times out, or hangs before that update, the run stays STARTED.** See [Troubleshooting: Test runs stuck in STARTED](guides/troubleshooting-started-tests.md) for causes and fixes.

### Remote Deployment

Deploy and run tests on a remote machine:

```bash
uv run isvctl deploy run <target-ip> -f isvctl/configs/suites/k8s.yaml

# With jumphost for air-gapped environments
uv run isvctl deploy run <target-ip> -j <jumphost>:<port> -u ubuntu -f isvctl/configs/suites/k8s.yaml
```

See [Remote Deployment Guide](guides/remote-deployment.md) for details.

## Environment Variables

| Variable | Description |
| -------- | ----------- |
| `AWS_ACCESS_KEY_ID` | AWS access key (for AWS tests) |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key (for AWS tests) |
| `AWS_REGION` | AWS region (default: us-west-2) |
| `ISV_SERVICE_ENDPOINT` | Required for result upload to ISV Lab Service |
| `ISV_SSA_ISSUER` | Required for result upload to ISV Lab Service |
| `ISV_CLIENT_ID` | Required for result upload to ISV Lab Service |
| `ISV_CLIENT_SECRET` | Required for result upload to ISV Lab Service |
| `NGC_API_KEY` | Required for NIM model benchmarks |
| `KUBECTL` | Optional kubectl-compatible CLI prefix (overrides auto-detection; for Kubernetes tests) |
| `K8S_PROVIDER` | Kubernetes provider hint - `microk8s`, `k3s`, `minikube`, or `kubectl` (auto-detected if unset; overridden by `KUBECTL`) |

## Next Steps

- [my-isv Scaffold](../isvctl/configs/providers/my-isv/scripts/README.md) - Adding your own platform? Start with `isvctl provider scaffold`
- [Validation Test Suites](../isvctl/configs/suites/README.md) - The platform-agnostic validation contract
- [AWS Reference Implementation](references/aws.md) - Working AWS examples to study
- [Configuration Guide](guides/configuration.md) - Config file format and options
- [External Validation Guide](guides/external-validation-guide.md) - Custom validations without modifying the repo
- [Local Development](guides/local-development.md) - Running tests locally
- [isvctl Reference](packages/isvctl.md) - Full isvctl documentation
