---
name: storage-api-stub-authoring
description: >-
  Guides implementation of Storage Provider shims in-place under
  isvctl/configs/providers/my-isv/ — fill TODO blocks in scripts/storage/api.py
  and edit storage-provider-manifest.yaml. Does not copy to new provider dirs
  or scaffold full provider trees. Walks through each method with pointed
  questions and runs isolated sanity probes. Use when the user mentions storage
  shim, StorageApi, storage-provider-manifest, StorageProviderApiCheck, or
  storage provider stubs.
compatibility: >-
  Requires this repo. Run commands from repo root after `uv sync`. Live
  probes need network/credentials the user grants (kubectl, cloud APIs, VMS
  REST).
---

# Storage API Stub Authoring

Interactive workflow for implementing a provider's storage shim. Follow
this skill end-to-end; do not skip intake or sanity gates.

## Scope boundaries (read first)

**Default workspace:** `isvctl/configs/providers/my-isv/`

Implement by **filling in the existing stubs** — replace `TODO` blocks in
`scripts/storage/api.py` and update the sibling manifest. **Do not** `cp` or
`cp -r` my-isv to `providers/<customer>/` unless the user explicitly requests
a separate handoff folder.

| In scope (my-isv) | Path |
| ----------------- | ---- |
| Required | `scripts/storage/api.py` — edit `MyStorageApi` TODO blocks in place |
| Required | `config/storage-provider-manifest.yaml` |
| Harness (exists) | `config/storage.yaml` — tweak in place if needed for tests |
| Optional | `scripts/storage/README.md` (document env vars when stub is done) |

AWS/VAST/weka paths under `providers/aws/`, `providers/vast/`, etc. are
**read-only references** — study them; do not copy their layout into new dirs.

**Out of scope — do not create, copy, or edit unless the user explicitly asks:**

- New provider directories (`providers/<customer>/`)
- Other `config/*.yaml` or `scripts/**` trees in my-isv (if any exist)
- Full-provider scaffold (`cp -r my-isv …`)

K8s `manifest_path` overrides in external eks configs are optional test wiring
only when the user wants k8s-integrated runs beyond `my-isv/config/storage.yaml`.

## Outcomes

By session end the user should have:

1. `config/storage-provider-manifest.yaml` declaring the provider and pointing at the shim
2. `scripts/storage/.../api.py` implementing `StorageApi` + `build_api()`
3. Documented env vars in `scripts/storage/README.md` (when stub is complete)
4. Passing isolated probes (`probe_shim.py`) before any full `isvctl test run`
5. *(Optional, on request)* `manifest_path` wired into `storage.yaml` or a k8s override

## Reference implementations (read before coding)

| Backend | Shim | Manifest | Config |
| ------- | ---- | -------- | ------ |
| AWS FSx Lustre | `isvctl/configs/providers/aws/scripts/storage/fsx-lustre/api.py` | `aws/config/storage-provider-manifest.yaml` | `aws/config/eks.yaml` |
| VAST NFS | `isvctl/configs/providers/vast/scripts/storage/vast/api.py` | `vast/config/storage-provider-manifest.yaml` | `vast/config/storage.yaml` |
| WEKA | `isvctl/configs/providers/weka/scripts/storage/weka/api.py` | `weka/config/storage-provider-manifest.yaml` | `weka/config/storage.yaml` |
| **Authoring target (edit in place)** | `my-isv/scripts/storage/api.py` | `my-isv/config/storage-provider-manifest.yaml` | `my-isv/config/storage.yaml` |

Contract: `isvtest/src/isvtest/core/storage_provider/api.py` (per-method
docstrings cover inputs/outputs, error conditions, and capability/qualifier semantics)
Check drivers:
- `isvtest/src/isvtest/validations/storage_provider.py` (`StorageProviderApiCheck`)
- `isvtest/src/isvtest/validations/storage_quota_enforcement.py`
  (`StorageDirectoryQuotaEnforcementCheck` — live CRUD + write enforcement; needs K8s)

Manifest (schema v1alpha2):

- Schema: `isvctl/schemas/storage-provider-manifest.schema.json`
- Fully-populated example: `my-isv/config/storage-provider-manifest.example.yaml`
- Manifest → step adapter: `isvctl/configs/providers/shared/storage_manifest_to_steps.py`
  (parses the manifest, loads no shim; drives the k8s suite via `storage-k8s.yaml`)

## Session workflow

Copy this checklist and update as you go:

```text
Progress:
- [ ] Phase 0: Intake + environment permission (discover what's there)
- [ ] Phase 1: Confirm my-isv files
- [ ] Phase 2: Generate manifest (discover + Q&A) + config wiring
- [ ] Phase 3: Implement StorageApi methods (one at a time)
- [ ] Phase 4: Isolated sanity probes
- [ ] Phase 5: Targeted StorageProviderApiCheck run
- [ ] Phase 6: Full suite guidance (optional)
```

---

## Phase 0: Intake and environment access

**Ask permission before any discovery command.** If granted, run probes
from repo root. If denied, ask the user to paste command output.

Work through [intake-questions.md](references/intake-questions.md) first.
Summarize answers in a short "Implementation profile" before writing code.

### Environment discovery (when permitted)

Discovery is the **first half of manifest generation**: probe the cluster/API to
propose field values, then confirm the rest by Q&A. The full discover-vs-ask
mapping for every manifest field is in
[manifest-generation.md](references/manifest-generation.md).

**Kubernetes** (adjust namespace/context as needed):

```bash
kubectl config current-context
kubectl get storageclass -o wide
kubectl get sc <sc-name> -o yaml    # provisioner, parameters, mountOptions
kubectl get csidriver
kubectl get nodes --show-labels    # -> node_selector for the CSI/filesystem checks
kubectl get pods -A | grep -i csi  # driver health hint + which nodes run node plugin
```

Map findings into the **v1alpha2 manifest** (which drives
`StorageProviderApiCheck` ONLY):

- `providers[].provider.*` / `identity.*` (infer vendor/protocol/type from the
  driver; ask for version)
- `providers[].capabilities.*` (what the shim's `properties()` reports)
- `providers[].shim.*` (module path / configmap / credentials_secret)

The StorageClass names, NFS expectations, and node selectors are NOT in the
manifest: the CSI / filesystem checks (`K8sCsiStorageTypesCheck`,
`K8sCsiDriverHealthCheck`, `K8sFile*`, `K8sNfsMountOptionsCheck`, …) read them
from the suite/provider config or the `K8S_CSI_*` env vars (see
[config-wiring.md](references/config-wiring.md)).

**Network locality:** If the management API is only reachable in-cluster (VAST VMS pattern), note that `isvctl deploy run` may be required instead of laptop-side `isvctl test run`.

---

## Phase 1: Work in my-isv

Open the existing files — they already contain `TODO` markers and `DEMO_MODE`:

```text
isvctl/configs/providers/my-isv/
├── config/storage-provider-manifest.yaml   # shim.module → ../scripts/storage/api.py
├── config/storage.yaml                     # manifest_path already set
└── scripts/storage/api.py                  # MyStorageApi — fill each TODO block
```

**Do not** create `providers/<customer>/` or copy these files elsewhere unless prompted by the user.

Before coding:

1. Read `scripts/storage/api.py` end-to-end — every method has a `TODO` block to replace
2. Read AWS/VAST reference shims for patterns matching intake answers
3. Update `config/storage-provider-manifest.yaml` (v1alpha2): `providers[].name`, `identity`/`provider`, `shim`, `capabilities` — see [manifest-generation.md](references/manifest-generation.md)
4. Keep `shim.module: ../scripts/storage/api.py` (flat my-isv layout)
5. Keep `DEMO_MODE` until real backend calls land — enables smoke without credentials

Rename `MyStorageApi` / `properties()` identity fields to match the backend; keep
`build_api()` at module level.

---

## Phase 2: Manifest generation and config wiring

Generate `config/storage-provider-manifest.yaml` (schema **v1alpha2**) using the
**discover-then-ask** workflow in
[manifest-generation.md](references/manifest-generation.md): probe the
environment to propose field values, ask the customer for what you can't
observe, and omit (don't guess) anything still unknown so the dependent check
skips cleanly. Then wire `manifest_path` into a test config **only when the user
wants to run checks** — see [config-wiring.md](references/config-wiring.md).

Two files ship beside the template — use them as you fill the blank one:

- `config/storage-provider-manifest.yaml` — blank template to edit in place
- `config/storage-provider-manifest.example.yaml` — fully-populated v1alpha2 reference (every field)

**The manifest is a contract, not docs.** For providers with a `shim:` block,
`StorageProviderApiCheck` cross-checks declared `identity`/`capabilities` against
the running shim and **fails on mismatch** (`manifest-consistency[<name>]`). Only
declare what the shim actually reports; omit a field to leave it unchecked.

**Validate the draft without a backend** (parses the manifest, loads no shim):

```bash
STORAGE_PROVIDER_MANIFEST=isvctl/configs/providers/my-isv/config/storage-provider-manifest.yaml \
  uv run python isvctl/configs/providers/shared/storage_manifest_to_steps.py | uv run python -m json.tool
```

**Critical couplings:**

| Artifact | Purpose |
| -------- | ------- |
| `shim.module` | Relative to manifest parent; must resolve to `api.py` with `build_api()` |
| `manifest_path` in provider YAML | Path passed to `StorageProviderApiCheck` (on-disk now; ConfigMap mount path in prod) |
| Env vars in shim `__init__` | Runtime config — manifest `attributes` are **informational only** |
| `K8S_CSI_*` env vars / config overrides | StorageClass names for the CSI/filesystem checks — set in config/env, NOT the manifest |

**K8s override pattern** (from `aws/config/eks.yaml`):

```yaml
tests:
  validations:
    k8s_storage:
      checks:
        StorageProviderApiCheck:
          manifest_path: "isvctl/configs/providers/my-isv/config/storage-provider-manifest.yaml"
```

**ConfigMap handoff** (document for customer, do not implement operator):

- Manifest → ConfigMap `storage-provider-manifest`, key `manifest.yaml`
- Shim `api.py` → per-provider ConfigMap referenced by `shim.configmap`
- Credentials → per-provider Secret; env vars at runtime

---

## Phase 3: Implement methods (one at a time)

Follow [method-walkthrough.md](references/method-walkthrough.md). **Implement and
probe each method before moving on.** Ask the pointed questions listed there.

### Decision tree: volume lifecycle

```text
Does a CSI driver dynamically provision PVCs on this cluster?
├─ YES (managed K8s — AWS FSx, VAST, WEKA pattern)
│    → create_volume / delete_volume raise NotSupportedError
│    → list_volumes MUST return existing CSI-provisioned volumes (count ≥ 1 for N-020 pass)
│    → Populate Volume.csi (driver + volume_handle) when discoverable
└─ NO (bare-metal / API-managed storage)
     → Implement create_volume + delete_volume
     → Tag volumes with isvtest-run-id for orphan sweep
```

### ProviderProperties

Declare capabilities honestly in `storage-provider-manifest.yaml`, and implement
only the corresponding methods in `scripts/storage/api.py`. `new_implementation()`
detects which surfaces are backed and advertises the merged capability set.
Directory-quota shims should also exercise
`StorageDirectoryQuotaEnforcementCheck` under a K8s config (optional
`pvc_name` / `pod_name` reuse for faster iteration — see provider READMEs).

---

## Phase 4: Isolated sanity probes

Run **before** full suite. Order matters.

### 4a. Manifest loads

```bash
uv run python -c "
from isvtest.core.storage import load_provider_registry
ps = load_provider_registry({'manifest_path': 'isvctl/configs/providers/my-isv/config/storage-provider-manifest.yaml'})
print([p.name for p in ps if p.has_shim])
"
```

### 4b. Shim probe script

```bash
uv run python .cursor/skills/storage-api-stub-authoring/scripts/probe_shim.py \
  --manifest isvctl/configs/providers/my-isv/config/storage-provider-manifest.yaml
```

Expect: `health_check` ok, `get_tenant_quota` with `hard_limit_bytes > 0`, and either
`create_volume` path or `list_volumes` returning ≥1 volume (CSI fallback).

### 4c. Backend-specific CLI probes

Use patterns from the reference READMEs:

- **AWS:** `aws sts get-caller-identity`, `aws fsx describe-file-systems`, `aws service-quotas get-service-quota`
- **VAST:** `curl -H "Authorization: Api-Token $VAST_TOKEN" "https://$VAST_ENDPOINT/api/quotas/"`
- **Generic:** whatever authenticated GET the shim's `health_check` uses

### 4d. Demo mode smoke (no credentials, optional)

Uses the existing `my-isv/config/storage.yaml` harness:

```bash
ISVCTL_DEMO_MODE=1 \
  uv run isvctl test run -f isvctl/configs/providers/my-isv/config/storage.yaml
```

### 4e. Contract / validation unit tests

```bash
# Shim contract package (ABC, loader, mock)
uv run pytest isvtest/src/isvtest/core/storage_provider/tests/

# StorageProviderApiCheck behavior (framework tests)
uv run pytest isvtest/tests/test_storage_provider.py

# Provider-specific hermetic shim tests, when added:
uv run pytest isvtest/tests/test_aws_fsx_lustre_shim.py
uv run pytest isvtest/tests/test_weka_shim.py
uv run pytest isvtest/tests/test_storage_quota_enforcement.py
```

**Gate:** Do not proceed to Phase 5 until 4a–4c pass (4d acceptable during scaffolding).

---

## Phase 5: Targeted acceptance check (optional)

Run only when the user wants end-to-end `StorageProviderApiCheck` validation.
Requires an existing `storage.yaml` or a k8s config with `manifest_path` set.

```bash
uv run isvctl test run -f isvctl/configs/providers/my-isv/config/storage.yaml
```

For K8s-integrated runs, add a `StorageProviderApiCheck` override to the
provider's **existing** eks/k8s config — do not create a new full provider tree.

**Expected CSI fallback (not a failure):**

```text
volume-provisioning[<name>] SKIPPED: create_volume not implemented; observed N CSI-provisioned volume(s)
```

**Common failures → fixes:**

| Symptom | Fix |
| ------- | --- |
| `AuthenticationError` on health_check | Fix creds / network / IAM |
| `hard_limit_bytes=0` | Wrong quota source or empty tenant |
| `observed 0 ... via list_volumes` | Create a PVC against the StorageClass first |
| Manifest not found | Run from repo root; fix `manifest_path` |

---

## Phase 6: Full suite (optional)

Only after Phase 5 passes. Import the storage suite
(`isvctl/configs/suites/storage.yaml`, often alongside `suites/k8s.yaml`) and align:

- StorageClass names via the `K8S_CSI_*` env vars (`K8S_CSI_BLOCK_SC`,
  `K8S_CSI_SHARED_FS_SC`, `K8S_CSI_NFS_SC`) or literal
  overrides in your config — these are NOT in the manifest
- `k8s_storage` CSI checks beyond the shim: `K8sCsiStorageTypesCheck`, `K8sCsiStorageQuotaApiCheck`, `K8sCsiProvisioningModesCheck`, `K8sCsiConcurrentPvcCheck`, `K8sCsiPvcExpandCheck`, `K8sCsiDriverHealthCheck`
- `k8s_filesystem` checks (`K8sFileLockingCheck`, `K8sCrossNodeWriteVisibilityCheck`, …) and `node_selector` (literal dict) when CSI requires labeled nodes

```bash
uv run isvctl test run -f isvctl/configs/providers/my-isv/config/<k8s-or-storage-config>.yaml
```

---

## Agent behavior rules

1. **Edit my-isv in place** — fill `scripts/storage/api.py` TODO blocks; never `cp -r my-isv` to a new provider dir unless the user explicitly asks for handoff.
2. **One method at a time** — implement `properties` + `__init__`, then `health_check`, then `get_tenant_quota`, then volume methods.
3. **Ask before assuming** — especially CSI vs API provisioning, tenant model, quota semantics.
4. **Match reference style** — env-driven config, tight IAM/API surface, `NotSupportedError` for CSI-owned lifecycle.
5. **Do not add setup scripts** — this skill does not author provider orchestration steps.
6. **Document env vars** in `scripts/storage/README.md` when the stub is done (in-scope).
7. **Test harness on request** — create `storage.yaml` or k8s `manifest_path` overrides only when the user wants to run checks.

## Additional resources

- [manifest-generation.md](references/manifest-generation.md) — discover-then-ask, field-by-field manifest authoring (v1alpha2)
- [intake-questions.md](references/intake-questions.md) — upfront questionnaire
- [method-walkthrough.md](references/method-walkthrough.md) — per-method questions and acceptance criteria
- [config-wiring.md](references/config-wiring.md) — manifest, overrides, CSI, ConfigMap
- [AWS stub implementation](../../../isvctl/configs/providers/aws/scripts/storage/fsx-lustre/api.py)
- [VAST stub implementation](../../../isvctl/configs/providers/vast/scripts/storage/vast/api.py)
- [WEKA stub implementation](../../../isvctl/configs/providers/weka/scripts/storage/weka/api.py)
