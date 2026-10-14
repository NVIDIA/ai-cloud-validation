<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Kubernetes upgrade evidence (K8S08/09/10)

The canonical Kubernetes suite grades three provider-neutral contracts:

| Test | Check | Provider step |
| --- | --- | --- |
| K8S08-01 | `K8sManagedUpgradePolicyCheck` | `describe_managed_upgrade_policy` |
| K8S09-01 | `K8sControlPlaneUpgradeCheck` | `control_plane_upgrade_test` |
| K8S10-01 | `K8sNodePoolUpgradeCheck` | `node_pool_upgrade_test` |

The shared validators consume provider-neutral evidence. The EKS reference adapter
performs real upgrades on an explicitly selected disposable CPU cluster. Other
providers can implement the scaffold contract. Unconfigured steps skip; configured
but unimplemented, failed or incomplete steps fail.

[upgrade-example.json](../../isvctl/configs/providers/my-isv/scripts/k8s/upgrade-example.json) contains complete illustrative output
for each check. The `my-isv` [adapter](../../isvctl/configs/providers/my-isv/scripts/k8s/upgrade_test.py) emits
these records only under `ISVCTL_DEMO_MODE=1`. Preview the three checks without a cluster:

```bash
ISVCTL_DEMO_MODE=1 uv run isvctl test run \
  -f isvctl/configs/providers/my-isv/config/k8s.yaml \
  -f isvctl/configs/providers/my-isv/config/overlays/k8s-upgrades.yaml \
  --phase test --label k8s_upgrades
```

Copy the [opt-in command overlay](../../isvctl/configs/providers/my-isv/config/overlays/k8s-upgrades.yaml) and replace the
adapter with your provider implementation. Load it after your ordinary provider
configuration using a second `-f` argument; it replaces the command step list with
only the upgrade steps. It does not declare a second Kubernetes suite. Keep
credentials and target cluster, pool and version settings in environment variables. Use a disposable managed
cluster, an eligible next minor release, and a pool with at least two nodes and
enough spare capacity for replacement pods. Ordinary `k8s.yaml` runs do not launch
upgrade operations. The opt-in steps run sequentially and stop on failure.

## Shared observation contract

Each result carries `success`, `platform: kubernetes`, and `test_name`. Active
upgrade results also carry `operation`.
`success` describes collection/execution, not certification. The operation has an
ID, `status: succeeded`, `started_at` and `completed_at`. Start the window before
submitting the upgrade; end it only after terminal provider status and independent
cluster convergence. All numeric timestamps use the same clock and seconds scale.
Capture the operation ID from the API rather than inventing a success flag.

For both active upgrade tests, run an application observer **independently of the
upgrade poller and Kubernetes API**. Use a replicated canary behind a stable Service
or ingress, returning a unique run-specific token. Each request opens a connection
without hidden retries and records `at` (request start), `status_code`, `response`
and `duration_seconds`; record timeouts and connection failures as failed requests,
never omit them. The application contract identifies `endpoint` and
`expected_response`. Probe before submission, throughout the upgrade and after
convergence. A single failed request or wrong response fails the check.

The suite enforces a maximum five-second gap between observations and a two-second
request timeout. These are validation config values, not values the provider
report can raise. Timestamps must be strictly increasing and bracket the entire
operation. PASS means **no observed outage within these sampling bounds**; it
does not establish uninterrupted availability between samples. Retain raw request
logs for review. Do not substitute API server health or Ready replica counts for
actual application responses.

The EKS [upgrade_observer.py](../../isvctl/configs/providers/aws/scripts/eks/upgrade_observer.py) supplies `ApplicationObserver` for the
application part of a provider adapter. Construct it with the endpoint, expected
canary response and a new JSONL artifact path. Call `start()` before submitting the
upgrade; it requires a successful baseline response. Keep it running until a probe
after convergence has completed, then call `stop()` to retrieve the `application`
contract. The observer uses a separate thread, opens fresh HTTP connections, rejects
redirects and records every failed attempt without retries. Evidence is flushed
after each probe; an existing artifact is never overwritten. Call `stop()` in the
adapter's `finally` block and treat collector or persistence errors as test failures.
Malformed or truncated HTTP responses are failed probes; collection continues so
the evidence can show recovery. If observer shutdown also fails, the EKS adapter
preserves the original `error` and `error_type` and records subsequent shutdown
failures in `observer_errors` without skipping canary cleanup.

Adapters must bound API calls, upgrade polling and cleanup within the step timeout
(two hours for each active upgrade). Emit `success: false` and a diagnostic `error`
on timeout or incomplete observations. Register cleanup before creating resources;
use `finally` and signal handling to delete only this run's canary/PDB resources and
restore test-only configuration, including the restrictive budget described below.
Report cleanup failures in `cleanup_errors`. Do not downgrade a completed cluster
upgrade as cleanup. If the provider operation remains active, report its status
and preserve the information needed for operator recovery.

## K8S08: provider-managed maintenance

Read the platform's actual managed-service configuration and regional version
catalogue. Report provider ownership, routine/security maintenance, references for
the provider's process and status API, and a configuration reference identifying
the observed service. `managed_control_plane` and `supported` must both be true.
Record the current full API-server `observed_version` and supported next-minor
`from`/`to` transitions. Published documentation alone does not establish that the
selected cluster is a supported managed service.

This read-only process/policy check does not claim to have observed an automatic
maintenance event or a delivered tenant notification. K8S09 separately exercises
the provider-executed minor upgrade and measures application continuity.

## K8S09: minor control-plane upgrade

Record `observed_version_before` and `observed_version_after` from the Kubernetes
API server (`/version`), independently of provider status. Set `target_version` to
the requested full release version and `operation.executed_by: provider`. The
check requires advancement by exactly one minor within the same major and arrival
at the requested patch release. Vendor version suffixes are accepted. A no-op,
patch-only update, incomplete update, downgrade or skipped minor fails.

Report whether an application maintenance window was required as the strict boolean
`maintenance_window_required`; only false passes. Collect the application trace
through the full operation as described above.

## K8S10: rolling node upgrade and disruption budgets

Initiate a real pool update as a tenant (`operation.initiated_by: tenant`). Pin the
selected pool membership for this experiment. For every node, record Kubernetes
UID, observed provider image/version `revision`, and initial/final readiness and
schedulability. Report `target_revision` and complete `nodes_before`/`nodes_after`
lists. Each selected node must change revision; final capacity must be restored.
`rollouts` records each original node UID, `drain_started_at` (first updater
attempt) and `drained_at` (observed removal of its protected pods). Drain intervals
may overlap only up to `max_unavailable_nodes` (default one, below pool size).
Replacement nodes may become ready before old nodes drain. The contract does not
invent a one-to-one pairing between old nodes and surge replacements. Initial and
final pool observations independently prove revision change and restored capacity.

Place protected canary pods on **every selected node** and verify the PDB selector
covers those pods. Use a replicated workload with `minAvailable` below the replica
count. Record the PDB UID, replica count and normal `min_available`. Collect PDB
samples at the same maximum gap throughout the operation: `uid`, `expected_pods`,
`desired_healthy`, `current_healthy`, `generation`, `observed_generation`, and `at`.
Require reconciled baseline/final status, complete coverage and a healthy count
at or above the desired budget. A one-generation lag while relaxing the budget
may last less than the observation bound; the older, stricter healthy count
remains enforced. Persistent or invalid generation lag fails. For integer
`minAvailable`, Kubernetes counts selected terminating pods in `expectedPods`
alongside their replacements. The count may exceed the replica target during a
drain, but must match it at baseline and completion. See the
[Kubernetes disruption controller](https://github.com/kubernetes/kubernetes/blob/v1.36.0/pkg/controller/disruption/disruption.go).

Exercise enforcement, not just PDB presence:

1. Temporarily make the canary budget restrictive (`disruptionsAllowed=0`), wait
   for its observed generation, and submit the pool update.
2. Observe **the provider updater's** attempted canary eviction being rejected
   because of the disruption budget. Capture `blocked_eviction` with the actual
   node/pod/PDB UIDs, timestamp, HTTP 429, `reason: DisruptionBudget` and zero
   allowed disruptions. Rate limiting or an unrelated manual eviction is not proof.
3. Restore the normal budget on the same PDB; wait for reconciliation. Observe
   the updater proceeding through protected Eviction API calls on each selected
   node. Report these successful calls in `evictions` with their timestamps, UIDs,
   method and response code. Include any direct-delete/force-drain attempts so the
   validator can reject them; do not normalize a DELETE to an eviction.
4. Observe full rollout completion and application/pool recovery before cleanup.

Set `audit_complete` only when the observer captured the updater's complete canary
drain activity throughout this run; fail collection if evidence is inaccessible or
lost. PDB sampling alone cannot prove that drains used the Eviction API. Kubernetes
documents that [direct deletion bypasses PDBs](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
and that [safe draining respects disruption budgets](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/).
Retain the original API/audit events alongside the normalized JSON for review.

## Live EKS reference run

Use [eks-upgrades.yaml](../../isvctl/configs/providers/aws/config/overlays/eks-upgrades.yaml).
Load it after `eks.yaml` to retain the standard suite's checks and bind only these
three upgrade steps; select `--label k8s_upgrades`.
The reference supports a managed AL2023 x86 CPU pool without a custom launch
template, at least two healthy nodes, `maxUnavailable=1`, and EKS audit logging
enabled **before** the test. The initial control plane must be exactly one minor
behind the target. Both minors must be in regional standard support. The pool
must require the target's recommended image. The canary must fit on every pool
member; the current fixture uses three replicas and two nodes.

```bash
export AWS_PROFILE=your-test-profile AWS_REGION=us-east-2
export EKS_UPGRADE_CLUSTER=your-disposable-cluster
export EKS_UPGRADE_NODE_POOL=upgrade-cpu
export EKS_UPGRADE_TARGET_VERSION=1.36
export EKS_UPGRADE_ALLOW="$EKS_UPGRADE_CLUSTER"
export EKS_UPGRADE_SOURCE_CIDRS=YOUR_CONTROLLER_PUBLIC_IP/32
export EKS_UPGRADE_ARTIFACT_DIR=/tmp/unique-upgrade-run
uv run isvctl test run \
  -f isvctl/configs/providers/aws/config/eks.yaml \
  -f isvctl/configs/providers/aws/config/overlays/eks-upgrades.yaml \
  --phase test --label k8s_upgrades
```

`EKS_UPGRADE_ALLOW` must match the exact cluster name. Use a fresh artifact directory
for each attempt. The adapter writes an isolated kubeconfig (mode 0600), application
JSONL, pool/PDB JSONL, scoped audit records and result JSON there. It never changes
the controller's default Kubernetes context or passes controller credentials to
pods. The Service restricts ingress to the supplied controller CIDRs. The controller
needs EKS describe/update permissions, read access to the public EKS AMI SSM
parameters and the cluster's CloudWatch audit group, plus Kubernetes privileges to
manage the uniquely named namespace and read selected nodes.

The adapter calls `UpdateClusterVersion` and `UpdateNodegroupVersion(force=False)`.
It does not simulate an upgrade by scaling down nodes. EKS's `eks:node-manager`
audit identity must demonstrate a real budget rejection before the test relaxes
its own budget. Every observed protected pod on every original node must have a
successful eviction audit record; missing identity mappings fail collection.
After convergence, the collector waits 60 seconds for CloudWatch delivery, reads
all pages through the completion time, and saves the raw records. Missing delivery
or incomplete UID coverage fails the check. This is bounded audit evidence, not a
guarantee against arbitrarily delayed or lost provider logs.

The adapter attempts canary cleanup on success, failure, SIGINT and SIGTERM.
Application/collector/cleanup errors remain failures. It does not cancel an EKS
update already accepted by AWS or attempt a version rollback. If
interrupted, inspect the captured operation ID/provider status before retrying.
Destroy disposable infrastructure after all experiments finish, including failures.

A minimal CPU fixture is provided under
[terraform-upgrade-test](../../isvctl/configs/providers/aws/scripts/eks/terraform-upgrade-test/main.tf).
It creates a separate VPC, two private CPU nodes, one NAT gateway, restricted
public API access and an audit log group. Initialize it with `TF_DATA_DIR` outside
the repository and use `-state=/tmp/your-run/terraform.tfstate` for plan/apply/destroy.
Supply `name`, `region`, `controller_cidr` and the initial `kubernetes_version`.
Review the plan, apply, run the upgrade configuration, then run `terraform destroy`
with the **same state and variables**. This removes the upgraded resources; do not
run apply to reconcile their versions back to the initial fixture version.

## Live verification: 2026-10-13

Executed the EKS workflow from the issue #661 branch in `us-east-2` on a new
CPU-only cluster with two `m6i.large` nodes. The production cluster was not modified.

| Check | Observed result |
| --- | --- |
| K8S08 | Passed: live managed configuration and supported version transition |
| K8S09 | Passed: `1.35.8` to `1.36.4`, 444 correct HTTP responses, no latency violations |
| K8S10 | Failed the two-second response budget: one of 752 correct HTTP 200 responses took 2.206 seconds |

The managed node update completed and restored two healthy nodes at the target
AMI. Its audit records include budget-rejected requests followed by three successful
Eviction API calls across both original nodes. All 750 PDB samples respected the
required healthy count, and the workload recovered all three replicas. PDB assertions
also passed when replayed independently against the saved, unmodified evidence.

The live run exposed integer-`minAvailable` counting of terminating pods; the
validator was corrected with regression coverage and the actual evidence replayed.
The latency violation remains a failed result. This observation does not isolate
whether the delay originated in the controller, network, load balancer or workload.

After testing, all 23 Terraform-managed resources were destroyed. Direct AWS
checks confirmed removal of the cluster, VPC, worker instances, load balancers,
active NAT gateways, elastic IPs, test IAM roles and audit log group. The Terraform
state is empty. Local verification passed 4,831 tests and all pre-commit checks.

## Live verification after refactor and review fixes: 2026-10-14

Ran the documented `eks.yaml` plus `eks-upgrades.yaml` command end to end on a
new disposable CPU-only EKS cluster in `us-east-2`, after merging the provider
layout refactor and fixing HTTP protocol-error handling and observer shutdown
diagnostics. All three checks passed with the unchanged two-second request budget
and five-second maximum observation gap.

| Check | Observed result |
| --- | --- |
| K8S08 | Passed: live managed configuration and supported version transition |
| K8S09 | Passed: `1.35.8` to `1.36.4`, 414 correct HTTP responses, maximum latency 0.276 seconds |
| K8S10 | Passed: both workers replaced, 695 correct HTTP responses, maximum latency 0.345 seconds |

Neither upgrade had a failed HTTP request, wrong response or latency violation.
Maximum probe gaps were 1.011 seconds or less. The node test collected 692 PDB
samples, observed an actual budget-rejected eviction (HTTP 429), then three
successful Eviction API calls across the two original workers. Drains were
sequential; the PDB maintained at least two healthy replicas and recovered all
three. Both replacement workers were healthy and schedulable at the target image.
Both test-owned canary namespaces were removed without cleanup errors.

The previous run's latency violation remains a failed historical result; this
passing run does not establish its cause or remove it from the evidence.
Verification covers the EKS CPU reference implementation, not GPU workloads or
other providers. Local checks passed 4,847 tests (one skipped, 280 deselected),
lint, pre-commit, scaffold demos and the three-check upgrade-overlay demo.

Afterward, all 23 Terraform-managed resources were destroyed. Eleven direct
AWS/state checks confirmed removal of the cluster, VPC, active workers, load
balancers, NAT gateways, elastic IPs, test IAM roles and audit log group, plus
empty Terraform state. The production cluster was not modified.
