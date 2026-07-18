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
| **Observability** ([guide](../../isvctl/configs/providers/gcp/scripts/observability/docs/gcp-observability.md)) | [`providers/gcp/config/observability.yaml`](../../isvctl/configs/providers/gcp/config/observability.yaml) | [`providers/gcp/scripts/observability/`](../../isvctl/configs/providers/gcp/scripts/observability/) | [`suites/observability.yaml`](../../isvctl/configs/suites/observability.yaml) |
| **Control Plane** ([guide](../../isvctl/configs/providers/gcp/scripts/control-plane/docs/gcp-control-plane.md)) | [`providers/gcp/config/control-plane.yaml`](../../isvctl/configs/providers/gcp/config/control-plane.yaml) | [`providers/gcp/scripts/control-plane/`](../../isvctl/configs/providers/gcp/scripts/control-plane/) | [`suites/control-plane.yaml`](../../isvctl/configs/suites/control-plane.yaml) |
| **K8s (GKE)** ([guide](../../isvctl/configs/providers/gcp/scripts/k8s/docs/gcp-k8s.md)) | [`providers/gcp/config/k8s.yaml`](../../isvctl/configs/providers/gcp/config/k8s.yaml) | [`providers/gcp/scripts/k8s/`](../../isvctl/configs/providers/gcp/scripts/k8s/) | [`suites/k8s.yaml`](../../isvctl/configs/suites/k8s.yaml) |

Shared GCP utilities (compute helpers, SSH wrappers, retry envelopes, error classifiers) are in [`providers/gcp/scripts/common/`](../../isvctl/configs/providers/gcp/scripts/common/).

Other domains (Bare Metal, EKS) are not yet implemented for GCP.

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
| `NETWORK_FIREWALL_TRUST_IP` | **Required** (vm, network, image-registry, observability) | none — fail closed (no fallback) | Trusted IPv4 source range(s) for SSH (tcp/22) and RDP (tcp/3389) firewall ingress. A bare IPv4 normalizes to `/32`; comma-separated IPv4 CIDRs are allowed. The suite never opens these admin ports from `0.0.0.0/0`: when this var is unset, empty, non-IPv4, or `0.0.0.0/0`, the affected step emits an operator error, sets `success=false`, and exits non-zero. The image-registry `launch_instance` and observability `launch_host` steps consume it the same way as the vm / network launch firewalls. |
| `GCP_VM_IMAGE` | Optional (vm) | public DLVM family `common-cu129-ubuntu-2204-nvidia-580` | Operator image short-name or self-link for `launch_instance` (flows to `--ami-id`); resolves as exact-name, then family alias, under the image project. See [§5](#5-gpu-image-and-docker-requirement-for-deploy_nim). |
| `GCP_VM_IMAGE_PROJECT` | Optional (vm) | `deeplearning-platform-release` | Project hosting the operator image (flows to `--image-project`). When unset (and `GCP_VM_IMAGE` is also unset) the stub falls back to the public DLVM project. See [§5](#5-gpu-image-and-docker-requirement-for-deploy_nim). |
| `GCP_IAM_SKIP_TEARDOWN` | Optional (iam) | unset — teardown runs | When `true`, the IAM `teardown` step returns success without deleting the service account it created; clean it up later with the self-contained `delete_user.py --username <username-from-create_user-output>` command (a standalone `--phase teardown` cannot resolve the per-run service-account name because `create_user` did not run in that process). See [IAM domain](#iam-domain-service-accounts). |
| `GCP_IMAGE_REGISTRY_SKIP_TEARDOWN` | Optional (image-registry) | unset — teardown runs | When `true`, the image-registry `teardown` step returns success without deleting the in-test resources (imported image, staging bucket + disk objects, instance, SSH firewall rule, local SSH key); forwarded as `--skip-destroy`. The GCP-namespaced override of the suite's vendor-neutral `IR_SKIP_TEARDOWN`. See the [Image Registry guide](../../isvctl/configs/providers/gcp/scripts/image-registry/docs/gcp-image-registry.md). |
| `EDGE_ENDPOINTS` | Optional (security) | unset — `InsecureProtocolsCheck` structured-skips | Comma-joined `host:port` HTTPS endpoints the provider-neutral raw-socket prober checks for plain-HTTP / legacy-TLS refusal. Every endpoint must also complete a modern TLS 1.2+ handshake; a closed, timed-out, or unreachable port fails rather than masquerading as secure protocol policy. See [Security domain](#security-domain). |
| `SEC02_MAX_TTL_SECONDS` | Optional (security) | `43200` | Positive integer upper bound (seconds) `ShortLivedCredentialsCheck` asserts observed node-equivalent and workload-equivalent token TTLs stay at-or-below; invalid or non-positive values fail configuration. The no-VM probe uses delegated IAM Credentials and Workload Identity Federation/STS. A non-skipped run requires both surfaces—one available surface does not substitute for the other. The default never false-fails; tighten only after a run confirms observed TTLs. |
| `GCP_KMS_KEY_ID` | Optional (security) | unset — `CustomerManagedKeyCheck` self-provisions a temporary key | Full Cloud KMS CryptoKey resource path of an existing tenant CMEK to reuse for the BYOK check instead of creating a throwaway key. |
| `OIDC_ISSUER_URL` | Optional (security) | unset — OIDC user-auth skips; SEC02 workload surface is unavailable | OIDC issuer (Workforce Identity Federation provider or Identity Platform) used for OIDC discovery/JWKS and the temporary SEC02 WIF provider. |
| `OIDC_AUDIENCE` | Optional (security) | unset — OIDC user-auth skips; SEC02 workload surface is unavailable | OIDC audience validated by the user-auth prober and allowed by the temporary SEC02 WIF provider. |
| `OIDC_TARGET_URL` | Optional (security) | unset — `OidcUserAuthCheck` structured-skips | Protected target endpoint (Cloud Run / IAP / GKE) the prober calls with each token fixture. |
| `GCP_SECURITY_ACCESS_LEVEL` | Optional (security) | unset — `least_privilege_test` structured-skips | Fully-qualified Access Context Manager access level (`accessPolicies/<id>/accessLevels/<name>`) used as the least-privilege network/source dimension (the `aws:SourceIp` analog; GCP IAM Conditions have no source-IP attribute). |
| `GCP_SECURITY_IMPERSONATION_SA` | **Required** for `ServiceAccountCredentialCheck` (security) | none — no skip path (fail or exclude) | Email of the service account `sa_credential_test` impersonates to prove keyless authentication. SEC02 also uses it as the delegated minter for the temporary node-equivalent service account. The run credential must hold `roles/iam.serviceAccountTokenCreator` on it; there is no long-lived-key fallback. `ServiceAccountCredentialCheck` has **no skip path**, so leaving this unset hard-fails that check — either set it or add `ServiceAccountCredentialCheck` to `exclude.tests`. |
| `GCP_OBSERVABILITY_SKIP_TEARDOWN` | Optional (observability) | unset — teardown runs | When `true`, the three observability teardown steps return success without deleting the in-test resources (host, VPC Flow Logs configuration, network); forwarded as `--skip-destroy`. See the [Observability guide](../../isvctl/configs/providers/gcp/scripts/observability/docs/gcp-observability.md). |
| `GCP_OBSERVABILITY_REGION` | Optional (observability) | `us-central1` | Region containing the observability subnetworks and the host's candidate zones (flows to every setup/test/teardown `--region`). |
| `GCP_OBSERVABILITY_NETWORK_CIDR` | Optional (observability) | `10.240.0.0/16` | Private aggregate CIDR for the run-scoped observability network; `create_network` carves the subnetwork range from it (flows to `create_network --cidr`). |
| `GCP_OBSERVABILITY_INSTANCE_TYPE` | Optional (observability) | `e2-standard-2` | Compute Engine machine type for the SSH syslog-probe host (flows to `launch_host --instance-type`). CPU-only; this domain needs neither Docker nor a GPU. |
| `GCP_OBSERVABILITY_IMAGE` | Optional (observability) | `ubuntu-2204-lts` | Compute Engine image short name, family alias, or self-link for the syslog-probe host (flows to `launch_host --ami-id`); resolves under the image project. |
| `GCP_OBSERVABILITY_IMAGE_PROJECT` | Optional (observability) | `ubuntu-os-cloud` | Project that owns the selected host image (flows to `launch_host --image-project`). |
| `GCP_OBSERVABILITY_SSH_USER` | Optional (observability) | `ubuntu` | Guest account used for injected-key SSH and the syslog probe (flows to `launch_host` / `host_syslogs` `--ssh-user`). |
| `GCP_K8S_LOCATION` | **Required** (k8s) | none — no safe default | GKE cluster + node-pool location (region or zone), `google_container_cluster.location`. Threaded to every lifecycle step and to `gcloud container clusters get-credentials` (flows to `--location`). |
| `GCP_K8S_CPU_MACHINE_TYPE` | **Required** (k8s) | none — no safe default | `node_config.machine_type` for the CPU node pool (the `create_test_node_pool` pool + the setup system pool); seeds `expected_instance_types_json` for `K8sNodePoolCheck` (flows to `--cpu-machine-type` / `--machine-type`). |
| `GCP_K8S_GPU_MACHINE_TYPE` | **Required** (k8s) | none — no safe default | `node_config.machine_type` for the GPU node pool. A GPU-capable machine, operator-owned (recommended `g2-standard-8` = 1 L4 + 32GB; the released NIM workloads need ≥18Gi node memory). Flows to `--gpu-machine-type` / `--machine-type`. |
| `GCP_K8S_GPU_ACCELERATOR_TYPE` | **Required** (k8s) | none — no safe default | `node_config.guest_accelerator.type` that lands GPU hardware on the GPU pool (recommended `nvidia-l4`, Ada arch; Turing `nvidia-tesla-t4` CrashLoops the NIM engine). Flows to `--gpu-accelerator-type` / `--accelerator-type`. |
| `GCP_K8S_KUBE_VERSION` | Optional (k8s) | unset — GKE REGULAR channel default | `min_master_version` / release-channel pin. Rendered as a single `--kube-version=<v>` token, or dropped when unset. |
| `GCP_K8S_CPU_NODE_COUNT` | Optional (k8s) | `1` | CPU node-pool `node_count` and the suite `expected_replicas` for `create_test_node_pool` (flows to `--node-count`). |
| `GCP_K8S_CPU_SCALE_NODE_COUNT` | Optional (k8s) | `2` | `update_test_node_pool` scale target; **must differ** from `GCP_K8S_CPU_NODE_COUNT` so the scale is observable by `K8sNodePoolCheck` (flows to `--node-count`). |
| `GCP_K8S_SYSTEM_MIN_NODES` | Optional (k8s) | `1` | Lower bound for the GKE **managed** cluster autoscaler on the CPU/system node pool (per zone); seeds the pool and enables managed autoscaling. GKE runs the autoscaler in its managed control plane, so there is no in-cluster cluster-autoscaler Deployment (flows to `--system-min-nodes`). |
| `GCP_K8S_SYSTEM_MAX_NODES` | Optional (k8s) | `3` | Upper bound for the GKE managed cluster autoscaler on the CPU/system node pool (per zone); caps managed scale-up. Must be `>=` `GCP_K8S_SYSTEM_MIN_NODES` (flows to `--system-max-nodes`). |
| `GCP_K8S_GPU_NODE_COUNT` | Optional (k8s) | `1` | GPU node-pool `node_count` for `create_test_gpu_node_pool` and the setup baseline GPU pool (flows to `--gpu-node-count` / `--node-count`). |
| `GCP_K8S_GPU_ACCELERATOR_COUNT` | Optional (k8s) | `1` | `node_config.guest_accelerator.count` (GPUs per node); feeds the exact GPU-total assertion (`total = node_count × count`) for `K8sGpuCapacityCheck` (flows to `--gpu-accelerator-count` / `--accelerator-count`). |
| `GCP_K8S_GPU_ZONES` | Optional (k8s) | the `GCP_K8S_LOCATION` zone | Ordered comma-separated candidate zones for the GPU node-pool capacity preflight. **Strongly recommended to list multiple** (e.g. `us-central1-a,us-central1-b,us-central1-c`) for scarce accelerators like L4, whose per-zone capacity is fragmented and stocks out intermittently (flows to `--gpu-node-locations`). |
| `GCP_K8S_NETWORK` | Optional (k8s) | `default` | VPC network (name or self-link) the GKE cluster attaches to and every standalone GPU capacity-preflight MIG uses. Unset or blank falls back to `default` (projects that retain the auto-created default VPC); **custom-VPC-only projects must set it** or setup fails because no `default` VPC exists. The `create_test_gpu_node_pool` step reads the primary cluster's network from Terraform state so its capacity probe never drifts to another VPC (flows to `--network`). |
| `GCP_K8S_SKIP_TEARDOWN` | Optional (k8s) | unset — teardown runs | When `true`, the `teardown` step preserves the cluster + node pools for debugging (forwarded as `--skip-destroy`); mirrors the oracle `AWS_SKIP_TEARDOWN`. See the [K8s (GKE) guide](../../isvctl/configs/providers/gcp/scripts/k8s/docs/gcp-k8s.md). |
| `GCP_K8S_AUTHORIZED_CIDRS` | Optional (k8s) | unset — control-plane public endpoint open; `K8sApiNetworkAclCheck` structured-skips | Comma-separated operator-approved CIDRs allowed to reach the GKE control-plane public endpoint. When set, setup enables GKE authorized networks (`master_authorized_networks_config`) and **rejects world-open `0.0.0.0/0` / `::/0`** (fail closed, never a silent ACL bypass); a bare IPv4 normalizes to `/32`. `K8sApiNetworkAclCheck` becomes substantive only when this **and** `GCP_K8S_UNAUTHORIZED_PROBE_CMD` are both set — the authorized list must name the runner's **actual egress CIDR**, not a documentation-only address (flows to `--authorized-cidrs`). |
| `GCP_K8S_UNAUTHORIZED_PROBE_CMD` | Optional (k8s) | unset — `K8sApiNetworkAclCheck` structured-skips | Shell command **template** that probes the cluster's Kubernetes API endpoint **from an outside vantage point that should be blocked** (e.g. SSH to a remote host that runs `curl` against the API) — not a local `curl` from the same runner that keeps authorized `kubectl` access. It **must contain the literal `{api_endpoint}`**, which setup substitutes with this run's resolved GKE API URL before emitting it into `K8sApiNetworkAclCheck.commands.unauthorized_probe`; when set, that check ENFORCES that the API refuses the blocked source. When **unset (the default)** the check has no outside vantage point and safely, honestly structured-skips — the override only ever ACTIVATES the probe, never weakens the check. GKE authorized-networks (`master_authorized_networks_config`, via `GCP_K8S_AUTHORIZED_CIDRS`) provides the capability (flows to `--unauthorized-probe-template`). |

The k8s (GKE) domain additionally **requires `RUN_ID`** (an 8-hex run-scope id;
`LS_RUN_ID` is an accepted alias): it scopes the GKE cluster / node-pool names
that teardown re-derives to delete, so every k8s lifecycle step hard-fails when
it is unset (an unscoped name teardown could not reclaim would leak expensive GPU
compute). Set `export RUN_ID=$(openssl rand -hex 4)` before a k8s run.

`GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` (§3) and `NGC_API_KEY` (§4) are also
read by the suite but are not part of the firewall / image-override contract
above. The security domain's five OIDC negative-token fixtures
(`OIDC_VALID_TOKEN`, `OIDC_WRONG_ISSUER_TOKEN`, `OIDC_WRONG_AUDIENCE_TOKEN`,
`OIDC_EXPIRED_TOKEN`, `OIDC_MISSING_REQUIRED_CLAIM_TOKEN`) are **sensitive** — keep
their values in your private `.env`, never in `.env.example`; they flow through
redacted `sensitive_args`, not settings reads. `OIDC_VALID_TOKEN` is also the
SEC02 WIF subject token; the four deliberately-invalid fixtures are used only by
`oidc_user_auth_test`. See [Security domain](#security-domain).

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
SEC02 / BYOK / least-privilege / tenant-isolation fixtures, briefly creates) Google Cloud
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
| `OIDC_ISSUER_URL` / `OIDC_AUDIENCE` | `OidcUserAuthCheck` structured-skips, and the SEC02 workload-equivalent surface is unavailable. If its node-equivalent surface runs, that partial SEC02 result fails; if both surfaces are structurally absent, the whole SEC02 check structured-skips. |
| `OIDC_TARGET_URL` | `OidcUserAuthCheck` structured-skips; SEC02 does not use this URL. |
| `OIDC_VALID_TOKEN` | The same SEC02 partial-availability rule applies because no WIF subject token can be exchanged; `OidcUserAuthCheck` also structured-skips when this positive token is absent. Once the issuer, audience, target URL, and valid token are configured, all four negative-token fixtures below are required; a missing negative token fails its corresponding probe. |
| `GCP_SECURITY_ACCESS_LEVEL` | `least_privilege_test` structured-skips (drops `LeastPrivilegePolicyCheck` + `MinimalRoleEnforcementCheck`). |
| `GCP_SECURITY_CLUSTER_NAME` | Optional; unset scans every GKE cluster in the effective API-endpoint region. A non-empty exact name that is not found hard-fails instead of becoming an empty-inventory pass. |
| `GCP_SECURITY_API_ENDPOINT_REGION` | Temporary optional override for only the read-only `api_endpoint_isolation` inventory. Unset defaults to the configured security `region`; resource-owning security steps always keep the configured region. A zero-target override run is deferred coverage, not proof. |
| `GCP_SECURITY_IMPERSONATION_SA` | `sa_credential_test` cannot impersonate and `ServiceAccountCredentialCheck` **hard-fails** (no skip path, no long-lived-key fallback) — set the var or add `ServiceAccountCredentialCheck` to `exclude.tests`. The SEC02 node-equivalent surface is also unavailable; a configured workload surface then produces a partial failed SEC02 result, while both surfaces absent allow only the SEC02 structured skip. |
| `GCP_SECURITY_SKIP_TEARDOWN` | Optional; unset or any value other than exact `true` runs normal cleanup. Exact `true` preserves fixtures for a later teardown using the same `RUN_ID`. |

The five OIDC token fixtures comprise one positive token (`OIDC_VALID_TOKEN`)
and four negative tokens (`OIDC_WRONG_ISSUER_TOKEN`,
`OIDC_WRONG_AUDIENCE_TOKEN`, `OIDC_EXPIRED_TOKEN`,
`OIDC_MISSING_REQUIRED_CLAIM_TOKEN`). They supply the prober's positive and
negative JWTs. Missing issuer, audience, target URL, or positive token causes a
structured skip. After those inputs enable the check, a missing negative token
fails its corresponding rejection probe rather than skipping the check.
They are sensitive and read from the environment / token files via redacted
`sensitive_args`; keep their values in your private `.env`. `OIDC_VALID_TOKEN`
is shared with the SEC02 WIF/STS exchange; the other four fixtures are exclusive
to the OIDC user-auth negative cases.

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

The principal running the security suite needs the following access on the test
project (consolidate the listed permissions into a custom role if preferred).
The first group is required for the corresponding non-optional checks; the
second group is needed only when that optional fixture or inventory exists.

Required when the check is enabled:

- `roles/cloudkms.viewer` — the KMS option and centralized-KMS inventories.
- `roles/compute.viewer` — BMC/API-isolation reads and centralized-KMS disk
  inventory. This role does **not** grant GKE access.
- `roles/storage.viewer` — centralized-KMS bucket inventory.
- `roles/container.clusterViewer` — `api_endpoint_isolation` lists GKE clusters
  (`container.clusters.list`). Missing access hard-fails
  `ApiEndpointIsolationCheck`; an empty readable cluster inventory is allowed.
- `roles/certificatemanager.viewer` — `cert_rotation_test` lists and reads
  Certificate Manager certificates. With the API enabled, missing access fails
  the check; a disabled API or empty readable inventory structured-skips because
  only provider-hidden control-plane certificates may exist.
- `roles/logging.viewer` — `audit_logging_test` reads Cloud Logging audit entries
  and log-bucket retention.
- `roles/iam.serviceAccountTokenCreator` on
  `GCP_SECURITY_IMPERSONATION_SA` — required by the no-skip
  `ServiceAccountCredentialCheck` and by SEC02's delegated node-equivalent
  token chain; missing access hard-fails the former and makes the latter surface
  unavailable.
- `roles/iam.serviceAccountAdmin` — SEC02 creates, marks, binds, and deletes its
  temporary node-equivalent service account. The test grants its configured
  delegated minter Token Creator on only that temporary account.
- `roles/iam.workloadIdentityPoolAdmin` — SEC02 creates and deletes a temporary
  OIDC Workload Identity pool/provider; crash recovery also lists those pools
  and providers. Providers are deleted before their parent pools.

`api_endpoint_isolation` defaults to the configured security `region`. To scope
the GKE posture portion to a dedicated fixture in a shared project, set
`GCP_SECURITY_CLUSTER_NAME` to that cluster's exact name; unset scans every
regional and zonal cluster in the effective scan region. Until a dedicated
fixture exists, `GCP_SECURITY_API_ENDPOINT_REGION` may temporarily move only
this read-only inventory away from unrelated clusters; every fixture-creating
security step remains in the configured region. This override does not affect
the independent global Private Service Connect DNS check, and a zero-target run
must be reported as deferred coverage rather than exercised endpoint proof.

Conditional access for optional coverage:

- `roles/cloudkms.admin` and
  `roles/cloudkms.cryptoKeyEncrypterDecrypter` — BYOK and tenant-isolation
  fixtures that create keys and run encrypt/decrypt. Missing fixture-create
  access makes tenant isolation structured-skip, while
  `CustomerManagedKeyCheck` has no skip path and fails.
- `roles/iam.serviceAccountAdmin` plus
  `roles/iam.serviceAccountTokenCreator` — least-privilege and tenant-isolation
  fixtures that create scoped service accounts and mint short-lived tokens.
- `roles/iam.roleAdmin` plus `roles/resourcemanager.projectIamAdmin` — required
  when `GCP_SECURITY_ACCESS_LEVEL` is set and `least_privilege_test` creates a
  scoped custom role and conditional project IAM binding (`getIamPolicy` /
  `setIamPolicy`). Missing access makes the two least-privilege validators
  structured-skip after exact cleanup.
- `roles/compute.admin` — customer-managed-key and tenant-isolation fixtures that
  create CMEK disks, VPCs, subnets, and instances.
- `roles/storage.admin` — least-privilege and tenant-isolation fixtures that
  create test buckets.
- `roles/dns.reader` — required when the project has Private Service Connect
  endpoints for Google APIs, so `api_endpoint_isolation` can list private zones
  and record sets and bind DNS evidence to each endpoint. If PSC endpoints exist
  and DNS inventory is denied, `ApiEndpointIsolationCheck` fails; with no PSC
  endpoints, this permission is not exercised.
- `roles/logging.configWriter` — allows the no-op `_Default` log-bucket
  `UpdateBucket` used to generate a real Admin Activity entry. Without it, the
  audit-entry half structured-skips; retention inspection still runs with
  `roles/logging.viewer`.

### Running

```bash
# Prerequisites: ADC + a resolvable project (GOOGLE_CLOUD_PROJECT or ADC).
uv run isvctl test run -f isvctl/configs/providers/gcp/config/security.yaml
```

Set `GCP_SECURITY_SKIP_TEARDOWN=true` to keep any fixture resources after a run.
The original `RUN_ID` supplies the mandatory name-scope ownership gate. The
independent second gate is resource-native provenance: `created-by=isvtest`
labels on CryptoKeys, disks, instances, and buckets; exact descriptions on
networks, subnetworks, service accounts, and roles; and the exact marked
role/member/condition/bucket tuple for the project IAM binding. A later
standalone cleanup is therefore **not** a bare `--phase teardown`: disable
preservation and re-export the original run's id first. Otherwise an exported
preservation flag keeps teardown disabled, or the sweep fails closed because
without a run id it can own nothing:

```bash
# Re-run cleanup later with preservation disabled and the SAME original run id:
GCP_SECURITY_SKIP_TEARDOWN=false RUN_ID=<original-run-id> uv run isvctl test run \
    -f isvctl/configs/providers/gcp/config/security.yaml --phase teardown
```

Each fixture step also cleans up after itself in a `finally` block; the
`teardown` step is a dual-gated safety net that only sweeps resources whose name
carries this run's id and whose resource-native provenance matches the exact
marker written by the corresponding create step.

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
