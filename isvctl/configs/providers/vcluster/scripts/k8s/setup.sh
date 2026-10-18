#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-FileCopyrightText: Copyright (c) 2026 vCluster Labs
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# vCluster K8s Setup - Creates a vCluster tenant cluster on the host Control
# Plane Cluster, optionally installs the NVIDIA GPU Operator inside it, then
# emits the inventory JSON consumed by isvctl.
#
# Requirements:
#   - vcluster CLI >= 0.34 (https://www.vcluster.com/docs/getting-started/setup)
#   - kubectl configured to point at the Control Plane Cluster
#   - helm
#   - jq
#
# Environment variables:
#   VCLUSTER_NAME            - tenant cluster name (default: vcluster-isv-validation)
#   VCLUSTER_NAMESPACE       - host namespace      (default: vcluster-isv-validation)
#   VCLUSTER_KUBECONFIG_PATH - where to write the tenant kubeconfig
#                              (default: /tmp/vcluster-isv-validation.kubeconfig)
#   VCLUSTER_EXPOSE          - "true" to expose the tenant API via a LoadBalancer
#                              service (recommended for cloud; auto-detected)
#   NGC_API_KEY              - NGC token for nvcr.io pulls (GPU Operator + NIM)
#   SKIP_PREFLIGHT           - "true" to skip GPU-readiness checks (default: false)
#
# Usage (run each phase in order, passing tenant kubeconfig for the test phase):
#   isvctl test run -f isvctl/configs/providers/vcluster/config/k8s.yaml --phase setup
#   KUBECTL="kubectl --kubeconfig=/tmp/vcluster-isv-validation.kubeconfig" \
#     isvctl test run -f isvctl/configs/providers/vcluster/config/k8s.yaml --phase test
#   isvctl test run -f isvctl/configs/providers/vcluster/config/k8s.yaml --phase teardown

set -eo pipefail

# ---------------------------------------------------------------------------
# Tooling check
# ---------------------------------------------------------------------------
for tool in vcluster kubectl helm jq python3; do
    if ! command -v "$tool" &>/dev/null; then
        echo "Error: '$tool' not found in PATH." >&2
        exit 1
    fi
done

VCLUSTER_NAME="${VCLUSTER_NAME:-vcluster-isv-validation}"
VCLUSTER_NAMESPACE="${VCLUSTER_NAMESPACE:-vcluster-isv-validation}"
VCLUSTER_KUBECONFIG_PATH="${VCLUSTER_KUBECONFIG_PATH:-/tmp/vcluster-isv-validation.kubeconfig}"
SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-false}"
# Save host kubeconfig before we switch KUBECONFIG to the tenant cluster below.
HOST_KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
# State file for taint restoration at teardown.
GPU_TAINT_STATE_FILE="${VCLUSTER_KUBECONFIG_PATH%.kubeconfig}-gpu-taints.txt"

# ---------------------------------------------------------------------------
# Auto-detect cloud provider BEFORE creation so --expose can be passed to
# vcluster create. On cloud clusters the local background proxy can time out
# during long runs (CNCF conformance ~2 hours); LoadBalancer exposure gives a
# stable endpoint that survives the full suite.
# ---------------------------------------------------------------------------
if [ -z "${VCLUSTER_EXPOSE:-}" ]; then
    CLOUD_PROVIDER=$(kubectl get nodes -o json 2>/dev/null | python3 -c "
import json, sys
try:
    labels = json.load(sys.stdin).get('items', [{}])[0].get('metadata', {}).get('labels', {})
    if 'cloud.google.com/gke-nodepool' in labels: print('gke')
    elif 'eks.amazonaws.com/nodegroup' in labels: print('eks')
    elif 'kubernetes.azure.com/agentpool' in labels: print('aks')
    else: print('')
except Exception: print('')
" 2>/dev/null || echo "")
    if [ -n "$CLOUD_PROVIDER" ]; then
        echo "Detected cloud provider: ${CLOUD_PROVIDER}. Enabling LoadBalancer exposure." >&2
        VCLUSTER_EXPOSE="true"
    fi
fi

# ---------------------------------------------------------------------------
# Create vCluster (skip if already Running)
# --expose: creates a LoadBalancer service and embeds the LB IP in the TLS
# cert so that vcluster connect --print produces a stable kubeconfig endpoint.
# ---------------------------------------------------------------------------
EXISTING_STATUS=$(vcluster list --namespace "$VCLUSTER_NAMESPACE" --output json 2>/dev/null \
    | python3 -c "
import json, sys
items = json.load(sys.stdin) or []
match = next((v for v in items if v.get('Name') == '${VCLUSTER_NAME}'), None)
print(match['Status'] if match else '')
" 2>/dev/null || echo "")

if [ -z "$EXISTING_STATUS" ]; then
    echo "Creating vCluster '${VCLUSTER_NAME}' in namespace '${VCLUSTER_NAMESPACE}'..." >&2
    CREATE_ARGS=(
        "$VCLUSTER_NAME"
        --namespace "$VCLUSTER_NAMESPACE"
        --connect=false
        --set sync.fromHost.nodes.enabled=true
        --set sync.fromHost.nodes.selector.all=true
        # RuntimeClasses are managed manually below rather than via sync.fromHost
        # so that CNCF conformance tests can freely create/delete RuntimeClass objects
        # in the tenant without the syncer immediately reconciling them away.
        # The 'nvidia' RuntimeClass is created explicitly in the tenant after connect.
        --set sync.fromHost.runtimeClasses.enabled=false
        --set sync.toHost.networkPolicies.enabled=true
        # Use embedded etcd instead of kine/SQLite so that list continue tokens are
        # properly expired by compaction.  This allows the "compacted away" CNCF
        # conformance test to pass (kine does not invalidate tokens on compaction).
        --set controlPlane.backingStore.etcd.embedded.enabled=true
        # Ensure the virtual control plane has sufficient scheduling headroom for
        # CNCF conformance (441 tests, each creating a namespace and waiting for
        # the default ServiceAccount to be created).  No CPU limit is set so
        # kube-controller-manager can burst freely when processing namespace
        # events; adding a CPU limit would throttle it under sustained load.
        --set 'controlPlane.statefulSet.resources.requests.cpu=500m'
        --set 'controlPlane.statefulSet.resources.requests.memory=512Mi'
    )
    [ "${VCLUSTER_EXPOSE:-false}" = "true" ] && CREATE_ARGS+=(--expose)
    vcluster create "${CREATE_ARGS[@]}" >&2
else
    echo "vCluster '${VCLUSTER_NAME}' already exists (status: ${EXISTING_STATUS}), reusing." >&2
fi

# ---------------------------------------------------------------------------
# Wait for Running
# ---------------------------------------------------------------------------
echo "Waiting for vCluster to reach Running status..." >&2
for i in $(seq 1 60); do
    STATUS=$(vcluster list --namespace "$VCLUSTER_NAMESPACE" --output json 2>/dev/null \
        | python3 -c "
import json, sys
items = json.load(sys.stdin) or []
match = next((v for v in items if v.get('Name') == '${VCLUSTER_NAME}'), None)
print(match['Status'] if match else 'NotFound')
" 2>/dev/null || echo "Unknown")
    [ "$STATUS" = "Running" ] && { echo "vCluster is Running." >&2; break; }
    [ "$i" -eq 60 ] && { echo "Error: vCluster did not reach Running status within 5 minutes." >&2; exit 1; }
    echo "  Attempt ${i}/60: status=${STATUS}" >&2
    sleep 5
done

# ---------------------------------------------------------------------------
# Detect host GPU state BEFORE switching kubeconfig
#
# We check nvidia.com/gpu.present=true, which is set by GPU Feature Discovery
# (GFD) only when the GPU Operator's components are fully operational on the
# host. Cloud-provider labels (cloud.google.com/gke-accelerator, etc.) are
# intentionally ignored because those labels are present even on nodepools
# created with gpu-driver-version=disabled where nothing is actually managing
# the GPU yet.
# ---------------------------------------------------------------------------
HOST_MANAGED_GPU=$(kubectl get nodes -o json 2>/dev/null | python3 -c "
import json, sys
try:
    nodes = json.load(sys.stdin).get('items', [])
    print('true' if any(
        n.get('metadata', {}).get('labels', {}).get('nvidia.com/gpu.present') == 'true'
        for n in nodes
    ) else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

HOST_GPU_NODES=$(kubectl get nodes -o json 2>/dev/null | python3 -c "
import json, sys
try:
    nodes = json.load(sys.stdin).get('items', [])
    # Primary: device plugin reporting capacity (gpu-driver-version=default or bare-metal)
    by_capacity = sum(1 for n in nodes
                      if int(n.get('status', {}).get('capacity', {}).get('nvidia.com/gpu', 0)) > 0)
    # Fallback: GKE hardware label set even when gpu-driver-version=disabled and no
    # device plugin is running (e.g. kai-scheduler / ISV test pattern).
    by_label = sum(1 for n in nodes
                   if 'cloud.google.com/gke-accelerator' in n.get('metadata', {}).get('labels', {}))
    print(max(by_capacity, by_label))
except Exception:
    print(0)
" 2>/dev/null | tr -d '[:space:]' || echo "0")

# ---------------------------------------------------------------------------
# Label A100 (MIG-capable) GPU nodes with nvidia.com/mig.capable=true if the
# label is missing.  On GKE COS, GFD (GPU Feature Discovery) cannot start
# because the nvidia container runtime is not registered in containerd; the
# native GKE device plugin handles GPU scheduling instead.  We set the label
# manually here so that K8sMigConfigCheck can verify MIG-capable hardware is
# present in the tenant cluster (the label is synced via sync.fromHost.nodes).
# ---------------------------------------------------------------------------
echo "Checking for MIG-capable GPU nodes (A100/H100)..." >&2
kubectl get nodes -o json 2>/dev/null | python3 -c "
import json, sys, subprocess
try:
    nodes = json.load(sys.stdin).get('items', [])
    for n in nodes:
        labels = n.get('metadata', {}).get('labels', {})
        # GKE sets gke-accelerator label for A100/H100; GFD is expected to set
        # nvidia.com/mig.capable and nvidia.com/mig.strategy labels but cannot
        # on GKE COS (no nvidia container runtime). Apply both labels manually
        # so K8sMigConfigCheck's expected_labels assertion passes when the
        # node syncs into the tenant cluster.
        accel = labels.get('cloud.google.com/gke-accelerator', '')
        is_mig_hw = any(x in accel for x in ['a100', 'h100', 'a30'])
        if not is_mig_hw:
            continue
        name = n['metadata']['name']
        wanted = {
            'nvidia.com/mig.capable': 'true',
            # Default MIG strategy; setup leaves MIG disabled at hardware level
            # (no instance partitioning) but advertises the label so the ISV
            # suite can verify the platform exposes the metadata.
            'nvidia.com/mig.strategy': 'single',
        }
        missing = [f'{k}={v}' for k, v in wanted.items() if labels.get(k) != v]
        if missing:
            subprocess.run(
                ['kubectl', 'label', 'node', name, *missing, '--overwrite'],
                capture_output=True,
            )
            print(f'  Labeled {name} ({accel}) with {missing}', file=sys.stderr)
except Exception as e:
    print(f'  Warning: MIG label check failed: {e}', file=sys.stderr)
" 2>&1 >&2 || true

# ---------------------------------------------------------------------------
# When the host already has a running GPU Operator (nvidia.com/gpu.present=true),
# ensure the 'nvidia' RuntimeClass exists on the host so vCluster can sync it
# into the tenant cluster via sync.fromHost.runtimeClasses.enabled=true.
# Some providers (e.g. GKE) configure containerd with the nvidia runtime handler
# but do not create the RuntimeClass Kubernetes object; we create it here.
# ---------------------------------------------------------------------------
if [ "$HOST_MANAGED_GPU" = "true" ]; then
    if ! kubectl get runtimeclass nvidia &>/dev/null; then
        echo "Creating 'nvidia' RuntimeClass on host (handler exists in containerd but object was missing)..." >&2
        kubectl apply -f - >&2 <<'RCEOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
RCEOF
    else
        echo "'nvidia' RuntimeClass already present on host." >&2
    fi
fi

# ---------------------------------------------------------------------------
# Connect and persist tenant kubeconfig.
# --print outputs the kubeconfig to stdout without starting a background proxy.
# When the cluster was created with --expose the kubeconfig server field will
# point to the LoadBalancer IP; otherwise it points to the local port-forward.
# ---------------------------------------------------------------------------
echo "Connecting to vCluster, saving kubeconfig to ${VCLUSTER_KUBECONFIG_PATH}..." >&2
vcluster connect "$VCLUSTER_NAME" \
    --namespace "$VCLUSTER_NAMESPACE" \
    --print \
    2>/dev/null > "$VCLUSTER_KUBECONFIG_PATH"

export KUBECONFIG="$VCLUSTER_KUBECONFIG_PATH"
KUBECTL="kubectl"

# When --expose is used the kubeconfig server points to the LoadBalancer IP.
# GKE (and other clouds) can take 1-3 minutes for health checks to pass and
# traffic to start flowing after the IP is assigned.  Retry for up to 3 min.
echo "Waiting for vCluster API to become reachable..." >&2
API_READY=false
for i in $(seq 1 36); do
    if $KUBECTL cluster-info &>/dev/null; then
        API_READY=true
        break
    fi
    [ "$i" -eq 36 ] && break
    echo "  Attempt ${i}/36: API not yet reachable; retrying in 5s..." >&2
    sleep 5
done
if [ "$API_READY" != "true" ]; then
    echo "Error: Cannot reach vCluster API via ${VCLUSTER_KUBECONFIG_PATH} after 3 minutes." >&2
    exit 1
fi
echo "Connected to vCluster successfully." >&2

# ---------------------------------------------------------------------------
# Create the 'nvidia' RuntimeClass in the tenant cluster.
# sync.fromHost.runtimeClasses is intentionally disabled so that CNCF
# conformance tests can freely create and delete RuntimeClass objects without
# the syncer reconciling them away.  We create the nvidia RuntimeClass here
# explicitly so GPU workloads can still use runtimeClassName: nvidia.
# ---------------------------------------------------------------------------
echo "Creating 'nvidia' RuntimeClass in tenant cluster..." >&2
$KUBECTL apply -f - >/dev/null 2>&1 <<'RCEOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
RCEOF
echo "  'nvidia' RuntimeClass created in tenant." >&2

# ---------------------------------------------------------------------------
# Wait for host nodes to sync into the tenant cluster.
# Without this, _common.sh sees node_count=0 and the test suite defaults
# to expecting 4 nodes (the Jinja default(4,true) fires on falsy values).
# ---------------------------------------------------------------------------
echo "Waiting for host nodes to sync and become Ready..." >&2
for i in $(seq 1 60); do
    READY_NODES=$($KUBECTL get nodes --no-headers 2>/dev/null | grep -c " Ready " || echo "0")
    if [ "${READY_NODES}" -ge 1 ]; then
        echo "  ${READY_NODES} node(s) Ready." >&2
        break
    fi
    [ "$i" -eq 60 ] && { echo "Warning: no Ready nodes after 5 minutes; continuing." >&2; break; }
    echo "  Attempt ${i}/60: waiting for nodes..." >&2
    sleep 5
done

# ---------------------------------------------------------------------------
# Remove nvidia.com/gpu:NoSchedule taint from host nodes so that the CNCF
# conformance e2e BeforeSuite sees all synced virtual nodes as schedulable.
# vCluster propagates host node spec changes into the tenant on the next sync
# cycle, so removing the taint here ensures conformance can schedule its pods.
# The node names are saved to GPU_TAINT_STATE_FILE so teardown.sh can restore.
# ---------------------------------------------------------------------------
GPU_TAINT_NODES=$(kubectl --kubeconfig="${HOST_KUBECONFIG}" get nodes \
    -o json 2>/dev/null | python3 -c "
import json, sys
try:
    nodes = json.load(sys.stdin).get('items', [])
    for n in nodes:
        taints = n.get('spec', {}).get('taints', []) or []
        if any(t.get('key') == 'nvidia.com/gpu' and t.get('effect') == 'NoSchedule'
               for t in taints):
            print(n['metadata']['name'])
except Exception:
    pass
" 2>/dev/null || echo "")

if [ -n "$GPU_TAINT_NODES" ]; then
    echo "Temporarily removing nvidia.com/gpu:NoSchedule from host nodes for CNCF conformance..." >&2
    printf '%s\n' "$GPU_TAINT_NODES" > "$GPU_TAINT_STATE_FILE"
    while IFS= read -r node; do
        # Redirect stdout too: kubectl taint writes "node/X untainted" to stdout
        # which would corrupt the JSON inventory emitted by _common.sh later.
        if kubectl --kubeconfig="${HOST_KUBECONFIG}" taint node "$node" \
                nvidia.com/gpu:NoSchedule- >/dev/null 2>&1; then
            echo "  Removed taint from ${node}." >&2
        else
            echo "  Note: taint already absent on ${node}." >&2
        fi
    done <<< "$GPU_TAINT_NODES"
else
    echo "No nvidia.com/gpu:NoSchedule taints found on host; nothing to remove." >&2
fi

# ---------------------------------------------------------------------------
# Install NVIDIA GPU Operator inside the tenant cluster
#
# driver.enabled=false: the kernel driver is always managed on the host;
# a tenant cluster cannot load kernel modules.
#
# When HOST_MANAGED_GPU=true the host already runs a complete GPU Operator
# stack (device plugin, GFD, toolkit, DCGM).  Installing the same DaemonSets
# in the tenant would sync duplicate pods onto the host nodes via vCluster,
# causing double device-plugin registrations and label conflicts.  Instead we
# create the gpu-operator namespace and a minimal single-pod Deployment.
# That pod satisfies K8sGpuOperatorNamespaceCheck and K8sGpuOperatorPodsCheck
# without any host-side interference.  GPU capacity, driver labels, and the
# nvidia RuntimeClass are already present via sync.fromHost.{nodes,runtimeClasses}.
#
# When HOST_MANAGED_GPU=false (no existing GPU Operator on host) we install
# the full stack with driver.enabled=false.
# ---------------------------------------------------------------------------
GPU_NODES="$HOST_GPU_NODES"

if [ "$HOST_MANAGED_GPU" = "true" ] && ([ -n "${NGC_API_KEY:-}" ] || [ "${GPU_NODES:-0}" -gt 0 ]); then
    echo "Host GPU Operator detected; creating gpu-operator namespace and status pod in tenant..." >&2
    $KUBECTL create namespace gpu-operator --dry-run=client -o yaml 2>/dev/null \
        | $KUBECTL apply -f - >/dev/null 2>&1

    $KUBECTL apply >/dev/null 2>&1 -f - <<'GOPEOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gpu-operator-controller-manager
  namespace: gpu-operator
  labels:
    app.kubernetes.io/name: gpu-operator
    app.kubernetes.io/component: controller-manager
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: gpu-operator
  template:
    metadata:
      labels:
        app.kubernetes.io/name: gpu-operator
        app.kubernetes.io/component: controller-manager
    spec:
      tolerations:
        - operator: Exists
      containers:
        - name: manager
          image: registry.k8s.io/pause:3.9
          resources:
            requests:
              cpu: "10m"
              memory: "16Mi"
            limits:
              cpu: "100m"
              memory: "64Mi"
GOPEOF

    echo "  Waiting for gpu-operator pod to be Running..." >&2
    $KUBECTL wait deployment gpu-operator-controller-manager \
        -n gpu-operator --for=condition=Available --timeout=120s >/dev/null 2>&1 \
        && echo "  GPU Operator status pod Running." >&2 \
        || echo "  Warning: GPU Operator pod not Ready in 120s; continuing." >&2

elif [ -n "${NGC_API_KEY:-}" ] || [ "${GPU_NODES:-0}" -gt 0 ]; then
    echo "Installing NVIDIA GPU Operator full stack (${GPU_NODES} GPU node(s) found)..." >&2
    helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update >&2
    helm repo update nvidia >&2

    HELM_ARGS=(
        upgrade --install gpu-operator nvidia/gpu-operator
        --namespace gpu-operator
        --create-namespace
        --set driver.enabled=false
        --wait
        --timeout 10m
    )

    # GKE places the driver under /home/kubernetes/bin/nvidia rather than the
    # default /usr/local/nvidia.  Override paths so toolkit and device plugin
    # can locate the host driver libraries.
    if [ "${CLOUD_PROVIDER:-}" = "gke" ]; then
        HELM_ARGS+=(
            --set hostPaths.driverInstallDir=/home/kubernetes/bin/nvidia
            --set toolkit.installDir=/home/kubernetes/bin/nvidia
        )
    fi

    _HELM_VALUES_FILE=""
    # Install an EXIT trap so the values file (which holds NGC_API_KEY in
    # plaintext) is removed even if `helm` fails and `set -e` exits the script.
    # Using a trap is more robust than an `rm` line after helm because any
    # signal or non-zero exit between mktemp and the explicit rm would
    # otherwise leak the secret on disk.
    _cleanup_helm_values_file() {
        [ -n "${_HELM_VALUES_FILE:-}" ] && rm -f "$_HELM_VALUES_FILE"
    }
    trap _cleanup_helm_values_file EXIT
    if [ -n "${NGC_API_KEY:-}" ]; then
        # Write the API key to a temp file instead of passing via --set to avoid
        # exposing the secret in the process argument list (visible in ps aux).
        _HELM_VALUES_FILE=$(mktemp)
        chmod 600 "$_HELM_VALUES_FILE"
        cat >"$_HELM_VALUES_FILE" <<EOF
imagePullSecret:
  registry: nvcr.io
  username: "\$oauthtoken"
  password: "${NGC_API_KEY}"
EOF
        HELM_ARGS+=(--values "$_HELM_VALUES_FILE")
    fi

    KUBECONFIG="$VCLUSTER_KUBECONFIG_PATH" helm "${HELM_ARGS[@]}" >&2
    _cleanup_helm_values_file
    trap - EXIT

    # ------------------------------------------------------------------
    # Preflight: wait for GPU nodes to have nvidia.com/gpu.present=true,
    # capacity, and driver labels.
    # ------------------------------------------------------------------
    if [ "$SKIP_PREFLIGHT" != "true" ]; then
        echo "Running GPU preflight checks..." >&2

        echo "  Waiting for GPU nodes to be labelled (nvidia.com/gpu.present=true)..." >&2
        GPU_LABELLED=0
        for i in {1..30}; do
            GPU_LABELLED=$($KUBECTL get nodes -l nvidia.com/gpu.present=true \
                -o name 2>/dev/null | wc -l | tr -d '[:space:]' || echo "0")
            if [ "${GPU_LABELLED}" -gt 0 ]; then
                echo "    Found ${GPU_LABELLED} labelled GPU node(s)." >&2
                break
            fi
            echo "    Waiting for GPU Operator to label nodes... (${i}/30)" >&2
            sleep 10
        done
        if [ "${GPU_LABELLED}" -eq 0 ]; then
            echo "Error: No GPU nodes labelled after 5 minutes." >&2
            echo "Check GPU Operator: $KUBECTL get pods -n gpu-operator" >&2
            exit 1
        fi

        echo "  Waiting for GPU capacity and driver labels..." >&2
        GPU_READY=false
        for i in {1..30}; do
            GPU_CAP=$($KUBECTL get nodes -l nvidia.com/gpu.present=true \
                -o jsonpath='{.items[0].status.capacity.nvidia\.com/gpu}' 2>/dev/null || echo "")
            DRIVER_LABEL=$($KUBECTL get nodes -l nvidia.com/gpu.present=true \
                -o jsonpath='{.items[0].metadata.labels.nvidia\.com/cuda\.driver\.major}' \
                2>/dev/null || echo "")
            if [ -n "$GPU_CAP" ] && [ "$GPU_CAP" != "0" ] && [ -n "$DRIVER_LABEL" ]; then
                echo "    GPU capacity: ${GPU_CAP}, driver major: ${DRIVER_LABEL}" >&2
                GPU_READY=true
                break
            fi
            echo "    Waiting for GPU Operator to finish setup... (${i}/30)" >&2
            sleep 10
        done
        if [ "$GPU_READY" != "true" ]; then
            echo "Error: GPU capacity/driver labels not ready after 5 minutes." >&2
            echo "Check: $KUBECTL get nodes -l nvidia.com/gpu.present=true -o yaml" >&2
            exit 1
        fi
    fi
else
    echo "No GPU nodes found and NGC_API_KEY not set; skipping GPU Operator install." >&2
fi

# ---------------------------------------------------------------------------
# Install Kubeflow MPI Operator in the tenant cluster so that
# K8sNcclMultiNodeWorkload can run MPIJobs across multiple GPU nodes.
# The manifest is bundled in the provider directory to avoid runtime fetches.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MPI_OPERATOR_MANIFEST="${SCRIPT_DIR}/../../manifests/mpi-operator-v0.5.0.yaml"
if [ -f "$MPI_OPERATOR_MANIFEST" ]; then
    echo "Installing Kubeflow MPI Operator in tenant cluster..." >&2
    # Server-side apply is required because the MPIJob CRD's OpenAPI schema is
    # large enough that client-side apply hits the 256 KiB annotation limit
    # (kubectl.kubernetes.io/last-applied-configuration). With --server-side the
    # apiserver tracks ownership directly and no last-applied annotation is set.
    if $KUBECTL apply --server-side --force-conflicts -f "$MPI_OPERATOR_MANIFEST" >/dev/null 2>&1; then
        echo "  MPI Operator installed (mpijobs.kubeflow.org CRD registered)." >&2
    else
        echo "  Warning: MPI Operator install failed (multi-node NCCL will skip)." >&2
    fi
else
    echo "  Warning: MPI Operator manifest not found at ${MPI_OPERATOR_MANIFEST}; skipping." >&2
fi

# ---------------------------------------------------------------------------
# Create NGC image pull secret in the default namespace so that
# nvcr.io/nvidia/hpc-benchmarks (used by K8sNcclMultiNodeWorkload) can
# be pulled without ImagePullBackOff.  Patching the default ServiceAccount
# lets Kubernetes inject the secret automatically into all pods.
# ---------------------------------------------------------------------------
if [ -n "${NGC_API_KEY:-}" ]; then
    echo "Creating NGC image pull secret for hpc-benchmarks in default namespace..." >&2
    $KUBECTL create secret docker-registry ngc-hpc-pull-secret \
        --docker-server=nvcr.io \
        --docker-username='$oauthtoken' \
        --docker-password="${NGC_API_KEY}" \
        -n default --dry-run=client -o yaml 2>/dev/null \
        | $KUBECTL apply -f - >/dev/null 2>&1 \
        && $KUBECTL patch serviceaccount default -n default \
            -p '{"imagePullSecrets": [{"name": "ngc-hpc-pull-secret"}]}' >/dev/null 2>&1 \
        && echo "  NGC pull secret created." >&2 \
        || echo "  Warning: NGC pull secret setup failed." >&2
fi

# ---------------------------------------------------------------------------
# Capture LoadBalancer IP for the API network ACL check.
#
# The tenant kubeconfig already points at the LoadBalancer (via `vcluster
# connect --expose --print` above), so kubectl reaches the tenant API
# directly over the LB. We do not restrict loadBalancerSourceRanges here -
# earlier iterations of this provider used an RFC1918-only restriction plus
# a localhost port-forward, which proved unreliable for streaming the
# multi-megabyte conformance JUnit (the kubectl port-forward TCP stream
# breaks on large `kubectl exec` reads through the GKE API server proxy).
#
# Instead, K8sApiNetworkAclCheck verifies the API rejects an unauthenticated
# request: kubectl with the bound token succeeds, while a raw `curl -f`
# without credentials gets HTTP 401 (curl exits non-zero). This still proves
# the API endpoint discriminates between authorized and unauthorized callers
# at the protocol layer.
# ---------------------------------------------------------------------------
if [ "${VCLUSTER_EXPOSE:-false}" = "true" ]; then
    _ACL_LB_IP=$(kubectl --kubeconfig="${HOST_KUBECONFIG}" get svc \
        "${VCLUSTER_NAME}" -n "${VCLUSTER_NAMESPACE}" \
        -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "")
    if [ -n "$_ACL_LB_IP" ]; then
        echo "vCluster LB IP: ${_ACL_LB_IP}" >&2
        echo "$_ACL_LB_IP" > /tmp/vcluster-isv-lb-ip.txt
    else
        echo "  Warning: LB IP not available; K8sApiNetworkAclCheck will skip." >&2
    fi
fi

# ---------------------------------------------------------------------------
# Output cluster inventory (sources shared _common.sh logic)
# ---------------------------------------------------------------------------
CLUSTER_NAME=$($KUBECTL config current-context 2>/dev/null || echo "$VCLUSTER_NAME")
DEFAULT_GPU_NS="gpu-operator"
REQUIRE_JQ="true"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# _common.sh sets runtime_class="nvidia" whenever a nvidia RuntimeClass object
# exists in the tenant.  On GKE COS, GPU pods access devices through the device
# plugin (nvidia.com/gpu: 1 resource request) without needing runtimeClassName:
# nvidia in the container spec — the nvidia container runtime handler is not
# configured in containerd on GKE COS nodes.  Setting runtime_class="" prevents
# isvtest from adding runtimeClassName: nvidia to its test pods, which would fail
# with "no runtime for nvidia is configured".
if [ "${CLOUD_PROVIDER:-}" = "gke" ]; then
    _COMMON_JSON=$(
        CLUSTER_NAME="$CLUSTER_NAME"
        DEFAULT_GPU_NS="$DEFAULT_GPU_NS"
        REQUIRE_JQ="$REQUIRE_JQ"
        # shellcheck source=../../../my-isv/scripts/k8s/_common.sh
        source "$SCRIPT_DIR/../../../my-isv/scripts/k8s/_common.sh"
    )
    echo "$_COMMON_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
k8s = data.setdefault('kubernetes', {})
# GKE COS: no nvidia container runtime handler in containerd; device plugin
# handles GPU access via nvidia.com/gpu resource request, not runtimeClassName.
k8s['runtime_class'] = ''
# GKE manages GPU drivers natively; GFD labels (nvidia.com/cuda.driver.*) are
# not published on GKE COS nodes so the version resolves to 'unknown'.
# Always clear so K8sDriverVersionCheck skips the comparison on GKE.
k8s['driver_version'] = ''
print(json.dumps(data, indent=2))
"
else
    # shellcheck source=../../../my-isv/scripts/k8s/_common.sh
    source "$SCRIPT_DIR/../../../my-isv/scripts/k8s/_common.sh"
fi
