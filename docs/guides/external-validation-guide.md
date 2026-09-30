# External Validation Guide

This guide explains how to create custom validation tests using the AI Cloud Validation framework **without modifying the repository**. You can use `isvctl` as a standalone tool with your own scripts and configuration files.

For the full configuration reference, see the [Configuration Guide](configuration.md).

## Overview

The AI Cloud Validation framework uses a **step-based architecture**:

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
git clone git@github.com:NVIDIA/ai-cloud-validation.git
cd ai-cloud-validation
git checkout v0.11.0
uv sync
```

Use the explicit release tag approved for the validation. Cloning `main`
without selecting a tag is the developer workflow.

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

## Capabilities and check requirements

You declare which **capabilities** you support. There are four, and they are
mutually exclusive execution environments — you run on one at a time:

`vm` · `bare_metal` · `kubernetes` · `slurm`

Each has a **platform suite** you owe by declaring it (`vm`, `bare_metal`,
`kubernetes`, `slurm`). Everything else is a **plain suite** (`storage`,
`network`, `iam`, ...) that mixes checks needing no particular infrastructure
with checks that presuppose some. Each check declares which:

```yaml
requires: []                # core - runs in every context
requires: [kubernetes]      # runs only under --capability kubernetes
requires: [vm, bare_metal]  # any-match: either context satisfies it
```

**Nothing is mandatory.** A check is in scope only if you declared the suite
containing it, so 100% is always relative to what you declared. Declaring a
subset legitimately yields zero checks from the suites you left out — that is
the design, not a gap.

One rule decides what runs, and it does not depend on how you named the config
(`--suite`, `-f` and `--label` behave identically):

> **A plain suite with no `--capability` runs its core checks.** Name a
> capability to add the checks gated on it.

If a step builds or destroys a fixture only some contexts need, gate it the same
way so a core run neither provisions nor leaks it:

```yaml
- name: teardown_cluster
  phase: teardown
  command: "./scripts/teardown_cluster.sh"
  requires: [kubernetes]
```

See the [Configuration Guide](configuration.md#capabilities-and-requires) for the
full model.

---

## Running Validations

```bash
# Canonical suite against an already-running system - no provider lifecycle
isvctl test run --suite storage --capability kubernetes --phase test

# One suite for your provider - the form the UI emits
isvctl test run --provider acme --suite storage                         # core checks
isvctl test run --provider acme --suite storage --capability kubernetes # + k8s checks
isvctl test run --provider acme --suite kubernetes                      # a platform suite

# See what would run, without executing anything
isvctl test run --provider acme --suite storage --capability vm --dry-run

# Point at a config file directly (same capability rule applies)
isvctl test run -f config.yaml

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

# Labels compose with suite selection
isvctl test run --provider acme --suite storage --capability vm --label min_req

# Re-run one failed check in its lifecycle context (pytest passthrough)
isvctl test run --provider acme --suite storage --capability kubernetes -- -k K8sCsiPvcExpandCheck

# Debug: full output on failure
isvctl test run -f config.yaml -v -- -s --tb=long
```

`--suite` on its own resolves the canonical file in `configs/suites/`.
Adding `--provider` resolves that provider's config instead, including any
lifecycle commands it defines.

Re-running a single failed check is pytest passthrough after `--`; there are no
dedicated rerun flags. Setup steps re-run, which is the deliberate trade for not
maintaining a dependency graph.

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
  network, observability, image registry, security, k8s, and Slurm. Each
  stub has a `TODO:` block and a demo-mode fallback.
- [**Test suite contracts**](../../isvctl/configs/suites/README.md) --
  per-step JSON-field breakdown for every domain.
- [**AWS reference**](../references/aws.md) - a working implementation for
  domains with AWS-backed reference scripts.

Preview the whole pipeline with no cloud:

```bash
make demo-test   # sets ISVCTL_DEMO_MODE=1 and runs all my-isv configs (~10s)
# Suites are listed in the Makefile MY_ISV_SUITES variable; DEMO_CAP_<suite>
# names the capability a suite runs under so its gated checks are exercised.
```

The k8s and Slurm examples are excluded: they drive a real cluster, so a
dummy-success stub has nothing to return for them.

---

## Related Documentation

- [Configuration Guide](configuration.md) - Full config reference (steps, schemas, validations, templates)
- [Validation Test Suites](../../isvctl/configs/suites/README.md) - Provider-agnostic test suites with step-by-step details
- [AWS Reference Implementation](../references/aws.md) - Working AWS examples for AWS-backed templates
- [isvctl Package](../packages/isvctl.md) - CLI documentation
- [Local Development](local-development.md) - Development setup
