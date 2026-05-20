# zCompute Provider — NVIDIA NCP Validation Suite

NCP Validation Suite provider for Zadara's zCompute platform.

zCompute exposes AWS-compatible EC2, IAM, and STS API endpoints. All AWS scripts
are reused via `ssl_wrapper.py` (network suite) or direct boto3 with per-service
endpoint overrides (VM/K8s suites). The main differences from AWS are documented
in `COMPATIBILITY_REPORT.md`.

## Quick Start

### 1. Set environment variables

All credentials live in one file — fill it in and source it before every run:

```bash
# Edit suite.env with your cluster credentials, then:
source isvctl/configs/providers/zcompute/suite.env
```

Key variables: `ZCOMPUTE_BASE_URL`, `AWS_ENDPOINT_URL_EC2`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_REGION=symphony`, `SYMP_*`, `ZCOMPUTE_TEST_AMI_ID`,
`ZCOMPUTE_TEST_INSTANCE_TYPE`, `NGC_API_KEY`.

### 2. Install dependencies

```bash
cd ISV-NCP-Validation-Suite
uv sync
```

### 3. Run a suite

```bash
# VM suite (GPU lifecycle + NIM)
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/vm.yaml

# Network suite
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/network.yaml

# Kubernetes / EKS-D suite
uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s.yaml
```

### 4. Clean up stale resources after failed runs

```bash
cd isvctl/configs/providers/zcompute/config
python3 ../scripts/network/cleanup_stale_resources.py
```

## Suite Status (as of 2026-05-20)

| Suite | Config | Status | Collected |
|-------|--------|--------|-----------|
| Control Plane | `config/control-plane.yaml` | ⚠️ PARTIAL PASS | 9/11 |
| IAM | `config/iam.yaml` | ✅ FULL PASS | 5/5 |
| VM | `config/vm.yaml` | ⚠️ PARTIAL PASS | 24/24 collected |
| Network | `config/network.yaml` | ⚠️ PARTIAL PASS | 10/10 collected |
| Kubernetes | `config/k8s.yaml` | ⚠️ PARTIAL PASS | 24/24 collected |
| Security | `config/security.yaml` | ⬜ NOT STARTED | |
| Image Registry | `config/image-registry.yaml` | ⬜ NOT STARTED | No S3 endpoint |
| Bare Metal | `config/bare_metal.yaml` | ⬜ NOT STARTED | May not apply |

## Known Differences from AWS

| Feature | AWS | zCompute |
|---------|-----|----------|
| Public IPs | Auto-assigned at launch | EIP must be allocated and associated manually |
| boto3 waiters | Supported | Not supported — replaced with poll loops |
| VPC/Subnet state | Immediately `available` | Starts `pending` — must poll for `available` |
| `describe_instances` vpc-id filter | Works | Ignored — returns all project instances |
| `TagSpecifications` | Supported everywhere | Not supported in SG/KeyPair creation |
| NACLs | Supported | Not supported (`AuthFailure`) — SG-only model |
| Route 53 | Supported | Not available |
| S3 | Supported | No endpoint |
| Serial console | `GetConsoleOutput` works | Returns 500 |
| Single AZ | Multiple AZs | Single AZ (`symphony`) |
| SSL | Valid cert | Self-signed — `verify=False` required |
| Region name | Standard AWS regions | `symphony` |

## Directory Structure

```
providers/zcompute/
├── config/          ← Suite configs (our overrides of NVIDIA's suite definitions)
├── scripts/
│   ├── vm/          ← launch_instance.py, start_instance.py, reboot_instance.py, etc.
│   ├── network/     ← ssl_wrapper.py, create_vpc.py, vpc_crud_test.py, cleanup_stale_resources.py
│   ├── control-plane/
│   ├── k8s/
│   └── common/      ← ec2.py (load_nvidia_modules, setup_gpu_dependencies, EIP utils)
├── suite.env        ← All environment variables (fill in and source before running)
├── CLUSTER-SETUP.md ← EKS-D cluster setup runbook
└── COMPATIBILITY_REPORT.md ← Detailed API compatibility notes and test history
```

## Critical Gaps (Certification Blockers)

1. **`iam:UpdateAccessKey` not implemented** — cannot disable access keys.
   Engineering ticket: [NK-19406](https://zadara.atlassian.net/browse/NK-19406)

2. **NACLs not supported** — `DescribeNetworkAcls`/`CreateNetworkAcl` return `AuthFailure`.
   Engineering ticket needed.

See `COMPATIBILITY_REPORT.md` for the full gap list.
