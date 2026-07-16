# GCP GKE Cluster — ISV Validation Guide

Operator walkthrough for the GCP **k8s** domain: provision a GKE GPU cluster,
run the NVIDIA ISV Kubernetes validation suite against it, and tear everything
down. This is the GCP analog of the [AWS EKS guide](../../../../aws/scripts/eks/docs/aws-eks.md);
the operator env-var contract the harness presence-gates lives in the tier-1
index [`docs/references/gcp.md`](../../../../../../../docs/references/gcp.md).

## Overview

The lifecycle runs in three phases, orchestrated by `isvctl`:

1. **Setup** — Terraform (`hashicorp/google` provider) provisions the primary
   `google_container_cluster` + a system (CPU) node pool + a baseline GPU node
   pool; `gcloud container clusters get-credentials` installs the kubeconfig for
   ambient `kubectl`; a two-gate GPU preflight gates inventory emission on the
   GPU driver being ready.
2. **Test** — the suite's in-cluster checks (nodes, GPU, nvidia-smi, pod health,
   CSI, NetworkPolicy, control-plane logs, conformance, workloads) run via
   ambient `kubectl`; the node-pool scale check runs here too.
3. **Teardown** — reclaim run PVC-backed disks, `terraform destroy` the cluster
   and node pools, backstop any orphaned Persistent Disk.

Lifecycle steps run as separate processes, so Terraform state is persisted on
disk (local backend, one `-state=` file per resource) and threaded across the
steps by the run-scoped names each step re-derives from `RUN_ID`.

## Prerequisites

### Required tools

```bash
gcloud --version                   # Google Cloud CLI (with a resolvable project + ADC)
gke-gcloud-auth-plugin --version   # GKE kubectl credential plugin
terraform --version                # Terraform >= 1.5
kubectl version --client
```

`gke-gcloud-auth-plugin` is a native Google Cloud CLI component, not a Python
dependency, so installing `isvctl` or running `uv sync` does not install it.
Install it using the method supported by your Google Cloud CLI distribution
(for example, `gcloud components install gke-gcloud-auth-plugin`) and follow
Google's
[GKE kubectl authentication instructions](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl#install_required_plugins).
It must be on `PATH` before a live run: setup calls
`gcloud container clusters get-credentials`, then uses `kubectl` against that
cluster.

### Authentication + project

GKE and the `google` Terraform provider both authenticate from Application
Default Credentials — the same chain the other GCP domains use. No new auth env
var is introduced.

```bash
gcloud auth application-default login          # or GOOGLE_APPLICATION_CREDENTIALS
export GOOGLE_CLOUD_PROJECT=your-project-id     # or rely on the ADC-bundled project
```

The run principal needs roughly: `roles/container.admin` (create/delete GKE
clusters + node pools), `roles/compute.admin` (the GPU-zone capacity preflight
stands up a throwaway probe MIG; the teardown backstop deletes orphaned disks),
and `roles/iam.serviceAccountUser` on the node service account.

### GPU quota

Quota for the chosen accelerator (e.g. `NVIDIA_L4_GPUS`) of at least
`gpu_node_count × accelerator_count` **in at least one candidate zone**. GKE GPU
capacity (especially L4) is zone-fragmented and stocks out intermittently — list
several candidate zones in `GCP_K8S_GPU_ZONES` (see below).

### NGC API key (NIM workloads)

```bash
export NGC_API_KEY=nvapi-XXXXX   # unset -> the NIM workloads self-skip
```

## Required run-scope id

`RUN_ID` is **required** for this domain (unlike the lighter GCP domains that
auto-generate a suffix). GKE provisions expensive GPU compute, and teardown
*re-derives* the cluster / node-pool names from `RUN_ID` to delete them — an
auto-generated value would orphan the cluster. Every lifecycle step hard-fails
with a clear "set RUN_ID" error when both `RUN_ID` and `LS_RUN_ID` are unset.

```bash
export RUN_ID=$(openssl rand -hex 4)
```

## Operator environment variables (k8s domain)

All twelve `GCP_K8S_*` settings and their required/optional status + defaults are
documented in the tier-1 index [`docs/references/gcp.md`](../../../../../../../docs/references/gcp.md#operator-environment-variables).
In short: `GCP_K8S_LOCATION`, `GCP_K8S_CPU_MACHINE_TYPE`, `GCP_K8S_GPU_MACHINE_TYPE`,
and `GCP_K8S_GPU_ACCELERATOR_TYPE` are **required** (no safe public default); the
other eight are optional overrides.

One of those optional overrides, `GCP_K8S_UNAUTHORIZED_PROBE_CMD`, activates the
outside-vantage API-network-ACL probe: set it to a shell command that reaches the
cluster's Kubernetes API from a source that *should* be blocked, and
`K8sApiNetworkAclCheck` enforces that the API refuses it. Leave it **unset** (the
default) and that check honestly structured-skips — there is no outside vantage
point to probe, so the override only ever activates the check, never weakens it.
The probe **must target the reviewed cluster's own API endpoint**: setup resolves
that endpoint from the installed kubeconfig (falling back to the GKE API) and
emits it as `steps.setup.kubernetes.api_endpoint`, which the suite binds to the
check's `api_endpoint`. The check then verifies the probe references that same
host:port and that kubectl points at it, so a probe aimed at a typo, stale, or
unrelated host is rejected instead of being misread as "ACL enforced". If the
probe is enabled but the endpoint cannot be resolved, setup **fails closed**
rather than emit an unbound probe.

Recommended shape (L4 on a `g2` machine — the released NIM/TRT-LLM GPU workloads
need an Ada-or-newer arch and ≥18Gi node memory):

```bash
export GCP_K8S_LOCATION=us-central1
export GCP_K8S_CPU_MACHINE_TYPE=e2-standard-4
export GCP_K8S_GPU_MACHINE_TYPE=g2-standard-8
export GCP_K8S_GPU_ACCELERATOR_TYPE=nvidia-l4
export GCP_K8S_GPU_ACCELERATOR_COUNT=1
# STRONGLY recommended for scarce accelerators: an ordered candidate zone list.
export GCP_K8S_GPU_ZONES=us-central1-a,us-central1-b,us-central1-c
```

## How the lifecycle executes

### GPU-zone capacity preflight (setup + create_test_gpu_node_pool)

A GKE node-pool `CREATE_NODE_POOL` operation **cannot be cancelled** and wedges
the cluster for GKE's full ~35-minute IGM retry window once it starts, so a GPU
pool is **never** created speculatively in an unprobed zone. Instead, each GPU
pool's zone is chosen by a preflight probe: for each candidate zone in order, a
throwaway **standalone size-1 Managed Instance Group** (a plain Compute MIG, not
a node pool — it deletes in seconds) mirroring the GPU shape is stood up, its
capacity signal read (`ZONE_RESOURCE_POOL_EXHAUSTED` in `list-errors` → no
capacity → next zone; instance reaching `STAGING`/`RUNNING` → capacity → use this
zone), then deleted on every exit path. The real GPU pool is created directly in
the first zone that showed capacity. A non-stockout probe failure (org policy,
quota, permission) is surfaced and fails the step — never misread as no-capacity.

### GKE-specific bridges (setup)

GKE's managed-driver GPU path installs NVIDIA drivers + the device plugin via
Google-managed DaemonSets in `kube-system` (no NVIDIA GPU Operator). To keep the
released checks honest without installing the operator, setup:

- creates a passthrough `nvidia` **RuntimeClass** (handler `runc`) so the
  released GPU-workload manifests that pin `runtimeClassName: nvidia` schedule on
  GKE's default runtime (the same runtime that already grants GPU access);
- labels GKE GPU nodes `nvidia.com/gpu.present=true` (mapping GKE's native
  `cloud.google.com/gke-accelerator` label to what the released GPU checks
  select on);
- emits `gpu_operator_namespace=kube-system` (where the managed GPU DaemonSets
  run) so the GPU-operator-namespace checks pass honestly;
- emits `driver_version` from **live nvidia-smi** (never the GKE
  `gke-gpu-driver-version` label, which is an install *mode*, not a version) —
  or leaves it unset so `K8sDriverVersionCheck` honestly skips.

The cluster is created with **Dataplane V2** (`datapath_provider =
ADVANCED_DATAPATH`) so Kubernetes NetworkPolicy is enforced natively, with
control-plane **logging** components enabled, and with **Managed Prometheus
disabled** (its `gmp-system/collector` DaemonSet does not tolerate the CPU test
pool's dedicated taint and would otherwise sit Pending and fail
`K8sNoPendingPodsCheck`). No released k8s check consumes Managed Prometheus.

### Node pools

`create_test_node_pool` (CPU, carries the dedicated `NoSchedule` taint that
`K8sNodePoolCheck` validates) and `create_test_gpu_node_pool` (GPU, untainted so
`K8sGpuPodAccessCheck` can schedule) each provision a `google_container_node_pool`
in its own state, coexisting with the baseline pools. `update_test_node_pool`
re-applies the SAME CPU pool state with a higher `node_count` (in-place scale).
Nodes are labeled `cloud.google.com/gke-nodepool=<name>` (the stable selector the
check polls) with `expected_instance_types` populated from the real
`node_config.machine_type`.

### Multi-cluster (shared VPC)

`create_test_shared_vpc_cluster` provisions a SECOND cluster on the SAME VPC
network as the primary (native on GKE) and emits the `multi_cluster` payload.
It is gated by `requires_available_validations: [K8sMultiClusterSameVpcCheck]`,
so it only runs under `ISVTEST_INCLUDE_UNRELEASED=1` (the check is unreleased).
The GKE `RUNNING` up-state is mapped to the contract sentinel `ACTIVE`.

## Subtests exercised

The full `suites/k8s.yaml` contract runs against the GKE cluster: node
count/readiness, GPU driver + nvidia-smi + capacity + pod-access + labels, GPU
Operator namespace/pods (satisfied by the managed DaemonSets in `kube-system`),
pod health / no-pending / no-error, MIG (self-skips on non-MIG GPUs), dual-stack
(auto), cluster-autoscaler (self-skips), NetworkPolicy (Dataplane V2), CSI
storage types / quota / tenant-scoping / provisioning modes (self-skip until a
StorageClass is named via `K8S_CSI_*`), OIDC issuer, conformance (`quick`),
control-plane metrics + logs (Cloud Logging), API network ACL (self-skips until
an outside-vantage probe is supplied), and the node-pool CRUD checks. GPU
workloads (NCCL, GPU-stress, NIM-1b) run when `NGC_API_KEY` is set.

**Excluded (STOPGAP, not a GKE gap):** `K8sNimHelmWorkload-3b` and
`K8sNimInferenceWorkload` — the released NIM workloads never cap `max_model_len`,
so llama-3.2-3b's 128K KV cache overflows a ≤24GB GPU (L4) and the vLLM engine
fails to start. This is an upstream NIM-workload limitation seen on every NCP,
not a GKE defect. The 1B sibling is KEPT. Remove the exclusion once the NIM
workloads cap `max_model_len`, or run on a >24GB GPU.

## Running

```bash
# Full run (setup -> test -> teardown).
uv run isvctl test run -f isvctl/configs/providers/gcp/config/k8s.yaml

# Preserve the cluster for debugging (teardown skipped).
GCP_K8S_SKIP_TEARDOWN=true uv run isvctl test run -f isvctl/configs/providers/gcp/config/k8s.yaml

# Include the unreleased multi-cluster check + its shared-VPC setup step.
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run -f isvctl/configs/providers/gcp/config/k8s.yaml
```

Wall-clock is roughly 30–60 minutes on a clean environment; a GPU-zone stockout
adds a walk through the `GCP_K8S_GPU_ZONES` candidates.

## Teardown

Teardown runs by default (even after failures). It reclaims run-created PVCs so
the `pd.csi.storage.gke.io` driver deletes their backing Persistent Disks BEFORE
the cluster is destroyed (a GKE cluster delete does NOT reclaim PVC-backed PDs —
they orphan as standalone Compute disks), then `terraform destroy`s the cluster,
then backstops any raced PD by THIS run's `goog-k8s-cluster-name` label. Node
pools are destroyed by their own steps first. `terraform init` runs
unconditionally before every destroy so a teardown-on-failure after setup bailed
early still reconciles a stale lock and no-ops cleanly.

```bash
# Standalone teardown for a previously-preserved run (re-use the SAME RUN_ID):
RUN_ID=<original-run-id> uv run isvctl test run \
  -f isvctl/configs/providers/gcp/config/k8s.yaml --phase teardown
```

## Troubleshooting

- **`RUN_ID (or LS_RUN_ID) is REQUIRED`** — set `RUN_ID` (see above); the domain
  refuses to provision GPU compute under an unscoped name teardown can't reclaim.
- **No GPU capacity in any candidate zone** — L4 capacity is zone-fragmented;
  widen `GCP_K8S_GPU_ZONES` or retry later. The error names the zones tried.
- **`RuntimeClass "nvidia" not found`** — setup creates the passthrough
  RuntimeClass; ensure setup completed before the test phase.
- **Control-plane logs empty** — `K8sControlPlaneLogsCheck` reads Cloud Logging;
  ensure the run principal can `logging.logEntries.list`. Each query is scoped
  with `--project "{{steps.setup.project}}"` — the project setup resolved through
  the full canonical chain (explicit → `GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT`
  → `google.auth.default()` ADC) — so the check queries the same project the
  cluster was provisioned in. It does **not** re-derive the project from a
  runtime `GOOGLE_CLOUD_PROJECT` env lookup, so a shell without that env still
  queries the correct project.
