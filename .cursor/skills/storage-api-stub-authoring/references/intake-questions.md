# Intake questionnaire

Ask these **before** writing code. Batch related questions; skip sections that
don't apply (e.g. skip K8s block for bare-metal-only).

Record answers in an "Implementation profile" the user can confirm.

## Default workspace (read first)

**Implement in place under `isvctl/configs/providers/my-isv/`** — do not copy
files to a new provider directory unless the user explicitly asks for a separate
handoff folder (e.g. `providers/weka/`).

| File | Action |
| ---- | ------ |
| `scripts/storage/api.py` | Replace `TODO` blocks in the existing `MyStorageApi` class |
| `config/storage-provider-manifest.yaml` | Update `providers[]`, `shim.module`, `csi`, `attributes` |
| `config/storage.yaml` | Add a `storage_manifest` step only when the user wants to run the shim checks |
| `scripts/storage/README.md` | Document env vars when the stub is complete |

Read AWS/VAST shims as **reference patterns only** — do not copy them into new
paths. The flat `my-isv/scripts/storage/api.py` layout is the authoring target.

**Scope reminder:** intake informs the manifest + `api.py` in `my-isv` only.
Do not plan network/vm/k8s provider scaffolding unless the user explicitly
expands scope.

---

## A. Customer and deployment context

1. **Provider identity** — `providers[].name` and `properties()` values for this
   backend (stays in `my-isv`; not a new directory slug).
2. **Storage product** — vendor + protocol (NFS, Lustre, block, …)?
3. **Deployment model**
   - Managed Kubernetes (CSI provisions PVCs)?
   - Bare-metal / standalone management host?
   - Hybrid (K8s + external management API)?
4. **Run StorageProviderApiCheck now?** If yes, use existing
   `my-isv/config/storage.yaml` (update in place if needed).
5. **Can I run discovery commands?** (kubectl, cloud CLI, curl to management API)
   - If yes: which context/credentials are available?
   - If no: ask user to paste `kubectl get sc`, management API sample response
6. **Separate provider folder?** Default **no** — only create
   `providers/<name>/` when the user explicitly wants a production handoff copy.

---

## B. CSI and volume lifecycle (drives create/delete vs list)

7. **Is storage managed by a CSI driver on the target cluster?**
   - If yes → `create_volume` / `delete_volume` → `NotSupportedError` (AWS/VAST pattern)
   - If no → implement full create/delete lifecycle
8. **CSI driver name** (`provisioner` field) — e.g. `fsx.csi.aws.com`, `scp.weka.io`
9. **StorageClass name(s)** to validate — set via the `K8S_CSI_*` env vars / config overrides (not the manifest)
10. **Volume handle format** in PV `spec.csi.volumeHandle` — ID vs path vs UUID?
11. **Are any CSI-provisioned volumes already present?** CSI fallback needs ≥1 visible volume
12. **fsType and mount options** the driver expects (for future mount checks)

---

## C. Management API and tenancy

13. **Management API type** — REST, cloud SDK (boto3), CLI wrapper, gRPC?
14. **Endpoint URL** — reachable from where tests run (laptop vs in-cluster runner pod)?
15. **Authentication** — API token, basic auth, IAM/IRSA, OAuth?
16. **Tenant isolation model**
    - AWS account ID (STS)?
    - Named tenant header (VAST `X-Tenant-Name`)?
    - Organization name (WEKA)?
    - Project/subscription/SVM ID?
17. **Single-tenant or multi-tenant?** Multi → one manifest `providers[]` entry per tenant/path
18. **Default tenant** — env var name (e.g. `STORAGE_TENANT_ID`, map in `__init__`)

---

## D. Quota semantics (get_tenant_quota)

19. **Where does `hard_limit_bytes` come from?**
    - Service quota (AWS)?
    - Parent directory quota (VAST root path)?
    - Filesystem budget (WEKA `total_budget`)?
    - License/capacity pool?
20. **What metric is `used_bytes`?** Allocated vs consumed vs effective
21. **Minimum quota** — must be > 0 for N-021 to pass
22. **Storage root path** — e.g. `VAST_STORAGE_PATH`, WEKA filesystem name, export path

---

## E. Volume inventory (list_volumes)

23. **How are volumes enumerated?** API list, quota tree children, filesystem IDs, CRD inventory
24. **Volume ID format** — stable identifier returned to callers
25. **Tag/label support?** If none, `tag_filters` never match (VAST pattern)
26. **State mapping** — backend states → `creating` | `available` | `failed` | `deleting`
27. **Cross-tenant leakage** — must never return volumes outside resolved tenant

---

## F. Future capabilities (document now, implement later)

28. **Directory quotas** — supported? Lustre-style vs VAST-style semantics?
29. **User quotas** — supported?
30. **Block storage** — separate provider entry with `type: block`?

---

## G. K8s config alignment

31. **Which k8s suite config** will import? (`isvctl/configs/suites/k8s.yaml` via a provider `storage-k8s.yaml`?)
32. **StorageClass → role mapping** (set via `K8S_CSI_*` env vars or literal config overrides; NOT the manifest)
    - block → `K8S_CSI_BLOCK_SC`
    - shared FS (incl. Lustre / other parallel FS) → `K8S_CSI_SHARED_FS_SC`
    - NFS → `K8S_CSI_NFS_SC`
33. **Which CSI checks matter for this backend?** (`K8sCsiStorageTypesCheck`, `K8sCsiProvisioningModesCheck`, …)
34. **Node selector / tolerations** for test runner pods? (e.g. `dedicated=system-workload`)
35. **NFS mount-option expectations** (`K8sNfsMountOptionsCheck`: version / proto / nconnect) — read off a live mount
36. **ConfigMap mount path** in production for `manifest_path` (default: `storage-provider-manifest`)

---

## H. Credentials and handoff

37. **Env var names** for endpoint, credentials, storage path (document in `scripts/storage/README.md`)
38. **Secret keys** for cluster handoff (map 1:1 to env vars)
39. **IAM actions** (AWS-style) or API scopes required — keep minimal
40. **TLS** — custom CA, insecure skip (dev only)?

---

## Quick routing from answers

| Answer | Implementation path |
| ------ | ------------------- |
| Default | Edit `my-isv/scripts/storage/api.py` TODO blocks + manifest in place |
| CSI + dynamic provisioning | AWS/VAST pattern; focus on `list_volumes` |
| API-managed volumes | Implement create/delete; keep `DEMO_MODE` until creds exist |
| API only in-cluster | `kubectl run isvctl-runner` pod pattern (see provider READMEs) |
| Multi tenant/path | Multiple `providers[]` entries in `my-isv` manifest |
| No existing PVCs | User must create one before N-020 passes |
| User wants prod handoff | Only then copy finished shim to `providers/<name>/` |
