<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# VAST storage shim

Storage Provider Shim for VAST Data. Drives
[`StorageProviderApiCheck`](../../../../../../isvtest/src/isvtest/validations/storage_provider.py)
against a live VAST cluster via the VMS REST API.

This implementation is a working reference example for exercising the storage
provider contract against representative VAST environments. It is not an
officially supported integration or a compatibility guarantee; validate the
behavior against your VAST release, topology, CSI driver, API permissions, and
quota model before relying on the results.

## How it works

The manifest at [`../../config/storage-provider-manifest.yaml`](../../config/storage-provider-manifest.yaml)
(schema `v1alpha2`) declares one `vast-nfs` provider and drives the management
API check:

- **Management API** — `shim.module` points at [`vast/api.py`](vast/api.py);
  `StorageProviderApiCheck` loads the shim in-process and runs the subtests
  below (config [`../../config/storage.yaml`](../../config/storage.yaml), no
  cluster needed).

The **Kubernetes CSI / filesystem** checks (`K8sCsi*` / `K8sFilesystem*` /
`K8sNfsMountOptions`) are config-driven, not manifest-driven: their StorageClass
names come from the `K8S_CSI_*` env vars and their NFS expectations are literal
overrides in [`../../config/storage-k8s.yaml`](../../config/storage-k8s.yaml)
(existing cluster). See "Kubernetes storage checks" below.

`StorageProviderApiCheck` reports a `manifest-consistency[vast-nfs]` subtest
that cross-checks the manifest's declared `type` / `capabilities` against the
shim's `properties()` (so the manifest is a contract, not just metadata) — it
probes every declared-`native` surface (tenant, volume read, and the
directory/user quota CRUD below) — plus the three subtests:

| Subtest | VMS call |
| ------- | -------- |
| `api-authentication[vast-nfs]` | `GET /api/quotas/` (validates credentials) |
| `volume-provisioning[vast-nfs]` | `GET /api/quotas/` (fallback — CSI driver owns lifecycle) |
| `tenant-quota[vast-nfs]` | `GET /api/quotas/` — aggregate `hard_limit` + `used_effective_capacity` |

Quota aggregation logic:

- If a directory quota exists at exactly `VAST_STORAGE_PATH`, its
  `hard_limit` is the tenant capacity ceiling.
- Otherwise the ceiling is the sum of all child-quota hard limits.
- Used bytes = sum of `used_effective_capacity` from child quotas.

The shim also implements `list_tenants` / `list_tenant_quotas` for the single
configured tenant (`VAST_TENANT`, empty = VMS default), declared
`tenantManagement: native`.

The VAST CSI driver (`csi.vastdata.com` / `scd.vastdata.com`) owns
volume lifecycle, so `create_volume` / `delete_volume` fall back to the
base raise (`NotSupportedError`), are declared `none` in the manifest,
and the acceptance suite falls back to inventorying existing volumes via
`list_volumes`.

## Directory and user quotas

The shim backs the directory- and user-quota surfaces (declared
`quotaManagement: native` in the manifest) and wraps the VMS quota
endpoints. `manifest-consistency[vast-nfs]` cross-checks these capabilities
(and the L2 qualifiers below) against the shim's `properties()`.

| Surface | VMS call |
| ------- | -------- |
| `list_directory_quotas(volume_id)` | `GET /api/quotas/` — the quota subtree rooted at the volume's path |
| `get_directory_quota(path \| id)` | `GET /api/quotas/?path=…` or `GET /api/quotas/<id>/` |
| `set_directory_quota(path, hard)` | `POST /api/quotas/` (create) or `PATCH /api/quotas/<id>/` (update `hard_limit` / `hard_limit_inodes`) |
| `delete_directory_quota(path \| id)` | `DELETE /api/quotas/<id>/` |
| `list_user_quotas(volume_id)` | `GET /api/quotas/<id>/` (`default_user_quota`) + `GET /api/userquotas/?quota_system_id=<id>` |
| `set_user_quota(user=None, hard)` | `PATCH /api/quotas/<id>/` (`is_user_quota` + `default_user_quota.hard_limit`) |
| `set_user_quota(user, hard)` | `POST /api/userquotas/` (`quota_id` / `identifier` / `identifier_type` / `hard_limit`) |
| `delete_user_quota(user)` | `DELETE /api/userquotas/<id>/` (override) or `PATCH` clearing `default_user_quota` |

Semantics (declared as capability qualifiers via `capability_qualifiers()`):

- **Directory quotas** — VAST mints the quota id (`idAssignment: backend`).
  `DirectoryQuota.path` is **volume-relative** (per the StorageProvider
  contract): the shim joins it under the volume's absolute export path for VMS
  calls and returns volume-relative paths, so `volume_id` + `path` addresses a
  quota the same way across providers. Overlapping subjects use `nested`
  accounting (the most-restrictive cap binds); inode limits are honored.
- **User quotas** — attach to a volume's directory quota via VAST's
  `quota_system_id` (== the volume/quota id). `user=None` addresses the fs-wide
  default-user slot (`defaultUserSlot: true`); a non-`None` user is an override
  whose identifier kind is inferred from the value (numeric → `uid`, otherwise
  → `username`). Inode limits are honored.

> NOTE: `StorageProviderApiCheck` in this suite drives only N-019/N-020/N-021
> (auth, volume-provisioning, tenant-quota) plus `manifest-consistency`. The
> directory/user-quota acceptance subtests (N-022…N-031) are not wired up here
> yet; the quota methods are exercised by the hermetic unit tests in
> `isvtest/tests/test_vast_shim.py`.

## Environment variables

| Variable | Required? | Effect |
| -------- | --------- | ------ |
| `VAST_ENDPOINT` | Yes | VMS hostname or URL (e.g. `vms.example.com` or `https://vms.example.com`). |
| `VAST_TOKEN` | Yes¹ | API token (`Authorization: Api-Token <token>`). |
| `VAST_USERNAME` | Yes¹ | VMS username for basic auth. |
| `VAST_PASSWORD` | Yes¹ | VMS password (required when `VAST_USERNAME` is set). |
| `VAST_STORAGE_PATH` | Yes | Root export path to scope to (matches StorageClass `root_export`, e.g. `/exports/k8s`). |
| `VAST_TENANT` | No | VAST tenant name (`X-Tenant-Name` header). Empty = VMS default tenant. |
| `VAST_VIP_POOL` | No | VIP pool FQDN or name; used to build the NFS `MountSpec.source` for each volume. |
| `VAST_INSECURE_SKIP_VERIFY` | No | `1` / `true` to disable TLS cert verification (dev/test only). |

¹ Either `VAST_TOKEN` or `VAST_USERNAME` + `VAST_PASSWORD` must be provided.

## Network locality requirement

The shim makes direct HTTPS calls to the VAST VMS (`VAST_ENDPOINT`).
Unlike the K8s checks — which go through `kubectl` and work from any
machine with a valid kubeconfig — the storage shim must run from a host
that has network access to the VMS endpoint.

In many deployments the VMS is only reachable from within the cluster
network. If `curl https://$VAST_ENDPOINT/api/quotas/` times out from
your laptop, use `isvctl deploy run` to execute the suite on a cluster
node instead:

```bash
# Run on a node that can reach the VMS (env vars are forwarded automatically)
uv run isvctl deploy run <node-ip> \
  -f isvctl/configs/providers/vast/config/storage.yaml

# If the node is behind a bastion:
uv run isvctl deploy run <node-ip> -j <jumphost> \
  -f isvctl/configs/providers/vast/config/storage.yaml
```

## Running against a cluster

From the repo root (on a machine that can reach the VMS):

```bash
export VAST_ENDPOINT=vms.example.com
export VAST_TOKEN=<your-api-token>        # or VAST_USERNAME + VAST_PASSWORD
export VAST_STORAGE_PATH=/exports/k8s     # match your StorageClass root_export

# Sanity-check network access and credentials:
curl -sS -H "Authorization: Api-Token $VAST_TOKEN" \
  "https://$VAST_ENDPOINT/api/quotas/" | python3 -m json.tool | head -40

# Run the storage checks:
uv run isvctl test run \
    -f isvctl/configs/providers/vast/config/storage.yaml
```

`volume-provisioning[vast-nfs]` reports **skipped (passed)** with
`created_volume not implemented; observed N CSI-provisioned volume(s)
via list_volumes()` — that is the expected fallback when the CSI driver
owns volume lifecycle, not a failure.

## Kubernetes storage checks

[`../../config/storage-k8s.yaml`](../../config/storage-k8s.yaml) imports the
canonical Kubernetes suite. StorageClass names come from the `K8S_CSI_*` env
vars; the VAST NFS mount-option expectations are literal overrides in that
config (`K8sNfsMountOptionsCheck`: `vers=4.1`, `proto=tcp`, `nconnect=16`). These
run over `kubectl` against the existing cluster (no VMS access needed):

```bash
# CSI + filesystem checks only (no VAST creds required):
export K8S_CSI_SHARED_FS_SC=vast-nfs \
       K8S_CSI_NFS_SC=vast-nfs
uv run isvctl test run \
    -f isvctl/configs/providers/vast/config/storage-k8s.yaml \
    -- -v -s -k "K8sCsi or K8sFile or K8sNfs"
```

Because the manifest declares a `shim:` block, adding `StorageProviderApiCheck`
to the selection (or running everything) also loads the shim, which needs the
`VAST_*` env vars above:

```bash
export VAST_ENDPOINT=vms.example.com VAST_TOKEN=<token> \
       VAST_STORAGE_PATH=/exports/k8s
uv run isvctl test run \
    -f isvctl/configs/providers/vast/config/storage-k8s.yaml \
    -- -v -s -k "K8sCsi or K8sFile or K8sNfs or StorageProviderApi"
```

> NOTE: the shim's `properties()` reports protocol `nfsv4` and the manifest
> declares `provider.protocols: [nfsv4]`; the on-wire NFS version (`vers=4.1`)
> is asserted by `K8sNfsMountOptionsCheck` (config/storage-k8s.yaml).

### Faster repeat runs of the quota check

`StorageDirectoryQuotaEnforcementCheck` spends most of its wall time
provisioning a PVC and waiting for a BusyBox pod to go Ready. When iterating,
create those once and point the check at them with `pvc_name` / `pvc_namespace`
/ `pod_name`. Each run then only creates its own probe subdirectory and quota
and removes both afterwards, leaving the pod and PVC for the next run:

```yaml
# quota-probe.yaml - apply once into a namespace you create
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: quota-probe, namespace: isvtest-quota }
spec:
  accessModes: [ReadWriteMany]
  storageClassName: vast-nfs
  resources: { requests: { storage: 5Gi } }
---
apiVersion: v1
kind: Pod
metadata: { name: quota-probe, namespace: isvtest-quota }
spec:
  # Match the harness pod's identity, or writes into /data may be squashed.
  securityContext: { runAsUser: 65534, runAsGroup: 65534, fsGroup: 65534 }
  containers:
    - name: probe
      image: busybox:1.36
      command: ["sh", "-c", "while true; do sleep 3600; done"]
      volumeMounts: [{ name: data, mountPath: /data }]
  volumes:
    - name: data
      persistentVolumeClaim: { claimName: quota-probe }
```

Then pass an override alongside the usual config (`-f` files deep-merge, so the
suite's `manifest_path` is retained):

```yaml
# quota-reuse.yaml
tests:
  validations:
    k8s_storage:
      checks:
        StorageDirectoryQuotaEnforcementCheck:
          pvc_namespace: isvtest-quota
          pvc_name: quota-probe
          pod_name: quota-probe
```

```bash
uv run isvctl test run \
    -f isvctl/configs/providers/vast/config/storage-k8s.yaml \
    -f quota-reuse.yaml \
    -- -v -s -k "StorageDirectoryQuotaEnforcement"
```

Delete the namespace when you are done - nothing reclaims it for you.

## Multiple tenants or storage paths

To validate multiple VAST tenants or root exports, add extra provider
entries to `storage-provider-manifest.yaml` and set the corresponding
env vars.  Each provider entry gets its own `build_api()` call.

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| All subtests time out (~75s each) | VMS not reachable from this machine | Use `isvctl deploy run <node-ip> -f ...` to run from inside the cluster network |
| `api-authentication[...] FAILED ... AuthenticationError` | Invalid token / expired password / wrong endpoint | Verify creds with a raw `curl` call (see above) |
| `api-authentication[...] FAILED ... SSL` | Self-signed VMS cert | Set `VAST_INSECURE_SKIP_VERIFY=1` (test only) |
| `tenant-quota[...] FAILED ... hard_limit_bytes=0` | No quotas exist under `VAST_STORAGE_PATH` | Check `VAST_STORAGE_PATH` matches your VMS quota tree |
| `volume-provisioning[...] SKIPPED ... observed 0 ...` | No PVCs provisioned against the VAST StorageClass | Create a PVC first, then re-run |
| `VAST_ENDPOINT must be set` | Env var unset | `export VAST_ENDPOINT=...` |
| `VAST_STORAGE_PATH must be set` | Env var unset | `export VAST_STORAGE_PATH=...` |

## See also

- [`isvtest/src/isvtest/core/storage_provider/`](../../../../../../isvtest/src/isvtest/core/storage_provider/) — `StorageApi` ABC + value types
- [`isvctl/configs/providers/aws/scripts/storage/fsx-lustre/`](../../../aws/scripts/storage/fsx-lustre/) — AWS FSx Lustre reference implementation
- [VAST quota documentation](https://kb.vastdata.com/documentation/docs/overview-of-quotas-1)
