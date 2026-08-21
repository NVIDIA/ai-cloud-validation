# Remote Deployment

Deploy AI Cloud Validation suite to a remote machine and run validation tests.

## Overview

The `isvctl deploy run` command packages and transfers the tools to a remote target, then executes validation tests. This is useful for:

- Testing clusters you can't access directly
- Air-gapped environments (via jumphost)
- CI/CD pipelines deploying to target clusters

## Prerequisites

- SSH connectivity to `<target-ip>` (key-based auth recommended)
- A user with sufficient permissions (e.g., docker group for container tests, or sudo if required)
- Network reachability to external services (ISV Lab Service, registries) unless using a jumphost

## Environment Variables

Set these on the local machine. The upload variables are read here, since
`deploy` reports the test run itself; the others are forwarded to the remote
`isvctl test run`:

| Variable | Description | Read |
| -------- | ----------- | ---- |
| `ISV_SERVICE_ENDPOINT` | Required for result upload to ISV Lab Service | locally |
| `ISV_SSA_ISSUER` | Required for result upload to ISV Lab Service | locally |
| `ISV_CLIENT_ID` | Required for result upload to ISV Lab Service | locally |
| `ISV_CLIENT_SECRET` | Required for result upload to ISV Lab Service | locally |
| `NGC_API_KEY` | Required for NIM model benchmarks | forwarded |
| `ISVTEST_INCLUDE_UNRELEASED` | Include checks not yet in `released_tests.json` | forwarded |
| `ISVTEST_BREAKFIX_ALLOW_MUTATION` | Explicitly allow a mutating break-fix validation | forwarded |
| `ISVTEST_BREAKFIX_NODE` | Exact Kubernetes node selected for break-fix validation | forwarded |

Anything else the tests need has to reach the target another way - a config file
under `isvctl/` travels in the deployment archive, so `-f` overrides are the
reliable route for values like StorageClass names.

## Usage

### Basic Deployment

```bash
# Deploy and run one suite on the remote machine
uv run isvctl deploy run <target-ip> --suite kubernetes

# Or name the config file directly
uv run isvctl deploy run <target-ip> -f isvctl/configs/suites/k8s.yaml
```

### Selecting a Capability

A plain suite such as `storage` runs its core checks unless a capability is
named; `--capability` is forwarded to the remote run. A platform suite already
runs under the capability it declares, so combining the two is rejected.

```bash
uv run isvctl deploy run <target-ip> --suite storage --capability kubernetes
```

### With Jumphost

For air-gapped environments, use a jumphost/bastion server:

```bash
# -j accepts <jumphost> or [user@]<jumphost> with optional :<port>
uv run isvctl deploy run <target-ip> -j <jumphost[:port]> -u ubuntu -f isvctl/configs/suites/k8s.yaml

# Example with user and custom port:
uv run isvctl deploy run 10.0.0.10 -j ubuntu@bastion.example.com:2222 -u ubuntu -f isvctl/configs/suites/k8s.yaml
```

### With Config Overrides

Later config files override earlier ones:

```bash
uv run isvctl deploy run <target-ip> -f isvctl/configs/suites/k8s.yaml -f my-overrides.yaml
```

### With Pytest Arguments

Pass extra pytest arguments after `--`:

```bash
uv run isvctl deploy run <target-ip> -f isvctl/configs/suites/slurm.yaml -- -v -s -k "test_name"
```

### Node Maintenance Validation

The BFX01-02 reference uses the NVIDIA Maintenance Operator API on the
target's active Kubernetes context. It never selects a node implicitly and
drains only its uniquely labelled probe workload.

```bash
ISVTEST_INCLUDE_UNRELEASED=1 ISVTEST_BREAKFIX_ALLOW_MUTATION=1 ISVTEST_BREAKFIX_NODE=<dedicated-test-node> uv run isvctl deploy run <target-ip> -f isvctl/configs/providers/kubernetes-node-maintenance.yaml -- -v -s -k ReturnNodeMaintenanceCheck
```

### With ISV Lab Service Integration

Upload results to the ISV Lab Service:

```bash
uv run isvctl deploy run <target-ip> -f isvctl/configs/suites/k8s.yaml --lab-id 35 --isv-software-version "2.1.0-rc3"
```

## Command Options

Run `uv run isvctl deploy run --help` for all available options:

| Option | Description |
| ------ | ----------- |
| `<target>` | Target machine IP or hostname |
| `--suite` | Canonical suite to run, instead of `-f` |
| `--capability` | Capability context for the remote run (plain suites only) |
| `-f, --config` | Config file(s) to use (can be specified multiple times) |
| `-u, --user` | SSH user on target machine |
| `-j, --jumphost` | Jumphost for air-gapped environments |
| `--lab-id` | Lab ID for result upload |
| `--isv-software-version` | ISV software version metadata |

## Platform-Specific Notes

### Slurm

For Slurm tests that use docker, ensure the remote user is in the docker group or configure privilege escalation if supported:

```bash
# If docker requires sudo on the remote host
sudo -E env "PATH=$PATH" isvctl test run -f isvctl/configs/suites/slurm.yaml
```

### Kubernetes

Ensure the remote user has `kubectl` access configured (e.g., `~/.kube/config` exists and is valid).

**Filesystem POSIX tests:** The pjdfstest source is downloaded at build time via the make command (`make vendor-pjdfstest`) which must be run on the remote host.

## Troubleshooting

**Connection refused:**

- Verify SSH access: `ssh <user>@<target-ip>`
- Check firewall rules on target
- Verify jumphost connectivity if using `-j`

**Permission denied:**

- Ensure SSH key is authorized on target
- Check user has required permissions (docker group, kubectl access, etc.)

**Tests fail remotely but work locally:**

- Verify environment variables are set locally (they're forwarded automatically)
- Check network access from target to required services (registries, ISV Lab Service)

## Related Documentation

- [Getting Started](../getting-started.md) - Installation and first steps
- [Configuration Guide](configuration.md) - Config file format and options
- [isvctl Reference](../packages/isvctl.md) - Full isvctl documentation
