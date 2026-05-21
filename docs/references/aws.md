# AWS Reference Implementation

The AWS implementation is a complete, working example of the ISV validation framework. Use it as a reference when implementing the [provider-agnostic templates](../../isvctl/configs/suites/README.md) for your own platform.

## How Templates and AWS Relate

```text
Template (provider-agnostic)                         AWS Reference (working example)
─────────────────────────────────────────            ──────────────────────────────────────────────
suites/vm.yaml                                       providers/aws/config/vm.yaml
providers/my-isv/scripts/vm/launch_instance.py       providers/aws/scripts/vm/launch_instance.py
     ↑ TODO block + demo-mode fallback                     ↑ full boto3 implementation
```

Most templates have corresponding AWS config and scripts that show how to fill
in the TODO blocks. Provider-neutral scaffolds without an AWS row below still
define the JSON contract for platforms that expose that domain.

## Available Modules

| Domain | Config | Scripts | Docs | Test Suite |
|--------|--------|---------|------|------------|
| **IAM** | [`providers/aws/config/iam.yaml`](../../isvctl/configs/providers/aws/config/iam.yaml) | [`providers/aws/scripts/iam/`](../../isvctl/configs/providers/aws/scripts/iam/) | [Guide](../../isvctl/configs/providers/aws/scripts/iam/docs/aws-iam.md) | [`suites/iam.yaml`](../../isvctl/configs/suites/iam.yaml) |
| **Network** | [`providers/aws/config/network.yaml`](../../isvctl/configs/providers/aws/config/network.yaml) | [`providers/aws/scripts/network/`](../../isvctl/configs/providers/aws/scripts/network/) | [Guide](../../isvctl/configs/providers/aws/scripts/network/docs/aws-network.md) | [`suites/network.yaml`](../../isvctl/configs/suites/network.yaml) |
| **VM** | [`providers/aws/config/vm.yaml`](../../isvctl/configs/providers/aws/config/vm.yaml) | [`providers/aws/scripts/vm/`](../../isvctl/configs/providers/aws/scripts/vm/) | [Guide](../../isvctl/configs/providers/aws/scripts/vm/docs/aws-vm.md) | [`suites/vm.yaml`](../../isvctl/configs/suites/vm.yaml) |
| **Bare Metal** | [`providers/aws/config/bare_metal.yaml`](../../isvctl/configs/providers/aws/config/bare_metal.yaml) | [`providers/aws/scripts/bare_metal/`](../../isvctl/configs/providers/aws/scripts/bare_metal/) | [Guide](../../isvctl/configs/providers/aws/scripts/bare_metal/docs/aws-bm.md) | [`suites/bare_metal.yaml`](../../isvctl/configs/suites/bare_metal.yaml) |
| **EKS** | [`providers/aws/config/eks.yaml`](../../isvctl/configs/providers/aws/config/eks.yaml) | [`providers/aws/scripts/eks/`](../../isvctl/configs/providers/aws/scripts/eks/) | [Guide](../../isvctl/configs/providers/aws/scripts/eks/docs/aws-eks.md) | [`suites/k8s.yaml`](../../isvctl/configs/suites/k8s.yaml) |
| **Control Plane** | [`providers/aws/config/control-plane.yaml`](../../isvctl/configs/providers/aws/config/control-plane.yaml) | [`providers/aws/scripts/control-plane/`](../../isvctl/configs/providers/aws/scripts/control-plane/) | [Guide](../../isvctl/configs/providers/aws/scripts/control-plane/docs/aws-control-plane.md) | [`suites/control-plane.yaml`](../../isvctl/configs/suites/control-plane.yaml) |
| **Image Registry** | [`providers/aws/config/image-registry.yaml`](../../isvctl/configs/providers/aws/config/image-registry.yaml) | [`providers/aws/scripts/image-registry/`](../../isvctl/configs/providers/aws/scripts/image-registry/) | [Guide](../../isvctl/configs/providers/aws/scripts/image-registry/docs/aws-image-registry.md) | [`suites/image-registry.yaml`](../../isvctl/configs/suites/image-registry.yaml) |
| **Security** | [`providers/aws/config/security.yaml`](../../isvctl/configs/providers/aws/config/security.yaml) | [`providers/aws/scripts/security/`](../../isvctl/configs/providers/aws/scripts/security/) | - | [`suites/security.yaml`](../../isvctl/configs/suites/security.yaml) |

Shared AWS utilities (error handling, EC2/VPC helpers) are in [`providers/aws/scripts/common/`](../../isvctl/configs/providers/aws/scripts/common/).

## Running AWS Validations

```bash
# Prerequisites: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION set

uv run isvctl test run -f isvctl/configs/providers/aws/config/iam.yaml
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml
uv run isvctl test run -f isvctl/configs/providers/aws/config/vm.yaml
uv run isvctl test run -f isvctl/configs/providers/aws/config/bare_metal.yaml
uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml
uv run isvctl test run -f isvctl/configs/providers/aws/config/control-plane.yaml
uv run isvctl test run -f isvctl/configs/providers/aws/config/image-registry.yaml
uv run isvctl test run -f isvctl/configs/providers/aws/config/security.yaml
```

## Using AWS as a Reference

When implementing a template for your platform:

1. Open the template script (e.g., `providers/my-isv/scripts/vm/launch_instance.py`)
2. Open the AWS equivalent side-by-side (e.g., `providers/aws/scripts/vm/launch_instance.py`)
3. Replace the TODO block with your platform's API calls, keeping the same JSON output fields
4. Read the AWS domain guide (linked above) for context on what each test validates and why
