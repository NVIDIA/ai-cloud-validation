# vCluster Provider — ISV NCP Validation

This provider runs the full ISV NCP Kubernetes validation suite against a
[vCluster](https://www.vcluster.com) tenant cluster provisioned on top of an
existing Kubernetes Control Plane Cluster with NVIDIA GPU nodes.

## What is vCluster?

vCluster is a CNCF-certified open-source project from vCluster Labs that
provisions isolated tenant clusters on top of any Kubernetes cluster.  Each
tenant cluster has its own virtual control plane (API server, scheduler,
controller manager) while sharing the host cluster's nodes and GPU hardware.
vCluster is CNCF-certified for Kubernetes 1.28-1.35 across three configurations:
[vcluster-standalone](https://github.com/cncf/k8s-conformance/tree/master/v1.35/vcluster-standalone),
[vcluster-with-private-nodes](https://github.com/cncf/k8s-conformance/tree/master/v1.35/vcluster-with-private-nodes),
and [vcluster-with-shared-nodes](https://github.com/cncf/k8s-conformance/tree/master/v1.35/vcluster-with-shared-nodes).
This provider validates the **shared-nodes** topology (`sync.fromHost.nodes`),
which is the configuration that enables tenant clusters to schedule GPU
workloads onto the Control Plane Cluster's physical GPU nodes.

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| `vcluster` CLI | >= 0.34 | Create / connect to tenant cluster |
| `kubectl` | >= 1.28 | Kubernetes client |
| `helm` | >= 3.14 | GPU Operator (when required) + NIM charts |
| `jq` | any | JSON parsing in setup / teardown scripts |

Environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KUBECONFIG` | yes | — | Kubeconfig pointing at the **Control Plane Cluster** |
| `NGC_API_KEY` | yes | — | NVIDIA NGC API key (image pulls + NIM deployment) |
| `VCLUSTER_NAME` | no | `vcluster-isv-validation` | Tenant cluster name |
| `VCLUSTER_NAMESPACE` | no | `vcluster-isv-validation` | Host namespace for the tenant cluster |
| `VCLUSTER_KUBECONFIG_PATH` | no | `/tmp/vcluster-isv-validation.kubeconfig` | Where setup writes the tenant kubeconfig |
| `CLOUD_PROVIDER` | no | auto-detected | Set `gke` to enable GKE-specific GPU handling |
| `SKIP_PREFLIGHT` | no | `false` | Skip GPU readiness waits (for pre-warmed clusters) |

## GPU Architecture

vCluster syncs GPU resources from the Control Plane Cluster into each tenant
cluster via `sync.fromHost.nodes.enabled=true`.  This makes GPU node capacity,
labels, and taints visible to workloads scheduled in the tenant cluster.

**GKE COS nodes**: containerd is configured with the NVIDIA device plugin model
(no `nvidia` runtime handler).  GPU pods use only `nvidia.com/gpu` resource
limits — no `runtimeClassName: nvidia` in pod specs.

**GPU Operator**: When the Control Plane Cluster's GPU Operator is already
running (`nvidia.com/gpu.present=true` on host nodes), `setup.sh` creates only
the `gpu-operator` namespace and a pause-image Deployment in the tenant instead
of installing the full chart.  This avoids duplicate DaemonSet pod registrations
on host nodes while satisfying `K8sGpuOperatorNamespaceCheck` and
`K8sGpuOperatorPodsCheck`.

## Running the Validation Suite

```bash
# Full run (setup → test → teardown) in one command:
NGC_API_KEY="$(cat /path/to/ngc-api-key)" \
  KUBECTL="kubectl --kubeconfig=/tmp/vcluster-isv-validation.kubeconfig" \
  isvctl test run -f isvctl/configs/providers/vcluster/config/k8s.yaml

# Setup only (creates the tenant cluster; does not run tests):
isvctl test run -f isvctl/configs/providers/vcluster/config/k8s.yaml --phase setup

# Test only (tenant cluster must already exist from a prior setup):
KUBECTL="kubectl --kubeconfig=/tmp/vcluster-isv-validation.kubeconfig" \
  isvctl test run -f isvctl/configs/providers/vcluster/config/k8s.yaml --phase test

# Teardown only:
isvctl test run -f isvctl/configs/providers/vcluster/config/k8s.yaml --phase teardown
```

## CNCF Conformance Skips

The `K8sCncfConformanceCheck` runs in `certified-conformance` mode (the full
441-test `[Conformance]` suite).  28 test patterns are skipped across 15
architectural limitation groups.  All skips are specific to the
`sync.fromHost.nodes` topology required for GPU node sharing; on dedicated
nodes with `virtualScheduler.enabled`, vCluster passes the full conformance
suite with zero skips.

The main limitation categories are:

- **HostPorts**: vCluster does not map virtual ports to physical host interfaces.
- **NodePort IP mismatch**: `sync.fromHost.nodes` reports pod-CIDR IPs as node
  InternalIPs, which differ from the VPC IPs where kube-proxy binds.
- **Read-only synced nodes**: Label and extended-resource patches on synced nodes
  are overwritten by the sync cycle; NodeSelector and preemption tests that
  patch node objects fail.
- **PV sync race**: Eventual-consistency PV synchronization causes transient
  "not found" errors in two PersistentVolume lifecycle tests.
- **CRD conversion webhook isolation**: The virtual API server cannot reach
  webhook pods in the tenant namespace across the control-plane/workload
  network boundary.
- **Runtime and DNS**: RuntimeClass objects are not synced to the host cluster;
  CoreDNS ExternalName CNAME forwarding does not satisfy conformance expectations
  in the GKE DNS topology.

See `config/k8s.yaml` for the full per-pattern justification.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/k8s/setup.sh` | Create (or reuse) the tenant cluster; install GPU Operator if needed; emit cluster inventory JSON |
| `scripts/k8s/teardown.sh` | Delete the tenant cluster and restore GPU node taints |
