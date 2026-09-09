<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# my-isv storage shim

Storage Provider Shim - one Python file per backend storage
provider implementing the [`StorageApi`](../../../../../../isvtest/src/isvtest/core/storage_provider/api.py)
ABC. Drives the storage acceptance subsets that
the [`StorageProviderApiCheck`](../../../../../../isvtest/src/isvtest/validations/storage_provider.py)
validation runs.

## The two pieces

```text
config/storage-provider-manifest.yaml   <- declares the provider(s)
                                            (see the JSON schema)
                  │
                  ▼ providers[].shim.module points at
scripts/storage/api.py                  <- StorageApi subclass +
                                            build_api() factory
```

The manifest is the discovery surface. `StorageProviderApiCheck` reads
the manifest from `manifest_path`, iterates each `providers[]` entry
with a `shim:` block, calls `build_api()`, and runs N-019/N-020/N-021
against the returned `StorageApi`. CSI-only providers (no `shim:` block)
are skipped at the shim layer and exercised by the existing `K8sCsi*`
checks instead.

## Manifest structure (schema v1alpha2)

The manifest describes the storage provider's shim contract — it drives
`StorageProviderApiCheck` only. At the package level it carries `namespace`
(the DNS domain you control) and optional `vendor` metadata; with each
provider's `id` the namespace forms the registration key `<namespace>/<id>`.
Per provider it carries:

- `provider` — implementor identity block (`name`, `description`, `type`,
  `protocols`, `version`) mirroring `ProviderProperties` / `VersionMetadata`.
  `type` + `protocols` + `version` are cross-checked against the shim's
  `properties()`. Top-level `name`/`type` are derived from it when omitted.
- `backend` — optional metadata for the storage system fronted (its `version`
  is an opaque vendor passthrough).
- `shim` — `kind` + `module` (plus production `configmap`/`credentials_secret`).
- `capabilities` — a hierarchical `native | default | none` block
  (`tenantManagement`, `volumeManagement`, `quotaManagement`).

StorageClass names, NFS mount-option expectations, node selectors, and kernel
modules are NOT in the manifest: the `K8sCsi*` / `K8sFilesystem*` checks read
them from the suite/provider config or the `K8S_CSI_*` env vars.

### The manifest is a contract

Declared identity (the `provider` block) and `capabilities` are not
documentation: for providers
with a `shim:` block, `StorageProviderApiCheck` cross-checks each declared
value and **fails on a mismatch** (a `manifest-consistency[<name>]` subtest,
plus `volumeManagement.create` enforced in `volume-provisioning` and
`tenant_id` in `tenant-quota`). Identity fields are checked against the shim's
`properties()`; each capability declared **supported** is **probed at
runtime** — the shim must not raise `NotSupportedError` / `NotImplementedError`
for it. So you can't declare `provider.protocols: [lustre]` or
`quotaManagement.directory: native` unless the shim actually backs it. Omit a
field to leave it unchecked; declare a surface `none` to leave it unprobed.

Two artifacts ship beside this README under `../../config/`:

- `storage-provider-manifest.yaml` — the **blank template** (one working
  provider, heavily commented). Copy and fill it in.
- `storage-provider-manifest.example.yaml` — a **fully-populated** multi-
  provider reference showing every field.

The schema is `isvctl/schemas/storage-provider-manifest.schema.json`.
`v1alpha1` manifests still load.

## Driving the tests

The manifest drives `StorageProviderApiCheck` ONLY. `storage_manifest_to_steps.py`
(`../../../shared/`) resolves the manifest path in a setup step and emits
`steps.setup.storage.manifest_path`, which the check loads in-process.

The CSI / NFS / POSIX filesystem checks are config-driven like every other suite
check: set their StorageClass names via the `K8S_CSI_*` env vars (or literal
overrides in your config). Resolution is **explicit YAML → `K8S_CSI_*` env var →
skip**. Two configs wire this up:

- `../../config/storage.yaml` — bare-metal shim-only run (no cluster):

  ```bash
  ISVCTL_DEMO_MODE=1 \
    uv run isvctl test run -f isvctl/configs/providers/my-isv/config/storage.yaml
  ```

- `../../config/storage-k8s.yaml` — runs the Kubernetes storage suite against an
  existing cluster (StorageProviderApiCheck from the manifest; CSI/filesystem
  checks from `K8S_CSI_*` env vars):

  ```bash
  export K8S_CSI_SHARED_FS_SC=my-isv-rwx
  uv run isvctl test run \
      -f isvctl/configs/providers/my-isv/config/storage-k8s.yaml \
      -- -k "K8sCsi or K8sFile or K8sNfs or StorageProviderApi"
  ```

## Authoring checklist

1. **Backend connection.** Edit `MyStorageApi.__init__` in
   [`api.py`](api.py) to read your endpoint URL / credentials from
   environment variables (or `/etc/shim/config.yaml` once the per-provider
   ConfigMap topology lands).
2. **`health_check`** - authenticated round-trip to the management API.
   Raise `AuthenticationError` on 401/403; return `None` on success.
   Drives N-019.
3. **`get_tenant_quota`** - return `TenantQuota(tenant_id, hard_limit_bytes,
   used_bytes, name)`. Drives N-021. The shim's default tenant comes from the
   `STORAGE_TENANT_ID` env var (the knob); the manifest's `tenant_id` is a
   cross-check assertion that must match what `get_tenant_quota` returns.
4. **`list_volumes`** - enumerate volumes in the selected tenant, honoring
   the optional `ids` / `tag_filters` arguments. Never leak foreign-tenant
   volumes regardless of filters.
5. **`create_volume` / `delete_volume`** (optional, off by default) - the
   scaffold ships these raising `NotSupportedError` with
   `volumeManagement.create` / `.delete` declared `none`: the managed-K8s
   default where the CSI driver owns provisioning, so the suite falls back to
   inventorying existing volumes via `list_volumes` (N-020). Implement both and
   flip those two entries to `native` only if your shim provisions volumes
   itself.
6. **Directory / user quota methods** (optional) - the scaffold ships
   these as stubs that raise `NotSupportedError`. Implement the ones your
   backend backs and declare the matching surface `native` in the
   manifest's `quotaManagement` block; leave the rest as raising stubs and
   declare them `none`. Validation probes each surface declared supported
   and fails if it raises the sentinel. Attach L2 qualifiers via the
   `capability_qualifiers()` hook.

Every method has a `TODO` block marking the spot to replace.

## Demo mode

The skeleton ships with a `DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE")
== "1"` gate on the implemented surfaces (`health_check`, `get_tenant_quota`,
`list_volumes`): real runs raise `NotImplementedError` /
`AuthenticationError` to make it obvious the backend isn't wired up; demo runs
return dummy data so the validation passes end-to-end. The optional
`create_volume` / `delete_volume` stay raising `NotSupportedError` either way
(volume-provisioning then reports a clean skip). To exercise the storage check in demo mode:

```bash
ISVCTL_DEMO_MODE=1 \
  uv run isvctl test run -f isvctl/configs/providers/my-isv/config/storage.yaml
```

## What ships at handover

Once your `api.py` is real, the production delivery bundles:

- The manifest (`config/storage-provider-manifest.yaml`) into a well-known
  ConfigMap (default name `storage-provider-manifest`).
- Each provider's `api.py` (plus optional `config.yaml`) into a
  provider-chosen per-provider ConfigMap.
- Per-provider credentials into a provider-chosen Secret.

The future operator sidecar (out of scope for this iteration) will load
the same `api.py` from its ConfigMap mount and serve the canonical REST
contract on `localhost:9090`.
