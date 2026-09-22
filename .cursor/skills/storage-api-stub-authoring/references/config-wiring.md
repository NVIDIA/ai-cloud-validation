# Config wiring

How manifest, shim, and (optionally) test harness connect.

## Scope

**Default workspace:** `isvctl/configs/providers/my-isv/`

Edit in place — fill `scripts/storage/api.py` TODO blocks and update
`config/storage-provider-manifest.yaml`. Do not copy to `providers/<name>/`
unless the user explicitly requests a handoff folder.

**Test harness:** `my-isv/config/storage.yaml` declares the `storage_manifest`
step (below). Run it plain for the shim checks, or with `--capability kubernetes`
when the user wants k8s-integrated runs.

## Two-artifact model

```text
config/storage-provider-manifest.yaml     # discovery — lists providers
        │
        │ providers[].shim.module (relative path)
        ▼
scripts/storage/api.py                    # MyStorageApi + build_api() (my-isv flat layout)
```

`StorageProviderApiCheck` reads `manifest_path` → loads each Python shim → runs storage api tests.

## manifest_path sources

The suite binds the `storage_provider_api` group to a `storage_manifest` step and
reads `{{ steps.storage_manifest.storage.manifest_path }}`. That step runs
`shared/storage_manifest_to_steps.py`, which resolves `STORAGE_PROVIDER_MANIFEST`
(relative to the config dir; the mounted ConfigMap path in prod) to an absolute
path.

**No `storage_manifest` step = checks skipped (`step_not_configured`).** Onboarded
providers declare the step.

## Provider YAML patterns

One `storage.yaml` per provider imports `suites/storage.yaml` (never alongside
`suites/k8s.yaml`) and declares the step, as in `my-isv/config/storage.yaml`:

```yaml
commands:
  storage:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: storage_manifest
        phase: test
        command: "python ../../shared/storage_manifest_to_steps.py"
        env:
          STORAGE_PROVIDER_MANIFEST: "storage-provider-manifest.yaml"
```

The same config runs the Kubernetes CSI/filesystem checks with
`--capability kubernetes`; its `setup` step (`requires: [kubernetes]`) reports
the cluster's StorageClasses.

## Manifest entry fields (schema v1alpha2)

For the full discover-vs-ask, field-by-field walkthrough see
[manifest-generation.md](manifest-generation.md). Summary shape:

```yaml
schema_version: v1alpha2
providers:
  - name: <unique-provider-name>       # subtest tag (derived from identity.provider.id if omitted)
    type: file                          # or block (derived from identity.storage_type if omitted)
    tenant_id: <optional-default>       # omit if shim resolves at runtime; quote numeric IDs

    identity:                           # static handoff identity (contract for shim providers)
      provider: { domain: <dns>, id: <label>, version: <semver> }
      backend: { vendor: <name>, version: <ver> }   # optional
      storage_type: file                # file | block
      storage_protocol: nfsv4           # lustre | nfsv4 | nfsv3 | smb | nvme | iscsi | ...

    shim:                               # omit entirely for providers with no mgmt API
      kind: python
      module: ../scripts/storage/api.py

    capabilities:                        # CONTRACT for shim providers (source of truth; probed at runtime)
      tenantManagement: native|default|none      # or { list, get, getQuota }
      volumeManagement:                          # state, or per-leaf:
        list: native|default|none
        get: native|default|none
        create: native|default|none
        delete: native|default|none
      quotaManagement: native|default|none       # or { directory: {...}, user: {...} }

    attributes:                          # informational — shim reads env vars
      <key>: <value>
```

`shim.module` resolves relative to the manifest file's parent directory.
`v1alpha1` manifests still load. Declared `identity`/`capabilities` are
cross-checked against the live shim and **fail on mismatch** — only declare what
the shim reports, or omit to skip the check. The manifest drives
`StorageProviderApiCheck` ONLY; it carries no CSI / topology / protocol data.

## CSI / filesystem alignment (config-driven, not from the manifest)

The `k8s_storage` CSI checks and `k8s_filesystem` checks are config-driven, like
every other suite check — they do NOT read the storage manifest. Checks in
`isvctl/configs/suites/storage.yaml` include `K8sCsiStorageTypesCheck`,
`K8sCsiStorageQuotaApiCheck`, `K8sCsiTenantScopedCredentialsCheck`,
`K8sCsiProvisioningModesCheck`, `K8sCsiConcurrentPvcCheck`,
`K8sCsiPvcExpandCheck`, `K8sCsiDriverHealthCheck`, and the `K8sFile*` /
`K8sNfsMountOptionsCheck` / `K8sNodeKernelModulesCheck` filesystem checks.

Set their StorageClass names via the `K8S_CSI_*` env vars (the suite templates
read these), or as literal overrides in the provider `storage.yaml`.
Resolution order is **explicit YAML → `K8S_CSI_*` env var → skip**.

Env vars (one per role):

- `K8S_CSI_BLOCK_SC`
- `K8S_CSI_SHARED_FS_SC`
- `K8S_CSI_NFS_SC`

NFS mount-option expectations (`K8sNfsMountOptionsCheck`), `node_selector`, and
`kernel_modules` (`K8sNodeKernelModulesCheck`) are likewise literal values in the
config (a check skips when its inputs are unset). See
`isvctl/configs/providers/vast/config/storage.yaml` for an example that sets
the VAST NFS expectations and documents the `K8S_CSI_*` exports.

**Agent task:** After `kubectl get storageclass`, map SC names into the
`K8S_CSI_*` env vars (or literal overrides in the provider `storage.yaml`).
The storage manifest stays focused on the shim contract.

## Directory-quota enforcement check

`StorageDirectoryQuotaEnforcementCheck` (suite `storage_provider_api`) needs a reachable
cluster and a shared-fs StorageClass (or an existing PVC). Optional reuse keys
avoid re-provisioning on every iteration:

| Key | Effect |
| --- | ------ |
| `pvc_name` / `pvc_namespace` | Reuse a bound RWX PVC |
| `pod_name` | Reuse a Ready pod that mounts that PVC at `/data` (requires `pvc_name`) |

See `vast/scripts/storage/README.md` or `weka/scripts/storage/README.md` for
override YAML examples.

## ConfigMap / Secret handoff (production)

Document for customer ops; validation suite reads files directly in Phase 1a.

| K8s object | Content | Default name |
| ---------- | ------- | ------------ |
| ConfigMap | `manifest.yaml` | `storage-provider-manifest` |
| ConfigMap | `api.py` (+ optional `config.yaml`) | provider-chosen per provider |
| Secret | API token / cloud creds | provider-chosen per provider |

Runtime: mount manifest → set `manifest_path` to mount path; inject Secret as env vars the shim reads.

## Demo mode

While scaffolding:

```bash
ISVCTL_DEMO_MODE=1
```

Shim returns dummy data; useful before real API credentials exist.
