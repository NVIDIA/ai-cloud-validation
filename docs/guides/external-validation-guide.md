# External Validation Guide

This guide explains how to create custom validation tests using the ISV validation framework **without modifying the repository**. You can use `isvctl` as a standalone tool with your own scripts and configuration files.

For the full configuration reference, see the [Configuration Guide](configuration.md).

## Overview

The ISV validation framework uses a **step-based architecture**:

```text
Config (YAML) --> Script (any language) --> JSON output --> Validations (assertions)
```

- **Scripts do the work** - Launch VMs, create clusters, test APIs (Python, Bash, Go, etc.)
- **Scripts output JSON** - Structured results to stdout
- **Validations check JSON** - Built-in assertion classes verify the output

---

## Quick Start

### 1. Install isvctl

```bash
# From source
git clone git@github.com:NVIDIA/ISV-NCP-Validation-Suite.git
cd ISV-NCP-Validation-Suite
uv sync
```

### 2. Create Your Project

```text
my-validations/
├── config.yaml           # Your validation config
├── scripts/
│   ├── provision.py      # Setup script
│   └── teardown.py       # Cleanup script
└── README.md
```

### 3. Run

```bash
uv run isvctl test run -f config.yaml
```

---

## Writing Scripts

Scripts must:

1. **Output valid JSON to stdout** (this is captured and validated)
2. **Exit 0 for success**, non-zero for failure
3. **Write logs/errors to stderr** (only stdout is captured as JSON)
4. **Include `success` and `platform` fields** in the JSON output

### Python Script Template

```python
#!/usr/bin/env python3
"""Provision cloud resources."""

import argparse
import json
import sys
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision cluster")
    parser.add_argument("--name", required=True, help="Cluster name")
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "kubernetes",  # or "vm", "network", etc.
    }

    try:
        # Your provisioning logic here
        result["success"] = True
        result["cluster_name"] = args.name
        result["node_count"] = 3
        result["endpoint"] = f"https://{args.name}.example.com"
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

Bash, Go, Terraform wrappers, or any language works - as long as valid JSON goes to stdout. See the [Configuration Guide](configuration.md#script-output-and-schema-validation) for more script examples.

---

## Configuration File

Here's a minimal working config:

```yaml
version: "1.0"

commands:
  myplatform:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: launch_instance
        phase: setup
        command: "python3 ./scripts/provision.py"
        args: ["--name", "{{cluster_name}}", "--region", "{{region}}"]
        timeout: 600

      - name: teardown
        phase: teardown
        command: "python3 ./scripts/teardown.py"
        args: ["--instance-id", "{{steps.launch_instance.instance_id}}"]
        timeout: 300

tests:
  platform: myplatform
  cluster_name: "my-validation"

  settings:
    region: "us-west-2"
    cluster_name: "test-cluster"

  validations:
    launch_checks:
      step: launch_instance
      checks:
        - StepSuccessCheck: {}
        - FieldExistsCheck:
            fields: ["instance_id", "public_ip"]
        - InstanceStateCheck:
            expected_state: "running"

    ssh_checks:
      step: launch_instance
      checks:
        - ConnectivityCheck: {}
        - GpuCheck:
            expected_gpus: 1

    teardown_checks:
      step: teardown
      checks:
        - StepSuccessCheck: {}
```

For full details on step options, template variables, schemas, and validation configuration, see the [Configuration Guide](configuration.md#config-structure).

---

## Validation Groups

Validations are grouped by meaningful category names. Set `step` at the group level or override per-check:

```yaml
validations:
  # Group-level step applies to all checks in the group
  setup_checks:
    step: setup
    checks:
      - StepSuccessCheck: {}
      - ClusterHealthCheck: {}

  # Per-check step overrides
  mixed:
    step: setup                   # default
    checks:
      - ClusterHealthCheck: {}    # uses default step
      - StepSuccessCheck:
          step: teardown          # overrides
```

For validation timing and phase control, see the [Configuration Guide](configuration.md#validation-configuration).

---

## Running Validations

```bash
# Run all phases
isvctl test run -f config.yaml

# Verbose output
isvctl test run -f config.yaml -v

# Run specific phase
isvctl test run -f config.yaml --phase setup

# Run only teardown (cleanup from a previous run)
isvctl test run -f config.yaml --phase teardown

# Merge configs (later overrides earlier)
isvctl test run -f base.yaml -f overrides.yaml

# Filter validations with labels or advanced pytest args
isvctl test run -f config.yaml -- -k "ConnectivityCheck"
isvctl test run -f config.yaml --label gpu
isvctl test run -f config.yaml -- -m "not slow"

# Debug: full output on failure
isvctl test run -f config.yaml -v -- -s --tb=long
```

> **Teardown behavior:** By default, teardown runs even when setup or test validations fail, ensuring cloud resources are cleaned up. Individual teardown step failures don't block remaining teardown steps (best-effort execution).

---

## Best Practices

1. **Always output valid JSON** - Even on failure: `{"success": false, "error": "..."}`
2. **Log to stderr** - Keep stdout clean for JSON
3. **Use settings for reusable values** - `region`, `instance_type`, etc.
4. **Set appropriate timeouts** - Account for cloud API latency
5. **Test scripts manually first** - Run standalone to verify JSON output
6. **Keep teardown idempotent** - Safe to re-run
7. **Never hardcode credentials** - Use environment variables or IAM roles

---

## Troubleshooting

**Script output issues:**

```bash
# Test script manually, verify valid JSON
python ./scripts/provision.py --name test 2>/dev/null | jq .
```

**Schema validation failures** - check required fields per schema in the [Configuration Guide](configuration.md#schema-auto-detection).

**SSH validation failures** - ensure step output includes:

- Host: `public_ip`, `host`, or `ssh_host`
- Key: `key_file`, `key_path`, or `ssh_key_path` (must exist, permissions 0600)
- User: `user` or `ssh_user` (default: `"ubuntu"`)

---

## Templates and scaffolds

For common validation scenarios, don't write your config from scratch - the
repo ships a ready-made scaffold:

- [**my-isv scaffold**](../../isvctl/configs/providers/my-isv/scripts/README.md) --
  copy-and-fill-in stubs covering IAM, control-plane, VM, bare metal,
  network, image registry, k8s, and Slurm. Each stub has a `TODO:` block
  and a demo-mode fallback.
- [**Test suite contracts**](../../isvctl/configs/suites/README.md) --
  per-step JSON-field breakdown for every domain.
- [**AWS reference**](../references/aws.md) - a complete working
  implementation of every stub in the scaffold.

Preview the whole pipeline with no cloud:

```bash
make demo-test   # sets ISVCTL_DEMO_MODE=1 and runs all 6 my-isv configs (~10s)
# Domains: iam, control-plane, vm, bare_metal, network, image-registry
```

---

## Related Documentation

- [Configuration Guide](configuration.md) - Full config reference (steps, schemas, validations, templates)
- [Validation Test Suites](../../isvctl/configs/suites/README.md) - Provider-agnostic test suites with step-by-step details
- [AWS Reference Implementation](../references/aws.md) - Working AWS examples for all templates
- [isvctl Package](../packages/isvctl.md) - CLI documentation
- [Local Development](local-development.md) - Development setup
