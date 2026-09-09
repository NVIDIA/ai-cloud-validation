<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# WEKA storage shim

Storage Provider Shim for WEKA. Drives
[`StorageProviderApiCheck`](../../../../../../isvtest/src/isvtest/validations/storage_provider.py)
against a live WEKA cluster via the REST API (`/api/v2`).

This implementation is a working reference example for exercising the storage
provider contract against representative WEKA environments. It is not an
officially supported integration or a compatibility guarantee; validate the
behavior against your WEKA release, topology, CSI driver, API permissions, and
quota model before relying on the results.

## How it works

The manifest at [`../../config/storage-provider-manifest.yaml`](../../config/storage-provider-manifest.yaml)
(schema `v1alpha2`) declares one `weka-shared-fs` provider; `shim.module` points
at [`weka/api.py`](weka/api.py); `StorageProviderApiCheck` loads the shim
in-process and runs the subtests below.

`StorageProviderApiCheck` reports a `manifest-consistency[weka-shared-fs]`
subtest that cross-checks the manifest's declared `type` / `provider.protocols`
/ `provider.version` / `capabilities` against the shim's `properties()` (so the
manifest is a contract, not just metadata) — it probes every declared-`native`
surface (tenant, volume read, and the directory + user quota CRUD below) — plus
the three subtests:

| Subtest | WEKA call |
| ------- | --------- |
| `api-authentication[weka-shared-fs]` | `GET /api/v2/fileSystems` (validates credentials) |
| `volume-provisioning[weka-shared-fs]` | `GET /api/v2/fileSystems` (fallback — CSI driver owns lifecycle) |
| `tenant-quota[weka-shared-fs]` | `GET /api/v2/fileSystems` — `total_budget` / `used_total` |

When `WEKA_STORAGE_PATH` is set, tenant quota follows the VAST-style
directory-quota aggregation model (parent hard limit or sum of child limits;
used bytes = sum of child `total_bytes`).

The WEKA CSI driver (`csi.weka.io`) owns volume lifecycle. The shim assumes
`weka/v2`: each PVC is a filesystem and its volume id is
`weka/v2/<filesystem>`. `list_volumes` inventories WEKA filesystems;
`get_volume` and directory-quota methods accept that handle or the bare
filesystem name.

`create_volume` / `delete_volume` fall back to the base raise
(`NotSupportedError`), are declared `none` in the manifest, and
`StorageProviderApiCheck` volume-provisioning uses the `list_volumes` CSI
fallback.

## Quotas

The shim implements directory and user quotas over the documented REST API
(`quotaManagement.directory: native`, `quotaManagement.user: native`):

| Surface | WEKA API |
| ------- | -------- |
| `quota.directory.{list,get,set,delete}` | REST — `resolvePath` + `PUT` / `PATCH` / `DELETE` on `/api/v2/fileSystems/{uid}/quota/{inode}` |

Directory quotas use `idAssignment: backend` (WEKA mints the `quota_id`), so
`set_directory_quota` requires a volume-relative `path`. Quotas are byte-only
(no inode enforcement). Creating a quota on a **non-empty** directory over REST
requires a Data Services container on the backend; without one WEKA restricts
`PUT` to empty directories (`PATCH` / `DELETE` on an existing quota — e.g. the
per-PVC quota the CSI driver already sets — are unaffected). The shim surfaces
that backend rule verbatim.

### End-to-end lifecycle + enforcement (`StorageDirectoryQuotaEnforcementCheck`)

Where `StorageProviderApiCheck` only *probes* the quota surfaces with sentinel ids,
[`StorageDirectoryQuotaEnforcementCheck`](../../../../../../isvtest/src/isvtest/validations/storage_quota_enforcement.py)
exercises the directory quota for real and proves it is enforced. It runs only
under the K8s config (it needs a mounted PVC) and reports two subtests:

| Subtest | What it does |
| ------- | ------------ |
| `directory-quota-crud[weka-shared-fs]` | Provisions an RWX PVC, mounts it, creates an empty subdirectory, then drives `set` (create) → `get` (verify hard limit) → `set` (update) → `get` → `list` (present) → `delete` → `get` (gone) over the shim. |
| `directory-quota-enforcement[weka-shared-fs]` | With the hard limit set on that subdirectory, writes below the limit (must succeed) and above it (must be blocked with a no-space / quota-exceeded error) from inside the pod. |

The subdirectory is empty at create time, so the `PUT` restriction above does
not apply. Pass a pre-provisioned PVC with `pvc_name` / `pvc_namespace`
(and optionally `pod_name` for a Ready mount pod at `/data`) instead of
provisioning one; tune limits with `pvc_size` / `create_hard_bytes` /
`enforcement_hard_bytes`. The check is skipped cleanly when no shared-fs
StorageClass is configured or no cluster is reachable.

### Faster repeat runs of the quota check

Most wall time is PVC provision + pod Ready. Create those once and pass
`pvc_name` / `pvc_namespace` / `pod_name`; each run only creates and removes
its probe subdirectory and quota:

```yaml
# quota-reuse.yaml — deep-merge with storage-k8s.yaml
tests:
  validations:
    k8s_storage:
      checks:
        StorageDirectoryQuotaEnforcementCheck:
          pvc_namespace: isvtest-quota
          pvc_name: quota-probe
          pod_name: quota-probe
```

Match the harness pod identity (`runAsUser`/`runAsGroup`/`fsGroup` 65534) so
writes into `/data` are not squashed. See the VAST storage README for a full
PVC+pod manifest example.

### User quotas (`quota.user: native`, WEKA 5.1.26+)

| Surface | WEKA API |
| ------- | -------- |
| `quota.user.{list,get,set,delete}` | REST — `GET` / `POST` / `DELETE` on `/api/v2/fileSystems/{uid}/quota/user` |

`{uid}` is the filesystem UUID, not its name. Arguments (`user_id`,
`hard_limit_bytes`, `soft_limit_bytes`) travel as query parameters, and `GET`
paginates via `next_token`. WEKA exposes no per-UID read, so `get_user_quota`
filters the list.

User subjects must be numeric UIDs — WEKA has no username form on this route, so
a non-numeric subject raises `ValidationError`. A hard limit of `0` means
unlimited on the backend and is surfaced as an absent limit rather than a
zero-byte allowance.

WEKA addresses one UID per call and has no filesystem-wide default-user slot, so
the shim advertises `defaultUserSlot: false` and refuses a request with
`user=None` (`NotSupportedError`). User quotas are also accepted only for
filesystem-backed (`weka/v2`) volumes: WEKA scopes them to a whole filesystem, so
applying one to a directory-backed volume would bind every other PVC sharing that
filesystem.

Clusters older than 5.1.26 answer the route with a 404 naming the route; the shim
reports that as `NotSupportedError` (a capability gap) rather than
`NotFoundError` (a missing quota), and names the required release.

Two backend prerequisites apply before the limits take effect:

1. Filesystems created before WEKA 5.1.20 need a one-time
   `weka fs quota enable-users`, which requires a Data Services container.
   Filesystems created on 5.1.20 or later have user quota accounting enabled
   automatically.
2. Enabling user quotas on an existing filesystem starts a background
   `QUOTA_COLORING` pass that stamps existing files with their UID. Quotas are
   not enforced on pre-existing data until it completes.

Ownership changes (`chown`) reattribute capacity asynchronously, so usage
counters may briefly still reflect the previous owner.

See [WEKA quota management](https://docs.weka.io/weka-filesystems-and-object-stores/quota-management)
for directory, filesystem, and tenant quota semantics on the backend.

## Environment variables

| Variable | Required? | Effect |
| -------- | --------- | ------ |
| `WEKA_ENDPOINTS` | Yes¹ | Comma-separated `host:port` list (matches CSI secret `endpoints`). |
| `WEKA_ENDPOINT` | Yes¹ | Single `host:port` fallback. |
| `WEKA_SCHEME` | No | URL scheme (default `https`). |
| `WEKA_USERNAME` | Yes | Cluster username (secret: `username`). Use an account with filesystem, directory-quota, and user-quota permissions. |
| `WEKA_PASSWORD` | Yes | Cluster password. |
| `WEKA_ORGANIZATION` | No | Organization for login and `tenant_id` (default `Root`). |
| `WEKA_FILESYSTEM` | Yes | Filesystem for tenant quota (matches StorageClass `filesystemName`). Volume directory quotas use the filesystem named by their `weka/v2` handle. |
| `WEKA_STORAGE_PATH` | No | Directory path prefix for scoped tenant-quota aggregation. |
| `WEKA_INSECURE_SKIP_VERIFY` | No | `1` / `true` to disable TLS cert verification (dev/test only). |

¹ Either `WEKA_ENDPOINTS` or `WEKA_ENDPOINT` must be provided.

### Mapping from a cluster secret

| Secret key | Env var |
| ---------- | ------- |
| `endpoints` | `WEKA_ENDPOINTS` |
| `scheme` | `WEKA_SCHEME` |
| `username` | `WEKA_USERNAME` |
| `password` | `WEKA_PASSWORD` |
| `organization` | `WEKA_ORGANIZATION` |

## Network locality requirement

The shim makes direct HTTPS calls to WEKA backend hosts on port 14000.
Unlike K8s checks — which go through `kubectl` — the storage shim must run
from a host that can reach those endpoints (typically inside the cluster
VPC). If probes time out from your laptop, use `isvctl deploy run` to
execute the suite on a cluster node.

## Running against a cluster

From the repo root (on a machine that can reach the WEKA API):

```bash
export WEKA_ENDPOINTS="weka-1.example.com:14000,weka-2.example.com:14000"
export WEKA_USERNAME=storage-csi-user
export WEKA_PASSWORD='...'
export WEKA_ORGANIZATION=Root
export WEKA_FILESYSTEM=test_fs
export WEKA_INSECURE_SKIP_VERIFY=1   # if using cluster-internal TLS

# Sanity probe:
uv run python .cursor/skills/storage-api-stub-authoring/scripts/probe_shim.py \
  --manifest isvctl/configs/providers/weka/config/storage-provider-manifest.yaml

# Full check:
uv run isvctl test run \
    -f isvctl/configs/providers/weka/config/storage.yaml
```

To also verify the directory-quota lifecycle **and** enforcement against the
live cluster your kubectl points at (provisions a PVC, mounts it, and writes
data), run the K8s config with the shared-fs StorageClass set:

```bash
export K8S_CSI_SHARED_FS_SC=weka-rwx
uv run isvctl test run \
    -f isvctl/configs/providers/weka/config/storage-k8s.yaml \
    -- -v -s -k "StorageDirectoryQuotaEnforcement"
```

`volume-provisioning[weka-shared-fs]` reports **skipped (passed)** when
`create_volume` is not implemented and `list_volumes` returns ≥1
CSI-provisioned filesystem volume — that is expected, not a failure.

If `list_volumes` returns 0 volumes, create a PVC against
your WEKA RWX StorageClass first:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: weka-probe-pvc
  namespace: default
spec:
  accessModes: [ReadWriteMany]
  storageClassName: weka-rwx
  resources:
    requests:
      storage: 5Gi
EOF
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| All subtests time out | WEKA API not reachable from this host | Run via `isvctl deploy run` from a cluster node |
| `AuthenticationError` | Wrong creds or org | Verify with `POST /api/v2/login` from a client pod |
| `hard_limit_bytes=0` | Wrong filesystem name | Check `WEKA_FILESYSTEM` matches `filesystemName` on the StorageClass |
| `observed 0 ... via list_volumes` | No filesystems visible to the organization | Create a PVC against your WEKA RWX StorageClass |

## See also

- [`isvtest/src/isvtest/core/storage_provider/`](../../../../../../isvtest/src/isvtest/core/storage_provider/) — `StorageApi` ABC
- [`isvctl/configs/providers/vast/scripts/storage/vast/`](../../../vast/scripts/storage/vast/) — VAST reference (directory-quota CSI pattern)
- [WEKA REST API](https://docs.weka.io/getting-started-with-weka/weka-rest-api-and-equivalent-cli-commands)
