# Manifest generation (discover, then ask)

How to fill `config/overlays/storage-provider-manifest.yaml` (schema **v1alpha2**) by
**probing the environment first** and **asking the customer only for what you
can't observe**. Discovery proposes values; Q&A confirms intent and fills gaps.

The manifest drives `StorageProviderApiCheck` ONLY: it imports each
`shim.module` and exercises the `StorageApi` contract — and **cross-checks the
declared `identity`/`capabilities` against the live shim, failing on mismatch**
(`manifest-consistency[<name>]`). It carries no CSI / topology / protocol data;
the `K8sCsi*` / `K8sFilesystem*` checks get their StorageClass names and
expectations from the suite/provider config or the `K8S_CSI_*` env vars (see
[config-wiring.md](config-wiring.md)).

So: **don't declare what you can't back up.** Discover it, or ask, or omit it
(omitted keys skip the dependent check cleanly rather than failing).

Two files ship beside the template:

- `config/overlays/storage-provider-manifest.yaml` — the blank template to fill (edit in place).
- `config/overlays/storage-provider-manifest.example.yaml` — a fully-populated multi-provider reference.
- Schema: `isvctl/schemas/storage-provider-manifest.schema.json`.

---

## Workflow

```text
1. Discover  → run read-only probes (kubectl / cloud CLI / curl), draft values  → verify: probes return data
2. Draft     → write a candidate manifest, mark every guessed field [ASSUMED]   → verify: it parses (adapter below)
3. Ask       → confirm assumptions + fill un-discoverable fields via Q&A        → verify: customer signs off on profile
4. Validate  → re-run the adapter + probe_shim.py                               → verify: storage classes / shim resolve
```

**Ask permission before running any discovery command.** If denied, paste the
relevant Q&A prompts and ask the customer to run them and share output.

### Validate the draft as you go

The manifest adapter parses the manifest **without loading any shim** (no
backend calls), so it is the fastest correctness loop while drafting:

```bash
STORAGE_PROVIDER_MANIFEST=isvctl/configs/providers/my-isv/config/overlays/storage-provider-manifest.yaml \
  uv run python isvctl/configs/shared/storage/storage_manifest_to_steps.py | uv run python -m json.tool
```

Confirm the emitted `storage.manifest_path` resolves to your manifest. (The
adapter only resolves the path for `StorageProviderApiCheck`; it does not load
the shim.) For the full contract check, run `probe_shim.py` (Phase 4).

---

## Field-by-field: discover vs. ask

Each section lists **how to observe the value** and **what to ask** when it
isn't observable. Quote AWS account IDs / numeric tenant IDs to keep YAML from
stripping leading zeros.

### Provider identity (`name`, `type`, `tenant_id`, `identity`)

| Field | Discover | Ask if not discoverable |
| ----- | -------- | ----------------------- |
| CSI provisioner (a hint for id/vendor inference, not a manifest field) | `kubectl get sc <name> -o jsonpath='{.provisioner}'`; `kubectl get csidriver` | "Which CSI driver / management product fronts this storage?" |
| `identity.provider.domain` / `.id` | Infer from the driver name (e.g. `fsx.csi.aws.com` → `aws.amazon.com` / `fsx-lustre`) | "What DNS-form domain + short id should identify your shim? (forms the `<domain>/<id>` key)" |
| `identity.provider.version` / `.vendor` / `.name` | `kubectl get csidriver <d> -o yaml` annotations; vendor docs | "Shim semver + vendor display name?" |
| `identity.backend.*` | Backend API/version endpoint (see `health_check` call); product banner | "What storage system + version sits behind the shim?" |
| `identity.storage_type` (`file`/`block`) | Provisioner family (FSx/NFS/Lustre → file; EBS/iSCSI/NVMe → block); `kubectl get sc <name> -o jsonpath='{.parameters}'` | "Is this a filesystem (RWX) or block (RWO) provider?" |
| `identity.storage_protocol` | Inspect a mounted PV: `kubectl exec <pod> -- cat /proc/mounts` (nfs vers, lustre); SC `parameters` | "Wire protocol: lustre / nfsv4 / nfsv3 / smb / nvme / iscsi / nvme-of / fc?" |
| `tenant_id` | Backend API tenant list; cloud account (`aws sts get-caller-identity`); VAST `X-Tenant-Name` | "Default backend tenant the checks should target?" |

`name`/`type` are derived from `identity.provider.id` / `identity.storage_type`
when omitted — but set them explicitly; `name` is the subtest tag.

### Shim (`shim`)

| Field | Discover | Ask if not discoverable |
| ----- | -------- | ----------------------- |
| `shim.kind` | Phase 1a is always `python` (in-process). `rest` is declared-but-skipped. | — |
| `shim.module` | Keep `../../scripts/storage/api.py` (relative to `config/overlays/`) | — |
| `shim.configmap` / `credentials_secret` | Production handoff names (not needed for local runs) | "ConfigMap/Secret names ops will mount in-cluster?" (informational) |

**Omit the entire `shim:` block for providers with no management API** — the
shim subtests skip for them.

> StorageClass names, NFS mount-option expectations, node selectors, and kernel
> modules are NOT manifest fields. The `K8sCsi*` / `K8sFilesystem*` checks read
> them from the suite/provider config or the `K8S_CSI_*` env vars — see
> [config-wiring.md](config-wiring.md).

### Capabilities (`capabilities`)

These are a **contract**, not documentation — and the manifest is the
**single source of truth** for which surfaces are supported. For providers
with a shim, `StorageProviderApiCheck` probes each capability declared
supported at runtime and fails if the shim raises `NotSupportedError` /
`NotImplementedError` for it. So set them from the shim implementation
decision, not a guess.

The block is hierarchical (`native` | `default` | `none`): a group state
cascades to its leaves; a leaf overrides its group. `native`/`default` mean the
surface is serviceable (the check probes it and expects no sentinel raise);
`none` opts out (the surface is not probed — leave the method as a raising
stub or base raise).

| Capability | Determined by | Notes |
| ---------- | ------------- | ----- |
| `volumeManagement.create` / `.delete` | CSI-owned lifecycle → `none` (raise `NotSupportedError`); API-owned → `native` | Enforced in `volume-provisioning` |
| `volumeManagement.list` / `.get` | Whether the shim enumerates / fetches volumes | usually `native` |
| `tenantManagement.list` / `.get` / `.getQuota` | Whether the shim enumerates tenants / fetches a tenant / reports its quota | `getQuota` usually `native` |
| `quotaManagement.directory.*` / `.user.*` | Whether the shim implements the directory / user quota methods | Deferred surface; usually `none` in Phase 1a |

Omit a capability to leave it unchecked while the shim is still stubbed.

---

## When you can't run discovery

If the user can't grant cluster/API access, send this copy-paste block and ask
them to return the output, then map it into the manifest (identity / capabilities)
and your test config (StorageClass names via `K8S_CSI_*`):

```bash
kubectl get storageclass -o wide
kubectl get csidriver
kubectl get sc <each-name> -o yaml          # provisioner + parameters + mountOptions
```

Fall back to [intake-questions.md](intake-questions.md) for everything the
output doesn't reveal (tenant model, quota source, identity vendor/version).

---

## Draft → confirm loop

1. Run discovery, fill the manifest, tag each guessed value with a `# ASSUMED`
   comment.
2. Run the adapter (above) and `probe_shim.py` (Phase 4) — fix anything that
   resolves empty/wrong.
3. Present an "Implementation profile" listing every `# ASSUMED` value and ask
   the customer to confirm or correct in one pass.
4. Remove the `# ASSUMED` tags once confirmed.
