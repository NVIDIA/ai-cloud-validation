# EKS-D Cluster Setup for NVIDIA NCP Certification

**Audience:** Engineers standing up a new EKS-D cluster on zCompute that will run the NCP validation suite.

This document covers every step from cluster bootstrap through final readiness check. It uses the **`eksd-install` tooling** (Zadara's install framework) and documents every known workaround discovered during the zCompute bring-up.

All steps are written to be **idempotent** — they check before acting. It is safe to re-run any section after a partial failure.

---

## Overview

| # | Step | Tool | Notes |
|---|------|------|-------|
| 1 | [Pre-requisites & install.env](#1-pre-requisites--installenv) | Manual | Fix NET_DEVICES, gather credentials |
| 2 | [Pre-create `zadara-cloud-config`](#2-pre-create-zadara-cloud-config-configmap) | kubectl | Must exist before step 4 |
| 3 | [Bootstrap EKS-D cluster](#3-bootstrap-eks-d-cluster) | eksd-install k8s | Control-plane + CPU workers |
| 4 | [Deploy Cilium CNI](#4-deploy-cilium-cni) | eksd-install cilium | Replaces Flannel; enforces NetworkPolicy |
| 5 | [Deploy zadara-vm-chart](#5-deploy-zadara-vm-chart) | eksd-install zadara-vm-chart | CCM, EBS CSI, GPU Operator, NFD, ALB, autoscaler |
| 6 | [Fix Cilium device config](#6-fix-cilium-device-config) | kubectl patch | `eth0` → `enp1s0` in cilium-config |
| 7 | [Fix AWS CCM](#7-fix-aws-cloud-controller-manager) | kubectl | Credentials, SSL cert, cloud.conf |
| 8 | [Join GPU workers](#8-join-gpu-workers) | eksd-install join | HGX nodes join after CCM is healthy |
| 9 | [Install MPI Operator](#9-mpi-operator) | kubectl apply | Required for K8sNcclMultiNodeWorkload |
| 10 | [Post-install cluster fixes](#10-post-install-cluster-fixes) | kubectl | Cilium operator scale, EBS CSI credentials |
| 11 | [RDMA NIC IP fix](#11-rdma-nic-ip-fix) | ssh | Fix duplicate IP on enp75s0np0 |
| 12 | [Pre-pull large images](#12-pre-pull-large-images) | DaemonSet | pytorch, hpc-benchmarks, NIM — ~30-45 min |
| 13 | [Install Helm](#13-install-helm) | script | Required for K8sNimHelmWorkload tests |
| 14 | [Final verification checklist](#14-final-verification-checklist) | kubectl | Confirm all prereqs pass |

---

## Environment Variables

Set these in your shell before running any commands in this guide:

```bash
export ZCOMPUTE_IP=172.29.0.20               # zCompute API endpoint IP
export AWS_ACCESS_KEY_ID=<your-key-id>        # zCompute EC2-compatible access key
export AWS_SECRET_ACCESS_KEY=<your-secret>    # zCompute EC2-compatible secret
export CLUSTER_NAME=nkqa11                    # Must match install.env CLUSTER_NAME
export PRIMARY_NIC=enp1s0                     # Primary NIC on all nodes (NOT eth0)
export KUBECONFIG=/etc/kubernetes/admin.conf  # Or wherever kubeadm placed it
export HGX_NODE_1_IP=<gpu-worker-0-ip>       # SSH-reachable IP
export HGX_NODE_2_IP=<gpu-worker-1-ip>       # SSH-reachable IP
```

---

## 1. Pre-requisites & install.env

### 1a. Correct NET_DEVICES in install.env

`eksd-install` reads its configuration from `install.env`. The `NET_DEVICES` value shipped as `eth0` but on zCompute VMs the primary interface is `enp1s0`. Cilium will crash-loop if this is wrong (see §6).

```bash
# On the control-plane node — edit install.env before running any eksd-install commands
grep NET_DEVICES install.env
# If it shows eth0, fix it:
sed -i 's/^NET_DEVICES=.*/NET_DEVICES="enp1s0"/' install.env
grep NET_DEVICES install.env  # confirm: NET_DEVICES="enp1s0"
```

### 1b. Confirm CLUSTER_NAME

```bash
grep CLUSTER_NAME install.env
# Expected: CLUSTER_NAME="nkqa11"  (or whatever your cluster name is)
# This value becomes kubernetesclusterid in cloud.conf — must match exactly.
```

### 1c. Confirm ZADARA_API_DOMAIN

```bash
grep ZADARA_API_DOMAIN install.env
# Expected: ZADARA_API_DOMAIN=172.29.0.20  (matches $ZCOMPUTE_IP above)
```

---

## 2. Pre-create `zadara-cloud-config` ConfigMap

`eksd-install zadara-vm-chart` (step 5) deploys the AWS Cloud Controller Manager, which mounts a ConfigMap named **`zadara-cloud-config`** in `kube-system`. If this ConfigMap does not exist before the chart is deployed, the CCM pod will fail with:

```
MountVolume.SetUp failed for volume "cloud-config": configmap "zadara-cloud-config" not found
```

Create it now, before running `eksd-install zadara-vm-chart`:

```bash
# Idempotent — kubectl apply is safe to re-run
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: zadara-cloud-config
  namespace: kube-system
data:
  cloud.conf: |
    [Global]
    region=${CLUSTER_NAME:+symphony}
    kubernetesclusterid=${CLUSTER_NAME}

    [ServiceOverride "ec2"]
    Service=ec2
    Region=symphony
    URL=https://${ZCOMPUTE_IP}/api/v2/aws/ec2/
    SigningRegion=symphony
EOF
```

> **Why `region=` not `zone=`?** The standard AWS CCM v1.27.1 validates the zone value and rejects `symphony` (non-standard AZ name). Using `region=symphony` bypasses that validation. The gcfg library (used by CCM for config parsing) also requires config keys to be all-lowercase with no hyphens — hence `kubernetesclusterid` (not `kubernetes-cluster-id`).

---

## 3. Bootstrap EKS-D Cluster

```bash
# On the control-plane node
eksd-install k8s
```

This uses `install.env` to run `kubeadm init` with EKS-D packages and sets up the control-plane. CPU worker nodes can be joined at this stage if they are included in `install.env`.

Verify the cluster came up:

```bash
kubectl get nodes
# Expected: control-plane and CPU workers in Ready state
kubectl get pods -n kube-system
# CoreDNS will be Pending until Cilium is deployed (step 4) — that is normal
```

---

## 4. Deploy Cilium CNI

Cilium is the CNI for this cluster (not Calico or Flannel). It enforces NetworkPolicy for `K8sNetworkPolicyCheck`.

```bash
eksd-install cilium
```

Verify (Cilium pods will be `Init:*` briefly, then `Running`):

```bash
kubectl get pods -n kube-system -l k8s-app=cilium
# Expected: one pod per node in Running state
```

> **Note:** Cilium pods will CrashLoopBackOff immediately after this step because `NET_DEVICES` in `cilium-config` still reflects `eth0`. The fix is in §6. Do **not** skip §6.

---

## 5. Deploy zadara-vm-chart

The `zadara-vm-chart` Helm chart is Zadara's meta-chart that installs:

- **AWS Cloud Controller Manager** (CCM)
- **AWS EBS CSI Driver**
- **NVIDIA GPU Operator** (device-plugin, DCGM, MIG manager, feature discovery)
- **Node Feature Discovery** (NFD)
- **Cluster Autoscaler**
- **AWS Load Balancer Controller**
- **nvidia-host-installer**

```bash
eksd-install zadara-vm-chart
```

> **Expected failures at this stage** — all are fixed in subsequent steps:
> - Cilium pods: CrashLoopBackOff (`direct routing device` error) — fixed in §6
> - CCM pod: `ContainerCreating` then `CrashLoopBackOff` (credentials / SSL) — fixed in §7
> - CoreDNS: `Pending` (waiting for CCM to remove the `node.cloudprovider.kubernetes.io/uninitialized` taint) — auto-resolves after §7

---

## 6. Fix Cilium Device Config

Cilium's ConfigMap was generated with `direct-routing-device: eth0` from `install.env`. On zCompute nodes the actual NIC is `enp1s0`. Patch both fields:

```bash
# Check current value first
kubectl get configmap cilium-config -n kube-system -o jsonpath='{.data.direct-routing-device}'
# If it shows "eth0", apply the patch:

kubectl patch configmap cilium-config -n kube-system \
  --type merge \
  -p '{"data":{"direct-routing-device":"enp1s0","devices":"enp1s0"}}'

# Rolling restart Cilium DaemonSet to pick up the change
kubectl rollout restart daemonset/cilium -n kube-system

# Wait for all Cilium pods to become Ready
kubectl rollout status daemonset/cilium -n kube-system --timeout=300s
kubectl get pods -n kube-system -l k8s-app=cilium
# Expected: all Running
```

After Cilium comes up, CoreDNS should also transition from Pending to Running (once CCM removes taints in §7).

---

## 7. Fix AWS Cloud Controller Manager

The CCM deployed by `zadara-vm-chart` needs three manual fixes: AWS credentials, SSL certificate trust, and cloud.conf cluster name. Apply them in this order.

### 7a. Create AWS Credentials Secret

zCompute does not have EC2 instance metadata (IMDS returns 404), so CCM cannot auto-discover credentials. Create them as a Kubernetes secret:

```bash
# Idempotent — dry-run + apply
kubectl create secret generic aws-cloud-controller-manager-credentials \
  --namespace kube-system \
  --from-literal "aws_access_key_id=${AWS_ACCESS_KEY_ID}" \
  --from-literal "aws_secret_access_key=${AWS_SECRET_ACCESS_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Patch the CCM DaemonSet to source these credentials as environment variables:

```bash
# Check if envFrom is already set
kubectl get daemonset aws-cloud-controller-manager -n kube-system \
  -o jsonpath='{.spec.template.spec.containers[0].envFrom}' 2>/dev/null

# If empty/missing, patch it:
kubectl patch daemonset aws-cloud-controller-manager -n kube-system \
  --type=json \
  -p='[{
    "op": "add",
    "path": "/spec/template/spec/containers/0/envFrom",
    "value": [{
      "secretRef": {"name": "aws-cloud-controller-manager-credentials"}
    }]
  }]'
```

### 7b. Trust the zCompute CA Certificate

zCompute uses a self-signed TLS certificate. Go (and therefore CCM) will fail with `x509: certificate signed by unknown authority` unless we mount the CA. The `SSL_CERT_FILE` environment variable tells Go to use an additional CA bundle.

```bash
# Fetch the zCompute self-signed CA cert
echo | openssl s_client -connect ${ZCOMPUTE_IP}:443 2>/dev/null \
  | openssl x509 > /tmp/zcompute-ca.crt

# Store it as a ConfigMap (idempotent)
kubectl create configmap zcompute-ca-cert \
  --namespace kube-system \
  --from-file=ca.crt=/tmp/zcompute-ca.crt \
  --dry-run=client -o yaml | kubectl apply -f -

# Mount the CA cert into the CCM DaemonSet
# First: add the volume (append to volumes array)
kubectl patch daemonset aws-cloud-controller-manager -n kube-system \
  --type=json \
  -p='[{
    "op": "add",
    "path": "/spec/template/spec/volumes/-",
    "value": {
      "name": "zcompute-ca",
      "configMap": {"name": "zcompute-ca-cert"}
    }
  }]'

# Then: add the volumeMount (append to mounts array)
kubectl patch daemonset aws-cloud-controller-manager -n kube-system \
  --type=json \
  -p='[{
    "op": "add",
    "path": "/spec/template/spec/containers/0/volumeMounts/-",
    "value": {
      "name": "zcompute-ca",
      "mountPath": "/etc/ssl/zcompute",
      "readOnly": true
    }
  }]'

# Set SSL_CERT_FILE so Go trusts it
kubectl patch daemonset aws-cloud-controller-manager -n kube-system \
  --type=json \
  -p='[{
    "op": "add",
    "path": "/spec/template/spec/containers/0/env",
    "value": [{"name": "SSL_CERT_FILE", "value": "/etc/ssl/zcompute/ca.crt"}]
  }]'
```

> **Tip:** If `env` already exists on the container, use `/-` to append instead of replacing the array. Check first with:
> `kubectl get daemonset aws-cloud-controller-manager -n kube-system -o jsonpath='{.spec.template.spec.containers[0].env}'`

### 7c. Verify/Update cloud.conf

The `zadara-cloud-config` ConfigMap (created in §2) must have the correct cluster ID and use `region=` (not `zone=`). Verify and update if needed:

```bash
kubectl get configmap zadara-cloud-config -n kube-system \
  -o jsonpath='{.data.cloud\.conf}'
```

Expected output:
```ini
[Global]
region=symphony
kubernetesclusterid=nkqa11

[ServiceOverride "ec2"]
Service=ec2
Region=symphony
URL=https://172.29.0.20/api/v2/aws/ec2/
SigningRegion=symphony
```

If it differs (e.g. has `zone=symphony` or `kubernetes-cluster-id`), patch it:

```bash
kubectl patch configmap zadara-cloud-config -n kube-system \
  --type merge \
  -p "{\"data\":{\"cloud.conf\":\"[Global]\\nregion=symphony\\nkubernetesclusterid=${CLUSTER_NAME}\\n\\n[ServiceOverride \\\"ec2\\\"]\\nService=ec2\\nRegion=symphony\\nURL=https://${ZCOMPUTE_IP}/api/v2/aws/ec2/\\nSigningRegion=symphony\\n\"}}"
```

> **gcfg key format:** The gcfg library (used by AWS CCM) maps struct field `KubernetesClusterID` to the config key `kubernetesclusterid` — all lowercase, no hyphens. Using `kubernetes-cluster-id` produces a parse error: `can't store data at section "Global", variable "kubernetes-cluster-id"`.

### 7d. Wait for CCM to Become Healthy

After all patches, restart the DaemonSet and wait:

```bash
kubectl rollout restart daemonset/aws-cloud-controller-manager -n kube-system
kubectl rollout status daemonset/aws-cloud-controller-manager -n kube-system --timeout=120s

# Verify CCM removed the uninitialized taint from all nodes
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.taints}{"\n"}{end}'
# node.cloudprovider.kubernetes.io/uninitialized should NOT appear on any node

# CoreDNS should now be Running
kubectl get pods -n kube-system -l k8s-app=kube-dns
```

---

## 8. Join GPU Workers

GPU workers (HGX nodes) are joined after the control-plane is healthy. Use `eksd-install join` on each GPU worker node.

**On each GPU worker node** (SSH in as ubuntu):

```bash
# Get the join command from the control-plane
# On control-plane:
eksd-install print-join-command   # or: kubeadm token create --print-join-command

# On each GPU worker:
eksd-install join   # uses install.env to determine the control-plane endpoint
# OR if join command is manual:
sudo kubeadm join <CONTROL_PLANE_IP>:6443 \
  --token <token> \
  --discovery-token-ca-cert-hash sha256:<hash>
```

Wait for GPU Operator to initialize on new nodes (driver installation takes 5–10 min on first join):

```bash
# On control-plane — watch GPU Operator pods
kubectl get pods -n nvidia-gpu-operator -w

# Wait for cuda-validator to complete on each GPU node (confirms GPUs working end-to-end)
kubectl wait --for=condition=Ready pods --all -n nvidia-gpu-operator --timeout=600s

# Verify GPUs are allocatable
kubectl get nodes -l nvidia.com/gpu.present=true \
  -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable."nvidia\.com/gpu"
# Expected: 8 GPUs per HGX node
```

---

## 9. MPI Operator

Required for `K8sNcclMultiNodeWorkload` (multi-node NCCL AllReduce test). This is the **only component not included in `zadara-vm-chart`**.

```bash
MPI_OPERATOR_VERSION="v0.5.0"

# Check if already installed
if kubectl get deployment mpi-operator -n mpi-operator >/dev/null 2>&1; then
    echo "MPI Operator already installed"
else
    kubectl apply --server-side -f \
        "https://raw.githubusercontent.com/kubeflow/mpi-operator/${MPI_OPERATOR_VERSION}/deploy/v2beta1/mpi-operator.yaml"
fi

# Verify
kubectl wait --for=condition=Available deployment/mpi-operator -n mpi-operator --timeout=120s
kubectl api-resources --api-group=kubeflow.org | grep mpijobs
# Expected: mpijobs   MPI  kubeflow.org/v2beta1
```

---

## 10. Post-Install Cluster Fixes

These fixes address known issues with how `zadara-vm-chart` and `eksd-install` configure the cluster that only manifest when running the NCP validation suite.

### 10a. Scale Cilium Operator to 1 Replica

`eksd-install cilium` deploys the `cilium-operator` Deployment with 2 replicas. The operator uses a hostPort and requires `node-role.kubernetes.io/control-plane` node affinity — but there is only 1 control-plane node. The second replica will be permanently Pending with `didn't have free ports for the requested pod ports`, causing `K8sNoPendingPodsCheck` to fail.

```bash
# Check current replica count
kubectl get deployment cilium-operator -n kube-system

# Scale to 1 (idempotent)
kubectl scale deployment cilium-operator -n kube-system --replicas=1

# Verify the pending pod terminates
kubectl get pods -n kube-system | grep cilium-operator
# Expected: exactly 1 Running pod
```

### 10b. Fix EBS CSI Driver Credentials and Endpoint

`zadara-vm-chart` deploys EBS CSI without AWS credentials or the correct EC2 endpoint. The controller will log `no EC2 IMDS role found` (IMDS returns 404 on zCompute) and fail to provision any PVC. This causes all `K8sCsi*` checks to fail.

```bash
# Create credentials secret in zadara-system
kubectl create secret generic aws-ebs-csi-credentials \
  --namespace zadara-system \
  --from-literal "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}" \
  --from-literal "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Inject credentials, region, and endpoint into ebs-plugin container
kubectl set env deployment/ebs-csi-controller \
  -n zadara-system \
  -c ebs-plugin \
  --from=secret/aws-ebs-csi-credentials

kubectl set env deployment/ebs-csi-controller \
  -n zadara-system \
  -c ebs-plugin \
  AWS_REGION=symphony \
  AWS_EC2_ENDPOINT=https://${ZCOMPUTE_IP}/api/v2/aws/ec2/

# Copy CA cert into zadara-system (the cert was created in kube-system for CCM — §7b)
kubectl get configmap zcompute-ca-cert -n kube-system -o json \
  | jq 'del(.metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.namespace)' \
  | kubectl apply -n zadara-system -f -

# Mount CA cert and set SSL_CERT_FILE
kubectl patch deployment ebs-csi-controller -n zadara-system --type=json -p='[
  {"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"zcompute-ca","configMap":{"name":"zcompute-ca-cert"}}},
  {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"zcompute-ca","mountPath":"/etc/ssl/zcompute","readOnly":true}},
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"SSL_CERT_FILE","value":"/etc/ssl/zcompute/ca.crt"}}
]'

kubectl rollout status deployment/ebs-csi-controller -n zadara-system --timeout=60s

# Verify PVC provisioning works
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ebs-verify-pvc
  namespace: default
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ebs-sc
  resources:
    requests:
      storage: 1Gi
EOF

# WaitForFirstConsumer — need a pod to trigger binding
kubectl run ebs-verify --image=busybox:1.36 --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"ebs-verify-pvc"}}],"containers":[{"name":"ebs-verify","image":"busybox:1.36","command":["sh","-c","echo ok"],"volumeMounts":[{"name":"d","mountPath":"/data"}]}]}}' \
  -- sh -c "echo ok"

kubectl wait --for=condition=Succeeded pod/ebs-verify --timeout=60s \
  && echo "EBS CSI OK" \
  && kubectl delete pod/ebs-verify pvc/ebs-verify-pvc -n default
```

---

## 11. RDMA NIC IP Fix

The HGX nodes have 8 Mellanox ConnectX RoCE NICs (`rocep75s0` through `rocep52s0`). The first NIC (`enp75s0np0`) may have the same IP on both GPU nodes, which prevents NCCL from using it. The other 7 NICs work correctly.

The NCP suite manifests work around this via `NCCL_IB_HCA=^rocep75s0` (exclude the broken NIC). However, the proper fix restores full 8-NIC bandwidth (~130+ GB/s vs ~115 GB/s).

### Check current state

```bash
ssh ubuntu@${HGX_NODE_1_IP} "ip addr show enp75s0np0 | grep inet"
ssh ubuntu@${HGX_NODE_2_IP} "ip addr show enp75s0np0 | grep inet"
# They should show DIFFERENT IPs — e.g. 10.20.0.16 and 10.20.0.17
# If both show .16, apply the fix below
```

### Fix

```bash
# Node 1: assign .16
ssh ubuntu@${HGX_NODE_1_IP} "
  sudo ip addr del 10.20.0.16/31 dev enp75s0np0 2>/dev/null || true
  sudo ip addr add 10.20.0.16/31 dev enp75s0np0
  sudo tee /etc/netplan/99-rdma-fix.yaml > /dev/null <<'EOF'
network:
  version: 2
  ethernets:
    enp75s0np0:
      addresses: [10.20.0.16/31]
EOF
  sudo netplan apply
"

# Node 2: assign .17
ssh ubuntu@${HGX_NODE_2_IP} "
  sudo ip addr del 10.20.0.16/31 dev enp75s0np0 2>/dev/null || true
  sudo ip addr del 10.20.0.17/31 dev enp75s0np0 2>/dev/null || true
  sudo ip addr add 10.20.0.17/31 dev enp75s0np0
  sudo tee /etc/netplan/99-rdma-fix.yaml > /dev/null <<'EOF'
network:
  version: 2
  ethernets:
    enp75s0np0:
      addresses: [10.20.0.17/31]
EOF
  sudo netplan apply
"
```

Once fixed, remove `NCCL_IB_HCA=^rocep75s0` from the MPIJob manifest (`isvtest/src/isvtest/workloads/manifests/k8s/nccl_allreduce_mpijob.yaml`).

---

## 11. Pre-Pull Large Images

NVIDIA workload images are 7–20 GB. Pull them once on all GPU nodes to avoid timeout failures during the test run. Three images are required:

| Image | Used by | Size |
|-------|---------|------|
| `nvcr.io/nvidia/hpc-benchmarks:25.04` | K8sNcclWorkload, K8sNcclMultiNodeWorkload | ~15 GB |
| `nvcr.io/nvidia/pytorch:25.04-py3` | K8sGpuStressWorkload | ~20 GB |
| `nvcr.io/nim/meta/llama-3.2-1b-instruct:latest` | K8sNimInferenceWorkload | ~8 GB |

```bash
# Requires NGC_API_KEY to be set
[[ -n "$NGC_API_KEY" ]] || { echo "Set NGC_API_KEY first"; exit 1; }

# Create NGC pull secret in default namespace (idempotent)
kubectl create secret docker-registry ngc-pull-secret \
    --docker-server=nvcr.io \
    --docker-username='$oauthtoken' \
    --docker-password="${NGC_API_KEY}" \
    --namespace default \
    --dry-run=client -o yaml | kubectl apply -f -

# Clean up any existing pre-pull DaemonSets
for ds in prepull-hpc prepull-pytorch prepull-nim; do
    kubectl delete daemonset "$ds" -n default 2>/dev/null || true
done

# Deploy all three pre-pull DaemonSets (they run in parallel — total ~30-45 min)
kubectl apply -f - <<'EOF'
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: prepull-hpc
  namespace: default
spec:
  selector:
    matchLabels:
      app: prepull-hpc
  template:
    metadata:
      labels:
        app: prepull-hpc
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      imagePullSecrets:
        - name: ngc-pull-secret
      initContainers:
        - name: pull
          image: nvcr.io/nvidia/hpc-benchmarks:25.04
          command: ["echo", "pulled"]
          resources:
            limits:
              nvidia.com/gpu: "1"
      containers:
        - name: done
          image: busybox:1.36
          command: [sleep, infinity]
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: prepull-pytorch
  namespace: default
spec:
  selector:
    matchLabels:
      app: prepull-pytorch
  template:
    metadata:
      labels:
        app: prepull-pytorch
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      imagePullSecrets:
        - name: ngc-pull-secret
      initContainers:
        - name: pull
          image: nvcr.io/nvidia/pytorch:25.04-py3
          command: ["echo", "pulled"]
          resources:
            limits:
              nvidia.com/gpu: "1"
      containers:
        - name: done
          image: busybox:1.36
          command: [sleep, infinity]
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: prepull-nim
  namespace: default
spec:
  selector:
    matchLabels:
      app: prepull-nim
  template:
    metadata:
      labels:
        app: prepull-nim
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      imagePullSecrets:
        - name: ngc-pull-secret
      initContainers:
        - name: pull
          image: nvcr.io/nim/meta/llama-3.2-1b-instruct:latest
          command: ["echo", "pulled"]
          resources:
            limits:
              nvidia.com/gpu: "1"
      containers:
        - name: done
          image: busybox:1.36
          command: [sleep, infinity]
EOF

# Wait for all three to complete on all GPU nodes
kubectl wait --for=condition=Ready pods -l app=prepull-hpc -n default --timeout=1800s
kubectl wait --for=condition=Ready pods -l app=prepull-pytorch -n default --timeout=1800s
kubectl wait --for=condition=Ready pods -l app=prepull-nim -n default --timeout=1800s

kubectl delete daemonset prepull-hpc prepull-pytorch prepull-nim -n default
echo "All images pre-pulled"
```

---

## 13. Install Helm

Required for `K8sNimHelmWorkload-1b` and `K8sNimHelmWorkload-3b`. Run on the manager VM where `isvctl` is executed.

```bash
# Idempotent — safe to re-run
if ! command -v helm &>/dev/null; then
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
helm version
# Expected: version.BuildInfo{Version:"v3.x.x", ...}
```

---

## 14. Final Verification Checklist

Run before starting the NCP suite. All items must pass (or be acknowledged as known gaps):

```bash
# 1. All nodes Ready
kubectl get nodes
# Expected: control-plane, 2x CPU workers, 2x GPU workers — all Ready

# 2. Cilium CNI running
kubectl get pods -n kube-system -l k8s-app=cilium
# Expected: one pod per node, all Running

# 3. NetworkPolicy API enforced by Cilium
kubectl api-resources | grep networkpolicies
# Expected: networkpolicies  NetworkPolicy  networking.k8s.io/v1

# 4. CCM healthy — no uninitialized taints
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.taints}{"\n"}{end}'
# Expected: no node.cloudprovider.kubernetes.io/uninitialized taint

# 5. EBS CSI running
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-ebs-csi-driver
# Expected: ebs-csi-controller and ebs-csi-node pods Running

# 6. Default StorageClass
kubectl get storageclass
# Expected: a default StorageClass backed by ebs.csi.aws.com

# 7. GPU Operator pods all Running/Completed
kubectl get pods -n nvidia-gpu-operator
# Expected: ~21 pods; cuda-validator should be Completed (= GPUs working)

# 8. GPUs allocatable
kubectl get nodes -l nvidia.com/gpu.present=true \
  -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable."nvidia\.com/gpu"
# Expected: 8 GPUs per HGX node

# 9. nvidia RuntimeClass
kubectl get runtimeclass nvidia
# Expected: nvidia runtime class present

# 10. MPI Operator
kubectl get pods -n mpi-operator
# Expected: mpi-operator Deployment Running
kubectl api-resources --api-group=kubeflow.org | grep mpijobs
# Expected: mpijobs listed

# 11. OIDC discovery endpoint
kubectl get --raw /.well-known/openid-configuration \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('issuer:', d['issuer'])"
# Expected: issuer printed (https://<control-plane-ip>:6443 or similar)

# 12. RDMA devices on GPU nodes
ssh ubuntu@${HGX_NODE_1_IP} "ls /dev/infiniband/"
ssh ubuntu@${HGX_NODE_2_IP} "ls /dev/infiniband/"
# Expected: uverbs0..uverbs7 (8 devices each)

# 13. RDMA IPs are unique
ssh ubuntu@${HGX_NODE_1_IP} "ip -4 addr show enp75s0np0 | grep inet"
ssh ubuntu@${HGX_NODE_2_IP} "ip -4 addr show enp75s0np0 | grep inet"
# Expected: different IPs (e.g. 10.20.0.16 vs 10.20.0.17)

# 14. Helm installed on manager VM
helm version
# Expected: version.BuildInfo{Version:"v3.x.x", ...}

# 15. Large images cached on GPU nodes (no pull on first test run)
for node in ${HGX_NODE_1_IP} ${HGX_NODE_2_IP}; do
  echo "=== $node ==="
  ssh ubuntu@$node "sudo crictl images 2>/dev/null | grep -E 'hpc-benchmarks|pytorch|llama'"
done
# Expected: hpc-benchmarks:25.04, pytorch:25.04-py3, llama-3.2-1b-instruct:latest on both nodes

# 16. Cilium operator running as single replica (no pending pods)
kubectl get pods -n kube-system | grep cilium-operator
# Expected: exactly 1/1 Running pod

# 17. EBS CSI can provision volumes (quick smoke test)
kubectl get pvc -A | grep -v Bound || echo "No unbound PVCs"
```

When all checks pass, run the NCP K8s suite:

```bash
export NGC_API_KEY=<your-ngc-key>   # required for NIM tests

uv run isvctl test run -f isvctl/configs/providers/zcompute/config/k8s.yaml 2>&1 | tee /tmp/ncp-k8s-run.log
```

---

## Runtime Environment Variables (for isvctl)

```bash
export KUBECONFIG=/etc/kubernetes/admin.conf    # points at the EKS-D cluster
export ZCOMPUTE_BASE_URL=https://${ZCOMPUTE_IP} # zCompute endpoint
export AWS_ACCESS_KEY_ID=<key>                   # zCompute credentials
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_REGION=symphony
export PYTHONPATH=isvctl/src:isvtest/src:isvreporter/src  # Mac only
export NGC_API_KEY=<key>                         # Optional: enables NIM tests
```

---

## Known Gaps / Remaining Work

| Gap | Effort | Notes |
|-----|--------|-------|
| **NIM tests** | Minutes | Set `NGC_API_KEY` and remove NIM tests from exclude list |
| **CNCF conformance** | 1–2 hours | Remove `K8sCncfConformanceCheck` from exclude list; hours-long run |
| **K8sApiNetworkAclCheck** | 30 min | Needs an external IP outside the cluster allow-list to probe from |
| **K8sNodePoolCheck** | 1–2 days | Write scripts calling zCompute EC2 API to join/drain/remove worker nodes dynamically in response to horizontal scaling events |
| **All 8 RoCE NICs** | 30 min | Fix the `enp75s0np0` IP conflict (§10), then remove `NCCL_IB_HCA=^rocep75s0` from nccl_allreduce_mpijob.yaml |
| **Cluster Autoscaler** | Unknown | CrashLoopBackOff in current cluster; not blocking NCP validation |
| **Route 53 support** | TBD | zCompute partially supports Route 53; evaluate which tests pass |

---

## Appendix: eksd-install Tooling Reference

The `eksd-install` commands used in this guide:

| Command | What it does |
|---------|-------------|
| `eksd-install k8s` | Runs `kubeadm init` with EKS-D packages per `install.env` |
| `eksd-install cilium` | Installs Cilium CNI (v1.19.4, tunnel mode) |
| `eksd-install zadara-vm-chart` | Installs the Zadara meta-chart (CCM, EBS CSI, GPU Operator, NFD, ALB, autoscaler) |
| `eksd-install join` | Prints or runs the `kubeadm join` command for additional worker nodes |
| `eksd-install print-join-command` | Prints the join command without executing it |

### zadara-vm-chart Components

Installed in `zadara-system` namespace:

- `aws-cloud-controller-manager` — DaemonSet, runs on control-plane
- `aws-ebs-csi-driver` — controller Deployment + node DaemonSet
- `gpu-operator` — Deployment + per-node DaemonSets
- `node-feature-discovery` (NFD)
- `cluster-autoscaler`
- `aws-load-balancer-controller`
- `nvidia-host-installer`

> **Note:** GPU Operator and other components may be deployed in `nvidia-gpu-operator` or `zadara-system` depending on the chart version. Use `kubectl get pods -A | grep -E 'gpu|nvidia'` to find them.

### Known install.env Issues

| Variable | Default (buggy) | Correct value | Impact |
|----------|----------------|---------------|--------|
| `NET_DEVICES` | `eth0` | `enp1s0` | Cilium crashes without the fix in §6 |
| `CLUSTER_NAME` | (varies) | Must match `kubernetesclusterid` in cloud.conf | CCM cannot find the cluster if mismatched |
