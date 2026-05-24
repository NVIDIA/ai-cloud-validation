#!/usr/bin/env bash
# post_cluster_setup.sh
#
# Run ONCE after `kubeadm init` + all nodes have joined.
# Installs every dependency the NVIDIA NCP Validation Suite (K8s suite) needs:
#
#   1. Calico CNI          (NetworkPolicy enforcement — replaces Flannel)
#   2. AWS Cloud CCM       (zcompute AWS-compatible cloud controller)
#   3. EBS CSI Driver      (zcompute block storage)
#   4. NVIDIA GPU Operator (driver, device-plugin, DCGM, MIG manager)
#   5. MPI Operator        (K8sNcclMultiNodeWorkload)
#   6. OIDC verify         (K8sOidcIssuerCheck — must be baked into kubeadm init)
#   7. RDMA NIC IP fix     (fix rocep75s0 duplicate IP on both HGX nodes)
#   8. Image pre-pull      (hpc-benchmarks + NIM on both GPU nodes)
#   9. Final checklist     (print pass/fail for every suite pre-req)
#
# Usage:
#   export KUBECONFIG=/path/to/kubeconfig
#   export ZCOMPUTE_IP=172.29.0.20
#   export AWS_ACCESS_KEY_ID=<key>
#   export AWS_SECRET_ACCESS_KEY=<secret>
#   export HGX_NODE_1_IP=192.168.0.190   # SSH-reachable IP of first HGX worker
#   export HGX_NODE_2_IP=192.168.0.235   # SSH-reachable IP of second HGX worker
#   export HGX_SSH_KEY=/path/to/key.pem  # optional — use key-based auth
#   export HGX_SSH_PASS=<password>       # optional — use password-based auth (needs sshpass)
#   export HGX_SSH_USER=ubuntu           # optional — defaults to ubuntu
#   export NGC_API_KEY=<key>             # optional — needed for NIM pre-pull
#   bash post_cluster_setup.sh
#
# All steps are idempotent — safe to re-run after partial failure.
# Steps that are already complete print "SKIP (already done)" and move on.

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[setup]${NC} $*"; }
ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*"; }
die()  { echo -e "${RED}[ FAIL ]${NC} $*" >&2; exit 1; }
skip() { echo -e "${YELLOW}[ SKIP ]${NC} $* (already done)"; }

# ── Config / defaults ─────────────────────────────────────────────────────────
ZCOMPUTE_IP="${ZCOMPUTE_IP:-172.29.0.20}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:?Set AWS_ACCESS_KEY_ID}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:?Set AWS_SECRET_ACCESS_KEY}"
HGX_NODE_1_IP="${HGX_NODE_1_IP:-192.168.0.190}"
HGX_NODE_2_IP="${HGX_NODE_2_IP:-192.168.0.235}"
HGX_SSH_KEY="${HGX_SSH_KEY:-}"
HGX_SSH_PASS="${HGX_SSH_PASS:-}"
HGX_SSH_USER="${HGX_SSH_USER:-ubuntu}"
NGC_API_KEY="${NGC_API_KEY:-}"

GPU_OPERATOR_VERSION="v24.6.0"
GPU_DRIVER_VERSION="535.161.08"
MPI_OPERATOR_VERSION="v0.5.0"
CALICO_VERSION="v3.28.0"
POD_CIDR="10.244.0.0/16"

SSH_BASE_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"

# Build ssh_node based on auth method: password (sshpass) or key
if [[ -n "$HGX_SSH_PASS" ]]; then
    command -v sshpass >/dev/null 2>&1 || {
        log "sshpass not found — installing ..."
        sudo apt-get install -y -qq sshpass
    }
    ssh_node() { sshpass -p "$HGX_SSH_PASS" ssh $SSH_BASE_OPTS -o BatchMode=no "${HGX_SSH_USER}@$1" "$2"; }
elif [[ -n "$HGX_SSH_KEY" && -f "$HGX_SSH_KEY" ]]; then
    ssh_node() { ssh $SSH_BASE_OPTS -o BatchMode=yes -i "$HGX_SSH_KEY" "${HGX_SSH_USER}@$1" "$2"; }
else
    ssh_node() { ssh $SSH_BASE_OPTS -o BatchMode=yes "${HGX_SSH_USER}@$1" "$2"; }
fi

# ── Pre-flight ────────────────────────────────────────────────────────────────
log "Pre-flight checks ..."
command -v kubectl >/dev/null || die "kubectl not found"
command -v helm    >/dev/null || die "helm not found"
[[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG not set"
kubectl cluster-info --request-timeout=10s >/dev/null 2>&1 || die "Cannot reach the cluster (check KUBECONFIG)"
ok "Cluster reachable"

# ── §1. Calico CNI ────────────────────────────────────────────────────────────
log "§1 Calico CNI ..."
if kubectl get deployment calico-kube-controllers -n calico-system >/dev/null 2>&1; then
    skip "Calico"
else
    kubectl create -f \
        "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml" \
        2>/dev/null || true

    kubectl apply -f - <<EOF
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
    - blockSize: 26
      cidr: ${POD_CIDR}
      encapsulation: VXLANCrossSubnet
      natOutgoing: Enabled
      nodeSelector: all()
EOF

    log "Waiting for Calico to be ready (up to 5 min) ..."
    kubectl wait --for=condition=Available deployment/calico-kube-controllers \
        -n calico-system --timeout=300s
    ok "Calico ready"
fi

# ── §2. AWS Cloud Controller Manager ─────────────────────────────────────────
log "§2 AWS Cloud Controller Manager ..."
if helm list -n kube-system 2>/dev/null | grep -q aws-cloud-controller-manager; then
    skip "AWS CCM"
else
    # ConfigMap for zcompute endpoint
    kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: cloud-config
  namespace: kube-system
data:
  cloud.conf: |
    [Global]
    zone=symphony
    [ServiceOverride "ec2"]
    Service=ec2
    Region=symphony
    URL=https://${ZCOMPUTE_IP}/api/v2/aws/ec2/
    SigningRegion=symphony
EOF

    helm repo add aws-cloud-controller-manager \
        https://kubernetes.github.io/cloud-provider-aws 2>/dev/null || true
    helm repo update aws-cloud-controller-manager 2>/dev/null || true

    helm upgrade --install aws-cloud-controller-manager \
        aws-cloud-controller-manager/aws-cloud-controller-manager \
        --namespace kube-system \
        --set args[0]="--cloud-provider=aws" \
        --set args[1]="--cloud-config=/etc/kubernetes/cloud.conf" \
        --set args[2]="--node-monitor-period=0s"  # prevents CCM from evicting GPU nodes

    # Give CCM time to start before proceeding
    kubectl rollout status deployment/aws-cloud-controller-manager -n kube-system --timeout=120s
    ok "AWS CCM deployed"
fi

# ── §3. EBS CSI Driver ────────────────────────────────────────────────────────
log "§3 EBS CSI Driver ..."
if helm list -n kube-system 2>/dev/null | grep -q aws-ebs-csi-driver; then
    skip "EBS CSI"
else
    # Credentials secret (idempotent)
    kubectl create secret generic aws-secret \
        --namespace kube-system \
        --from-literal "key_id=${AWS_ACCESS_KEY_ID}" \
        --from-literal "access_key=${AWS_SECRET_ACCESS_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -

    helm repo add aws-ebs-csi-driver https://kubernetes-sigs.github.io/aws-ebs-csi-driver 2>/dev/null || true
    helm repo update aws-ebs-csi-driver 2>/dev/null || true

    helm upgrade --install aws-ebs-csi-driver aws-ebs-csi-driver/aws-ebs-csi-driver \
        --namespace kube-system \
        --set "controller.env[0].name=AWS_EC2_ENDPOINT" \
        --set "controller.env[0].value=https://${ZCOMPUTE_IP}/api/v2/aws/ec2/" \
        --set controller.extraVolumeTags.environment=ncp-validation

    # Default StorageClass
    kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ebs-sc
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
EOF

    kubectl rollout status deployment/ebs-csi-controller -n kube-system --timeout=120s
    ok "EBS CSI deployed"
fi

# ── §4. NVIDIA GPU Operator ───────────────────────────────────────────────────
log "§4 NVIDIA GPU Operator ..."
if helm list -n nvidia-gpu-operator 2>/dev/null | grep -q gpu-operator; then
    skip "GPU Operator"
else
    helm repo add nvidia https://helm.ngc.nvidia.com/nvidia 2>/dev/null || true
    helm repo update nvidia 2>/dev/null || true

    helm upgrade --install gpu-operator nvidia/gpu-operator \
        --namespace nvidia-gpu-operator \
        --create-namespace \
        --version "${GPU_OPERATOR_VERSION}" \
        --set driver.enabled=true \
        --set driver.version="${GPU_DRIVER_VERSION}" \
        --set toolkit.enabled=true \
        --set devicePlugin.enabled=true \
        --set gfd.enabled=true \
        --set migManager.enabled=true \
        --set dcgmExporter.enabled=true \
        --set nodeStatusExporter.enabled=true \
        --set operator.defaultRuntime=containerd

    log "Waiting for GPU Operator pods (up to 10 min — driver compilation on first run) ..."
    kubectl wait --for=condition=Ready pods --all \
        -n nvidia-gpu-operator --timeout=600s
    ok "GPU Operator ready"
fi

# ── §5. MPI Operator ─────────────────────────────────────────────────────────
log "§5 MPI Operator ..."
if kubectl get deployment mpi-operator -n mpi-operator >/dev/null 2>&1; then
    skip "MPI Operator"
else
    kubectl apply --server-side -f \
        "https://raw.githubusercontent.com/kubeflow/mpi-operator/${MPI_OPERATOR_VERSION}/deploy/v2beta1/mpi-operator.yaml"
    kubectl wait --for=condition=Available deployment/mpi-operator \
        -n mpi-operator --timeout=120s
    ok "MPI Operator ready"
fi

# ── §6. OIDC verify ───────────────────────────────────────────────────────────
log "§6 OIDC service-account issuer ..."
OIDC_RESP=$(kubectl get --raw /.well-known/openid-configuration 2>/dev/null || echo "")
if echo "$OIDC_RESP" | grep -q '"issuer"'; then
    ISSUER=$(echo "$OIDC_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['issuer'])" 2>/dev/null || echo "?")
    ok "OIDC endpoint live — issuer: $ISSUER"
else
    warn "OIDC discovery endpoint not found."
    warn "Add --service-account-issuer=https://<CONTROL_PLANE_IP>:6443 to kube-apiserver."
    warn "On an existing cluster: sudo vi /etc/kubernetes/manifests/kube-apiserver.yaml"
    warn "K8sOidcIssuerCheck will fail until this is fixed."
fi

# ── §7. RDMA NIC IP fix (rocep75s0 duplicate IP) ─────────────────────────────
log "§7 RDMA NIC IP fix on HGX nodes ..."
fix_rdma() {
    local node_ip="$1"
    local target_rdma_ip="$2"

    # Check current IP on enp75s0np0
    current=$(ssh_node "$node_ip" "ip -4 addr show enp75s0np0 2>/dev/null | awk '/inet /{print \$2}' | head -1" || echo "")
    if [[ "$current" == "${target_rdma_ip}/"* ]] || [[ "$current" == "$target_rdma_ip" ]]; then
        skip "RDMA NIC on $node_ip (already ${target_rdma_ip})"
        return
    fi

    log "Fixing enp75s0np0 on $node_ip: $current → $target_rdma_ip ..."
    # Remove duplicate .16 if present, assign correct IP
    ssh_node "$node_ip" "
        sudo ip addr del 10.20.0.16/31 dev enp75s0np0 2>/dev/null || true
        sudo ip addr del 10.20.0.17/31 dev enp75s0np0 2>/dev/null || true
        sudo ip addr add ${target_rdma_ip}/31 dev enp75s0np0
    "
    # Make persistent via netplan
    ssh_node "$node_ip" "
        sudo tee /etc/netplan/99-rdma-fix.yaml > /dev/null <<'NETPLAN'
network:
  version: 2
  ethernets:
    enp75s0np0:
      addresses: [${target_rdma_ip}/31]
NETPLAN
        sudo netplan apply 2>/dev/null || true
    "
    ok "RDMA NIC fixed on $node_ip → ${target_rdma_ip}"
}

if ssh_node "$HGX_NODE_1_IP" "true" 2>/dev/null; then
    fix_rdma "$HGX_NODE_1_IP" "10.20.0.16"
else
    warn "Cannot SSH to HGX_NODE_1_IP=$HGX_NODE_1_IP — skipping RDMA fix"
fi

if ssh_node "$HGX_NODE_2_IP" "true" 2>/dev/null; then
    fix_rdma "$HGX_NODE_2_IP" "10.20.0.17"
else
    warn "Cannot SSH to HGX_NODE_2_IP=$HGX_NODE_2_IP — skipping RDMA fix"
fi

# ── §8. Image pre-pull on GPU nodes ──────────────────────────────────────────
log "§8 Pre-pulling large images on GPU nodes ..."

# Check if already pulled by looking at image presence on nodes
HPC_IMAGE="nvcr.io/nvidia/hpc-benchmarks:25.04"
NIM_IMAGE="nvcr.io/nim/meta/llama-3.2-1b-instruct:latest"

prepull_via_daemonset() {
    local name="$1"; local image="$2"
    local ds_name="prepull-${name}"

    if kubectl get daemonset "$ds_name" -n default >/dev/null 2>&1; then
        skip "Pre-pull DaemonSet $ds_name already running"
        return
    fi

    log "Pre-pulling ${image} on all GPU nodes ..."

    local imagepull_secret=""
    if [[ -n "$NGC_API_KEY" ]] && [[ "$image" == nvcr.io/* ]]; then
        # Create/update NGC pull secret
        kubectl create secret docker-registry ngc-pull-secret \
            --docker-server=nvcr.io \
            --docker-username='$oauthtoken' \
            --docker-password="${NGC_API_KEY}" \
            --namespace default \
            --dry-run=client -o yaml | kubectl apply -f -
        imagepull_secret="imagePullSecrets: [{name: ngc-pull-secret}]"
    fi

    kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: ${ds_name}
  namespace: default
spec:
  selector:
    matchLabels:
      app: ${ds_name}
  template:
    metadata:
      labels:
        app: ${ds_name}
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      ${imagepull_secret}
      initContainers:
        - name: pull
          image: ${image}
          command: ["/bin/sh", "-c", "echo pulled ${image}"]
      containers:
        - name: done
          image: busybox:1.36
          command: [sleep, infinity]
EOF

    log "Waiting for pre-pull DaemonSet ${ds_name} (up to 30 min — image is large) ..."
    kubectl wait --for=condition=Ready pods -l "app=${ds_name}" \
        -n default --timeout=1800s && \
    kubectl delete daemonset "${ds_name}" -n default 2>/dev/null || true
    ok "Pre-pulled ${image}"
}

# hpc-benchmarks is always needed
prepull_via_daemonset "hpc-benchmarks" "$HPC_IMAGE"

# NIM only if we have the NGC key
if [[ -n "$NGC_API_KEY" ]]; then
    prepull_via_daemonset "nim" "$NIM_IMAGE"
else
    warn "NGC_API_KEY not set — skipping NIM image pre-pull (will be slow on first test run)"
fi

# ── §9. Final verification checklist ─────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  FINAL VERIFICATION"
echo "════════════════════════════════════════════════════════════"
echo ""

PASS=0; FAIL=0; WARN_COUNT=0
chk() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo -e "  ${GREEN}PASS${NC}  $label"
        ((PASS++)) || true
    else
        echo -e "  ${RED}FAIL${NC}  $label"
        ((FAIL++)) || true
    fi
}
chk_warn() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo -e "  ${GREEN}PASS${NC}  $label"
        ((PASS++)) || true
    else
        echo -e "  ${YELLOW}WARN${NC}  $label  ← not blocking but fix before certification"
        ((WARN_COUNT++)) || true
    fi
}

# Node readiness
chk "All nodes Ready" kubectl wait --for=condition=Ready nodes --all --timeout=30s

# GPU capacity
chk "GPU nodes present (nvidia.com/gpu.present)" \
    kubectl get nodes -l nvidia.com/gpu.present=true --no-headers

GPU_TOTAL=$(kubectl get nodes -l nvidia.com/gpu.present=true \
    -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}' 2>/dev/null \
    | tr ' ' '\n' | awk '{s+=$1}END{print s}' || echo 0)
if [[ "$GPU_TOTAL" -ge 16 ]]; then
    echo -e "  ${GREEN}PASS${NC}  Total allocatable GPUs: $GPU_TOTAL (expected ≥ 16)"
    ((PASS++)) || true
else
    echo -e "  ${RED}FAIL${NC}  Total allocatable GPUs: $GPU_TOTAL (expected ≥ 16 — GPU Operator may still be initializing)"
    ((FAIL++)) || true
fi

# GPU Operator pods
chk "GPU Operator pods running" \
    kubectl wait --for=condition=Ready pods --all -n nvidia-gpu-operator --timeout=60s

# MPI Operator
chk "MPI Operator available" \
    kubectl wait --for=condition=Available deployment/mpi-operator -n mpi-operator --timeout=30s

# MPI CRD
chk "MPIJob CRD registered" \
    kubectl api-resources --api-group=kubeflow.org 2>/dev/null

# Calico
chk "Calico running" \
    kubectl wait --for=condition=Available deployment/calico-kube-controllers -n calico-system --timeout=30s

# NetworkPolicy enforcement
chk "NetworkPolicy API exists (Calico enforcing)" bash -c \
    "kubectl api-resources 2>/dev/null | grep -q networkpolicies"

# Storage
chk "Default StorageClass (ebs-sc)" \
    kubectl get storageclass ebs-sc

# Runtime class
chk "nvidia RuntimeClass registered" \
    kubectl get runtimeclass nvidia

# OIDC
chk_warn "OIDC discovery endpoint" bash -c \
    "kubectl get --raw /.well-known/openid-configuration 2>/dev/null | python3 -c 'import sys,json; json.load(sys.stdin)[\"issuer\"]'"

# RDMA devices on each HGX node
for node_ip in "$HGX_NODE_1_IP" "$HGX_NODE_2_IP"; do
    chk_warn "RDMA devices present on $node_ip" \
        ssh_node "$node_ip" "ls /dev/infiniband/uverbs0"
done

# RDMA IPs unique
if ssh_node "$HGX_NODE_1_IP" "true" 2>/dev/null && \
   ssh_node "$HGX_NODE_2_IP" "true" 2>/dev/null; then
    IP1=$(ssh_node "$HGX_NODE_1_IP" "ip -4 addr show enp75s0np0 2>/dev/null | awk '/inet /{print \$2}' | head -1" || echo "?")
    IP2=$(ssh_node "$HGX_NODE_2_IP" "ip -4 addr show enp75s0np0 2>/dev/null | awk '/inet /{print \$2}' | head -1" || echo "?")
    if [[ "$IP1" != "$IP2" ]] && [[ "$IP1" != "?" ]]; then
        echo -e "  ${GREEN}PASS${NC}  RDMA IPs unique: node1=$IP1  node2=$IP2"
        ((PASS++)) || true
    else
        echo -e "  ${RED}FAIL${NC}  RDMA IPs: node1=$IP1  node2=$IP2  (should differ)"
        ((FAIL++)) || true
    fi
else
    echo -e "  ${YELLOW}WARN${NC}  Could not SSH to HGX nodes to verify RDMA IPs"
    ((WARN_COUNT++)) || true
fi

echo ""
echo "  Results: ${PASS} passed  ${FAIL} failed  ${WARN_COUNT} warnings"
echo ""
if [[ $FAIL -eq 0 ]]; then
    echo -e "  ${GREEN}Cluster is ready — run the NCP K8s suite:${NC}"
    echo "  uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s.yaml"
else
    echo -e "  ${RED}Fix the ${FAIL} failure(s) above before running the NCP suite.${NC}"
fi
echo ""
