# zCompute × NVIDIA NCP Validation Suite — Compatibility Report

**Last updated:** 2026-05-20
**Author:** Amit Orenshtein, Zadara Storage
**Suite version:** NVIDIA ISV-NCP-Validation-Suite (experimental preview)
**zCompute clusters under test:**
- `172.29.0.20` — HGX GPU cluster (primary certification target)

---

## What We Are Doing

NVIDIA's NCP (NVIDIA Cloud Partner) certification program validates that a cloud
provider's infrastructure can reliably run NVIDIA GPU workloads — AI training,
inference, and GPU-accelerated applications.

This effort maps the NVIDIA ISV-NCP-Validation-Suite onto Zadara's zCompute
platform, which exposes AWS-compatible API endpoints. All provider-specific
work lives in `providers/zcompute/`.

---

## zCompute API Endpoints

```
https://172.29.0.20/api/v2/aws/<service>/
```

| Service | Endpoint | Status |
|---------|----------|--------|
| EC2 | `/api/v2/aws/ec2/` | ✅ Working |
| IAM | `/api/v2/aws/iam/` | ✅ Working |
| STS | `/api/v2/aws/sts/` | ✅ Working |
| S3 | N/A | ❌ No endpoint |
| Route53 | N/A | ❌ Not available |
| CloudWatch/CloudTrail | N/A | ❌ Not available |

**SSL:** Self-signed certificates — all boto3 clients use `verify=False` via
botocore URLLib3Session patch.

**Region:** `symphony` (single AZ, type `local-zone`).

---

## Confirmed Working API Operations

| Service | Operation | Notes |
|---------|-----------|-------|
| STS | `GetCallerIdentity` | |
| EC2 | `DescribeRegions` | Returns `symphony` |
| EC2 | `RunInstances` | Returns empty `Instances[]` — patched to find instance by key name |
| EC2 | `DescribeInstances` | Ignores `vpc-id` and `InstanceIds` filters — returns all project instances |
| EC2 | `StartInstances` / `StopInstances` / `RebootInstances` | |
| EC2 | `TerminateInstances` | May return `InternalServerError` for pending instances — retry needed |
| EC2 | `CreateVpc` / `DeleteVpc` | VPC starts `pending` — poll for `available` |
| EC2 | `CreateSubnet` / `DeleteSubnet` | Subnet starts `pending` — poll for `available` |
| EC2 | `CreateSecurityGroup` / `DeleteSecurityGroup` | `TagSpecifications` not supported |
| EC2 | `AuthorizeSecurityGroupIngress/Egress` / `RevokeSecurityGroupIngress/Egress` | |
| EC2 | `CreateVpcPeeringConnection` / `AcceptVpcPeeringConnection` / `DeleteVpcPeeringConnection` | |
| EC2 | `DescribeVpcPeeringConnections` | Returns `InternalFailure` — symp CLI fallback used |
| EC2 | `AllocateAddress` / `AssociateAddress` / `DisassociateAddress` / `ReleaseAddress` | EIPs use 172.28.x.x range |
| EC2 | `CreateInternetGateway` / `AttachInternetGateway` / `DeleteInternetGateway` | |
| EC2 | `CreateRouteTable` / `CreateRoute` / `AssociateRouteTable` | |
| EC2 | `CreateKeyPair` / `DeleteKeyPair` | `TagSpecifications` not supported; returns RSA PKCS#1 format (not OpenSSH) |
| EC2 | `DescribeKeyPairs` | Returns empty `KeyPairs[]` instead of `InvalidKeyPair.NotFound` — patched |
| EC2 | `DescribeAvailabilityZones` | Returns single AZ: `symphony` |
| EC2 | `DescribeImages` | Returns account images |
| EC2 | `ModifyVpcAttribute` | Works |
| EC2 | `ModifySubnetAttribute` (MapPublicIpOnLaunch) | Returns `AuthFailure` — silently ignored |
| IAM | `ListUsers`, `CreateUser`, `DeleteUser`, `GetUser` | |
| IAM | `CreateAccessKey`, `DeleteAccessKey`, `ListAccessKeys` | |
| IAM | `CreateGroup`, `ListGroups`, `DeleteGroup` | Used as tenant proxy |

## Confirmed NOT Working

| Service | Operation | Error | Impact |
|---------|-----------|-------|--------|
| IAM | `UpdateAccessKey` | `NotImplementedException` | **CRITICAL** — cannot disable keys. Ticket NK-19406 |
| IAM | `ListUserPolicies` | `AuthFailure` | Skipped — no inline policies on test users |
| EC2 | `GetConsoleOutput` | `500 InternalFailure` | Serial console not available |
| EC2 | `DescribeNetworkAcls` / `CreateNetworkAcl` | `AuthFailure` | **CRITICAL** — NACLs not supported, SG-only model |
| EC2 | `DescribeVpcPeeringConnections` | `InternalFailure` | Workaround: symp CLI fallback |
| boto3 | All waiters | `WaiterError` / `NotSupported` | Replaced with poll loops in ssl_wrapper.py and scripts |

---

## Test Suite Status

### ⚠️ Control Plane — PARTIAL PASS (9/11)

| Check | Result | Notes |
|-------|--------|-------|
| API Health (STS/EC2/IAM) | ✅ | |
| AccessKeyCreatedCheck / TenantCreatedCheck / AuthenticatedCheck | ✅ | |
| AccessKeyDisabledCheck | ❌ BLOCKED | `UpdateAccessKey` not implemented — ticket NK-19406 |
| AccessKeyRejectedCheck | ⛔ EXCLUDED | Depends on disable |
| TenantListedCheck / TenantInfoCheck / StepSuccessCheck ×2 | ✅ | |

---

### ✅ IAM — FULL PASS (5/5)

All checks passing.

---

### ⚠️ VM — PARTIAL PASS (24/24 collected, as of 2026-05-20)

**Instance:** `zh1.52xlarge` (208 vCPUs, ~1.87TB RAM, 8× H100 SXM5 80GB)
**AMI:** `ami-8269e586aa484003948818fadcbb475a` (Ubuntu 24.04 server cloudimg)

| Check | Result | Notes |
|-------|--------|-------|
| InstanceStateCheck / InstanceCreatedCheck / CloudInitCheck | ✅ | EIP allocated for public IP |
| InstanceListCheck / InstanceTagCheck | ✅ | |
| ConnectivityCheck / OsCheck (ssh, start, reboot) | ✅ | |
| VcpuPinningCheck / PciBusCheck / HostSoftwareCheck / DriverCheck / CpuInfoCheck | ✅ | |
| ContainerRuntimeCheck | ✅ | `nvidia_docker` subtest fails (NVIDIA GPG key expired) |
| InstanceStopCheck / InstanceStartCheck / InstanceRebootCheck | ✅ | |
| StableIdentifierCheck ×2 | ✅ | |
| GpuCheck (initial / post-start / post-reboot) | ⏳ IN PROGRESS | NVML driver/library version mismatch being fixed |
| SerialConsoleCheck / ConsoleRbacCheck | ⛔ EXCLUDED | `GetConsoleOutput` returns 500 |
| NimHealthCheck / NimModelCheck / NimInferenceCheck | ⏳ IN PROGRESS | NGC key with NIM entitlement received, testing |

**Key zCompute VM behaviors:**
- No auto-assigned public IP — EIP allocated at launch, released at teardown
- `RunInstances` returns empty `Instances[]` — patched to find instance by key name + LaunchTime
- Instance may go to `shutoff` — monitoring loop detects and auto-starts
- NVIDIA modules not auto-loaded at boot — `load_nvidia_modules()` runs via SSH after launch
- Docker, CUDA, NVIDIA Container Toolkit not on base image — installed at launch (~15 min)
- SSH key returned in RSA PKCS#1 format — converted to OpenSSH via `ssh-keygen`
- Driver install order critical: load modules BEFORE adding CUDA apt repo (CUDA repo ships newer nvidia-utils that mismatches kernel module)

---

### ⚠️ Network — PARTIAL PASS (10/10 collected, all phases PASS, as of 2026-05-20)

All test phases (setup/test/teardown) pass cleanly. 10/10 collected checks pass.

| Check | Result | Notes |
|-------|--------|-------|
| VpcCrudCheck | ✅ | |
| SubnetConfigCheck | ✅ | Single AZ, `require_multi_az: false` |
| VpcIsolationCheck | ✅ | symp CLI fallback for peering describe |
| SgCrudCheck | ✅ | `TagSpecifications` removed, `create_tags` used after |
| SecurityBlockingCheck | ✅ | NACLs skipped (SG-only model) |
| VpcIpConfigCheck | ✅ | `auto_assign_ip_mode: instance` |
| DhcpIpManagementCheck | ✅ | EIP allocated; SSH verified DHCP lease, IP match, DNS |
| StablePrivateIpCheck | ✅ | IP stable across stop/start |
| FloatingIpCheck | ✅ | EIP switch ~1.6s (limit 10s) |
| VpcPeeringCheck | ✅ | |
| NetworkConnectivityCheck / TrafficFlowCheck | ⛔ EXCLUDED | Require SSM agent |
| LocalizedDnsCheck | ⛔ EXCLUDED | Route 53 not available |
| SgWorkloadScopingCheck ×4 | ⛔ EXCLUDED | NACLs + VPC endpoints not supported |
| SdnLogging ×3 | ⛔ EXCLUDED | Not in released_tests.json |
| ByoipCheck / BackendSwitchFabric / NvlinkDomain | ⛔ EXCLUDED | Not applicable |

**All network fixes live in `scripts/network/ssl_wrapper.py`.**

---

### ⚠️ Kubernetes (EKS-D) — PARTIAL PASS (24/24 collected, as of 2026-05-19)

**Cluster:** EKS-D v1.30.4 — 1 control plane + 1 CPU worker + 2× HGX GPU workers (16× H100 total)

Single-node NCCL: **110 GB/s** | Multi-node NCCL: **31 GB/s avg / 116 GB/s peak** over RoCE

All 24 collected tests pass. 8 excluded pending engineering work:
`K8sOidcIssuerCheck`, `K8sNetworkPolicyCheck`, `K8sApiNetworkAclCheck`,
`K8sCncfConformanceCheck`, `K8sNodePoolCheck`, NIM ×3.

---

### ⬜ Security — NOT STARTED
### ⬜ Image Registry — NOT STARTED (no S3 endpoint)
### ⬜ Bare Metal — NOT STARTED (may not apply)

---

## Known Gaps

| # | Gap | Severity | Status |
|---|-----|----------|--------|
| 1 | `iam:UpdateAccessKey` not implemented | 🔴 CRITICAL | Ticket NK-19406 |
| 2 | NACLs not supported (SG-only model) | 🔴 CRITICAL | Needs engineering ticket |
| 3 | NGC API key with NIM entitlement | 🟠 HIGH | Received, testing |
| 4 | No S3 endpoint | 🟠 HIGH | Open |
| 5 | OIDC not configured (K8s) | 🟡 MEDIUM | Runbook ready, ~30 min |
| 6 | NetworkPolicy not enforced (Flannel→Calico) | 🟡 MEDIUM | Runbook ready, ~1 day |
| 7 | NVIDIA Container Toolkit GPG key expired | 🟡 MEDIUM | `nvidia_docker` subtest fails |
| 8 | rocep75s0 duplicate IP on HGX nodes | 🟡 MEDIUM | Workaround: excluded NIC |
| 9 | Single AZ only (`symphony`) | 🟢 MITIGATED | `require_multi_az: false` |
| 10 | Serial console unavailable | 🔵 LOW | `GetConsoleOutput` returns 500 |

---

## Run Log

| Date | Suite | Result | Notes |
|------|-------|--------|-------|
| 2026-05-11 | control-plane | ⚠️ PARTIAL PASS | 9/11 — UpdateAccessKey gap |
| 2026-05-11 | iam | ✅ FULL PASS | 5/5 |
| 2026-05-12–13 | vm runs 1–9 | ❌→⚠️ | Iterating on GPU/Docker/CUDA setup |
| 2026-05-14 | vm run 10 | ⚠️ 21/24 | DriverCheck ✅, GpuCheck×3 remaining |
| 2026-05-15 | k8s run 1 | ⚠️ 22/25 | GPU stress image pull timeout |
| 2026-05-15 | k8s run 2 | ✅ 23/25 | 2 expected skips (MPI multi-node, 1 GPU node) |
| 2026-05-15–16 | k8s (multi-node NCCL) | ✅ 24/24 | MPI Operator added, RoCE configured, 116 GB/s peak |
| 2026-05-19 | network (full suite) | ✅ 10/10 | All phases PASS including DHCP/stable IP/floating IP |
| 2026-05-20 | vm (NIM enabled) | ⚠️ 21+/27 | NIM: Payment Required (old key). GpuCheck NVML mismatch being fixed |

---

## Next Steps

1. ✅ Control-plane — partial pass (UpdateAccessKey gap documented)
2. ✅ IAM — full pass
3. ✅ Network — 10/10 collected, all phases pass
4. ✅ K8s — 24/24 collected, multi-node NCCL over RoCE
5. 🔄 VM — fix GpuCheck NVML version mismatch (driver load order fix deployed)
6. 🔄 VM/K8s NIM tests — NGC key with NIM entitlement received, testing
7. 🔴 `iam:UpdateAccessKey` — escalate to zCompute engineering (ticket NK-19406)
8. 🔴 NACLs — file engineering ticket
9. ⬜ OIDC configuration (K8s) — runbook ready
10. ⬜ Calico migration (K8s NetworkPolicy) — runbook ready
11. ⬜ Security suite
12. ⬜ Image Registry suite
