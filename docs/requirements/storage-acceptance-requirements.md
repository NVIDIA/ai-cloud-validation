<!-- GENERATED FILE - DO NOT EDIT BY HAND. Source: storage-acceptance-requirements.yaml. Run `make plan`. -->

# NVIDIA DGXC Storage Acceptance Test for NCPs and CSPs

> Structured source of record: `storage-acceptance-requirements.yaml` (version google-docs-snapshot-2026-07).
> Requirement IDs are this document's own native identifiers (N-001..N-033),
> a distinct namespace from the offtake HSS/DIR requirements. Upstream 'PRD
> Ref' cross-references are tracked in the traceability matrix, not here.
> Edit the YAML, not this file.

## CSI Driver Presence

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| N-001 | CSI driver pods running | CSI controller Deployment and node DaemonSet pods are running and healthy (all pods Running; desired == available for the DaemonSet). | active |
| N-002 | CSIDriver object registered | The CSIDriver object exists in the cluster for the NCP provisioner. | active |
| N-003 | StorageClass exists | At least one StorageClass references the NCP CSI provisioner. | active |

## Dynamic Provisioning Lifecycle

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| N-004 | PVC create + bind | A PVC against the NCP StorageClass reaches Bound and a PV is created (within 10 minutes). | active |
| N-005 | PV capacity matches request | The provisioned PV capacity is >= the PVC request. | active |
| N-006 | Pod mount + read/write | A pod can mount the PVC, write a file, and read it back with matching content and no I/O errors. | active |
| N-007 | Multiple concurrent PVCs | Multiple PVCs created in parallel against the same StorageClass all reach Bound and are backed by distinct PVs. | active |
| N-008 | PVC expand (if supported) | Patching a PVC to a larger size grows the PV capacity and the pod-visible size (where expansion is supported). | active |

## Filesystem Semantics

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| N-009 | POSIX compliance (pjdfstest) | The applicable pjdfstest suites run and the export/results are provided. | active |
| N-010 | File locking (flock) | flock() acquired from one pod causes another pod to block or receive EAGAIN - locking behaves correctly across pods on the same PVC. | active |
| N-011 | Cross-node write visibility | A file written from a pod on node A is visible with correct content from a pod on node B within one second. | active |
| N-012 | Cross-node attribute consistency | After extending a file on node A, a stat from node B reflects the updated size and mtime within the vendor-documented attribute-cache window. | active |
| N-013a | Large directory listing, files | A directory containing 1,000,000 files lists without error or truncation. | active |
| N-013b | Large directory listing, subdirectories | A directory containing 500,000 subdirectories lists without error or truncation. | active |

## NFS Mount Configuration

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| N-014 | NFS version | NFS mounts use the expected NFS version (for example, vers=4.1), verified from mount output on the worker node. | active |
| N-015 | nconnect parameter | NFS mounts use the configured nconnect value (mount -t nfs4 output shows nconnect=<expected>). | active |
| N-016 | Transport protocol | NFS mounts use the expected transport protocol (proto=rdma or proto=tcp). | active |
| N-017 | Readahead tuning | Where a vendor kernel module is required it is loaded and readahead is configured (lsmod shows the module; /sys/class/bdi/*/read_ahead_kb matches expected). | active |
| N-018 | Vendor kernel module present | Any required DKMS or vendor kernel modules are installed and loaded on all GPU and CPU worker nodes. | active |

## Storage Management APIs

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| N-019 | API authentication | Authentication to the storage management API succeeds using the configured credential (basic auth or bearer token) - HTTP 200 on an authenticated request. | active |
| N-020 | Volume Provisioning API | An API provisions a volume of a requested size within NVIDIA's tenant, accessible from cluster nodes using the specified credentials. | active |
| N-021 | Tenant-level quota exists | A functional API reports overall storage utilization within the tenant and usage against the tenant quota. | active |
| N-022 | List quotas | A functional API returns the list of quotas configured within the NVIDIA tenancy. | active |
| N-023 | Get quota by ID | A functional API returns utilization against a given quota. | active |
| N-024 | Quota reflects consumed capacity | After writing data, re-querying the quota shows increased reported consumption within the vendor-documented timeframe. | active |
| N-025 | Multi-tenant isolation | Quota listings contain only paths within the tenant's namespace - no visibility of quotas outside the NVIDIA tenancy. | active |
