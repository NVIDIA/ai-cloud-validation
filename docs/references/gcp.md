# GCP Target Implementation

The GCP implementation is a target-platform port of the ISV validation framework. The [AWS reference](aws.md) is the canonical example for how the suite expects each provider to behave; the GCP scripts under `providers/gcp/` translate that contract onto Google Cloud's API surface (Compute Engine, IAM, etc.).

This page covers the operator setup needed to run `isvctl` tests against GCP.

## Available Modules

| Domain | Config | Scripts | Test Suite |
|--------|--------|---------|------------|
| **VM** | [`providers/gcp/config/vm.yaml`](../../isvctl/configs/providers/gcp/config/vm.yaml) | [`providers/gcp/scripts/vm/`](../../isvctl/configs/providers/gcp/scripts/vm/) | [`suites/vm.yaml`](../../isvctl/configs/suites/vm.yaml) |
| **Network** | [`providers/gcp/config/network.yaml`](../../isvctl/configs/providers/gcp/config/network.yaml) | [`providers/gcp/scripts/network/`](../../isvctl/configs/providers/gcp/scripts/network/) | [`suites/network.yaml`](../../isvctl/configs/suites/network.yaml) |
| **IAM** | [`providers/gcp/config/iam.yaml`](../../isvctl/configs/providers/gcp/config/iam.yaml) | [`providers/gcp/scripts/iam/`](../../isvctl/configs/providers/gcp/scripts/iam/) | [`suites/iam.yaml`](../../isvctl/configs/suites/iam.yaml) |
| **Security** | [`providers/gcp/config/security.yaml`](../../isvctl/configs/providers/gcp/config/security.yaml) | [`providers/gcp/scripts/security/`](../../isvctl/configs/providers/gcp/scripts/security/) | [`suites/security.yaml`](../../isvctl/configs/suites/security.yaml) |
| **Image Registry** ([guide](../../isvctl/configs/providers/gcp/scripts/image-registry/docs/gcp-image-registry.md)) | [`providers/gcp/config/image-registry.yaml`](../../isvctl/configs/providers/gcp/config/image-registry.yaml) | [`providers/gcp/scripts/image-registry/`](../../isvctl/configs/providers/gcp/scripts/image-registry/) | [`suites/image-registry.yaml`](../../isvctl/configs/suites/image-registry.yaml) |

Shared GCP utilities (compute helpers, SSH wrappers, retry envelopes, error classifiers) are in [`providers/gcp/scripts/common/`](../../isvctl/configs/providers/gcp/scripts/common/).

Other domains (Bare Metal, EKS, Control Plane) are not yet implemented for GCP.

## Prerequisites

### 1. Operator GCP environment

- A GCP project with **billing enabled** and the **Compute Engine API** enabled.
- **L4 GPU quota** (`NVIDIA_L4_GPUS`) of at least 1 in at least one zone listed under [Supported zones](#supported-zones-for-l4-gpu-vm-tests) below. The VM domain's `launch_instance` step provisions a `g2-standard-8` instance for the lifecycle, console-RBAC, and NIM subtests.
- The principal running the tests (user or service account) needs roughly these roles on the project (consolidate via custom role if preferred):
  - `roles/compute.admin` — create / delete / start / stop / reboot instances and firewall rules.
  - `roles/iam.serviceAccountAdmin` + `roles/iam.serviceAccountUser` — `console_rbac` self-provisions short-lived probe service accounts and mints access tokens against them.
  - `roles/iam.serviceAccountTokenCreator` on the probe SAs (granted dynamically by the test itself, but the project-level binding is needed to allow the test to grant it).

### 2. Authentication

`isvctl` calls into the GCP Python SDKs (`google-cloud-compute`, `google-auth`, etc.) which use Application Default Credentials. Either:

```bash
# Option A — user credentials (recommended for local dev / interactive runs)
gcloud auth application-default login

# Option B — service account key file
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json
```

### 3. Project ID resolution

The GCP scripts resolve the active project ID in this order:

1. Explicit `--project` argument (rarely passed by the suite; reserved for ad-hoc invocations).
2. `GOOGLE_CLOUD_PROJECT` environment variable.
3. `GCLOUD_PROJECT` environment variable.
4. The project bundled with Application Default Credentials (set by `gcloud auth application-default login`).

If none resolve, every step fails fast with a structured `credentials_missing` error.

```bash
# Most operators just set this once:
export GOOGLE_CLOUD_PROJECT=your-project-id
```

### 4. NIM API key (for `deploy_nim` step)

The shared NIM deployment script reads `NGC_API_KEY` from the environment. If unset, the step short-circuits with `success=True, skipped=True` and the rest of the suite continues — useful for environments where NIM isn't part of the operator's validation scope.

```bash
export NGC_API_KEY=nvapi-...
```

### 5. GPU image (and Docker requirement for `deploy_nim`)

`launch_instance` defaults to the public GCP Deep Learning VM Image:

| Field | Default |
|---|---|
| `--image-project` | `deeplearning-platform-release` |
| `--image-family` | `common-cu129-ubuntu-2204-nvidia-580` |

This image is published by Google, ships with the NVIDIA driver + CUDA toolkit, and works out-of-the-box for lifecycle / serial-console / RBAC / describe coverage.

**It does NOT ship Docker.** The `deploy_nim` step pulls and runs a NIM container, so it requires a Docker engine on the launched VM. Operators who need NIM coverage have two options:

1. **Bring a custom image** that has Docker + NVIDIA Container Toolkit preinstalled. The recommended path is to set the override once in your shell / `.env` so every run reuses the same pin:

    ```bash
    # Add to your .env or shell environment.
    export GCP_VM_IMAGE=<your-image-family-or-name>
    export GCP_VM_IMAGE_PROJECT=<your-gcp-project>
    ```

    The provider config reads both env vars via Jinja and falls back to the public DLVM when either is unset. For one-off runs you can also override per-invocation:

    ```bash
    uv run isvctl test run -f isvctl/configs/providers/gcp/config/vm.yaml \
        --set image_project=<your-gcp-project> \
        --set image=<your-image-family-or-name>
    ```

    Either path wires `image_project` and `image` through to `launch_instance`'s `--image-project` / `--ami-id` arguments. The image short-name resolves in the operator project first; if not found there, the resolver falls back to the default DLVM project.

2. **Skip NIM** by leaving `NGC_API_KEY` unset (see §4 above). The `deploy_nim` and `teardown_nim` steps short-circuit cleanly and every instance-lifecycle step proceeds. The run still reports `[FAIL] TEST` because of `ContainerRuntimeCheck` (see note below) — accept that as a documented limitation of the default image.

**Note on `ContainerRuntimeCheck`**: the `host_os` validator group includes `ContainerRuntimeCheck`, which asserts Docker is installed and runnable on the launched VM. It runs on every `vm` invocation regardless of `NGC_API_KEY`. On the default DLVM image this validator fails with `Docker not available`, and the run reports `[FAIL] TEST` even though every instance-lifecycle and NIM-policy-skip step passes. Only option 1 (a custom image with Docker preinstalled) produces a fully clean PASS.

Operators without an NGC entitlement should pick option 2; operators with one and no custom image can install Docker on the default image inside their own cloud-init / startup-script, but the simplest path is a custom image where `docker run --gpus all` works at boot.

### 6. Trusted SSH ingress source (`NETWORK_FIREWALL_TRUST_IP`) — required

The VM, network, and image-registry suites open an SSH (tcp/22) firewall rule so
they can reach the launched VM. There is **no open-internet default**: the only
trusted source for that ingress is the operator environment variable
`NETWORK_FIREWALL_TRUST_IP`. It is **required** — if it is unset, empty,
non-IPv4, or normalizes to `0.0.0.0/0`, `launch_instance` fails fast with an
operator error and the run exits non-zero before creating any resource.

```bash
# A single operator IP (normalizes to a /32 host rule):
export NETWORK_FIREWALL_TRUST_IP=203.0.113.4

# Or one/more IPv4 CIDRs (comma-separated):
export NETWORK_FIREWALL_TRUST_IP=203.0.113.0/24,198.51.100.0/24
```

Set it to the public egress IP/CIDR of the host running the suite (for a
cloud runner, its NAT egress range). The VM, network, and image-registry launch
firewalls set their `sourceRanges` to the normalized list, and a pre-existing
rule that allows `0.0.0.0/0` on tcp/22 is not eligible for verified-reuse.

## Operator environment variables

The GCP suite reads these operator environment variables. Set the required ones
before a `live` run — live mode is rejected when a required var is unset.

| Variable | Required? | Default / fallback | Purpose |
|----------|-----------|--------------------|---------|
| `NETWORK_FIREWALL_TRUST_IP` | **Required** (vm, network, image-registry) | none — fail closed (no fallback) | Trusted IPv4 source range(s) for SSH (tcp/22) and RDP (tcp/3389) firewall ingress. A bare IPv4 normalizes to `/32`; comma-separated IPv4 CIDRs are allowed. The suite never opens these admin ports from `0.0.0.0/0`: when this var is unset, empty, non-IPv4, or `0.0.0.0/0`, the affected step emits an operator error, sets `success=false`, and exits non-zero. The image-registry `launch_instance` step consumes it the same way as the vm / network launch firewalls. |
| `GCP_VM_IMAGE` | Optional (vm) | public DLVM family `common-cu129-ubuntu-2204-nvidia-580` | Operator image short-name or self-link for `launch_instance` (flows to `--ami-id`); resolves as exact-name, then family alias, under the image project. See [§5](#5-gpu-image-and-docker-requirement-for-deploy_nim). |
| `GCP_VM_IMAGE_PROJECT` | Optional (vm) | `deeplearning-platform-release` | Project hosting the operator image (flows to `--image-project`). When unset (and `GCP_VM_IMAGE` is also unset) the stub falls back to the public DLVM project. See [§5](#5-gpu-image-and-docker-requirement-for-deploy_nim). |
| `GCP_IAM_SKIP_TEARDOWN` | Optional (iam) | unset — teardown runs | When `true`, the IAM `teardown` step returns success without deleting the service account it created (run a standalone `--phase teardown` later to clean up). See [IAM domain](#iam-domain-service-accounts). |
| `GCP_IMAGE_REGISTRY_SKIP_TEARDOWN` | Optional (image-registry) | unset — teardown runs | When `true`, the image-registry `teardown` step returns success without deleting the in-test resources (imported image, staging bucket + disk objects, instance, SSH firewall rule, local SSH key); forwarded as `--skip-destroy`. The GCP-namespaced override of the suite's vendor-neutral `IR_SKIP_TEARDOWN`. See the [Image Registry guide](../../isvctl/configs/providers/gcp/scripts/image-registry/docs/gcp-image-registry.md). |
| `EDGE_ENDPOINTS` | Optional (security) | unset — `InsecureProtocolsCheck` structured-skips | Comma-joined `host:port` HTTPS endpoints the provider-neutral raw-socket prober checks for plain-HTTP / legacy-TLS refusal. See [Security domain](#security-domain). |
| `SEC02_MAX_TTL_SECONDS` | Optional (security) | `43200` | Upper bound (seconds) `ShortLivedCredentialsCheck` asserts observed node / workload token TTLs stay at-or-below. The default never false-fails; tighten only after a run confirms observed TTLs. |
| `GCP_KMS_KEY_ID` | Optional (security) | unset — `CustomerManagedKeyCheck` self-provisions a temporary key | Full Cloud KMS CryptoKey resource path of an existing tenant CMEK to reuse for the BYOK check instead of creating a throwaway key. |
| `OIDC_ISSUER_URL` | Optional (security) | unset — `OidcUserAuthCheck` structured-skips | OIDC issuer (Workforce Identity Federation provider or Identity Platform) the black-box prober fetches discovery + JWKS from. |
| `OIDC_AUDIENCE` | Optional (security) | unset — `OidcUserAuthCheck` structured-skips | OIDC audience the prober validates (the IAP OAuth client ID for OAuth-client flows; resource URL for SA JWTs). |
| `OIDC_TARGET_URL` | Optional (security) | unset — `OidcUserAuthCheck` structured-skips | Protected target endpoint (Cloud Run / IAP / GKE) the prober calls with each token fixture. |
| `GCP_SECURITY_ACCESS_LEVEL` | Optional (security) | unset — `least_privilege_test` structured-skips | Fully-qualified Access Context Manager access level (`accessPolicies/<id>/accessLevels/<name>`) used as the least-privilege network/source dimension (the `aws:SourceIp` analog; GCP IAM Conditions have no source-IP attribute). |
| `GCP_SECURITY_IMPERSONATION_SA` | **Required** for `ServiceAccountCredentialCheck` (security) | none — no skip path (fail or exclude) | Email of the service account `sa_credential_test` impersonates to prove keyless authentication; it also feeds the workload half of `short_lived_credentials_test` when ADC has no bound service account. The run credential must hold `roles/iam.serviceAccountTokenCreator` on it; there is no long-lived-key fallback. `ServiceAccountCredentialCheck` has **no skip path**, so leaving this unset hard-fails that check — either set it or add `ServiceAccountCredentialCheck` to `exclude.tests`. |

`GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` (§3) and `NGC_API_KEY` (§4) are also
read by the suite but are not part of the firewall / image-override contract
above. The security domain's five OIDC negative-token fixtures
(`OIDC_VALID_TOKEN`, `OIDC_WRONG_ISSUER_TOKEN`, `OIDC_WRONG_AUDIENCE_TOKEN`,
`OIDC_EXPIRED_TOKEN`, `OIDC_MISSING_REQUIRED_CLAIM_TOKEN`) are **sensitive** — keep
their values in your private `.env`, never in `.env.example`; they flow through
the `oidc_user_auth_test` step's redacted `sensitive_args` block, not a settings
read. See [Security domain](#security-domain).

```bash
# Required before any GCP live run that creates or reuses SSH/RDP firewalls
# (set to your workstation / CI egress IP — a single host normalizes to /32):
export NETWORK_FIREWALL_TRUST_IP=203.0.113.10
```

## Running GCP Validations

```bash
# Prerequisites: ADC + GOOGLE_CLOUD_PROJECT set per "Authentication" above.
# Optional: NGC_API_KEY for NIM coverage.

uv run isvctl test run -f isvctl/configs/providers/gcp/config/vm.yaml
```

The VM suite exercises 11 subtests end-to-end: launch (with GPU + cloud-init + SSH stability gate), tag verification, serial console output, console-RBAC probe (creates two short-lived probe service accounts + a second probe VM), idempotent stop / start / reboot lifecycle, describe (host OS / driver / CPU / container runtime checks), NIM deploy + inference, teardown of all created resources. Wall-clock is roughly 30–45 minutes on a clean operator environment; capacity stockout in one zone triggers a documented walk to the next zone in the preferred list.

## IAM domain (service accounts)

Google Cloud has no human IAM users, so the IAM suite is an adaptation: the
closest managed, provider-owned identity primitive is a **service account**.
The suite's `create_user` step creates a uniquely-named service account,
`test_credentials` proves a credential minted for it authenticates, and
`teardown` deletes it.

Because hardened organizations commonly block user-managed service-account
keys (`constraints/iam.disableServiceAccountKeyCreation`), the portable
**primary credential path is keyless**: `create_user` grants the ADC principal
`roles/iam.serviceAccountTokenCreator` on the new service account and mints a
short-lived (600s) OAuth2 access token via
`IAMCredentials.generateAccessToken`. No JSON key file is ever written, so
`iam.disableServiceAccountKeyCreation` does **not** need to be disabled. The
AWS-shaped output field names are preserved for contract compatibility:
`access_key_id` is the service account `unique_id` (non-secret) and
`secret_access_key` is the short-lived token (redacted from logs).

### IAM roles

The principal running the IAM suite (user or service account) needs, on the
project:

- `roles/iam.serviceAccountAdmin` — create and delete the test service account.
- `roles/iam.serviceAccountTokenCreator` — the suite grants this dynamically on
  the *new* service account, so the running principal needs the project-level
  binding that lets it set that policy and call `generateAccessToken`.

The newly-granted `tokenCreator` binding is eventually-consistent — convergence
up to ~180s has been observed on hardened orgs — so `create_user` retries the
token mint (12 × 15s) and its step timeout floor is 420s.

### Running

```bash
# Prerequisites: ADC + a resolvable project (GOOGLE_CLOUD_PROJECT or ADC).
uv run isvctl test run -f isvctl/configs/providers/gcp/config/iam.yaml
```

Set `GCP_IAM_SKIP_TEARDOWN=true` to keep the created service account after a
run. The service account is named with a per-run random suffix
(`isv-test-user-<disc>-<run>@<project>.iam.gserviceaccount.com`), so a fresh
standalone `isvctl test run --phase teardown` cannot clean it up: `create_user`
did not execute in that process, so the teardown step's
`{{steps.create_user.username}}` reference is unresolved. Instead, copy the
`username` value printed by the original `create_user` step and delete the
service account directly:

```bash
uv run python3 isvctl/configs/providers/gcp/scripts/iam/delete_user.py \
  --username <username-from-create_user-output> \
  --project=<project>   # optional; the delete uses the projects/-/ wildcard
```

## Security domain

The security suite is a validations-only domain: each step reads (or, for the
BYOK / least-privilege / tenant-isolation fixtures, briefly creates) Google Cloud
resources and prints a JSON result that a provider-agnostic validator inspects.
The [AWS reference](aws.md) is the closest implemented analog; the GCP port maps
each check onto the equivalent Google Cloud surface (Cloud KMS, IAM, GKE, Cloud
Logging, Certificate Manager, IAM Credentials, Admin SDK Directory).

Most security operator inputs are **optional** — an unset variable resolves to a
default that makes the matching check structured-skip or self-provision a
temporary resource, so a near-zero-config run still completes (with the
env-gated checks skipped rather than failed). The one exception is
`GCP_SECURITY_IMPERSONATION_SA`: `ServiceAccountCredentialCheck` has **no skip
path**, so leaving it unset hard-fails that check unless you exclude it (see the
row below):

| Variable | Effect when unset |
|----------|-------------------|
| `EDGE_ENDPOINTS` | `InsecureProtocolsCheck` structured-skips (no endpoints to probe). |
| `SEC02_MAX_TTL_SECONDS` | Defaults to `43200`; `ShortLivedCredentialsCheck` still runs. |
| `GCP_KMS_KEY_ID` | `CustomerManagedKeyCheck` creates and then cleans up a temporary CryptoKey + CMEK disk. |
| `OIDC_ISSUER_URL` / `OIDC_AUDIENCE` / `OIDC_TARGET_URL` | `OidcUserAuthCheck` structured-skips. |
| `GCP_SECURITY_ACCESS_LEVEL` | `least_privilege_test` structured-skips (drops `LeastPrivilegePolicyCheck` + `MinimalRoleEnforcementCheck`). |
| `GCP_SECURITY_IMPERSONATION_SA` | `sa_credential_test` cannot impersonate and `ServiceAccountCredentialCheck` **hard-fails** (no skip path, no long-lived-key fallback) — set the var or add `ServiceAccountCredentialCheck` to `exclude.tests`. Also feeds the workload half of `short_lived_credentials_test` when ADC has no bound SA. |

The five OIDC negative-token fixtures (`OIDC_VALID_TOKEN`,
`OIDC_WRONG_ISSUER_TOKEN`, `OIDC_WRONG_AUDIENCE_TOKEN`, `OIDC_EXPIRED_TOKEN`,
`OIDC_MISSING_REQUIRED_CLAIM_TOKEN`) supply the prober's positive + negative JWTs.
They are sensitive and read from the environment / token files via the redacted
`sensitive_args` block; keep their values in your private `.env`.

### Standing posture required by `CentralizedKmsCheck`

`CentralizedKmsCheck` (SEC09-03) has **no operator env var and no skip path**: it
inspects the project's standing Cloud KMS posture and requires **≥1 ENABLED
CryptoKey AND ≥1 CMEK-encrypted resource**, with every CMEK-referencing resource
resolving to a key (no unresolved references). A project with no Cloud KMS usage
is an honest hard fail, not a platform gap. Before a security run, pre-provision
a keyring + ENABLED key + at least one CMEK-encrypted resource (for example a
CMEK-encrypted Persistent Disk) so the check has a real posture to inspect.
(`CustomerManagedKeyCheck` self-provisions a temporary key when `GCP_KMS_KEY_ID`
is unset; `CentralizedKmsCheck` does **not** — it reads standing posture only.)

### MFA enforcement

`MfaEnforcedCheck` (SEC07-01) reads org 2-Step-Verification state via the Admin
SDK Directory API and has no skip path. The canonical run credential is typically
not a Cloud Identity / Workspace admin, so the check is excluded in this
configuration (`exclude.tests: [MfaEnforcedCheck]`). Re-enable it once a
Workspace-admin service account with domain-wide delegation and
`admin.directory.user.readonly` is provisioned.

### IAM roles

The principal running the security suite needs, on the project (consolidate via
a custom role if preferred):

- `roles/cloudkms.viewer` (+ `roles/cloudkms.admin` and
  `roles/cloudkms.cryptoKeyEncrypterDecrypter` for the BYOK / tenant-isolation
  fixtures that create keys and roundtrip encrypt/decrypt).
- `roles/iam.serviceAccountAdmin` + `roles/iam.serviceAccountTokenCreator` — the
  least-privilege, tenant-isolation, and `sa_credential_test` checks create
  scoped test service accounts and mint short-lived tokens. `sa_credential_test`
  additionally needs `roles/iam.serviceAccountTokenCreator` on
  `GCP_SECURITY_IMPERSONATION_SA`.
- `roles/iam.roleAdmin` — `least_privilege_test` creates a scoped custom role.
- `roles/compute.admin` — `customer_managed_key_test` / `tenant_isolation_test`
  create CMEK disks, VPCs, and instances; the BMC + API-isolation checks read
  networks, firewalls, routes, and GKE clusters.
- `roles/storage.admin` — the least-privilege + tenant-isolation probes create
  test buckets.
- `roles/logging.viewer` — `audit_logging_test` reads Cloud Logging audit entries
  and log-bucket retention.

### Running

```bash
# Prerequisites: ADC + a resolvable project (GOOGLE_CLOUD_PROJECT or ADC).
uv run isvctl test run -f isvctl/configs/providers/gcp/config/security.yaml
```

Set `GCP_SECURITY_SKIP_TEARDOWN=true` to keep any fixture resources after a run.
The `teardown` step proves ownership **solely** from the run id embedded in each
fixture name, so a later standalone cleanup is **not** a bare `--phase teardown`:
re-export the original run's id first, otherwise the sweep fails closed (with no
run id it can own nothing and would otherwise be a success-looking no-op that
leaves the preserved fixtures behind):

```bash
# Re-run cleanup later with the SAME run id the original run used:
RUN_ID=<original-run-id> uv run isvctl test run \
    -f isvctl/configs/providers/gcp/config/security.yaml --phase teardown
```

Each fixture step also cleans up after itself in a `finally` block; the
`teardown` step is a dual-gated safety net that only sweeps resources whose name
carries this run's id and a created-by label.

## Supported zones for L4 GPU VM tests

The VM domain's zone-walk prefers GCP zones with observed L4 capacity. The reviewed list (in priority order) is:

```text
us-central1-a / -b / -c        us-east4-a / -b / -c       us-east1-c / -d
us-west1-a / -b                us-west4-a / -b
europe-west4-a / -b            europe-west1-b / -c
asia-southeast1-a / -b / -c    asia-northeast1-a / -c     asia-east1-a / -b / -c
```

The list reflects multi-week capacity observation from spring 2026; capacity drifts, so an operator can probe a zone before a long run with:

```bash
gcloud compute accelerator-types list --filter='name=nvidia-l4 AND zone:<zone>'
```

## Org-policy considerations

GCP organizations sometimes apply policies that block specific operations the VM suite needs. Common ones to check or exempt:

- `compute.disableSerialPortAccess` — must allow. The `console_rbac` test validates that serial-console access is properly RBAC-restricted; the test itself reads serial output via IAM-mediated short-lived tokens.
- `iam.disableServiceAccountKeyCreation` — does NOT need to be disabled. The suite uses `IAMCredentials.generateAccessToken` (no key material), not service-account JSON keys.
- `compute.requireOsLogin` — must allow per-instance SSH-key metadata, which the suite uses to establish SSH for the post-launch stability gate and NIM health checks. If OS Login is enforced project-wide, the SSH gate fails; either exempt the test instances or grant the operator the OS Login roles.

## Resources

- GCP IAM permissions for Compute Engine: <https://cloud.google.com/compute/docs/access/iam>
- L4 GPU zones (current as of GCP docs): <https://cloud.google.com/compute/docs/gpus/gpu-regions-zones>
- Application Default Credentials: <https://cloud.google.com/docs/authentication/application-default-credentials>
