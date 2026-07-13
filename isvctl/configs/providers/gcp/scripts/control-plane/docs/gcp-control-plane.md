# GCP Control Plane Validation Guide

This guide is the operator walkthrough for validating GCP control-plane
connectivity, tenant lifecycle management, and the object-storage data path with
the ISV validation framework. It is the GCP port of the AWS control-plane
provider ([`providers/aws/config/control-plane.yaml`](../../../../aws/config/control-plane.yaml)
+ [`providers/aws/scripts/control-plane/`](../../../../aws/scripts/control-plane/));
the [AWS reference](../../../../../../../docs/references/aws.md) is the canonical
contract and the GCP scripts under `providers/gcp/scripts/control-plane/`
translate it onto Cloud Storage, IAM Admin, and Resource Manager. For the
NCP-level operator contract (auth, project resolution, the shared operator
env-var table) see [`docs/references/gcp.md`](../../../../../../../docs/references/gcp.md).

## Overview

The GCP control-plane validation proves three provider-neutral capabilities:

1. **API Health** (`check_api`, setup) — Resolve Application Default Credentials
   and the project id with `google.auth.default()`, then probe each configured
   service with one read-only call. The resolved project id is the `account_id`;
   the authenticated Cloud Storage probe gates top-level success.
2. **Tenant Lifecycle** (`create_tenant` → `list_tenants` → `get_tenant` →
   `delete_tenant`) — Model a tenant as a Resource Manager **TagValue** under a
   dedicated parent **TagKey**, then create / list / retrieve / delete it.
3. **Object Data Path** (`s3_object_lifecycle`, test, DATASVC-XX-01 / issue #262)
   — Create a temporary Cloud Storage bucket, put a small payload, get it back
   and byte-compare, delete the object, and delete the bucket in the same step.

**Architecture:**

- **Scripts**: GCP-specific (`google-cloud-storage`, `google-cloud-iam`,
  `google-cloud-resource-manager`) — perform CRUD operations, output JSON.
- **Validations**: Platform-agnostic — check JSON output against the suite
  contract ([`suites/control-plane.yaml`](../../../../../suites/control-plane.yaml)).
- **Phases**: Setup → Test → Teardown.

### How each step maps onto GCP

| Step | Phase | AWS oracle | GCP translation |
|------|-------|-----------|-----------------|
| `check_api` | setup | STS `get_caller_identity` + per-service read | `google.auth.default()` for identity/project + Cloud Storage `list_buckets`, IAM Admin `list_service_accounts`, Resource Manager `get_project` read-only probes. Access-denied still proves reachability. |
| `create_tenant` | setup | `resource-groups:CreateGroup` | `TagKeysClient.create_tag_key` (project-parented) then `TagValuesClient.create_tag_value`, each waited to DONE. Emits `tenant_name` = `TagValue.namespaced_name`, `tenant_id` = `TagValue.name`. |
| `list_tenants` | test | `resource-groups:ListGroups` | Resolve `--group-name` with `get_namespaced_tag_value`, then `list_tag_values(parent=...)`; `found_target` is an exact permanent-name match. |
| `get_tenant` | test | `resource-groups:GetGroup` | `TagValuesClient.get_namespaced_tag_value` → `tenant_name` / `tenant_id` / `description`. |
| `s3_object_lifecycle` | test | S3 create-bucket / put / get / delete | Cloud Storage `create_bucket` + `Blob.upload_from_string` / `download_as_bytes` (byte compare) / `delete`, bucket deleted in `finally`. |
| `delete_tenant` | teardown | `resource-groups:DeleteGroup` | Prove presence/absence by exact list readback (`list_tag_keys(parent=projects/<project>)` then `list_tag_values(parent=<key.name>)`), then `delete_tag_value(name=...)` before `delete_tag_key(name=...)`, each waited to DONE. NotFound on an exact delete is idempotent success; a list error (including `PermissionDenied`) is a visible failure, never absence. |

### Subtests exercised

| Validation group | Check | Step |
|------------------|-------|------|
| `api_health` | `FieldExistsCheck` (`account_id`, `tests`) + `FieldValueCheck` (`success`) | check_api |
| `setup_checks` | `TenantCreatedCheck` | create_tenant |
| `tenant_lifecycle` | `TenantListedCheck` | list_tenants |
| `tenant_lifecycle` | `TenantInfoCheck` | get_tenant |
| `s3_object_lifecycle` | `StepSuccessCheck` + `FieldExistsCheck` (`bucket_name`, `object_key`, `operations`) + `CrudOperationsCheck` (`put`, `get`, `delete`) | s3_object_lifecycle |
| `teardown_checks` | `StepSuccessCheck-delete_tenant` | delete_tenant |

## Access-key HMAC lifecycle is not enabled by default

The suite also defines an access-key lifecycle
(`create_access_key` → `test_access_key` → `disable_access_key` →
`verify_key_rejected` → `delete_access_key`). The GCP analog is a Cloud Storage
**HMAC key** created for a service account and used to sign S3-compatible
requests against `https://storage.googleapis.com`.

That lifecycle requires the test principal to create and delete a disposable
service account and to create, inspect, change the state of, authenticate with,
and delete its run-owned HMAC key. A pre-existing shared key cannot prove that
lifecycle and must never be deactivated or deleted by the test.

Therefore the five HMAC steps are **not wired** in
[`config/control-plane.yaml`](../../../config/control-plane.yaml), and their
validations are excluded by name:

```yaml
exclude:
  tests:
    - AccessKeyCreatedCheck
    - AccessKeyAuthenticatedCheck
    - AccessKeyDisabledCheck
    - AccessKeyRejectedCheck
    - StepSuccessCheck-delete_access_key
```

This is a permission precondition, **not** a permanent GCP platform gap. To
enable the HMAC lifecycle, grant the test principal service-account lifecycle
permissions (for example, `roles/iam.serviceAccountAdmin`) and full HMAC-key
control (`roles/storage.hmacKeyAdmin`), and ensure applicable organization
policies permit HMAC key creation and authentication. Re-enable the steps only
after a preflight proves create / get / update / deactivate / reactivate /
delete control over a disposable HMAC key in its owning project. The
provider-native object data path (`s3_object_lifecycle`) stays enabled without
depending on the optional HMAC credential.

## Prerequisites

### Authentication and project

Control-plane services are project-scoped and globally addressed; `--region`
selects only the Cloud Storage bucket location and does not route the global IAM
/ Resource Manager / tag APIs. Supply Application Default Credentials (ADC)
from any supported user or service-account authentication method; the tests do
not require a particular credential-delivery mechanism. For local user ADC:

```bash
gcloud auth application-default login
# Set the ACTIVE project in the gcloud CLI configuration. google.auth.default()
# resolves the project from the Cloud SDK active config ([core]/project), so this
# is the project the stubs receive when --project is not passed:
gcloud config set project my-project
```

The stubs resolve the project through `resolve_project()`, which checks, in
precedence order: an explicit `--project` argument (the provider config wires
none, so this is reserved for ad-hoc invocations), then the `GOOGLE_CLOUD_PROJECT`
environment variable, then `GCLOUD_PROJECT`, and finally the project bundled with
Application Default Credentials via `google.auth.default()` (the active gcloud CLI
configuration set above). The orchestrator forwards your full shell environment to
spawned stubs, so exporting `GOOGLE_CLOUD_PROJECT` (or `GCLOUD_PROJECT`) is a
working per-process override that takes precedence over the ADC-resolved project —
you do not have to mutate your global `gcloud config set project`. Note that
`gcloud auth application-default set-quota-project` only sets the
billing/quota-attribution project written to the ADC file — it does **not** change
the project `google.auth.default()` returns when no project environment variable is
set (google-auth reads that from the Cloud SDK config, per its own `gcloud config
set project` fallback warning). There are **no** control-plane-specific operator
environment variables beyond this standard project-selection set.

### Required APIs

Enable these services on the project:

```bash
gcloud services enable \
    storage.googleapis.com \
    iam.googleapis.com \
    cloudresourcemanager.googleapis.com
```

### Required IAM roles

The principal supplied through ADC, including a service account, needs at
minimum:

- `roles/storage.admin` (or bucket create/list/get + object put/get/delete) for
  `check_api` and `s3_object_lifecycle`.
- `roles/iam.serviceAccountViewer` (list service accounts) for the `check_api`
  IAM probe.
- `roles/resourcemanager.tagAdmin` (create/list/get/delete TagKeys and
  TagValues) for the tenant lifecycle.
- `roles/browser` or `roles/resourcemanager.projectViewer` (get project) for the
  `check_api` Resource Manager probe.

## Quick Start

```bash
# Install dependencies
uv sync

# Run control-plane validation
uv run isvctl test run -f isvctl/configs/providers/gcp/config/control-plane.yaml
```

**Duration**: ~1-2 minutes (Resource Manager tag operations are async).

## Run notes

- **Name collisions.** Every created resource (TagKey, TagValue, bucket) carries
  a run-id suffix, so parallel runs never collide and an operator can group
  artifacts by run id. A run-scoped short-name collision is NOT proof of run
  ownership, so `create_tenant` treats an `AlreadyExists` on either the TagKey or
  the TagValue as a hard create failure: the colliding tag is never adopted or
  reused, and teardown is never pointed at it. A bucket name collision fails the
  create the same way (bucket names are a global namespace, so a collision is not
  proof of run ownership).
- **Async waits.** Resource Manager tag create/delete are long-running
  operations; the create/delete steps block on the operation to DONE before
  emitting a result, and the provider-config step timeouts (300s for
  create/delete/object steps) cover the two-operation wait stacks with margin.
- **Teardown.** `delete_tenant` proves the exact TagValue and its parent TagKey
  by fully consuming a project-scoped `list_tag_keys` then a key-scoped
  `list_tag_values` (the namespaced getters return `PermissionDenied` for both an
  absent and an unreadable resource, so they cannot prove absence). It deletes
  only the exact TagValue named by the forwarded `create_tenant` output, then its
  parent TagKey; a NotFound on an exact delete is idempotent success, and a list
  error (including `PermissionDenied`) stays a visible failure rather than being
  read as absence. An unexpected sibling value under the dedicated key retains the
  parent and fails the step. `s3_object_lifecycle` is self-contained (it deletes
  its own bucket in a `finally` block), so no separate object teardown is wired.

## Troubleshooting

### "project ID not found"

Run `gcloud auth application-default login`, then either export
`GOOGLE_CLOUD_PROJECT=my-project` (forwarded to the stubs and honored ahead of
ADC) or set the active project in the gcloud CLI configuration with `gcloud config
set project my-project` (which `google.auth.default()` reads as the ADC fallback,
distinct from the ADC quota project). Alternatively pass `--project` if the
provider config is extended to wire it.

### `PermissionDenied` on a `check_api` probe

`check_api` treats access-denied as endpoint-reachable (authenticated but not
authorized) and keeps the visible note, so a single denied probe does not fail
the step. Grant the roles above if you want every probe to pass cleanly.

### Tenant create fails with `AlreadyExists`

`create_tenant` rejects an `AlreadyExists` on the TagKey or TagValue as a hard
failure and never adopts the colliding resource — a run-scoped short-name
collision is not proof that the existing tag belongs to this run, and adopting it
could point teardown at a resource this run never created. Do not assume the
colliding tag is run-owned.

Clean up only against a tenant you have already recorded and proven is yours. A
successful `create_tenant` emits three values that together identify the exact
run-owned tenant, and `delete_tenant` requires all three to authorize a delete —
it deletes only when a resident tag's permanent `name` matches the forwarded
permanent id:

- `tenant_name` — the TagValue namespaced name
  (`<project>/<key-short>/<value-short>`), passed as `--group-name`.
- `tenant_id` — the permanent TagValue id (`tagValues/<id>`), passed as
  `--tenant-id`.
- `tenant_key_id` — the permanent parent TagKey id (`tagKeys/<id>`), passed as
  `--tenant-key-id`.

Passing `--group-name` alone leaves both id arguments empty, so teardown cannot
prove ownership: it retains the resources untouched and reports failure rather
than deleting on a forgeable run-scoped short name. Rerun the self-contained
cleanup from the repository root (the same `uv run` environment the Quick Start
established) with all three recorded values:

```bash
uv run python isvctl/configs/providers/gcp/scripts/control-plane/delete_tenant.py \
    --region us-central1 \
    --group-name <project>/<key-short>/<value-short> \
    --tenant-id tagValues/<id> \
    --tenant-key-id tagKeys/<id>
```

A tag you cannot tie to a recorded run handle — no matching permanent
`tenant_id` / `tenant_key_id` — must be removed manually and deliberately with
`gcloud resource-manager tags values delete` / `keys delete`, and only after
confirming out-of-band that it is not owned by another run still in flight.

## Related Documentation

- [GCP operator reference](../../../../../../../docs/references/gcp.md) — NCP-level auth, project resolution, module index.
- [AWS control-plane guide](../../../../aws/scripts/control-plane/docs/aws-control-plane.md) — canonical oracle contract.
- [Control-plane suite](../../../../../suites/control-plane.yaml) — provider-agnostic validation contract.
- [Output Schemas](../../../../../../../isvctl/src/isvctl/config/output_schemas.py) — JSON schema definitions.
