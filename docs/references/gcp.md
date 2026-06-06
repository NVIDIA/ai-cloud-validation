# GCP Target Implementation

The GCP implementation is a target-platform port of the ISV validation framework. The [AWS reference](aws.md) is the canonical example for how the suite expects each provider to behave; the GCP scripts under `providers/gcp/` translate that contract onto Google Cloud's API surface (Compute Engine, IAM, etc.).

This page covers the operator setup needed to run `isvctl` tests against GCP.

## Available Modules

| Domain | Config | Scripts | Test Suite |
|--------|--------|---------|------------|
| **VM** | [`providers/gcp/config/vm.yaml`](../../isvctl/configs/providers/gcp/config/vm.yaml) | [`providers/gcp/scripts/vm/`](../../isvctl/configs/providers/gcp/scripts/vm/) | [`suites/vm.yaml`](../../isvctl/configs/suites/vm.yaml) |
| **Network** | [`providers/gcp/config/network.yaml`](../../isvctl/configs/providers/gcp/config/network.yaml) | [`providers/gcp/scripts/network/`](../../isvctl/configs/providers/gcp/scripts/network/) | [`suites/network.yaml`](../../isvctl/configs/suites/network.yaml) |
| **IAM** | [`providers/gcp/config/iam.yaml`](../../isvctl/configs/providers/gcp/config/iam.yaml) | [`providers/gcp/scripts/iam/`](../../isvctl/configs/providers/gcp/scripts/iam/) | [`suites/iam.yaml`](../../isvctl/configs/suites/iam.yaml) |

Shared GCP utilities (compute helpers, SSH wrappers, retry envelopes, error classifiers) are in [`providers/gcp/scripts/common/`](../../isvctl/configs/providers/gcp/scripts/common/).

Other domains (Bare Metal, EKS, Control Plane, Image Registry, Security) are not yet implemented for GCP.

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

The VM suite opens an SSH (tcp/22) firewall rule so it can reach the launched
probe VM. There is **no open-internet default**: the only trusted source for
that ingress is the operator environment variable `NETWORK_FIREWALL_TRUST_IP`.
It is **required** — if it is unset, empty, non-IPv4, or normalizes to
`0.0.0.0/0`, `launch_instance` fails fast with an operator error and the run
exits non-zero before creating any resource.

```bash
# A single operator IP (normalizes to a /32 host rule):
export NETWORK_FIREWALL_TRUST_IP=203.0.113.4

# Or one/more IPv4 CIDRs (comma-separated):
export NETWORK_FIREWALL_TRUST_IP=203.0.113.0/24,198.51.100.0/24
```

Set it to the public egress IP/CIDR of the host running the suite (for a
cloud runner, its NAT egress range). The VM suite's launch firewall sets its
`sourceRanges` to the normalized list, and a pre-existing rule that allows
`0.0.0.0/0` on tcp/22 is not eligible for verified-reuse.

## Operator environment variables

The GCP suite reads these operator environment variables. Set the required ones
before a `live` run — live mode is rejected when a required var is unset.

| Variable | Required? | Default / fallback | Purpose |
|----------|-----------|--------------------|---------|
| `NETWORK_FIREWALL_TRUST_IP` | **Required** (vm, network) | none — fail closed (no fallback) | Trusted IPv4 source range(s) for SSH (tcp/22) and RDP (tcp/3389) firewall ingress. A bare IPv4 normalizes to `/32`; comma-separated IPv4 CIDRs are allowed. The suite never opens these admin ports from `0.0.0.0/0`: when this var is unset, empty, non-IPv4, or `0.0.0.0/0`, the affected step emits an operator error, sets `success=false`, and exits non-zero. |
| `GCP_VM_IMAGE` | Optional (vm) | public DLVM family `common-cu129-ubuntu-2204-nvidia-580` | Operator image short-name or self-link for `launch_instance` (flows to `--ami-id`); resolves as exact-name, then family alias, under the image project. See [§5](#5-gpu-image-and-docker-requirement-for-deploy_nim). |
| `GCP_VM_IMAGE_PROJECT` | Optional (vm) | `deeplearning-platform-release` | Project hosting the operator image (flows to `--image-project`). When unset (and `GCP_VM_IMAGE` is also unset) the stub falls back to the public DLVM project. See [§5](#5-gpu-image-and-docker-requirement-for-deploy_nim). |
| `GCP_IAM_SKIP_TEARDOWN` | Optional (iam) | unset — teardown runs | When `true`, the IAM `teardown` step returns success without deleting the service account it created (run a standalone `--phase teardown` later to clean up). See [IAM domain](#iam-domain-service-accounts). |

`GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` (§3) and `NGC_API_KEY` (§4) are also
read by the suite but are not part of the firewall / image-override contract
above.

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
run (clean up later with `--phase teardown`).

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
