# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared orchestration helpers for the GCP (GKE) k8s lifecycle stubs.

The k8s domain drives its cluster / node-pool CREATE / SCALE / DESTROY through
`terraform` (official hashicorp/google provider) and installs the kubeconfig +
observes inventory through `gcloud container clusters get-credentials` + ambient
`kubectl` — the GKE analog of the AWS EKS realism oracle
(providers/aws/scripts/eks/*), which provisions via `terraform apply` and lands
the kubeconfig with `aws eks update-kubeconfig`. This module centralizes the
subprocess wrappers, the run-scope guard, GKE name normalization, the GPU-zone
capacity preflight probe, and the kubectl inventory/labeling so each lifecycle
stub stays small.

Cross-domain GCP facts (project resolution via ADC, run-id name collision) are
reused from providers/gcp/scripts/common. This module is imported directly by
the sibling k8s stubs (setup.py, create_node_pool.py, ...) — it declares no
argparse of its own.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# providers/gcp/scripts/ on the path so `common.*` resolves the same way the
# other GCP stubs import it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.compute import resolve_project  # resolved via the sys.path insert above

SCRIPT_DIR = Path(__file__).resolve().parent
CLUSTER_TF_DIR = SCRIPT_DIR / "terraform"
NODE_POOL_TF_DIR = SCRIPT_DIR / "terraform-node-pool"
SHARED_VPC_TF_DIR = SCRIPT_DIR / "terraform-shared-vpc-cluster"

# GKE cluster / node-pool names are RFC 1035 (lowercase letters, digits,
# hyphens; start with a letter; <=40 for cluster names). Reserve room for the
# `-<8-hex run id>` suffix.
_GKE_NAME_MAX = 40
_RUN_ID_LEN = 8

# Zone-capacity stockout tokens (substring match — GCE returns the
# "..._WITH_DETAILS" variant, so never exact-equals).
_STOCKOUT_TOKENS = (
    "ZONE_RESOURCE_POOL_EXHAUSTED",
    "does not have enough resources",
    "state:STOCKOUT",
    "STOCKOUT",
)

# GCP's documented transient API error taxonomy: HTTP 429 / 500 / 503 / 504 map to
# ResourceExhausted / InternalServerError / ServiceUnavailable / DeadlineExceeded,
# all "retry with bounded backoff" (as opposed to the terminal permission /
# not-found / conflict / credential buckets that never clear on their own). A
# gcloud CLI surface renders these as either a canonical status word/token OR an
# HTTP/code-tagged status number, so recognize BOTH the word forms below and a
# status number seen in a code/status/http/error context (via _TRANSIENT_STATUS_RE).
# The regex deliberately never matches a BARE number, so a 403/404/409 permission,
# not-found, or conflict message keeps its own distinct classification — only a
# 429/500/503/504 tagged as an HTTP/API status is read as transient. Lowercase
# forms only (matched against the already-lowercased output).
_TRANSIENT_TOKENS = (
    "resource_exhausted",
    "resourceexhausted",
    "rate limit exceeded",
    "ratelimitexceeded",
    "rate_limit_exceeded",
    "too many requests",
    "service unavailable",
    "serviceunavailable",
    "temporarily unavailable",
    "currently unavailable",
    "backend error",
    "backenderror",
    "internal error",
    "internal_error",
    "internalservererror",
    "internal server error",
    "deadline exceeded",
    "deadline_exceeded",
    "deadlineexceeded",
    "try again later",
)
_TRANSIENT_STATUS_RE = re.compile(r"(?:code|status|error|http\S*)[\s:=\]\[\"'>,]*\b(?:429|500|503|504)\b")

# Terminal not-found (404) / conflict (409) status NUMBERS are recognized ONLY in an
# explicit code/status/http/error context — NEVER as a bare substring. A run-scoped
# resource name or project id can literally contain the digits "404"/"409", and
# `_run` folds the WHOLE failing command (run-scoped names + project) into its
# timeout diagnostic, so a bare-substring match could misread a TIMEOUT (or any
# unrelated output) as a clean not-found/conflict — letting the existence/absence
# helpers report a still-billable resource absent. Same context-anchored shape as
# _TRANSIENT_STATUS_RE (deliberately never matches a bare number); the textual
# `not found` / `already exists` tokens keep their own distinct match in
# _classify_cli_output.
_NOT_FOUND_STATUS_RE = re.compile(r"(?:code|status|error|http\S*)[\s:=\]\[\"'>,]*\b404\b")
_CONFLICT_STATUS_RE = re.compile(r"(?:code|status|error|http\S*)[\s:=\]\[\"'>,]*\b409\b")

# Stable marker `_run` appends to a TIMED-OUT command's folded output. A timeout is
# an INCOMPLETE read — the command never returned a disposition — so it must classify
# as transient (retry/raise), never as terminal not-found/conflict, even though the
# folded command echo may carry a run-scoped name/project containing 404/409 digits.
_TIMEOUT_MARKER = "[timed out after "

# Integrated-GPU machine families carry their accelerator via the machine type
# itself (g2 = L4, a2 = A100, a3 = H100/H200), so a raw-compute probe VM for
# these needs NO `--accelerator` flag. Separate-accelerator shapes (e.g. n1)
# DO. Both still need `--maintenance-policy=TERMINATE`.
_INTEGRATED_GPU_PREFIXES = ("g2-", "a2-", "a3-")


class LifecycleError(RuntimeError):
    """A lifecycle failure carrying a pre-classified error bucket + detail.

    ``detail`` already folds the failing terraform/gcloud/kubectl output tail so
    the operator can diagnose from the emit_error message alone (isvctl drops the
    step's raw stderr).
    """

    def __init__(self, bucket: str, detail: str) -> None:
        super().__init__(detail)
        self.bucket = bucket
        self.detail = detail


# --------------------------------------------------------------------------- #
# Run-scope + naming                                                          #
# --------------------------------------------------------------------------- #


def run_scope_id() -> str:
    """Return the REQUIRED run-scope id (``RUN_ID`` or ``LS_RUN_ID``).

    k8s provisions expensive GPU compute whose teardown RE-DERIVES the resource
    names from this id, so — unlike the lighter GCP domains that auto-generate a
    random suffix — this domain HARD-FAILS when the id is unset. An
    auto-generated value would make create and destroy compute different names
    and orphan the GKE cluster (a costly GPU leak). That is why this helper is a
    deliberate exception to the "suffix helper MUST NOT raise" convention: an
    unset id must fail loudly rather than silently leak billable GPU compute that
    teardown could never re-derive a name for. ONE source feeds both the guard
    and the name suffix so they can never diverge.
    """
    sid = os.environ.get("RUN_ID") or os.environ.get("LS_RUN_ID") or ""
    sid = sid.strip()
    if not sid:
        raise LifecycleError(
            "config_error",
            "[bucket=config_error] RUN_ID (or LS_RUN_ID) is REQUIRED for the k8s "
            "domain: it scopes the GKE cluster / node-pool names that teardown "
            "re-derives to delete. Refusing to provision expensive GPU compute "
            "under an unscoped name that teardown could not reclaim. Set RUN_ID "
            "and retry.",
        )
    return sid[:_RUN_ID_LEN]


# Cloud-side ownership marker key. scoped_name() truncates the run id to
# _RUN_ID_LEN chars for the RFC-1035 GKE name cap, so a run-scoped NAME alone
# cannot prove ownership (two runs whose ids share the first 8 chars collide on
# the same cluster name). The adopt paths therefore require this label, carrying
# the FULL run identity, before importing a same-named cluster into state.
OWNERSHIP_LABEL_KEY = "isv-ncp-run-id"


def full_run_scope_id() -> str:
    """Return the FULL, untruncated run identity as a GCE-label-safe value.

    Unlike ``run_scope_id`` (truncated to 8 chars for the GKE name cap), this is
    the WHOLE ``RUN_ID`` / ``LS_RUN_ID`` normalized to a GCP resource-label value
    (lowercase letters, digits, ``-`` and ``_``, capped at 63). It is the exact
    ownership proof the adopt paths require: a run-scoped name match is NOT enough
    to authorize importing (and later destroying) a pre-existing cluster, so the
    owning run stamps this full identity as a cloud-side label at create and every
    adopt verifies it before import.
    """
    raw = (os.environ.get("RUN_ID") or os.environ.get("LS_RUN_ID") or "").strip()
    if not raw:
        # Reuse the run_scope_id guard so an unset id fails identically everywhere.
        run_scope_id()
    value = re.sub(r"[^a-z0-9_-]", "-", raw.lower()).strip("-_")
    return value[:63] or run_scope_id()


def normalize_gke_name(base: str) -> str:
    """Lowercase + RFC-1035-normalize a base name (no run-id suffix)."""
    name = base.strip().lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    if not name or not name[0].isalpha():
        name = f"isv-{name}".strip("-")
    return name


def scoped_name(base: str) -> str:
    """RFC-1035-normalize ``base`` and append the run-scope id, capped to 40.

    Truncates the BASE (never the run-id suffix) so the run id always survives —
    the suffix is what makes the name unique + teardown-reclaimable.
    """
    sid = run_scope_id()
    prefix = normalize_gke_name(base)
    keep = _GKE_NAME_MAX - (len(sid) + 1)
    if keep < 1:
        keep = 1
    prefix = prefix[:keep].rstrip("-") or "isv"
    return f"{prefix}-{sid}"


def _scoped_pool_name(cluster_name: str, role: str) -> str:
    """RFC-1035 name of a node pool declared INSIDE a cluster terraform module.

    Mirrors the ``locals`` derivation in terraform/main.tf and
    terraform-shared-vpc-cluster/main.tf EXACTLY so the adopt paths can import
    the pools that carry no separate tfstate. The common short-name case keeps
    the familiar ``<cluster>-<role>`` spelling; an over-long cluster name (a long
    direct --cluster-name override) falls back to the same trimmed
    ``<base>-<role>-<sid>`` the terraform locals produce (both the run-id tail and
    the role discriminator always survive), so the imported resource ids always
    match the names terraform manages. The terraform ``_*_base_keep`` reserves
    ``len(role) + 2`` chars for the ``-<role>-<sid>`` suffix — 5 for a 3-char role
    (sys/gpu), 4 for a 2-char role (np) — which this single formula reproduces.
    """
    np_max = 40
    sid = cluster_name.rsplit("-", 1)[-1] if "-" in cluster_name else cluster_name
    suffix = f"-{sid}"
    base = cluster_name[: -len(suffix)] if cluster_name.endswith(suffix) else cluster_name
    keep = max(1, np_max - len(sid) - (len(role) + 2))
    pool_base = base[: min(len(base), keep)]
    short = f"{cluster_name}-{role}"
    return short if len(short) <= np_max else f"{pool_base}-{role}-{sid}"


def baseline_pool_names(cluster_name: str) -> tuple[str, str]:
    """(system, gpu) baseline node-pool names for the primary cluster module."""
    return _scoped_pool_name(cluster_name, "sys"), _scoped_pool_name(cluster_name, "gpu")


def secondary_pool_name(cluster_name: str) -> str:
    """Node-pool name for the secondary shared-VPC cluster module (role "np")."""
    return _scoped_pool_name(cluster_name, "np")


def state_file_for_pool(pool_name_scoped: str) -> str:
    """Local tfstate filename for a node pool, derived from its scoped name.

    Create / scale / destroy of one pool all resolve the SAME file because they
    resolve the SAME scoped pool name, so state threads across the separate
    lifecycle-step processes without an extra env var.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", pool_name_scoped)
    return f"np-{safe}.tfstate"


def cluster_state_file() -> str:
    return f"cluster-{run_scope_id()}.tfstate"


def shared_vpc_state_file() -> str:
    return f"shared-vpc-{run_scope_id()}.tfstate"


def cluster_state_path_for_node_pool() -> str:
    """Relative path (from the node-pool module dir) to the primary state."""
    return f"../terraform/{cluster_state_file()}"


# --------------------------------------------------------------------------- #
# Output / diagnostics                                                        #
# --------------------------------------------------------------------------- #


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(result: dict[str, Any]) -> int:
    """Print the result JSON to stdout and return the process exit code."""
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


def fold_tail(text: str, *, limit: int = 2500) -> str:
    """Return the last ``limit`` chars of ``text`` (collapsed) for emit_error."""
    if not text:
        return ""
    text = text.strip()
    if len(text) > limit:
        text = "...(truncated)... " + text[-limit:]
    return text


def error_result(platform: str, exc: BaseException, **extra: Any) -> dict[str, Any]:
    """Build a structured failure result carrying the ``[bucket=<name>]`` token."""
    if isinstance(exc, LifecycleError):
        bucket, detail = exc.bucket, exc.detail
    else:
        bucket, detail = "unknown_error", f"[bucket=unknown_error] {exc}"
    result: dict[str, Any] = {
        "success": False,
        "platform": platform,
        "error_type": bucket,
        "error": detail,
    }
    result.update(extra)
    return result


# --------------------------------------------------------------------------- #
# Subprocess wrappers                                                         #
# --------------------------------------------------------------------------- #


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int,
    echo: bool = True,
) -> tuple[int, str]:
    """Run a command, capturing combined stdout+stderr; echo to our stderr.

    Returns ``(returncode, combined_output)``. Never raises on a non-zero exit —
    callers decide how to classify. TimeoutExpired is converted into a
    non-zero-style result so the caller can fold whatever partial output ran.
    """
    if echo:
        log(f"+ {' '.join(args)}")
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return 124, f"{partial}\n{_TIMEOUT_MARKER}{timeout}s running: {' '.join(args)}]"
    if echo and proc.stdout:
        sys.stderr.write(proc.stdout)
        sys.stderr.flush()
    return proc.returncode, proc.stdout or ""


def _classify_cli_output(output: str) -> str:
    """Map a failing CLI output to a disposition bucket (best-effort)."""
    low = output.lower()
    # A TIMED-OUT command never returned a real disposition — the folded echo of the
    # whole command (run-scoped names + project ids) can contain 404/409 digits, so
    # classify the timeout as transient FIRST, before any status-number match, so an
    # unreadable-by-timeout describe is never misread as clean not-found/conflict.
    if _TIMEOUT_MARKER in low:
        return "transient"
    if any(tok.lower() in low for tok in _STOCKOUT_TOKENS):
        return "transient"
    if "quota_exceeded" in low or "quota exceeded" in low:
        return "transient"
    # Rate-limit (429), internal-server (500), service-unavailable (503), and
    # deadline-exceeded (504) responses are GCP's documented transient bucket —
    # retry with bounded backoff instead of treating them as a terminal failure.
    # Checked before the terminal buckets so a server-side 5xx/429 is retried;
    # genuine permission/not-found/conflict/credential outputs carry none of these
    # tokens and keep their own bucket.
    if any(tok in low for tok in _TRANSIENT_TOKENS) or _TRANSIENT_STATUS_RE.search(low):
        return "transient"
    if (
        "permission denied" in low
        or "permission_denied" in low
        or "does not have permission" in low
        or "org policy" in low
        or "constraintviolation" in low
        or ("constraint" in low and "denied" in low)
    ):
        return "access_denied"
    if "not found" in low or "notfound" in low or _NOT_FOUND_STATUS_RE.search(low):
        return "not_found"
    if "already exists" in low or "alreadyexists" in low or _CONFLICT_STATUS_RE.search(low):
        return "conflict"
    if "unauthenticated" in low or "invalid_grant" in low or "credentials" in low:
        return "credentials_invalid"
    return "unknown_error"


def _is_transient_cleanup_error(output: str) -> bool:
    """A best-effort delete failure worth retrying: rate-limit / quota / an
    in-flight resource-still-in-use race (a PD the CSI controller has not yet
    detached, a MIG mid-teardown). Permission / not-found / config errors are
    NOT retried — they will never clear on their own."""
    low = output.lower()
    if _classify_cli_output(output) == "transient":
        return True
    return (
        "in use" in low
        or "resourceinuse" in low
        or "resource_in_use" in low
        or "resourcenotready" in low
        or "try again" in low
    )


# --------------------------------------------------------------------------- #
# Terraform                                                                   #
# --------------------------------------------------------------------------- #


def _tf_env(tf_vars: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    for key, value in tf_vars.items():
        if isinstance(value, (list, dict)):
            env[f"TF_VAR_{key}"] = json.dumps(value)
        else:
            env[f"TF_VAR_{key}"] = str(value)
    return env


def terraform_init(module_dir: Path, *, timeout: int = 300) -> None:
    """Run `terraform init` UNCONDITIONALLY (idempotent, reconciles a stale lock).

    Never gated on `.terraform` directory existence: a dir can exist while the
    provider selection / lock is stale (e.g. a prior run whose setup bailed
    before its own apply), which would abort a later `terraform destroy` with
    "Inconsistent dependency lock file". init is cheap when already initialized.
    """
    rc, out = _run(
        ["terraform", "init", "-input=false", "-upgrade"],
        cwd=module_dir,
        timeout=timeout,
    )
    if rc != 0:
        raise LifecycleError(
            _classify_cli_output(out),
            f"[bucket={_classify_cli_output(out)}] terraform init failed in {module_dir.name}: {fold_tail(out)}",
        )


def terraform_apply(
    module_dir: Path,
    state_file: str,
    tf_vars: dict[str, Any],
    *,
    timeout: int,
) -> None:
    env = _tf_env(tf_vars)
    rc, out = _run(
        ["terraform", "apply", "-auto-approve", "-input=false", f"-state={state_file}"],
        cwd=module_dir,
        env=env,
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] terraform apply failed in {module_dir.name} (state={state_file}): {fold_tail(out)}",
        )


def terraform_destroy(
    module_dir: Path,
    state_file: str,
    tf_vars: dict[str, Any],
    *,
    timeout: int,
) -> None:
    env = _tf_env(tf_vars)
    rc, out = _run(
        ["terraform", "destroy", "-auto-approve", "-input=false", f"-state={state_file}"],
        cwd=module_dir,
        env=env,
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] terraform destroy failed in {module_dir.name} (state={state_file}): {fold_tail(out)}",
        )


def terraform_state_has(module_dir: Path, state_file: str, address: str, *, timeout: int = 60) -> bool:
    """True when ``address`` is already tracked in ``state_file``.

    Decides whether a resource must be ADOPTED (imported) before apply. A fresh
    per-step worktree starts with an EMPTY local state even though the harness may
    have preserved the run-scoped resource that an earlier per-step worker in the
    same run provisioned (GCP_K8S_SKIP_TEARDOWN keeps every run resource alive);
    without adoption the apply re-CREATEs and collides on a 409 "already exists".
    """
    if not state_exists(module_dir, state_file):
        return False
    rc, out = _run(
        ["terraform", "state", "list", f"-state={state_file}", address],
        cwd=module_dir,
        timeout=timeout,
        echo=False,
    )
    return rc == 0 and address in out


def terraform_import(
    module_dir: Path,
    state_file: str,
    address: str,
    resource_id: str,
    tf_vars: dict[str, Any],
    *,
    timeout: int = 600,
) -> bool:
    """Import an EXISTING cloud resource into ``state_file`` so a later apply
    reconciles it in place instead of colliding on a 409 "already exists".

    Returns True on a successful import and False when the resource is genuinely
    absent (a clean not-found), so the caller lets apply CREATE it. Any other
    failure (auth / permission) RAISES a classified LifecycleError — a present but
    unimportable resource must never be silently treated as absent.
    """
    env = _tf_env(tf_vars)
    rc, out = _run(
        ["terraform", "import", "-input=false", f"-state={state_file}", address, resource_id],
        cwd=module_dir,
        env=env,
        timeout=timeout,
    )
    if rc == 0:
        return True
    bucket = _classify_cli_output(out)
    if bucket == "not_found":
        return False
    raise LifecycleError(
        bucket,
        f"[bucket={bucket}] terraform import {address} <- {resource_id} failed in {module_dir.name}: {fold_tail(out)}",
    )


def terraform_refresh_only(
    module_dir: Path,
    state_file: str,
    tf_vars: dict[str, Any],
    *,
    timeout: int = 600,
) -> None:
    """Reconcile state + recompute root-module outputs from live infrastructure
    WITHOUT any create/modify/replace (`terraform apply -refresh-only`).

    The adopt path uses this instead of a full apply for a freshly-imported
    ``google_container_cluster``: the provider reads ``initial_node_count`` back
    from the API as 0 (the default pool was removed at create), which differs from
    the config's create-time value and would force a full cluster REPLACE under a
    normal apply. refresh-only can only update state, never destroy/recreate the
    live cluster, and it still populates the outputs the node-pool / shared-VPC
    modules read via terraform_remote_state.
    """
    env = _tf_env(tf_vars)
    rc, out = _run(
        ["terraform", "apply", "-refresh-only", "-auto-approve", "-input=false", f"-state={state_file}"],
        cwd=module_dir,
        env=env,
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] terraform apply -refresh-only failed in {module_dir.name} (state={state_file}): {fold_tail(out)}",
        )


def terraform_output_raw(module_dir: Path, state_file: str, name: str) -> str:
    rc, out = _run(
        ["terraform", "output", f"-state={state_file}", "-raw", name],
        cwd=module_dir,
        timeout=60,
        echo=False,
    )
    if rc != 0:
        raise LifecycleError(
            "not_found",
            f"[bucket=not_found] terraform output '{name}' not readable from {state_file}: {fold_tail(out)}",
        )
    return out.strip()


def terraform_output_json(module_dir: Path, state_file: str, name: str) -> Any:
    rc, out = _run(
        ["terraform", "output", f"-state={state_file}", "-json", name],
        cwd=module_dir,
        timeout=60,
        echo=False,
    )
    if rc != 0:
        raise LifecycleError(
            "not_found",
            f"[bucket=not_found] terraform output '{name}' not readable from {state_file}: {fold_tail(out)}",
        )
    return json.loads(out)


def state_exists(module_dir: Path, state_file: str) -> bool:
    return (module_dir / state_file).is_file()


def classify_state(module_dir: Path, state_file: str, address: str, *, timeout: int = 120) -> str:
    """Classify a local Terraform state for ``address``: 'absent' | 'empty' | 'tracked' | 'unreadable'.

    ``Path.exists()`` alone (``state_exists``) cannot tell a never-written state from
    a VALID-EMPTY one a successful ``terraform destroy`` left behind, nor from a state
    whose address is present-but-unreadable — yet the teardown safety net must treat
    all four differently. Classify with ``terraform state list`` (per the terraform
    state semantics: file existence is not the discriminator):

      * the state FILE is absent                    -> 'absent';
      * ``terraform state list`` FAILS              -> 'unreadable' (never read as empty);
      * the list contains ``address``               -> 'tracked';
      * the list succeeds WITHOUT ``address``        -> 'empty'
        (a fresh empty state, or the valid-empty state a successful destroy leaves —
        e.g. the delete-test pool after its test-phase destroy).

    Callers reconcile 'absent'/'empty' against live cloud state (reporting idempotent
    success only after confirmed absence) and treat 'unreadable' as
    ``ownership_unprovable``. Run ``terraform_init`` before this so a local-backend
    state read never trips an "initialization required" error.
    """
    if not state_exists(module_dir, state_file):
        return "absent"
    rc, out = _run(
        ["terraform", "state", "list", f"-state={state_file}"],
        cwd=module_dir,
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        return "unreadable"
    addresses = [line.strip() for line in out.splitlines() if line.strip()]
    return "tracked" if address in addresses else "empty"


# --------------------------------------------------------------------------- #
# gcloud / kubectl                                                            #
# --------------------------------------------------------------------------- #


def gcloud(args: list[str], *, timeout: int = 180, echo: bool = True) -> tuple[int, str]:
    return _run(["gcloud", *args], timeout=timeout, echo=echo)


def kubectl(args: list[str], *, timeout: int = 120, echo: bool = True) -> tuple[int, str]:
    return _run(["kubectl", *args], timeout=timeout, echo=echo)


def install_kubeconfig(cluster_name: str, location: str, project: str, *, timeout: int = 180) -> None:
    """Install the kubeconfig where ambient kubectl reads it (GKE analog of
    `aws eks update-kubeconfig`)."""
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "get-credentials",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
        ],
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] `gcloud container clusters get-credentials` failed "
            f"for {cluster_name} in {location}: {fold_tail(out)}",
        )


def gke_cluster_exists(cluster_name: str, location: str, project: str, *, timeout: int = 120) -> bool:
    """True when a GKE cluster with this run-scoped name already exists.

    The run-scoped name uniquely identifies THIS run's cluster, so an existing one
    is the cluster an earlier per-step worker in the run provisioned and the
    harness preserved (GCP_K8S_SKIP_TEARDOWN); setup ADOPTS it instead of colliding
    on create. A describe failure that is NOT a clean not-found RAISES — an
    auth/permission error must never be misread as "absent, safe to create".
    """
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "describe",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=value(name)",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc == 0:
        return True
    bucket = _classify_cli_output(out)
    if bucket == "not_found":
        return False
    raise LifecycleError(
        bucket,
        f"[bucket={bucket}] could not determine whether GKE cluster {cluster_name} exists in {location}: {fold_tail(out)}",
    )


def gke_node_pool_exists(cluster_name: str, pool_name: str, location: str, project: str, *, timeout: int = 120) -> bool:
    """True when the run-scoped node pool already exists on the cluster (adopt gate)."""
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "describe",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=value(name)",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc == 0:
        return True
    bucket = _classify_cli_output(out)
    if bucket == "not_found":
        return False
    raise LifecycleError(
        bucket,
        f"[bucket={bucket}] could not determine whether node pool {pool_name} exists on {cluster_name}: {fold_tail(out)}",
    )


def gke_node_pool_zone(
    cluster_name: str, pool_name: str, location: str, project: str, *, timeout: int = 120
) -> str | None:
    """Resolve an EXISTING node pool's actual zone — TRI-STATE.

    Returns:
      * the pool's first zone   — the pool exists and its locations read cleanly;
      * ``None``                — the pool is CONFIRMED ABSENT (describe not_found),
                                  so the caller may safely run the capacity preflight
                                  to place a fresh pool;
      * raises ``LifecycleError`` — the describe was UNREADABLE (transient / auth /
                                  permission) OR succeeded but rendered no
                                  zone-shaped token (malformed output).

    Why tri-state, not "None on any failure": when ADOPTING/reconciling an existing
    GPU pool the caller reuses its ACTUAL zone rather than re-running the
    non-deterministic capacity preflight, because handing a reconcile apply a
    different ``node_locations`` would drift (potentially REPLACE) the live pool.
    Collapsing an unreadable read to ``None`` would let the caller substitute a
    fabricated, capacity-selected zone and feed it into reconciliation of a pool
    that STILL EXISTS — silently mutating/replacing a preserved, run-owned GPU pool
    on a transient read blip. So only a CONFIRMED-absent pool falls back to
    preflight; an existing-or-unknown pool must yield its real zone or FAIL CLOSED.
    """
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "describe",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=value(locations)",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        if bucket == "not_found":
            # Pool is genuinely gone: safe for the caller to run capacity preflight.
            return None
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not read the zone of existing GPU node pool "
            f"{pool_name} on {cluster_name}; refusing to substitute a "
            f"capacity-selected zone that could drift/replace the live pool: {fold_tail(out)}",
        )
    # `value(locations)` renders the zone list joined by ';' (gcloud may also
    # prepend a WARNING line); return the first zone-shaped token found.
    for line in out.splitlines():
        for token in re.split(r"[;,]", line):
            token = token.strip()
            if re.fullmatch(r"[a-z]+-[a-z0-9]+-[a-z]", token):
                return token
    # Describe SUCCEEDED but rendered no zone-shaped token: the pool exists yet its
    # zone is unreadable/malformed. Fail closed rather than fall back to preflight
    # and reconcile the existing pool onto a fabricated zone.
    raise LifecycleError(
        "unknown_error",
        f"[bucket=unknown_error] GPU node pool {pool_name} on {cluster_name} describe "
        f"returned no zone-shaped locations token; refusing to substitute a "
        f"capacity-selected zone that could drift/replace the live pool: {fold_tail(out)}",
    )


# Recognized GKE node-pool status tokens (gcloud may merge a WARNING line into a
# `value(status)` render, so only a known token is accepted as a pool state).
_GKE_POOL_STATES = frozenset(
    {"PROVISIONING", "RUNNING", "RUNNING_WITH_ERROR", "RECONCILING", "STOPPING", "ERROR", "STATUS_UNSPECIFIED"}
)
# Terminally-unhealthy GKE node-pool states: a pool here has failed and will not
# converge on its own, so a completion gate must fail closed rather than wait out
# its whole timeout.
_GKE_POOL_ERROR_STATES = frozenset({"ERROR", "RUNNING_WITH_ERROR"})

# CLI failure buckets a readiness poll must SURFACE immediately instead of waiting
# out: expired/invalid credentials, a missing IAM permission, or a resource that
# is genuinely gone never clear on their own, so blocking the full readiness
# budget only delays an actionable diagnostic and buries it under a generic
# timeout. A 'transient' read (rate-limit / quota / stockout / network blip) or an
# unclassifiable 'unknown_error' is instead RETAINED and retried, then surfaced
# verbatim if the wait ultimately expires — never silently read as "keep waiting".
_TERMINAL_READ_BUCKETS = frozenset({"access_denied", "credentials_invalid", "not_found"})


def gke_node_pool_status(
    cluster_name: str, pool_name: str, location: str, project: str, *, timeout: int = 120
) -> tuple[str, LifecycleError | None]:
    """Return ``(status, read_failure)`` for a live GKE node pool.

    ``status`` is a recognized GKE status token (RUNNING, RECONCILING,
    PROVISIONING, ERROR, RUNNING_WITH_ERROR, STOPPING, ...) or '' when the describe
    SUCCEEDED but rendered no recognized token (e.g. a merged gcloud WARNING line);
    the caller keeps waiting on ''.

    ``read_failure`` is None on a clean read. On a FAILED describe it is a
    classified LifecycleError (constructed, never raised here) carrying the folded
    gcloud output, so the completion gate can surface a terminal
    auth/permission/not-found failure IMMEDIATELY and retain a transient one for
    its timeout diagnostic — instead of silently collapsing a command failure into
    an empty (indistinguishable "still waiting") status that is waited out for the
    whole readiness budget."""
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "describe",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=value(status)",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        return "", LifecycleError(
            bucket,
            f"[bucket={bucket}] `gcloud container node-pools describe` failed for pool "
            f"{pool_name} on {cluster_name} in {location}: {fold_tail(out)}",
        )
    for line in out.splitlines():
        token = line.strip()
        if token in _GKE_POOL_STATES:
            return token, None
    return "", None


_GKE_EFFECT_TO_K8S = {
    "NO_SCHEDULE": "NoSchedule",
    "PREFER_NO_SCHEDULE": "PreferNoSchedule",
    "NO_EXECUTE": "NoExecute",
}


def verify_adopted_node_pool_shape(
    cluster_name: str,
    pool_name: str,
    location: str,
    project: str,
    expected_machine_type: str,
    expected_labels: dict[str, Any],
    expected_taints: list[dict[str, Any]],
    *,
    expected_node_count: int | None = None,
    expected_node_locations: list[str] | None = None,
    expected_accelerator_type: str = "",
    expected_accelerator_count: int = 0,
    timeout: int = 120,
) -> None:
    """Prove an ADOPTED node pool actually has the shape this step would emit.

    On the adopt (import + refresh-only) path the emitted ``expected_*`` outputs
    are derived from Terraform INPUT variables, not from the live pool, so a
    preserved same-name pool with a different machine type / labels / taints would
    be reported with a fabricated shape the released K8sNodePoolCheck then asserts.
    Read the live pool and fail CLOSED unless its machine type matches exactly and
    every expected label / taint is actually present (GKE may add its own labels,
    so those expectations must be a SUBSET of the live set, never exact-equal).

    The requested NODE COUNT, node LOCATIONS, and GPU accelerator TYPE/COUNT are
    verified against the live pool too. ``desired_size`` (the emitted
    ``expected_replicas``) is read from the pool's own refreshed Terraform state, so
    on the adopt path it would otherwise be seeded by the pool's OWN live drift —
    letting the released count check compare the live count against itself. Requiring
    the live count / locations / accelerator to equal the CONTRACT inputs closes that
    self-seeding gap: a preserved same-name pool of a different size, zone placement,
    or GPU shape fails closed instead of being emitted with a self-fulfilling
    contract. A describe failure RAISES — an unverifiable adopted pool is never
    emitted with an assumed shape."""
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "describe",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=json",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not describe adopted node pool {pool_name} on "
            f"{cluster_name} to verify its shape before emitting it: {fold_tail(out)}",
        )
    try:
        pool = json.loads(out) or {}
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] node-pool describe returned unparseable JSON while "
            f"verifying adopted pool {pool_name} shape: {exc}",
        ) from exc
    config = pool.get("config") or {}

    observed_machine = str(config.get("machineType", "") or "")
    if expected_machine_type and observed_machine != expected_machine_type:
        raise LifecycleError(
            "config_error",
            f"[bucket=config_error] adopted node pool {pool_name} machine type mismatch: "
            f"expected '{expected_machine_type}' but the live pool runs '{observed_machine}'. "
            "Refusing to emit a fabricated pool shape for a preserved pool that does not "
            "match the contract inputs.",
        )

    observed_labels = config.get("labels") or {}
    observed_labels = {str(k): str(v) for k, v in observed_labels.items()} if isinstance(observed_labels, dict) else {}
    for key, value in (expected_labels or {}).items():
        if observed_labels.get(str(key)) != str(value):
            raise LifecycleError(
                "config_error",
                f"[bucket=config_error] adopted node pool {pool_name} is missing expected "
                f"node label {key}={value} (live labels: {sorted(observed_labels)}). Refusing "
                "to emit a pool shape the live pool does not actually carry.",
            )

    observed_taints = {
        (
            str(t.get("key", "")),
            str(t.get("value", "")),
            _GKE_EFFECT_TO_K8S.get(str(t.get("effect", "")), str(t.get("effect", ""))),
        )
        for t in (config.get("taints") or [])
        if isinstance(t, dict)
    }
    for taint in expected_taints or []:
        want = (str(taint.get("key", "")), str(taint.get("value", "")), str(taint.get("effect", "")))
        if want not in observed_taints:
            raise LifecycleError(
                "config_error",
                f"[bucket=config_error] adopted node pool {pool_name} is missing expected "
                f"taint {want[0]}={want[1]}:{want[2]} (live taints: {sorted(observed_taints)}). "
                "Refusing to emit a pool shape the live pool does not actually enforce.",
            )

    # Requested per-zone node count (GKE reports it as initialNodeCount). The test
    # pool is single-zone (node_locations pinned) and never autoscaled, so the live
    # value equals the contract input on a genuine same-run adopt; a preserved
    # same-name pool of a different size must NOT be emitted with its own live count
    # as the expected_replicas the released count check then trivially satisfies.
    if expected_node_count is not None:
        try:
            observed_count = int(pool.get("initialNodeCount", 0) or 0)
        except (TypeError, ValueError):
            observed_count = -1
        if observed_count != int(expected_node_count):
            raise LifecycleError(
                "config_error",
                f"[bucket=config_error] adopted node pool {pool_name} node-count mismatch: "
                f"expected {int(expected_node_count)} but the live pool runs {observed_count}. "
                "Refusing to emit a fabricated replica count for a preserved pool that does not "
                "match the contract input (the live count must not seed its own expectation).",
            )

    # Requested zone placement. A preserved pool placed in a different zone would
    # otherwise be adopted and emitted as though it matched the requested locations.
    if expected_node_locations:
        observed_locations = {str(z) for z in (pool.get("locations") or [])}
        want_locations = {str(z) for z in expected_node_locations}
        if not want_locations.issubset(observed_locations):
            raise LifecycleError(
                "config_error",
                f"[bucket=config_error] adopted node pool {pool_name} node-location mismatch: "
                f"expected {sorted(want_locations)} but the live pool runs in {sorted(observed_locations)}. "
                "Refusing to emit a pool whose zone placement does not match the contract input.",
            )

    # Requested GPU accelerator type/count (GPU pools only; GKE exposes each as
    # config.accelerators[].acceleratorType / acceleratorCount, the latter a string).
    if expected_accelerator_type and expected_accelerator_count > 0:
        observed_accels: list[str] = []
        matched = False
        for accel in config.get("accelerators") or []:
            if not isinstance(accel, dict):
                continue
            a_type = str(accel.get("acceleratorType", "") or "")
            try:
                a_count = int(accel.get("acceleratorCount", 0) or 0)
            except (TypeError, ValueError):
                a_count = -1
            observed_accels.append(f"{a_type}x{a_count}")
            if a_type == expected_accelerator_type and a_count == int(expected_accelerator_count):
                matched = True
        if not matched:
            raise LifecycleError(
                "config_error",
                f"[bucket=config_error] adopted node pool {pool_name} GPU accelerator mismatch: "
                f"expected {expected_accelerator_type}x{int(expected_accelerator_count)} but the live "
                f"pool exposes {observed_accels or '[]'}. Refusing to emit a GPU pool shape the live "
                "pool does not actually carry.",
            )


# --------------------------------------------------------------------------- #
# API-server endpoint resolution (binds the ACL probe to the reviewed cluster)#
# --------------------------------------------------------------------------- #


def _normalize_api_endpoint(url: str) -> str | None:
    """Return a normalized ``https://host:port`` URL, or None if not usable.

    The K8sApiNetworkAclCheck only enforces its target-origin and
    kubeconfig-consistency guards when ``api_endpoint`` is a valid HTTPS URL
    (scheme + host + port). We emit an explicit port so the value is
    unambiguous; the validator normalizes both sides to origins, so a
    ``:443``-suffixed value still matches a bare ``https://host`` kubeconfig
    server. Anything that is not HTTPS-with-a-host resolves to None so callers
    fail closed rather than emit a value the validator would reject.
    """
    url = (url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    try:
        port = parts.port if parts.port is not None else 443
    except ValueError:
        return None
    host = parts.hostname
    if ":" in host and not host.startswith("["):  # bracket bare IPv6 literals
        host = f"[{host}]"
    return f"https://{host}:{port}"


def _server_from_kubeconfig(raw: str) -> str:
    """Extract ``clusters[0].cluster.server`` from `kubectl config view` JSON."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    clusters = payload.get("clusters") if isinstance(payload, dict) else None
    if not isinstance(clusters, list) or not clusters or not isinstance(clusters[0], dict):
        return ""
    return str((clusters[0].get("cluster") or {}).get("server") or "").strip()


def resolve_api_endpoint(cluster_name: str, location: str, project: str, *, timeout: int = 60) -> str | None:
    """Resolve the normalized ``https://host:port`` Kubernetes API server URL.

    Tries the installed kubeconfig first — that is the SAME server string the
    ACL validator derives via ``kubectl config view --minify``, so the emitted
    ``api_endpoint`` and the validator's kubeconfig-consistency check always
    agree — then falls back to the GKE control-plane endpoint from the API
    (a bare host/IP, wrapped as ``https://host``). Returns None when neither
    source yields a usable HTTPS URL; a caller that has enabled the
    outside-vantage ACL probe MUST fail closed on None so the probe can never
    be scored against an unbound (or unrelated) endpoint.
    """
    # 1) kubeconfig — authoritative: what kubectl (and the validator) target.
    rc, out = kubectl(["config", "view", "--minify", "-o", "json"], timeout=timeout, echo=False)
    if rc == 0:
        normalized = _normalize_api_endpoint(_server_from_kubeconfig(out))
        if normalized:
            return normalized
    # 2) GKE API fallback — `describe --format=value(endpoint)` is a bare host.
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "describe",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=value(endpoint)",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc == 0:
        host = out.strip().splitlines()[-1].strip() if out.strip() else ""
        normalized = _normalize_api_endpoint(f"https://{host}" if host else "")
        if normalized:
            return normalized
    return None


# --------------------------------------------------------------------------- #
# Operator network / API-ACL capability inputs                                #
# --------------------------------------------------------------------------- #


def normalize_sentinel(value: str) -> str:
    """Map the provider-config `none` sentinel (used so the arg renderer never
    drops an empty value token) back to absence. Returns the stripped value, or
    "" when it is empty or the literal `none` (case-insensitive)."""
    v = (value or "").strip()
    return "" if v.lower() == "none" else v


def normalize_network(value: str) -> str:
    """Resolve the operator-selected VPC network: the `none` sentinel or a blank
    value falls back to `default` (projects that retain the auto-created default
    VPC). A non-blank value (name or self-link) is used verbatim."""
    v = normalize_sentinel(value)
    return v or "default"


def _net_identity(value: str) -> str:
    """Reduce a network/subnetwork name or self-link to a comparable identity:
    the last path segment, lower-cased (so `default` and
    `projects/p/global/networks/default` compare equal)."""
    v = (value or "").strip().rstrip("/")
    return v.rsplit("/", 1)[-1].lower() if v else ""


def _with_host_prefix(token: str) -> str:
    """Return a bare IP address as a single-HOST CIDR whose prefix matches the
    address family; a token that already carries a ``/prefix`` is returned as-is.

    A bare value is parsed as an IP ADDRESS first so the host prefix is correct
    per family: IPv4 -> ``/32``, IPv6 -> ``/128``. The old behavior appended a
    literal ``/32`` to EVERY bare value, which turned a bare IPv6 host such as
    ``2001:db8::1`` into an IPv6 ``/32`` network — a catastrophic widening of a
    control-plane allow-list from one host to 2**96 addresses. A token that is not
    a bare address (already has ``/``, or is not a parseable IP) is returned
    unchanged so the caller's own CIDR parse raises/normalizes it uniformly. This
    is the ONE canonical normalizer shared by the request and readback paths so a
    bare host and its live readback always compare equal."""
    if "/" in token:
        return token
    try:
        addr = ipaddress.ip_address(token)
    except ValueError:
        return token
    return f"{token}/{addr.max_prefixlen}"


def normalize_authorized_cidrs(raw: str) -> list[str]:
    """Parse the comma-separated control-plane authorized CIDR list.

    The `none` sentinel or a blank value returns [] (authorized networks left
    unconfigured). A bare IPv4 normalizes to /32 and a bare IPv6 to /128 (a single
    host, never a widened range). Every entry must be a valid CIDR and MUST NOT be
    world-open (0.0.0.0/0 or ::/0) — a world-open entry defeats the ACL, so it is a
    hard config_error, never a silent pass."""
    v = normalize_sentinel(raw)
    if not v:
        return []
    out: list[str] = []
    for token in v.split(","):
        token = token.strip()
        if not token:
            continue
        candidate = _with_host_prefix(token)
        try:
            net = ipaddress.ip_network(candidate, strict=False)
        except ValueError as exc:
            raise LifecycleError(
                "config_error",
                f"[bucket=config_error] GCP_K8S_AUTHORIZED_CIDRS entry '{token}' is not a valid IP CIDR: {exc}.",
            ) from exc
        if net.prefixlen == 0:
            raise LifecycleError(
                "config_error",
                "[bucket=config_error] GCP_K8S_AUTHORIZED_CIDRS may not contain a world-open "
                f"range ('{token}' normalizes to {net}); authorized networks must name the "
                "runner's actual egress CIDR, never 0.0.0.0/0 or ::/0.",
            )
        out.append(str(net))
    return out


def render_unauthorized_probe(template: str, api_endpoint: str) -> str:
    """Substitute the run's resolved API URL into the outside-vantage probe
    template. The template MUST contain the literal ``{api_endpoint}`` — without
    it the probe would target a fixed host and could report a false ACL PASS, so
    a missing placeholder is a config_error."""
    t = normalize_sentinel(template)
    if not t:
        return ""
    if "{api_endpoint}" not in t:
        raise LifecycleError(
            "config_error",
            "[bucket=config_error] GCP_K8S_UNAUTHORIZED_PROBE_CMD must contain the literal "
            "{api_endpoint} so setup can bind the probe to THIS run's resolved GKE API URL; "
            "a fixed-host probe could report a false API-ACL PASS.",
        )
    return t.replace("{api_endpoint}", api_endpoint)


def verify_and_read_network(
    cluster_name: str,
    location: str,
    project: str,
    expected_network: str,
    *,
    timeout: int = 120,
) -> tuple[str, str]:
    """Read the live GKE cluster's network + subnetwork and verify the network
    matches the operator-selected value (normalized name/self-link identity).

    Returns ``(network, subnetwork)`` observed from the live cluster so setup's
    success is derived from real state, not only the Terraform input. A describe
    failure or a network mismatch RAISES so setup never emits a cluster attached
    to an unexpected VPC."""
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "describe",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=json",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not describe cluster {cluster_name} to verify its network: {fold_tail(out)}",
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] cluster describe returned unparseable JSON while verifying the network: {exc}",
        ) from exc
    observed_network = str(data.get("network", "") or "")
    observed_subnetwork = str(data.get("subnetwork", "") or "")
    if _net_identity(observed_network) != _net_identity(expected_network):
        raise LifecycleError(
            "config_error",
            "[bucket=config_error] GKE cluster network mismatch: operator selected "
            f"'{expected_network}' but the live cluster attached to '{observed_network}'. "
            "Refusing to emit inventory for a cluster on an unexpected VPC.",
        )
    return observed_network, observed_subnetwork


def same_network(a: str, b: str) -> bool:
    """True when two network/subnetwork names or self-links denote the same VPC
    resource (compares the last path segment, case-insensitive)."""
    return _net_identity(a) == _net_identity(b)


def _normalize_cidr_set(cidrs: list[str]) -> set[str]:
    """Normalize a list of CIDR strings into a comparable set (bare IPv4 -> /32,
    bare IPv6 -> /128, canonical network form) via the SAME `_with_host_prefix`
    normalizer the request path uses, so a requested bare host and its live
    readback compare equal. Unparseable tokens are kept lower-cased so a genuine
    mismatch never silently compares equal."""
    out: set[str] = set()
    for token in cidrs:
        token = (token or "").strip()
        if not token:
            continue
        candidate = _with_host_prefix(token)
        try:
            out.add(str(ipaddress.ip_network(candidate, strict=False)))
        except ValueError:
            out.add(token.lower())
    return out


def verify_authorized_networks(
    cluster_name: str,
    location: str,
    project: str,
    expected_cidrs: list[str],
    *,
    timeout: int = 120,
) -> list[str]:
    """Read the live GKE master_authorized_networks source set and require it to
    equal the operator-requested authorized CIDRs (normalized, order-independent).

    K8sApiNetworkAclCheck's outside-vantage probe only proves ENFORCEMENT when the
    live allow-list is exactly the policy the operator asked for. A fresh create
    could omit the block (empty var), and a reused/adopted cluster could carry a
    DIFFERENT live allow-list than the requested CIDRs — in either case a failing
    remote command would misread as ACL enforcement. Fail CLOSED (config_error) on
    a missing or mismatched policy; return the observed CIDRs on success."""
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "describe",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=json",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not describe cluster {cluster_name} to verify its "
            f"authorized networks: {fold_tail(out)}",
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] cluster describe returned unparseable JSON while "
            f"verifying authorized networks: {exc}",
        ) from exc
    config = data.get("masterAuthorizedNetworksConfig") or {}
    observed_blocks = config.get("cidrBlocks") or []
    observed = [
        str(block.get("cidrBlock", "")).strip()
        for block in observed_blocks
        if isinstance(block, dict) and str(block.get("cidrBlock", "")).strip()
    ]
    expected_set = _normalize_cidr_set(expected_cidrs)
    observed_set = _normalize_cidr_set(observed)
    if not observed_set or observed_set != expected_set:
        raise LifecycleError(
            "config_error",
            "[bucket=config_error] GKE master_authorized_networks mismatch on "
            f"{cluster_name}: requested {sorted(expected_set)} but the live cluster "
            f"enforces {sorted(observed_set) or '[]'}. Refusing to emit an API-ACL probe "
            "against a cluster whose live allow-list differs from the requested policy — "
            "a failing probe would misread as ACL enforcement.",
        )
    return sorted(observed_set)


def read_cluster_membership(
    cluster_name: str,
    location: str,
    project: str,
    *,
    timeout: int = 120,
) -> tuple[str, str, str]:
    """Describe the live GKE cluster and return ``(network, subnetwork, status)``.

    ``status`` maps the GKE up-state 'RUNNING' to the contract sentinel 'ACTIVE'
    (see gke_cluster_status_active); any other reachable state is surfaced
    verbatim. A failed describe RAISES a classified LifecycleError so a
    membership check never reads an unknown cluster as valid."""
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "describe",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=json",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not describe cluster {cluster_name} to read its "
            f"network membership: {fold_tail(out)}",
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] cluster describe returned unparseable JSON while "
            f"reading network membership: {exc}",
        ) from exc
    network = str(data.get("network", "") or "")
    subnetwork = str(data.get("subnetwork", "") or "")
    raw_status = str(data.get("status", "") or "").strip().upper()
    status = "ACTIVE" if raw_status == "RUNNING" else (raw_status or "UNKNOWN")
    return network, subnetwork, status


# --------------------------------------------------------------------------- #
# Cloud-side ownership marker (adopt-safety)                                   #
# --------------------------------------------------------------------------- #


def _read_cluster_labels(
    cluster_name: str, location: str, project: str, *, timeout: int = 120
) -> dict[str, str] | None:
    """Return the live cluster's resource labels, or None when it is a clean
    not-found. Any other describe failure RAISES so an unreadable cluster is never
    silently treated as un-owned (which would defeat the fail-closed adopt gate)."""
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "describe",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=json",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        if bucket == "not_found":
            return None
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not describe cluster {cluster_name} to read its "
            f"ownership marker: {fold_tail(out)}",
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] cluster describe returned unparseable JSON while "
            f"reading the ownership marker: {exc}",
        ) from exc
    labels = data.get("resourceLabels") or {}
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


def verify_cluster_ownership(cluster_name: str, location: str, project: str, *, timeout: int = 120) -> None:
    """Fail CLOSED unless the live cluster carries THIS run's exact ownership marker.

    Adoption imports an EXISTING cloud cluster into Terraform state, making it
    eligible for a later ``terraform destroy``. A bare run-scoped NAME match must
    never authorize that import: a stale cluster from another run whose id shares
    this run's 8-char prefix, or an operator-precreated cluster that collides on the
    name, would otherwise be adopted and destroyed as though this run owned it — a
    destructive reuse-safety failure. Require the full-run-identity label
    (``OWNERSHIP_LABEL_KEY``) the owning run stamped at create."""
    labels = _read_cluster_labels(cluster_name, location, project, timeout=timeout)
    if labels is None:
        raise LifecycleError(
            "not_found",
            f"[bucket=not_found] cluster {cluster_name} vanished before its ownership marker "
            "could be verified for adoption; retry.",
        )
    expected = full_run_scope_id()
    observed = labels.get(OWNERSHIP_LABEL_KEY)
    if observed != expected:
        raise LifecycleError(
            "config_error",
            f"[bucket=config_error] refusing to adopt GKE cluster {cluster_name}: its "
            f"cloud-side ownership marker {OWNERSHIP_LABEL_KEY}='{observed or '<absent>'}' does "
            f"not match this run's identity '{expected}'. A run-scoped name match alone does not "
            "prove ownership; a stale, colliding, or operator-precreated same-name cluster must "
            "not be imported into Terraform state (and later destroyed) as though this run owned "
            "it. Tear down the pre-existing cluster or use a fresh RUN_ID.",
        )


def ensure_cluster_ownership_label(
    cluster_name: str, location: str, project: str, *, fresh_create: bool, timeout: int = 300
) -> None:
    """Confirm — or, only for a cluster THIS run just created, stamp — the
    cloud-side full-run-identity ownership marker. FAIL CLOSED so a foreign cluster
    is never relabeled as run-owned:

    * marker already == this run -> no-op (the common path: the cluster is stamped
      ATOMICALLY at Terraform creation via `resource_labels`, so both a fresh
      create and an in-state/adopted cluster normally match here).
    * marker present but a DIFFERENT run -> REFUSE (raise). Overwriting it (the old
      behavior) would let a state-tracked cluster that was deleted and replaced by a
      colliding same-name cluster be relabeled — and later destroyed — as though
      this run owned it.
    * marker ABSENT -> only a fresh create (a cluster we KNOW we just provisioned)
      may stamp it, as a belt-and-suspenders backstop if the Terraform label write
      has not yet propagated to describe. An adopted/in-state cluster carrying NO
      marker is NOT backfilled from local state alone — it fails closed, because a
      genuinely run-owned cluster was stamped at creation, so a missing marker means
      the live resource is not the one this run created.
    """
    labels = _read_cluster_labels(cluster_name, location, project)
    expected = full_run_scope_id()
    observed = labels.get(OWNERSHIP_LABEL_KEY) if labels is not None else None
    if observed == expected:
        return
    if observed is not None:
        raise LifecycleError(
            "config_error",
            f"[bucket=config_error] refusing to (re)stamp ownership on cluster {cluster_name}: "
            f"its live marker {OWNERSHIP_LABEL_KEY}='{observed}' belongs to a DIFFERENT run, not "
            f"this run's identity '{expected}'. A state-tracked cluster that was deleted and "
            "replaced by a colliding same-name cluster must never be relabeled (and later "
            "destroyed) as run-owned. Tear down the pre-existing cluster or use a fresh RUN_ID.",
        )
    if not fresh_create:
        raise LifecycleError(
            "config_error",
            f"[bucket=config_error] cluster {cluster_name} is tracked as run-owned but carries NO "
            f"cloud-side ownership marker {OWNERSHIP_LABEL_KEY}; refusing to backfill it from local "
            "state alone (the marker is stamped atomically at Terraform creation). A missing marker "
            "on an adopted cluster means the live resource is not the one this run created.",
        )
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "update",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            f"--update-labels={OWNERSHIP_LABEL_KEY}={expected}",
        ],
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not stamp the ownership marker "
            f"{OWNERSHIP_LABEL_KEY}={expected} on {cluster_name}; refusing to leave an owned "
            f"cluster unprovable for a later cross-worker adopt: {fold_tail(out)}",
        )


def destroy_ownership_ok(cluster_name: str, location: str, project: str) -> tuple[bool, str]:
    """Gate a state-targeted cluster DESTROY on live ownership; returns (ok, reason).

    A destroy is state-targeted, so a state entry that now resolves to a colliding
    same-name FOREIGN cluster (the original was deleted and replaced out of band)
    could otherwise destroy a resource this run does not own. Fail CLOSED unless
    ownership is POSITIVELY proven — permit the destroy ONLY when:
    * the live ownership marker equals this run's identity (we own it), OR
    * the cluster is a clean, positively classified ``not_found`` (already gone, so
      the state-targeted destroy is an idempotent no-op reconcile).
    EVERY other outcome leaves ownership unproven and returns ``ok=False``:
    * a marker present-but-a-different-run (a replaced same-name FOREIGN cluster),
    * a marker entirely absent on a live cluster (a genuinely run-owned cluster is
      stamped at Terraform creation, so a missing marker means the live resource is
      not ours), AND
    * a marker that is UNREADABLE — an authentication, permission, transport, or
      malformed-response describe failure. An unreadable marker MUST NOT authorize
      destruction: it cannot prove the same-name cluster belongs to this run, so
      treating a describe flake as "proceed" could destroy a foreign cluster. The
      caller surfaces this as a visible structured failure; a later rerun, once the
      marker is readable again, recovers and destroys the genuinely-owned cluster."""
    try:
        labels = _read_cluster_labels(cluster_name, location, project)
    except LifecycleError as exc:
        return False, (
            f"ownership marker for cluster {cluster_name} is unreadable ({exc.detail}); "
            "refusing to destroy a state-targeted cluster whose run ownership cannot be "
            "proven (an unreadable marker is never treated as owned)"
        )
    if labels is None:
        return True, "live cluster already absent; state-targeted destroy is a no-op reconcile"
    expected = full_run_scope_id()
    observed = labels.get(OWNERSHIP_LABEL_KEY)
    if observed == expected:
        return True, "live ownership marker matches this run"
    return False, (
        f"live cluster {cluster_name} carries ownership marker {OWNERSHIP_LABEL_KEY}="
        f"'{observed or '<absent>'}', not this run's '{expected}'; refusing to destroy a cluster "
        "this run does not own (state may point at a deleted-and-replaced same-name cluster)"
    )


def discard_cluster_state(module_dir: Path, state_file: str) -> None:
    """Discard a STALE primary/secondary cluster state file so setup rebuilds the
    state-owned cluster from scratch.

    Used when local Terraform state still tracks a cluster the cloud no longer has
    (deleted out of band, or a partial prior create): a refresh-only would just drop
    the phantom from state and never rebuild it, so later readiness/autoscaling
    checks would fail loudly instead of restoring the cluster. The state file only
    ever tracks THIS run's own cluster + its in-module baseline pools, so discarding
    it is safe — the fresh apply that follows recreates every resource under the
    same run-scoped names."""
    try:
        (module_dir / state_file).unlink(missing_ok=True)
        # terraform writes a .backup sidecar; drop it too so the next apply starts
        # from a truly empty state rather than resurrecting the phantom.
        (module_dir / f"{state_file}.backup").unlink(missing_ok=True)
    except OSError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] could not discard stale cluster state {state_file} in "
            f"{module_dir.name} to reconcile a cloud-absent cluster: {exc}",
        ) from exc


# --------------------------------------------------------------------------- #
# Ambiguous-create / state-absent reconciliation (no billable escape)         #
# --------------------------------------------------------------------------- #


def wait_cluster_absent(
    cluster_name: str, location: str, project: str, *, timeout: int = 1800, poll_interval: int = 15
) -> None:
    """Block until the exact GKE cluster is CONFIRMED absent (describe -> not_found).

    ``terraform destroy`` / ``gcloud ... delete`` returns once the delete OPERATION is
    accepted, but only confirmed cloud absence proves a billable cluster is truly gone.
    Poll ``gke_cluster_exists`` (tri-state: False only on a clean not_found; an
    unreadable describe RAISES) and RAISE on timeout, so a reconcile never reports a
    cluster reclaimed while it may still exist."""
    deadline = time.time() + timeout
    while True:
        if not gke_cluster_exists(cluster_name, location, project):
            return
        if time.time() >= deadline:
            raise LifecycleError(
                "cleanup_incomplete",
                f"[bucket=cleanup_incomplete] GKE cluster {cluster_name} in {location} is still "
                f"present {timeout}s after its reconcile destroy was issued; refusing to report it "
                "reclaimed while a billable cluster may remain.",
            )
        time.sleep(poll_interval)


def wait_node_pool_absent(
    cluster_name: str, pool_name: str, location: str, project: str, *, timeout: int = 1200, poll_interval: int = 15
) -> None:
    """Block until the exact GKE node pool is CONFIRMED absent (describe -> not_found).

    Poll ``gke_node_pool_exists`` (tri-state: False only on a clean not_found; an
    unreadable describe RAISES) and RAISE on timeout, so a reconcile never reports a
    pool reclaimed while it may still exist."""
    deadline = time.time() + timeout
    while True:
        if not gke_node_pool_exists(cluster_name, pool_name, location, project):
            return
        if time.time() >= deadline:
            raise LifecycleError(
                "cleanup_incomplete",
                f"[bucket=cleanup_incomplete] GKE node pool {pool_name} on {cluster_name} is still "
                f"present {timeout}s after its reconcile destroy was issued; refusing to report it "
                "reclaimed while a billable pool may remain.",
            )
        time.sleep(poll_interval)


def reconcile_orphaned_cluster(
    module_dir: Path,
    state_file: str,
    address: str,
    cluster_name: str,
    location: str,
    project: str,
    tf_vars: dict[str, Any],
    *,
    destroy_timeout: int,
    wait_timeout: int = 1800,
) -> str:
    """Ambiguous-create / state-absent reconciliation for the EXACT GKE cluster.

    A ``terraform apply`` timeout or interruption can submit the deterministic cluster
    create and leave the exact resource present before its local state address is
    durable; an absent or valid-empty state then hides a billable leak. Describe the
    exact deterministic cluster in the known project/location and reconcile:

      * confirmed not-found (``gke_cluster_exists`` False) -> 'absent' (clean; nothing
        leaked);
      * present AND carrying THIS run's full ownership marker -> import the exact
        address (when not already tracked), ``terraform destroy``, and wait for
        CONFIRMED cloud absence -> 'reclaimed';
      * present but the ownership marker is UNREADABLE or belongs to a DIFFERENT run ->
        ``verify_cluster_ownership`` RAISES a visible failure and the live cluster is
        left untouched.

    Only the ONE exact deterministic name is ever touched — never a prefix, label
    inventory, or project-wide deletion sweep."""
    if not gke_cluster_exists(cluster_name, location, project):
        return "absent"
    # Present: FAIL CLOSED unless the live cluster carries THIS run's exact marker.
    verify_cluster_ownership(cluster_name, location, project)
    cluster_id = f"projects/{project}/locations/{location}/clusters/{cluster_name}"
    terraform_init(module_dir)
    if not terraform_state_has(module_dir, state_file, address):
        terraform_import(module_dir, state_file, address, cluster_id, tf_vars)
    terraform_destroy(module_dir, state_file, tf_vars, timeout=destroy_timeout)
    wait_cluster_absent(cluster_name, location, project, timeout=wait_timeout)
    return "reclaimed"


def reconcile_orphaned_node_pool(
    module_dir: Path,
    state_file: str,
    address: str,
    cluster_name: str,
    cluster_location: str,
    pool_name: str,
    project: str,
    tf_vars: dict[str, Any],
    *,
    destroy_timeout: int,
    wait_timeout: int = 1200,
) -> str:
    """Ambiguous-create / state-absent reconciliation for the EXACT GKE node pool.

    The node-pool analog of ``reconcile_orphaned_cluster`` for a pool beneath its
    exact parent cluster:

      * parent cluster confirmed not-found, or the pool confirmed not-found -> 'absent'
        (clean; a pool cannot outlive its parent);
      * pool present AND its EXACT parent cluster carries THIS run's full ownership
        marker -> import the exact pool address (when not already tracked),
        ``terraform destroy``, and wait for CONFIRMED absence -> 'reclaimed';
      * pool present but the parent's ownership marker is UNREADABLE or a DIFFERENT run
        -> ``verify_cluster_ownership`` RAISES; the live pool is left untouched.

    A node pool carries no independent ownership label, so the current-run marker is
    required on its EXACT PARENT cluster — never a bare pool-name match."""
    if not gke_cluster_exists(cluster_name, cluster_location, project):
        return "absent"
    if not gke_node_pool_exists(cluster_name, pool_name, cluster_location, project):
        return "absent"
    verify_cluster_ownership(cluster_name, cluster_location, project)
    pool_id = f"projects/{project}/locations/{cluster_location}/clusters/{cluster_name}/nodePools/{pool_name}"
    terraform_init(module_dir)
    if not terraform_state_has(module_dir, state_file, address):
        terraform_import(module_dir, state_file, address, pool_id, tf_vars)
    terraform_destroy(module_dir, state_file, tf_vars, timeout=destroy_timeout)
    wait_node_pool_absent(cluster_name, pool_name, cluster_location, project, timeout=wait_timeout)
    return "reclaimed"


def apply_cluster_with_recovery(
    module_dir: Path,
    state_file: str,
    address: str,
    cluster_name: str,
    location: str,
    project: str,
    tf_vars: dict[str, Any],
    *,
    apply_timeout: int,
    reconcile_destroy_timeout: int,
) -> None:
    """``terraform_apply`` a cluster module, reconciling an AMBIGUOUS create on failure.

    A ``terraform apply`` timeout / interruption can submit the GKE cluster create and
    leave the exact resource present before its state address is durable. Treat EVERY
    non-successful apply as an ambiguous create: reconcile the exact deterministic
    cluster (confirmed-absent is clean; a run-owned leak is imported, destroyed, and
    waited to confirmed absence; unreadable/mismatched ownership fails visibly) BEFORE
    re-raising, so a partially-created cluster can never escape cleanup with no durable
    state entry. The original apply diagnostic stays the reported failure unless the
    reconcile surfaces a more serious ownership/cleanup anomaly."""
    try:
        terraform_apply(module_dir, state_file, tf_vars, timeout=apply_timeout)
    except LifecycleError as apply_exc:
        try:
            outcome = reconcile_orphaned_cluster(
                module_dir,
                state_file,
                address,
                cluster_name,
                location,
                project,
                tf_vars,
                destroy_timeout=reconcile_destroy_timeout,
            )
        except LifecycleError as reconcile_exc:
            raise LifecycleError(
                reconcile_exc.bucket,
                f"{reconcile_exc.detail} (surfaced while reconciling an ambiguous cluster create "
                f"after apply failed: {apply_exc.detail})",
            ) from apply_exc
        log(
            f"note: reconciled ambiguous cluster create after apply failure — exact cluster "
            f"{cluster_name} was {outcome}; re-raising the original apply failure."
        )
        raise


def apply_node_pool_with_recovery(
    module_dir: Path,
    state_file: str,
    address: str,
    cluster_name: str,
    cluster_location: str,
    pool_name: str,
    project: str,
    tf_vars: dict[str, Any],
    *,
    apply_timeout: int,
    reconcile_destroy_timeout: int,
) -> None:
    """``terraform_apply`` a node-pool module, reconciling an AMBIGUOUS create on failure.

    The node-pool analog of ``apply_cluster_with_recovery``: on ANY apply failure,
    reconcile the exact deterministic pool beneath its run-owned parent (import +
    destroy + wait when owned, clean when confirmed-absent, fail-visibly on unreadable
    / mismatched parent ownership) BEFORE re-raising. Use ONLY on the FRESH-create path
    — an in-state re-apply (e.g. a scale) already has a durable state address that
    teardown destroys normally, so an apply failure there is a scale failure, not an
    ambiguous create."""
    try:
        terraform_apply(module_dir, state_file, tf_vars, timeout=apply_timeout)
    except LifecycleError as apply_exc:
        try:
            outcome = reconcile_orphaned_node_pool(
                module_dir,
                state_file,
                address,
                cluster_name,
                cluster_location,
                pool_name,
                project,
                tf_vars,
                destroy_timeout=reconcile_destroy_timeout,
            )
        except LifecycleError as reconcile_exc:
            raise LifecycleError(
                reconcile_exc.bucket,
                f"{reconcile_exc.detail} (surfaced while reconciling an ambiguous node-pool create "
                f"after apply failed: {apply_exc.detail})",
            ) from apply_exc
        log(
            f"note: reconciled ambiguous node-pool create after apply failure — exact pool "
            f"{pool_name} on {cluster_name} was {outcome}; re-raising the original apply failure."
        )
        raise


def recreate_baseline_system_pool(
    cluster_name: str,
    pool_name: str,
    location: str,
    project: str,
    *,
    machine_type: str,
    node_zone: str,
    node_count: int,
    min_nodes: int,
    max_nodes: int,
    timeout: int = 1200,
) -> None:
    """API-create a genuinely-absent baseline SYSTEM pool during adopt reconcile.

    Mirrors ``terraform/main.tf`` ``google_container_node_pool.system`` (single
    zone, GKE-managed autoscaling, cloud-platform scopes). Uses ``gcloud`` rather
    than a Terraform apply because a normal apply on an imported cluster would force
    a full cluster REPLACE (initial_node_count reads back as 0); a node-pool create
    can only ADD the pool and can NEVER replace the existing cluster."""
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "create",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--node-locations",
            node_zone,
            "--machine-type",
            machine_type,
            "--num-nodes",
            str(node_count),
            "--enable-autoscaling",
            "--min-nodes",
            str(min_nodes),
            "--max-nodes",
            str(max_nodes),
            "--scopes",
            "https://www.googleapis.com/auth/cloud-platform",
        ],
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not recreate absent baseline system pool {pool_name} on "
            f"{cluster_name}: {fold_tail(out)}",
        )


def recreate_baseline_gpu_pool(
    cluster_name: str,
    pool_name: str,
    location: str,
    project: str,
    *,
    machine_type: str,
    node_zone: str,
    node_count: int,
    accelerator_type: str,
    accelerator_count: int,
    timeout: int = 1800,
) -> None:
    """API-create a genuinely-absent baseline GPU pool during adopt reconcile.

    Mirrors ``terraform/main.tf`` ``google_container_node_pool.gpu`` (fixed
    single-zone pool, LATEST GKE-managed driver, cloud-platform scopes). gcloud
    (not Terraform) so it can only ADD the pool and never replace the cluster."""
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "create",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--node-locations",
            node_zone,
            "--machine-type",
            machine_type,
            "--num-nodes",
            str(node_count),
            "--accelerator",
            f"type={accelerator_type},count={accelerator_count},gpu-driver-version=LATEST",
            "--scopes",
            "https://www.googleapis.com/auth/cloud-platform",
        ],
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not recreate absent baseline GPU pool {pool_name} on "
            f"{cluster_name}: {fold_tail(out)}",
        )


def recreate_secondary_node_pool(
    cluster_name: str,
    pool_name: str,
    location: str,
    project: str,
    *,
    machine_type: str,
    node_zone: str,
    node_count: int,
    timeout: int = 1200,
) -> None:
    """API-create a genuinely-absent secondary shared-VPC node pool during adopt
    reconcile. Mirrors ``terraform-shared-vpc-cluster/main.tf``
    ``google_container_node_pool.secondary`` (machine_type + node_count default to
    the module's own defaults). gcloud so it can never replace the cluster."""
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "create",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--node-locations",
            node_zone,
            "--machine-type",
            machine_type,
            "--num-nodes",
            str(node_count),
            "--scopes",
            "https://www.googleapis.com/auth/cloud-platform",
        ],
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not recreate absent secondary node pool {pool_name} on "
            f"{cluster_name}: {fold_tail(out)}",
        )


# Module default for the secondary shared-VPC node pool (mirrors
# terraform-shared-vpc-cluster/variables.tf `machine_type` / `node_count`
# defaults); the create stub relies on those defaults, so a genuinely-absent
# secondary pool is recreated with the SAME shape.
SECONDARY_POOL_MACHINE_TYPE = "e2-standard-4"
SECONDARY_POOL_NODE_COUNT = 1


# --------------------------------------------------------------------------- #
# Managed (GKE) node-pool autoscaling readback + reconcile                    #
# --------------------------------------------------------------------------- #


def _read_node_pool_autoscaling(
    cluster_name: str, pool_name: str, location: str, project: str, *, timeout: int = 120
) -> dict[str, Any]:
    """Return the live node pool's autoscaling config: ``{enabled, min, max}``."""
    rc, out = gcloud(
        [
            "container",
            "node-pools",
            "describe",
            pool_name,
            "--cluster",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=json",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not describe node pool {pool_name} on {cluster_name} "
            f"to read its autoscaling config: {fold_tail(out)}",
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] node-pool describe returned unparseable JSON while reading autoscaling: {exc}",
        ) from exc
    autoscaling = data.get("autoscaling") or {}
    return {
        "enabled": bool(autoscaling.get("enabled")),
        "min": autoscaling.get("minNodeCount"),
        "max": autoscaling.get("maxNodeCount"),
    }


def _enable_node_pool_autoscaling(
    cluster_name: str,
    pool_name: str,
    location: str,
    project: str,
    min_nodes: int,
    max_nodes: int,
    *,
    timeout: int = 600,
) -> None:
    """Reconcile GKE-managed autoscaling on an existing node pool (adopt path)."""
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "update",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--enable-autoscaling",
            "--node-pool",
            pool_name,
            "--min-nodes",
            str(min_nodes),
            "--max-nodes",
            str(max_nodes),
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] could not enable managed autoscaling on {pool_name} "
            f"({min_nodes}..{max_nodes}): {fold_tail(out)}",
        )


def verify_system_autoscaling(
    cluster_name: str,
    pool_name: str,
    location: str,
    project: str,
    expected_min: int,
    expected_max: int,
    *,
    timeout: int = 600,
) -> dict[str, Any]:
    """Read back GKE-managed autoscaling on the system pool and require it to be
    enabled with the requested min/max bounds; return provider-native evidence.

    A fresh terraform create already carries the autoscaling block, so the first
    readback matches. An ADOPTED pool (refresh-only, no apply) may predate the
    bounds, so reconcile it via ``gcloud container clusters update`` and re-read.
    Fail CLOSED (config_error) when the live pool still does not match — setup
    never emits managed-autoscaler evidence for a pool that is not autoscaling."""
    live = _read_node_pool_autoscaling(cluster_name, pool_name, location, project)
    if not (live["enabled"] and live["min"] == expected_min and live["max"] == expected_max):
        _enable_node_pool_autoscaling(
            cluster_name, pool_name, location, project, expected_min, expected_max, timeout=timeout
        )
        live = _read_node_pool_autoscaling(cluster_name, pool_name, location, project)
    if not (live["enabled"] and live["min"] == expected_min and live["max"] == expected_max):
        raise LifecycleError(
            "config_error",
            f"[bucket=config_error] system pool {pool_name} managed autoscaling mismatch: "
            f"requested enabled min={expected_min} max={expected_max} but the live pool is "
            f"enabled={live['enabled']} min={live['min']} max={live['max']}.",
        )
    return {
        "provider": "managed",
        "node_pool": pool_name,
        "enabled": True,
        "min_nodes": expected_min,
        "max_nodes": expected_max,
    }


# --------------------------------------------------------------------------- #
# GPU-zone capacity preflight probe                                           #
# --------------------------------------------------------------------------- #


def _is_integrated_gpu(machine_type: str) -> bool:
    return any(machine_type.startswith(p) for p in _INTEGRATED_GPU_PREFIXES)


def _delete_probe(project: str, zone: str, mig_name: str, template_name: str) -> list[str]:
    """Best-effort delete of the throwaway probe MIG + template on every exit path.

    Never raises (cleanup must not mask the probe's own capacity result), but each
    delete result is CHECKED and its outcome RETURNED: a safe transient failure is
    retried, and if a resource is still not confirmed gone the EXACT retained name
    is both logged and returned so the caller can surface it and teardown's
    run-scoped probe backstop (delete_orphan_gpu_probes) can reclaim it. A silently
    dropped delete could leak a billable size-1 GPU MIG.

    Returns the list of retained (unconfirmed) resource identifiers; empty when
    both deletes are confirmed gone.
    """
    retained: list[str] = []
    if not _delete_probe_resource(
        [
            "compute",
            "instance-groups",
            "managed",
            "delete",
            mig_name,
            "--zone",
            zone,
            "--project",
            project,
            "--quiet",
        ],
        kind="probe MIG",
        name=f"{mig_name} (zone {zone})",
    ):
        retained.append(f"probe MIG {mig_name} (zone {zone})")
    if not _delete_probe_resource(
        ["compute", "instance-templates", "delete", template_name, "--project", project, "--quiet"],
        kind="probe instance-template",
        name=template_name,
    ):
        retained.append(f"probe instance-template {template_name}")
    return retained


def _delete_probe_resource(args: list[str], *, kind: str, name: str, retries: int = 2) -> bool:
    """Delete one throwaway probe resource, retrying safe transient failures.

    Returns True when the resource is confirmed gone (deleted, or already
    not_found); False when an unrecovered failure leaves it possibly-retained. A
    retained resource is surfaced as a named warning rather than silently
    discarded, and the False return lets the caller + teardown backstop reclaim it.
    """
    for attempt in range(retries + 1):
        rc, out = gcloud(args, timeout=180, echo=False)
        if rc == 0 or _classify_cli_output(out) == "not_found":
            return True
        if _is_transient_cleanup_error(out) and attempt < retries:
            time.sleep(5)
            continue
        log(f"warning: {kind} {name} not confirmed deleted (rc={rc}); reclaim manually: {fold_tail(out, limit=400)}")
        return False
    return False


def _retained_probes_marker_path() -> Path:
    """Durable per-run signal file recording that a GPU capacity-preflight probe
    delete was left unconfirmed. Lives beside the primary tfstate so it persists
    across the separate setup and teardown lifecycle-step processes in the run's
    worktree (git-ignored, see terraform/.gitignore)."""
    return CLUSTER_TF_DIR / f"retained-probes-{run_scope_id()}.marker"


def _read_pending_probe_names(path: Path) -> list[str]:
    """Probe names currently recorded in the marker.

    Returns [] ONLY when the marker is DEFINITIVELY absent (never created, or
    already cleared once every probe was confirmed reclaimed). Any OTHER read
    error is RE-RAISED: a transiently-unreadable EXISTING marker must never be
    mistaken for an empty one, or a caller merging/rewriting it would silently
    erase a previously recorded — and still billable — cleanup obligation."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _atomic_write_marker(path: Path, names: list[str]) -> None:
    """Atomically (re)write the retained-probe marker so an interrupted write can
    never destroy the existing one.

    The in-place ``Path.write_text`` this replaces TRUNCATES the marker before the
    new bytes land, so a failure (disk full, SIGKILL) after truncation would strand
    an emptied marker and lose an unconfirmed probe name. Instead write a sibling
    temp file, flush+fsync it, then ``os.replace`` it over the target — the replace
    is atomic, so any failure leaves the PRIOR marker fully intact for the next run
    (and this call's caller to observe as a write error and fail closed)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(names) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def mark_probes_pending(names: list[str]) -> None:
    """Persist probe resource NAMES as pending-reclaim BEFORE they are created.

    The inline probe delete runs in a ``finally``, but a HARD process kill (SIGKILL,
    OOM, node eviction, harness timeout) during the SUCCESSFUL template/MIG create or
    the subsequent capacity wait bypasses ``finally`` entirely — leaving a standalone
    billable size-1 GPU MIG with NO marker. The preservation (``--skip-destroy``)
    teardown then short-circuits on the empty marker and that MIG bills forever.
    Recording the names UP FRONT closes that pre-marker interruption window: the
    marker survives the kill, so preservation teardown still runs the run-scoped
    backstop and reclaims the leak.

    FAIL-CLOSED: refuse to create a billable probe we cannot track (raise) rather
    than create an untracked one. A normal (non-skip) teardown reclaims it
    unconditionally via the run-scoped backstop, so failing loudly here never leaks.
    The read+merge+write is retried once before it is treated as fatal. Fail-closed
    covers BOTH edges: an unreadable EXISTING marker re-raises from
    ``_read_pending_probe_names`` (never merged as empty, so a prior probe is never
    dropped), and the write is atomic (``_atomic_write_marker`` replaces in-place
    truncation) so a mid-write failure leaves the prior marker intact. The names are
    cleared again by ``clear_pending_probes`` once their inline delete is CONFIRMED,
    or wiped wholesale by the teardown backstop once reclaim is confirmed."""
    wanted = [name for name in names if name]
    if not wanted:
        return
    path = _retained_probes_marker_path()
    last_exc: OSError | None = None
    for attempt in range(2):
        try:
            merged = list(dict.fromkeys(_read_pending_probe_names(path) + wanted))
            _atomic_write_marker(path, merged)
            return
        except OSError as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(1)
    raise LifecycleError(
        "cleanup_incomplete",
        f"[bucket=cleanup_incomplete] could not persist the pending GPU capacity-probe "
        f"tracker at {path} ({last_exc}); refusing to CREATE a billable probe MIG we cannot "
        "track, which a kill before the inline delete would strand and the preservation "
        "(--skip-destroy) teardown path would miss.",
    )


def retained_probes_pending() -> bool:
    """True when an inline GPU-probe delete was left unconfirmed this run (the
    marker file exists and is non-empty).

    FAIL CLOSED on an INDETERMINATE marker: a read/stat error returns True so the
    preservation (``--skip-destroy``) path conservatively RUNS the run-scoped probe
    backstop (derived purely from the run id; a harmless no-op when nothing leaked)
    instead of mis-reporting "nothing pending" while a billable MIG survives. Only
    a marker that is DEFINITIVELY absent (or present-but-empty) returns False."""
    path = _retained_probes_marker_path()
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError as exc:
        log(
            f"warning: retained GPU-probe marker at {path} is unreadable ({exc}); running the "
            "run-scoped probe backstop to fail closed rather than skip cleanup"
        )
        return True


def clear_retained_probes_marker() -> None:
    """Remove the retained-probe marker once the backstop confirmed reclaim."""
    try:
        _retained_probes_marker_path().unlink(missing_ok=True)
    except Exception:  # best-effort cleanup of a local signal file
        pass


def clear_pending_probes(names: list[str]) -> None:
    """Drop names from the pending-probe tracker once their inline delete is CONFIRMED
    (both MIG + template gone).

    Best-effort AND fail-safe: if anything goes wrong the names simply STAY pending
    and the teardown backstop reclaims them (a harmless no-op once they are already
    deleted), so this never fails a found-capacity zone. Two destructive edges are
    closed: an unreadable EXISTING marker is never treated as empty (which would
    unlink a marker still holding OTHER pending probes), and the rewrite is atomic
    (never truncating in place). The marker file is removed ONLY after a successful
    read confirms it is now empty, so the common preservation path stays a cheap
    short-circuit without ever discarding an unconfirmed obligation."""
    drop = {name for name in names if name}
    if not drop:
        return
    path = _retained_probes_marker_path()
    try:
        existing = _read_pending_probe_names(path)
    except OSError as exc:
        # The marker EXISTS but is transiently unreadable. Never interpret that as
        # empty and unlink it — that would erase still-pending obligations NOT in
        # `drop`. Leave the marker intact; the run-scoped teardown backstop still
        # reclaims (a harmless no-op once the probe is already deleted).
        log(f"warning: retained GPU-probe marker at {path} unreadable ({exc}); left intact for teardown")
        return
    remaining = [name for name in existing if name not in drop]
    if remaining == existing:
        return  # nothing of ours to drop — avoid a needless rewrite/truncate
    try:
        if remaining:
            _atomic_write_marker(path, remaining)
        else:
            # Reached ONLY via a successful read confirming every name left in the
            # marker was in `drop` (all inline deletes CONFIRMED) — safe to remove.
            path.unlink(missing_ok=True)
    except OSError:
        pass  # leave the tracker in place; the run-scoped backstop still reclaims


def _note_retained_probes(retained: list[str]) -> None:
    """Log any probe resource whose inline delete was NOT confirmed this run.

    The names are run-scoped (``isv-gpumig-<run_id>-*`` / ``isv-gpuprobe-<run_id>-*``),
    and were already recorded as pending by ``mark_probes_pending`` BEFORE the probe
    was created, so they REMAIN in the marker (never cleared here) and teardown's
    run-scoped ``delete_orphan_gpu_probes`` backstop reclaims them deterministically —
    select_gpu_zone stays best-effort about the cloud DELETE (it must not fail a found
    capacity zone on a transient cleanup hiccup)."""
    if retained:
        log(
            f"note: GPU capacity preflight left {len(retained)} probe resource(s) "
            f"unconfirmed-deleted ({'; '.join(retained)}); they remain recorded pending and "
            "teardown's run-scoped probe backstop will reclaim them."
        )


def _reclaim_probe(project: str, zone: str, mig_name: str, template_name: str) -> None:
    """Inline-delete this zone's probe MIG + template and reconcile the pending tracker.

    Both confirmed gone -> drop them from the tracker (keeps the common preservation
    teardown a cheap short-circuit). Any unconfirmed -> the names STAY pending (they
    were recorded by ``mark_probes_pending`` before create) and are logged, so
    teardown's run-scoped backstop reclaims the billable MIG. Never raises — cleanup
    must not mask the probe's own capacity result."""
    retained = _delete_probe(project, zone, mig_name, template_name)
    if retained:
        _note_retained_probes(retained)
    else:
        clear_pending_probes([mig_name, template_name])


def _zone_region(zone: str) -> str:
    """Region of a compute zone (``us-central1-a`` -> ``us-central1``)."""
    return zone.rsplit("-", 1)[0]


def _resolve_probe_subnet(project: str, network: str, region: str) -> str | None:
    """Name of a subnetwork in ``region`` that belongs to ``network`` (or None).

    A custom-mode VPC (``auto_create_subnetworks=false``) — the exact case
    ``GCP_K8S_NETWORK`` targets — auto-creates NO subnets, so a probe instance
    template built with only ``--network`` cannot place: GCE requires an explicit
    ``--subnet`` in the template's region. Auto-mode networks (e.g. ``default``)
    DO carry a same-named regional subnet, so this resolves uniformly for both.
    The candidate subnets are filtered to the selected network's own identity
    (``_net_identity``), so a same-named subnet on ANOTHER VPC is never chosen —
    preserving the exact-network-identity guarantee the capacity probe relies on.
    Returns None (caller falls back to ``--network`` only, the auto-mode path)
    when no subnet in ``region`` belongs to the network or the read fails.
    """
    net_id = _net_identity(network)
    if not net_id:
        return None
    rc, out = gcloud(
        [
            "compute",
            "networks",
            "subnets",
            "list",
            "--project",
            project,
            "--filter",
            f"region:( {region} )",
            "--format=json",
        ],
        timeout=60,
        echo=False,
    )
    if rc != 0 or not out.strip():
        return None
    try:
        subnets = json.loads(out)
    except json.JSONDecodeError:
        return None
    for sn in subnets if isinstance(subnets, list) else []:
        if _net_identity(str(sn.get("network", ""))) == net_id:
            name = sn.get("name")
            if name:
                return str(name)
    return None


def select_gpu_zone(
    project: str,
    candidate_zones: list[str],
    machine_type: str,
    accelerator_type: str,
    accelerator_count: int,
    *,
    network: str = "default",
    probe_timeout: int = 90,
    poll_interval: int = 12,
) -> str:
    """Return the first candidate zone with GPU capacity for the requested shape.

    A GKE node-pool CREATE op cannot be cancelled and wedges the cluster for
    ~35 min once RUNNING, so the pool is NEVER created speculatively. Instead a
    throwaway STANDALONE size-1 Managed Instance Group (a plain compute MIG, not
    a node pool) mirroring the GPU shape is stood up in each candidate zone, its
    capacity signal read, then deleted on EVERY exit path — a stocked-out probe
    costs ~30-90s, not a ~35-min wedge.

    Signals:
      * NO CAPACITY  -> list-errors carries a stockout token -> try next zone.
      * HAS CAPACITY -> the instance reaches STAGING/RUNNING  -> use this zone.
      * CONFIG/POLICY/QUOTA error (create fails, or the instance is rejected
        async with a NON-stockout error) -> surface + FAIL; never walk on.
    """
    if not candidate_zones:
        raise LifecycleError(
            "config_error",
            "[bucket=config_error] no candidate GPU zones supplied to the capacity "
            "preflight (GCP_K8S_GPU_ZONES empty and no location zone derivable).",
        )

    integrated = _is_integrated_gpu(machine_type)
    tried: list[str] = []
    for zone in candidate_zones:
        zone = zone.strip()
        if not zone:
            continue
        tried.append(zone)
        disc = secrets.token_hex(2)
        template_name = normalize_gke_name(f"isv-gpuprobe-{run_scope_id()}-{disc}")[:60]
        mig_name = normalize_gke_name(f"isv-gpumig-{run_scope_id()}-{disc}")[:60]
        log(f"GPU capacity preflight: probing zone {zone} (mig={mig_name})...")

        # Record BOTH probe names as pending-reclaim BEFORE the first create. A hard
        # kill during the template/MIG create or the capacity wait bypasses the
        # inline-delete finally below, so without this pre-create marker a billable
        # size-1 GPU MIG could survive with no signal and the preservation teardown
        # would short-circuit clean. FAIL-CLOSED: a marker we cannot write refuses the
        # create rather than standing up an untracked probe.
        mark_probes_pending([template_name, mig_name])

        selected_network = network or "default"
        region = _zone_region(zone)
        # Resolve a subnet in THIS zone's region that belongs to the selected VPC.
        # A custom-mode VPC has no auto subnet, so a bare --network probe fails to
        # place; an explicit --subnet (pinned to the region) makes both auto- and
        # custom-mode networks placeable. None -> fall back to --network only.
        probe_subnet = _resolve_probe_subnet(project, selected_network, region)

        tmpl_args = [
            "compute",
            "instance-templates",
            "create",
            template_name,
            "--project",
            project,
            "--machine-type",
            machine_type,
            # REQUIRED for EVERY GPU-bearing VM (integrated or separate) — GCE
            # rejects the default onHostMaintenance=MIGRATE on any GPU VM.
            "--maintenance-policy",
            "TERMINATE",
            "--no-address",
            # The operator-selected VPC (same network the cluster attaches to);
            # a probe on another network would not prove the pool's GPU shape is
            # placeable in the cluster's actual substrate.
            "--network",
            selected_network,
            "--image-family",
            "debian-12",
            "--image-project",
            "debian-cloud",
            "--boot-disk-size",
            "50GB",
        ]
        # Pin an explicit regional subnet when one was resolved. --network stays
        # so gcloud still validates the subnet belongs to the selected VPC (exact
        # network-identity check retained); --region tells the global template
        # which regional subnet to bind, matching this candidate zone's region.
        if probe_subnet:
            tmpl_args += ["--subnet", probe_subnet, "--region", region]
        # CONDITIONAL: only a separate-accelerator shape (e.g. n1 + T4) needs the
        # explicit --accelerator; an integrated-GPU machine already carries it.
        if not integrated and accelerator_type:
            tmpl_args += [
                "--accelerator",
                f"type={accelerator_type},count={accelerator_count}",
            ]

        rc, out = gcloud(tmpl_args, timeout=180)
        if rc != 0:
            # A create failure WITHOUT a stockout token is a config/setup error
            # (e.g. malformed template) — surface + FAIL, never treat as
            # no-capacity and walk on.
            if not _output_has_stockout(out):
                _reclaim_probe(project, zone, mig_name, template_name)
                bucket = _classify_cli_output(out)
                raise LifecycleError(
                    bucket,
                    f"[bucket={bucket}] GPU probe instance-template create failed in "
                    f"{zone} with a NON-stockout error (config/policy/quota, not "
                    f"capacity): {fold_tail(out)}",
                )
            _reclaim_probe(project, zone, mig_name, template_name)
            log(f"  zone {zone}: stockout on template create; trying next zone")
            continue

        try:
            capacity = _probe_mig_capacity(
                project,
                zone,
                mig_name,
                template_name,
                probe_timeout=probe_timeout,
                poll_interval=poll_interval,
            )
        finally:
            _reclaim_probe(project, zone, mig_name, template_name)

        if capacity == "capacity":
            log(f"  zone {zone}: HAS capacity -> selecting")
            return zone
        if capacity == "stockout":
            log(f"  zone {zone}: stockout -> trying next zone")
            continue
        # capacity == "error:<detail>" — a NON-stockout failure: an async policy /
        # quota rejection, an unreadable capacity signal, or a probe timeout.
        # NEVER walk the zone list on this (that would emit a misleading
        # "no capacity" remediation for what is actually an API / policy /
        # timeout failure). Classify the retained detail so the operator sees the
        # real bucket; an unclassifiable read/timeout failure is transient.
        detail = capacity.split(":", 1)[1].strip() if ":" in capacity else capacity
        bucket = _classify_cli_output(detail)
        if bucket == "unknown_error":
            bucket = "transient"
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] GPU capacity probe in {zone} did not yield a usable "
            f"capacity signal (NON-stockout, so no zone walk applies): {fold_tail(detail)}",
        )

    raise LifecycleError(
        "transient",
        f"[bucket=transient] no GPU capacity for {accelerator_type} x{accelerator_count} "
        f"on {machine_type} in any candidate zone {tried}. Capacity is "
        f"zone-fragmented and shifts; retry or widen GCP_K8S_GPU_ZONES.",
    )


def _output_has_stockout(output: str) -> bool:
    low = output.lower()
    return any(tok.lower() in low for tok in _STOCKOUT_TOKENS)


def _probe_mig_capacity(
    project: str,
    zone: str,
    mig_name: str,
    template_name: str,
    *,
    probe_timeout: int,
    poll_interval: int,
) -> str:
    """Stand up the size-1 probe MIG and read its capacity signal.

    Returns "capacity" | "stockout" | "error:<detail>". "stockout" is returned
    ONLY on an explicit stockout token (so the caller may walk to the next zone);
    a read failure or a deadline with no capacity/stockout signal returns
    "error:<detail>" so the caller FAILS with the retained diagnostic instead of
    fabricating a misleading "no capacity" verdict for a permission / policy /
    quota / transport / malformed-response failure.
    """
    rc, out = gcloud(
        [
            "compute",
            "instance-groups",
            "managed",
            "create",
            mig_name,
            "--zone",
            zone,
            "--project",
            project,
            "--template",
            template_name,
            "--size",
            "1",
        ],
        timeout=180,
    )
    if rc != 0:
        if _output_has_stockout(out):
            return "stockout"
        return f"error:{out}"

    deadline = time.time() + probe_timeout
    last_read_error: str | None = None
    while time.time() < deadline:
        # (1) list-errors: a stockout or async rejection surfaces here early. A
        # NONZERO read is a polling failure (permission/policy/quota/transport) —
        # retained, never conflated with "no errors observed".
        rc_e, err_out = gcloud(
            [
                "compute",
                "instance-groups",
                "managed",
                "list-errors",
                mig_name,
                "--zone",
                zone,
                "--project",
                project,
                "--format=json",
            ],
            timeout=60,
            echo=False,
        )
        if rc_e != 0:
            last_read_error = f"list-errors read failed (rc={rc_e}): {fold_tail(err_out, limit=600)}"
        elif err_out.strip() and err_out.strip() != "[]":
            if _output_has_stockout(err_out):
                return "stockout"
            return f"error:async instance rejection (non-stockout): {fold_tail(err_out, limit=600)}"

        # (2) list-instances: reaching STAGING/RUNNING (or currentAction NONE)
        # means the zone can PLACE the shape. A nonzero read or malformed JSON is
        # a polling failure, retained rather than silently treated as no-capacity.
        rc_i, inst_out = gcloud(
            [
                "compute",
                "instance-groups",
                "managed",
                "list-instances",
                mig_name,
                "--zone",
                zone,
                "--project",
                project,
                "--format=json",
            ],
            timeout=60,
            echo=False,
        )
        if rc_i != 0:
            last_read_error = f"list-instances read failed (rc={rc_i}): {fold_tail(inst_out, limit=600)}"
        elif inst_out.strip():
            try:
                instances = json.loads(inst_out)
            except json.JSONDecodeError:
                last_read_error = f"list-instances returned malformed JSON: {fold_tail(inst_out, limit=600)}"
                instances = []
            else:
                last_read_error = None  # a clean read clears a prior transient error
            for inst in instances:
                status = (inst.get("instanceStatus") or "").upper()
                action = (inst.get("currentAction") or "").upper()
                if status in ("STAGING", "RUNNING") or action == "NONE":
                    return "capacity"
        time.sleep(poll_interval)

    # Bounded poll elapsed with no STAGING/RUNNING and NO explicit stockout token.
    if last_read_error is not None:
        # The window closed while capacity reads were still failing — surface the
        # retained API failure, never a fabricated stockout.
        return f"error:capacity signal unreadable in {zone}: {last_read_error}"
    # Reads were clean but the shape never placed: an explicit probe TIMEOUT, not
    # a stockout (which GCE signals with a token). Surfaced so it is not misread
    # as zone-fragmented no-capacity remediation.
    return (
        f"error:probe timed out after {probe_timeout}s in {zone} with no STAGING/RUNNING "
        f"instance and no stockout token (slow provisioning or an unsignalled constraint)."
    )


# --------------------------------------------------------------------------- #
# Cluster bootstrap: RuntimeClass + GPU node labeling                         #
# --------------------------------------------------------------------------- #

_RUNTIMECLASS_MANIFEST = "apiVersion: node.k8s.io/v1\nkind: RuntimeClass\nmetadata:\n  name: nvidia\nhandler: runc\n"


def apply_nvidia_runtimeclass(*, timeout: int = 60) -> None:
    """Create the passthrough `nvidia` RuntimeClass (handler `runc`), idempotent.

    GKE's managed-driver path creates no `nvidia` RuntimeClass, yet several
    released GPU-workload manifests pin `runtimeClassName: nvidia` literally and
    are rejected at admission without it. An HONEST passthrough bridge:
    `runtimeClassName: nvidia` routes to GKE's DEFAULT runtime `runc` — the same
    runtime that already grants GPU access via the device plugin — so the pods
    schedule without installing the GPU Operator. handler MUST be `runc` (GKE has
    no `nvidia` containerd handler; that would fail at sandbox creation).
    """
    proc = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=_RUNTIMECLASS_MANIFEST,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        combined = (proc.stdout or "") + (proc.stderr or "")
        bucket = _classify_cli_output(combined)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] failed to apply the passthrough nvidia RuntimeClass: {fold_tail(combined)}",
        )


def strip_baseline_pool_markers(*, timeout: int = 60) -> None:
    """Remove the reserved isv.ncp.validation/pool marker from BASELINE nodes.

    isvtest's CSI probe pods pin (required nodeAffinity) to nodes where
    ``isv.ncp.validation/pool`` DoesNotExist — they must stay off transient test
    pools whose CSI node-plugin DaemonSet may not be Ready on a freshly joined
    node. The baseline system / GPU pools must therefore NOT carry that key. The
    terraform config no longer sets it, but a PRESERVED/adopted cluster still has
    the old label baked into its live baseline nodes (setup adopts via
    refresh-only, which never pushes the config change), which would leave every
    CSI probe pod unschedulable (0/N nodes: node affinity mismatch). Strip it here
    on every setup: select nodes that HAVE the marker with a NON-test value
    (baseline) and drop it. Test pools (value=test) are untouched — the
    K8sNodeCountCheck exclusion still depends on their marker — and a fresh
    cluster (no baseline marker) matches nothing. Best-effort: a transient kubectl
    error must not fail setup.
    """
    rc, out = kubectl(
        [
            "label",
            "nodes",
            "-l",
            "isv.ncp.validation/pool,isv.ncp.validation/pool!=test",
            "isv.ncp.validation/pool-",
        ],
        timeout=timeout,
    )
    if rc != 0:
        log(f"warning: stripping baseline pool marker returned rc={rc}: {fold_tail(out, limit=400)}")


# --------------------------------------------------------------------------- #
# Kubeflow MPI Operator prerequisite (multi-node NCCL MPIJob controller)      #
# --------------------------------------------------------------------------- #

# The pinned, provider-owned Kubeflow MPI Operator v0.8.2 (v2beta1) release
# manifest, vendored locally so setup never downloads a GitHub/raw-URL manifest
# at runtime. K8sNcclMultiNodeWorkload requires the mpijobs.kubeflow.org CRD +
# controller to run; without it the released workload structured-skips and the
# multi-node GPU communication path goes uncovered.
MPI_OPERATOR_MANIFEST = SCRIPT_DIR / "manifests" / "mpi-operator-v0.8.2.yaml"
_MPI_OPERATOR_NAMESPACE = "mpi-operator"
_MPI_OPERATOR_DEPLOYMENT = "mpi-operator"
_MPI_OPERATOR_CRD = "mpijobs.kubeflow.org"


def install_mpi_operator(*, timeout: int = 300) -> None:
    """Apply the vendored, pinned Kubeflow MPI Operator manifest and gate on its
    readiness so the released multi-node NCCL workload has its MPIJob controller.

    The manifest is a LOCAL provider-owned asset (never fetched at runtime); a
    missing asset, a failed apply, or a CRD/controller that never becomes ready
    RAISES a classified LifecycleError so setup fails loudly rather than shipping
    a cluster whose multi-node NCCL coverage would silently skip."""
    if not MPI_OPERATOR_MANIFEST.is_file():
        raise LifecycleError(
            "config_error",
            f"[bucket=config_error] the vendored MPI Operator manifest {MPI_OPERATOR_MANIFEST} "
            "is missing; the multi-node NCCL MPIJob prerequisite cannot be installed.",
        )
    # Server-side apply keeps the adopt/setup re-run path idempotent: the API
    # server tracks field ownership and reconciles each field in place, so a
    # re-apply of this pinned manifest never accumulates client-side
    # last-applied-annotation drift. --force-conflicts lets setup re-assert
    # ownership of any field a prior apply (or GKE) already manages.
    rc, out = kubectl(
        ["apply", "--server-side", "--force-conflicts", "-f", str(MPI_OPERATOR_MANIFEST)],
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] failed to apply the vendored MPI Operator manifest: {fold_tail(out)}",
        )
    rc, out = kubectl(
        ["wait", "--for=condition=Established", f"crd/{_MPI_OPERATOR_CRD}", f"--timeout={timeout}s"],
        timeout=timeout + 30,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] the {_MPI_OPERATOR_CRD} CRD did not become Established: {fold_tail(out)}",
        )
    rc, out = kubectl(
        [
            "-n",
            _MPI_OPERATOR_NAMESPACE,
            "wait",
            "--for=condition=Available",
            f"deployment/{_MPI_OPERATOR_DEPLOYMENT}",
            f"--timeout={timeout}s",
        ],
        timeout=timeout + 30,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] the MPI Operator controller Deployment did not become Available: {fold_tail(out)}",
        )


# --------------------------------------------------------------------------- #
# Two-gate GPU preflight + inventory                                          #
# --------------------------------------------------------------------------- #


def _kubectl_json(args: list[str], *, timeout: int = 60) -> Any:
    rc, out = kubectl(args, timeout=timeout, echo=False)
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _kubectl_json_required(args: list[str], what: str, *, timeout: int = 60) -> Any:
    """Like ``_kubectl_json`` but for a REQUIRED live read.

    Raises a structured LifecycleError when the command fails, returns nothing, or
    returns malformed JSON — so a missing inventory signal fails loudly with
    diagnostics instead of silently degrading to an empty list that would let
    setup emit synthetic 'success' inventory.
    """
    rc, out = kubectl(args, timeout=timeout, echo=False)
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] required cluster inventory read failed ({what}): {fold_tail(out)}",
        )
    if not out.strip():
        raise LifecycleError(
            "transient",
            f"[bucket=transient] required cluster inventory read returned no output ({what}).",
        )
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise LifecycleError(
            "unknown_error",
            f"[bucket=unknown_error] required cluster inventory read returned malformed JSON ({what}): {exc}",
        ) from exc


# GKE installs the NVIDIA driver + device plugin via Google-managed DaemonSets in
# kube-system (no NVIDIA GPU Operator). The device-plugin pod reports Ready only
# AFTER the managed driver install completes on its node, so its Ready condition
# is the provider-native, NO-IMAGE-PULL driver-ready signal. (The mandatory GPU
# readiness gate MUST NOT depend on pulling a public-registry CUDA image.)
_GPU_DEVICE_PLUGIN_NS = "kube-system"
_GPU_DEVICE_PLUGIN_SELECTOR = "k8s-app=nvidia-gpu-device-plugin"
# nvidia-smi paths inside the managed device-plugin container (the host driver
# install dir is mounted in); tried in order for the OPTIONAL version read.
_MANAGED_NVIDIA_SMI_PATHS = (
    "/usr/local/nvidia/bin/nvidia-smi",
    "/home/kubernetes/bin/nvidia/bin/nvidia-smi",
    "nvidia-smi",
)


def _gpu_device_plugin_pod_ready_on_node(node_name: str, *, timeout: int = 30) -> str | None:
    """Return the name of a READY GKE-managed GPU device-plugin DaemonSet pod on
    ``node_name`` (kube-system, ``k8s-app=nvidia-gpu-device-plugin``), or None.

    This is the provider-native driver-ready signal — the plugin only reports
    Ready after the managed NVIDIA driver install finishes on the node — so the
    readiness gate confirms the driver is up WITHOUT scheduling a probe pod that
    pulls any workload/CUDA image."""
    pods = _kubectl_json(
        [
            "get",
            "pods",
            "-n",
            _GPU_DEVICE_PLUGIN_NS,
            "-l",
            _GPU_DEVICE_PLUGIN_SELECTOR,
            "--field-selector",
            f"spec.nodeName={node_name}",
            "-o",
            "json",
        ],
        timeout=timeout,
    )
    items = (pods or {}).get("items", []) if isinstance(pods, dict) else []
    for pod in items:
        conditions = (pod.get("status", {}) or {}).get("conditions", []) or []
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            name = (pod.get("metadata", {}) or {}).get("name")
            if name:
                return name
    return None


def wait_two_gate_gpu_ready(
    gpu_pool_name: str,
    expected_count: int,
    *,
    timeout: int = 900,
    poll_interval: int = 15,
) -> str | None:
    """Block until the BASELINE GPU pool is provider-native GPU-ready on EVERY one
    of its ``expected_count`` nodes, then bridge them to nvidia.com/gpu.present.

    Scoped to the baseline pool's OWN selector
    (``cloud.google.com/gke-nodepool=<gpu_pool_name>``) and gated on ALL
    ``expected_count`` nodes satisfying the three explicit-True, NO-image-pull
    signals — node Ready=True, nonzero allocatable ``nvidia.com/gpu``, AND a Ready
    GKE-managed GPU device-plugin DaemonSet pod on the node (which reports Ready
    only after the managed driver install completes). A single first-ready node,
    or an adopted/preserved validation pool's nodes on another pool, can never
    satisfy this gate for a multi-node baseline pool, so setup can never derive
    inventory from partial or wrong-pool GPU readiness.

    Returns the driver version read best-effort from an already-running managed
    pod (``kubectl exec nvidia-smi``, no new pull), or None when it cannot be read
    — driver_version is OPTIONAL inventory (K8sDriverVersionCheck skips on empty),
    and readiness NEVER depends on reading it. A timeout, labeling error, missing
    node, or readback mismatch RAISES so setup's success stays false.
    """
    pool_selector = f"cloud.google.com/gke-nodepool={gpu_pool_name}"
    # Pool-scoped, all-node completion gate (same explicit-True signals + labeled
    # readback the test GPU pool uses); it labels exactly the baseline pool's
    # ready nodes with nvidia.com/gpu.present=true so the released GPU checks
    # discover them.
    wait_gpu_pool_ready_and_bridge(
        pool_selector,
        expected_count,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    # Readiness is satisfied on every baseline node. Read the driver version
    # best-effort from an already-running managed pod (no image pull).
    return _probe_driver_version()


# --------------------------------------------------------------------------- #
# Pool-scoped GPU completion gate (create_test_gpu_node_pool)                  #
# --------------------------------------------------------------------------- #


def _ready_gpu_node_names(label_selector: str) -> list[str]:
    """Names of nodes matching ``label_selector`` that satisfy all three
    provider-native GPU-ready signals: Ready=True, nonzero allocatable
    ``nvidia.com/gpu``, AND a Ready GKE-managed GPU device-plugin DaemonSet pod
    on the node (the no-image-pull driver-ready signal).

    Scoped to ONE pool's own selector (``cloud.google.com/gke-nodepool=<pool>``),
    so one pool's nodes never satisfy ANOTHER pool's readiness count — both the
    setup baseline pool and each test GPU pool gate on their own pool selector.
    """
    nodes = _kubectl_json(["get", "nodes", "-l", label_selector, "-o", "json"])
    items = (nodes or {}).get("items", []) if isinstance(nodes, dict) else []
    ready: list[str] = []
    for node in items:
        name = (node.get("metadata", {}) or {}).get("name")
        status = node.get("status", {}) or {}
        allocatable = status.get("allocatable", {}) or {}
        gpu = allocatable.get("nvidia.com/gpu")
        conditions = status.get("conditions", []) or []
        is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        if name and is_ready and gpu and str(gpu) not in ("", "0") and _gpu_device_plugin_pod_ready_on_node(name):
            ready.append(name)
    return ready


def _label_gpu_present_and_verify(node_names: list[str], *, timeout: int = 120) -> None:
    """Apply ``nvidia.com/gpu.present=true`` to the NAMED nodes and read it back.

    Labeling BY NODE NAME (never a bare ``-l`` selector) means a ``kubectl label``
    that matched zero nodes cannot masquerade as success. The readback re-reads
    the ``nvidia.com/gpu.present=true`` selector the released GPU checks discover
    on and asserts every named node now appears, so a silent labeling no-op or a
    partial apply cannot pass. An empty node set, labeling error, or readback miss
    RAISES so the caller's step success stays false.
    """
    if not node_names:
        raise LifecycleError(
            "unknown_error",
            "[bucket=unknown_error] GPU pool completion gate found no Ready GPU nodes to label.",
        )
    rc, out = kubectl(
        ["label", "nodes", *node_names, "nvidia.com/gpu.present=true", "--overwrite"],
        timeout=timeout,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] failed to apply nvidia.com/gpu.present=true to the GPU "
            f"pool's nodes {node_names}: {fold_tail(out)}",
        )
    labeled = _kubectl_json(["get", "nodes", "-l", "nvidia.com/gpu.present=true", "-o", "json"])
    items = (labeled or {}).get("items", []) if isinstance(labeled, dict) else []
    discovered = {(n.get("metadata", {}) or {}).get("name") for n in items}
    missing = sorted(n for n in node_names if n not in discovered)
    if missing:
        raise LifecycleError(
            "unknown_error",
            "[bucket=unknown_error] GPU discovery-label readback mismatch: node(s) "
            f"{missing} are not selectable via nvidia.com/gpu.present=true after labeling.",
        )


def wait_gpu_pool_ready_and_bridge(
    label_selector: str,
    expected_count: int,
    *,
    timeout: int = 360,
    poll_interval: int = 15,
) -> list[str]:
    """Pool-scoped GPU completion gate for ``create_test_gpu_node_pool``.

    Blocks until ``expected_count`` nodes matching THIS pool's own
    ``label_selector`` are Ready with nonzero allocatable ``nvidia.com/gpu``, then
    applies and reads back ``nvidia.com/gpu.present=true`` on exactly those nodes.
    Returns the bridged pool node names on success.

    This is the shared pool-scoped primitive: ``wait_two_gate_gpu_ready`` calls it
    with the baseline GPU pool selector, and ``create_test_gpu_node_pool`` calls it
    with the test pool selector. Scoping to THIS pool's own selector means another
    pool's already-Ready nodes can never let a create step emit success before its
    OWN nodes are Ready and discoverable — the exact false-success this gate
    prevents. A timeout, labeling error, missing node, or readback mismatch RAISES
    so the caller's step success stays false and the released GPU checks never run
    against an unready or undiscoverable pool.
    """
    if expected_count <= 0:
        raise LifecycleError(
            "config_error",
            "[bucket=config_error] GPU pool completion gate needs a positive expected node "
            f"count, got {expected_count}.",
        )
    deadline = time.time() + timeout
    ready_names: list[str] = []
    while time.time() < deadline:
        ready_names = _ready_gpu_node_names(label_selector)
        if len(ready_names) >= expected_count:
            break
        log(
            f"  waiting for {expected_count} Ready GPU node(s) in pool '{label_selector}' "
            f"with allocatable nvidia.com/gpu (have {len(ready_names)})..."
        )
        time.sleep(poll_interval)
    else:
        raise LifecycleError(
            "transient",
            f"[bucket=transient] GPU pool completion gate timed out: only {len(ready_names)}/"
            f"{expected_count} node(s) matching '{label_selector}' reached Ready with "
            f"allocatable nvidia.com/gpu within {timeout}s.",
        )
    _label_gpu_present_and_verify(ready_names)
    log(
        f"  GPU pool '{label_selector}' ready: {len(ready_names)} node(s) bridged to "
        "nvidia.com/gpu.present=true and readback-verified."
    )
    return ready_names


def _ready_pool_node_names(label_selector: str) -> list[str]:
    """Names of nodes matching ``label_selector`` whose Kubernetes Ready condition
    is True. Scoped to ONE pool's own selector
    (``cloud.google.com/gke-nodepool=<pool>``) so another pool's Ready nodes never
    satisfy this pool's readiness count. CPU analog of ``_ready_gpu_node_names``
    without the GPU-allocatable / device-plugin gate."""
    nodes = _kubectl_json(["get", "nodes", "-l", label_selector, "-o", "json"])
    items = (nodes or {}).get("items", []) if isinstance(nodes, dict) else []
    ready: list[str] = []
    for node in items:
        name = (node.get("metadata", {}) or {}).get("name")
        conditions = (node.get("status", {}) or {}).get("conditions", []) or []
        is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        if name and is_ready:
            ready.append(name)
    return ready


def wait_cpu_pool_ready(
    cluster_name: str,
    pool_name: str,
    location: str,
    project: str,
    label_selector: str,
    expected_count: int,
    *,
    timeout: int = 240,
    poll_interval: int = 15,
) -> list[str]:
    """Pool-scoped completion gate for a CPU node pool (create / in-state scale /
    adopt).

    Enforces the SAME observable-completion contract the GPU pool gate and the
    fresh Terraform apply enforce, so an ADOPTED or still-reconciling CPU pool can
    never emit success before it has actually converged:

      1. The live GKE node pool reaches a healthy ``RUNNING`` state — a
         ``RECONCILING`` / ``PROVISIONING`` pool is still settling (keep waiting);
         an ``ERROR`` / ``RUNNING_WITH_ERROR`` pool RAISES immediately so success
         stays false instead of waiting out the whole timeout.
      2. ``expected_count`` nodes matching THIS pool's own ``label_selector`` are
         Ready in Kubernetes.

    Scoping to the pool's own selector means another pool's already-Ready nodes can
    never satisfy this pool's readiness count. A timeout, a terminal-error pool
    state, or too few Ready nodes RAISES so the caller's step success stays false
    and the released node-pool checks never run against an unready pool."""
    if expected_count <= 0:
        raise LifecycleError(
            "config_error",
            f"[bucket=config_error] CPU pool completion gate needs a positive expected node count, got {expected_count}.",
        )
    deadline = time.time() + timeout
    ready_names: list[str] = []
    pool_status = ""
    last_read_error: LifecycleError | None = None
    while time.time() < deadline:
        pool_status, read_error = gke_node_pool_status(cluster_name, pool_name, location, project)
        if read_error is not None:
            if read_error.bucket in _TERMINAL_READ_BUCKETS:
                # Bad credentials, a missing permission, or a genuinely absent
                # pool never converges — surface it now instead of polling out the
                # whole budget and burying the diagnostic under a generic timeout.
                raise read_error
            last_read_error = read_error  # transient/unknown: retain, keep polling
        else:
            last_read_error = None  # a clean read clears any retained transient error
            if pool_status in _GKE_POOL_ERROR_STATES:
                raise LifecycleError(
                    "unknown_error",
                    f"[bucket=unknown_error] CPU node pool {pool_name} on {cluster_name} is in a "
                    f"terminal-unhealthy state '{pool_status}'; refusing to emit success for an errored pool.",
                )
            if pool_status == "RUNNING":
                ready_names = _ready_pool_node_names(label_selector)
                if len(ready_names) >= expected_count:
                    break
        log(
            f"  waiting for CPU pool '{pool_name}' to be RUNNING with {expected_count} Ready "
            f"node(s) (status={pool_status or 'unknown'}, ready={len(ready_names)})..."
        )
        time.sleep(poll_interval)
    else:
        detail = (
            f"[bucket=transient] CPU pool completion gate timed out: pool {pool_name} "
            f"status='{pool_status or 'unknown'}', only {len(ready_names)}/{expected_count} "
            f"node(s) matching '{label_selector}' reached Ready within {timeout}s."
        )
        if last_read_error is not None:
            # The budget closed while node-pool reads were still failing — carry the
            # retained API failure so the operator sees WHY, not just "timed out".
            detail += f" Last node-pool read failure: {last_read_error.detail}"
        raise LifecycleError("transient", detail)
    log(f"  CPU pool '{pool_name}' ready: RUNNING with {len(ready_names)} Ready node(s).")
    return ready_names


def _probe_driver_version(*, timeout: int = 45) -> str | None:
    """Best-effort read of the GPU driver version from the ALREADY-RUNNING
    GKE-managed GPU device-plugin DaemonSet pod (``kubectl exec nvidia-smi``).

    This is provider-native and pulls NO new image: it execs into the managed
    driver container (the host driver install dir is mounted in), reading the
    SAME nvidia-smi ``driver_version`` signal K8sDriverVersionCheck uses — never a
    label install-mode literal. Returns the version string, or None when it
    cannot be read. driver_version is OPTIONAL inventory (K8sDriverVersionCheck
    skips on empty), so a failed read leaves it unset rather than blocking
    anything — the mandatory readiness gate never depends on it or on a
    public-registry CUDA image pull.
    """
    gpu_nodes = _kubectl_json(["get", "nodes", "-l", "nvidia.com/gpu.present=true", "-o", "json"])
    items = (gpu_nodes or {}).get("items", []) if isinstance(gpu_nodes, dict) else []
    for node in items:
        node_name = (node.get("metadata", {}) or {}).get("name")
        if not node_name:
            continue
        pod = _gpu_device_plugin_pod_ready_on_node(node_name)
        if not pod:
            continue
        for smi in _MANAGED_NVIDIA_SMI_PATHS:
            rc, out = kubectl(
                [
                    "exec",
                    pod,
                    "-n",
                    _GPU_DEVICE_PLUGIN_NS,
                    "--",
                    smi,
                    "--query-gpu=driver_version",
                    "--format=csv,noheader",
                ],
                timeout=timeout,
                echo=False,
            )
            if rc == 0:
                for line in out.splitlines():
                    line = line.strip()
                    if re.match(r"^\d+\.\d+", line):
                        return line
    return None


def gather_inventory(
    cluster_name: str,
    *,
    driver_version: str | None = None,
    api_endpoint: str | None = None,
    unauthorized_probe_cmd: str = "",
    autoscaler: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observe the live cluster via kubectl and build the kubernetes/csi blocks.

    Every field is OBSERVED (never a static literal): node counts, GPU counts,
    and runtime_class come from live kubectl reads. The REQUIRED node/GPU reads
    RAISE (structured diagnostics) when the cluster cannot be read or returns
    malformed JSON — setup never emits synthetic 'success' inventory — and the
    GPU-per-node count is only ever the OBSERVED capacity (no requested-count
    fallback that would report unverified GPUs). ``driver_version`` may be passed
    in when the two-gate preflight already resolved it, to avoid a second
    nvidia-smi pod.
    """
    if driver_version is None:
        driver_version = _probe_driver_version()

    # Count only BASELINE nodes (exclude the transient test pools marked
    # isv.ncp.validation/pool=test). The harness preserves run-scoped resources,
    # so an ADOPTED cluster already carries THIS run's test-pool nodes at setup
    # time; counting them would inflate node_count / GPU totals past what the
    # released checks expect (they exclude pool=test on the LIVE side, so the
    # setup baseline they compare against must exclude it too). The `!=test`
    # selector also matches nodes with no pool marker, so a fresh cluster's
    # baseline nodes are unaffected.
    baseline_selector = "isv.ncp.validation/pool!=test"
    nodes = _kubectl_json_required(["get", "nodes", "-l", baseline_selector, "-o", "json"], "node inventory")
    node_items = nodes.get("items", []) if isinstance(nodes, dict) else []
    node_count = len(node_items)
    node_names = [n.get("metadata", {}).get("name") for n in node_items]

    gpu_nodes = _kubectl_json_required(
        ["get", "nodes", "-l", f"nvidia.com/gpu.present=true,{baseline_selector}", "-o", "json"],
        "GPU node inventory",
    )
    gpu_items = gpu_nodes.get("items", []) if isinstance(gpu_nodes, dict) else []
    gpu_node_count = len(gpu_items)
    gpu_per_node = 0
    total_gpus = 0
    for node in gpu_items:
        cap = (node.get("status", {}).get("capacity", {}) or {}).get("nvidia.com/gpu", "0")
        try:
            n = int(cap)
        except (TypeError, ValueError):
            n = 0
        total_gpus += n
        gpu_per_node = max(gpu_per_node, n)

    # runtime_class observed live: the passthrough RuntimeClass exists after
    # apply_nvidia_runtimeclass, so report it honestly (empty otherwise).
    rc_rt, _ = kubectl(["get", "runtimeclass", "nvidia"], timeout=30, echo=False)
    runtime_class = "nvidia" if rc_rt == 0 else ""

    kubernetes = {
        # driver_version: the REAL nvidia-smi version, or omitted/empty when
        # unresolved (GKE manages the version — never the install-mode label).
        "driver_version": driver_version or "",
        "node_count": node_count,
        "nodes": node_names,
        "gpu_node_count": gpu_node_count,
        "gpu_per_node": gpu_per_node,
        "total_gpus": total_gpus,
        # GKE installs GPU drivers + device plugin via Google-managed DaemonSets
        # in kube-system (no NVIDIA GPU Operator); that namespace has running GPU
        # pods, so the operator-namespace checks pass honestly.
        "gpu_operator_namespace": "kube-system",
        # GKE is a managed control plane (no control-plane pods in a namespace);
        # kube-system is the scaffold default. K8sControlPlaneLogsCheck reads
        # Cloud Logging via the provider-config command overrides.
        "control_plane_namespace": "kube-system",
        "runtime_class": runtime_class,
        "gpu_resource_name": "nvidia.com/gpu",
        # GKE runs the Cluster Autoscaler in its MANAGED control plane — there is
        # NO in-cluster cluster-autoscaler Deployment to name (emitting one would
        # point the released Deployment-shaped check at a nonexistent object).
        # Instead emit PROVIDER-NATIVE autoscaler evidence (provider=managed,
        # node_pool, enabled, min/max) read back + verified live off the system
        # node pool, so a future provider-aware validator mode can consume it. The
        # released K8sClusterAutoscalerCheck stays on its structured-skip (its
        # default in-cluster Deployment probe finds nothing) until that mode ships.
        "autoscaler": autoscaler or {},
        # Outside-vantage API-ACL probe, already RENDERED against this run's
        # resolved api_endpoint by setup (from --unauthorized-probe-template).
        # The suite feeds it into K8sApiNetworkAclCheck.commands.unauthorized_probe
        # and the check enforces the block. Empty (the default sentinel normalized
        # to absence) keeps the check on its safe structured-skip — the probe is
        # only ever ACTIVATED, never weakened.
        "unauthorized_probe_cmd": unauthorized_probe_cmd,
        # The reviewed cluster's normalized API server URL (resolve_api_endpoint,
        # from the installed kubeconfig or the GKE API). The suite binds it to
        # K8sApiNetworkAclCheck.api_endpoint so an enabled unauthorized_probe is
        # verified to target THIS cluster (target-origin + kubeconfig-consistency
        # guards) — without it a probe that trivially fails against a typo, stale,
        # or unrelated host would be misread as "ACL enforced". Setup fails closed
        # (never reaches here) when the probe is enabled but this cannot resolve.
        "api_endpoint": api_endpoint or "",
    }

    # CSI: block_storage_class is the explicit operator choice (K8S_CSI_BLOCK_SC)
    # when set, otherwise a deterministically-discovered live pd.csi.storage.gke.io
    # StorageClass so the block-storage checks execute against a real GKE class.
    # shared_fs / nfs / static fields stay explicit K8S_CSI_* capability inputs;
    # an empty value makes those checks structured-skip rather than fail.
    csi = {
        "block_storage_class": _resolve_block_storage_class(),
        "shared_fs_storage_class": os.environ.get("K8S_CSI_SHARED_FS_SC", ""),
        "nfs_storage_class": os.environ.get("K8S_CSI_NFS_SC", ""),
        "static_volume_handle": "",
        "static_driver_name": "",
    }
    return {"kubernetes": kubernetes, "csi": csi}


# GKE Persistent Disk CSI provisioner — the StorageClass provisioner the block
# checks (storage-types / quota / dynamic-provisioning) exercise on GKE.
_GKE_PD_CSI_PROVISIONER = "pd.csi.storage.gke.io"


def _resolve_block_storage_class() -> str:
    """Resolve the CSI block StorageClass for the block-storage checks.

    A non-empty K8S_CSI_BLOCK_SC is the explicit operator choice and wins. Else
    query the live cluster for pd.csi.storage.gke.io StorageClasses and choose
    deterministically: prefer the default-annotated class, then `standard-rwo`,
    then the lexicographically first match. A successful query with no matching
    class returns "" (the block checks then honestly structured-skip); only a
    failed query is an inventory error (RAISES), never a silent empty that would
    masquerade as "no class exists"."""
    explicit = os.environ.get("K8S_CSI_BLOCK_SC", "").strip()
    if explicit:
        return explicit
    data = _kubectl_json_required(["get", "storageclass", "-o", "json"], "StorageClass inventory")
    items = data.get("items", []) if isinstance(data, dict) else []
    candidates: list[str] = []
    default_names: list[str] = []
    for sc in items:
        if sc.get("provisioner") != _GKE_PD_CSI_PROVISIONER:
            continue
        meta = sc.get("metadata", {}) or {}
        name = meta.get("name")
        if not name:
            continue
        candidates.append(name)
        annotations = meta.get("annotations", {}) or {}
        if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
            default_names.append(name)
    if default_names:
        return sorted(default_names)[0]
    if "standard-rwo" in candidates:
        return "standard-rwo"
    return sorted(candidates)[0] if candidates else ""


# --------------------------------------------------------------------------- #
# Teardown: reclaim run-created PVC-backed Persistent Disks                    #
# --------------------------------------------------------------------------- #


def reclaim_run_pvcs(*, timeout: int = 240) -> None:
    """Delete run-created PVCs so the pd.csi.storage.gke.io driver reclaims each
    backing Persistent Disk BEFORE the cluster is destroyed (a GKE cluster delete
    does NOT reclaim PVC-backed PDs — they orphan as standalone Compute disks).
    Best-effort: teardown must proceed even if this races."""
    rc, out = kubectl(
        ["delete", "pvc", "--all", "--all-namespaces", "--ignore-not-found", "--timeout=120s"],
        timeout=timeout,
    )
    if rc != 0:
        log(f"warning: PVC reclaim returned rc={rc}: {fold_tail(out, limit=400)}")
    # Give the CSI controller a brief window to delete the backing PDs.
    time.sleep(15)


def delete_orphan_pds(project: str, cluster_name: str, *, timeout: int = 180, retries: int = 2) -> dict[str, Any]:
    """Backstop: delete Compute Engine disks in ANY zone whose
    goog-k8s-cluster-name label == THIS run's cluster_name (exact-ownership by
    the run's own cluster label — never a broad name-pattern sweep). The scan is
    zone-agnostic on purpose: the baseline pool and the test GPU pool each run
    their own zone-capacity selection, so run-owned PVC-backed disks can land in
    different zones; scoping to a single zone would miss a raced PD in the other
    pool's zone. Each discovered disk is deleted in its own reported zone.

    Returns a structured reclaim result::

        {"deleted": [names], "failed": [names], "list_error": str | None}

    ``list_error`` (with empty deleted/failed) means the disk LISTING itself
    failed, so run-owned disks could NOT be confirmed reclaimed — never conflated
    with an empty successful listing. ``failed`` non-empty means specific disks
    survived even after retrying safe transient failures. The caller emits
    ``cleanup_errors`` + ``success=False`` whenever either is set, so a leaked
    billable disk can never present as a clean teardown.
    """
    if not cluster_name:
        return {"deleted": [], "failed": [], "list_error": None}
    rc, out = gcloud(
        [
            "compute",
            "disks",
            "list",
            "--project",
            project,
            "--filter",
            f"labels.goog-k8s-cluster-name={cluster_name}",
            "--format=value(name,zone.basename())",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        # Listing FAILED — distinct from an empty successful listing. We cannot
        # confirm no run-owned disks remain, so the caller must not report clean.
        return {"deleted": [], "failed": [], "list_error": fold_tail(out, limit=600)}

    deleted: list[str] = []
    failed: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        disk, disk_zone = parts[0].strip(), parts[1].strip()
        # `_run` merges gcloud's stderr into stdout, so a diagnostic line can
        # masquerade as a 2-column `value(name,zone.basename())` row — e.g. an
        # empty match emits "WARNING: The following filter keys were not present
        # in any resource : labels.goog-k8s-cluster-name", which would otherwise
        # parse as a disk named "WARNING:" in a bogus zone "The" and get reported
        # as an un-reclaimed (billable) disk, falsely failing an otherwise-clean
        # teardown. A real disk row always carries a valid zone basename in the
        # second column, so gate on that shape and drop any non-disk chatter.
        if not disk or not _ZONE_RE.match(disk_zone):
            continue
        # Describe the EXACT disk and confirm a deletable/detached live state BEFORE
        # deleting it. The backstop reclaims PVC-backed disks the CSI driver leaked
        # AFTER the cluster is confirmed gone, so a genuine orphan is detached. A
        # disk still ATTACHED to a live instance (its `users` list is non-empty)
        # means a consumer — most likely a cluster whose destroy did not actually
        # complete — still holds it; force-deleting it could pull a disk out from
        # under a live cluster. A describe error that is not a clean not-found leaves
        # the disk's state UNKNOWN. In both cases refuse to delete and report the
        # disk (via `failed`) so the caller emits cleanup_errors + success=False
        # rather than a destructive best-effort delete.
        state, detail = _disk_reclaim_state(project, disk, disk_zone, timeout=timeout)
        if state == "gone":
            # Already deleted (the CSI driver won the race) — confirmed reclaimed.
            deleted.append(disk)
            continue
        if state != "detached":
            log(f"warning: disk {disk} in {disk_zone} not reclaimed ({state}): {detail}")
            failed.append(disk)
            continue
        if _delete_disk_with_retry(project, disk, disk_zone, timeout=timeout, retries=retries):
            deleted.append(disk)
        else:
            failed.append(disk)
    return {"deleted": deleted, "failed": failed, "list_error": None}


def _disk_reclaim_state(project: str, disk: str, disk_zone: str, *, timeout: int = 120) -> tuple[str, str]:
    """Describe one exact disk and classify whether the PD backstop may delete it.

    Returns one of:
      * ``("detached", "")``          - present and NOT attached to any instance -> safe to delete.
      * ``("attached", detail)``      - a live instance still holds it (``users`` non-empty);
                                        a consumer (most likely a cluster whose destroy did
                                        not complete) still uses it -> must NOT delete.
      * ``("gone", "")``              - describe returns a clean not-found -> already reclaimed.
      * ``("describe_error", detail)``- describe failed for another reason -> the disk's
                                        deletable state cannot be confirmed -> must NOT delete.
    """
    rc, out = gcloud(
        [
            "compute",
            "disks",
            "describe",
            disk,
            "--zone",
            disk_zone,
            "--project",
            project,
            "--format=json",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        if _classify_cli_output(out) == "not_found":
            return ("gone", "")
        return ("describe_error", fold_tail(out, limit=400))
    try:
        payload = json.loads(out) or {}
    except json.JSONDecodeError as exc:
        return ("describe_error", f"unparseable disk describe JSON: {exc}")
    users = payload.get("users") or []
    if users:
        return ("attached", f"still attached to {len(users)} instance(s): {users}")
    return ("detached", "")


def _delete_disk_with_retry(project: str, disk: str, disk_zone: str, *, timeout: int = 180, retries: int = 2) -> bool:
    """Delete one run-owned disk; retry safe transient failures. Returns True when
    the disk is confirmed gone (deleted, or already not_found), False otherwise."""
    for attempt in range(retries + 1):
        rc, out = gcloud(
            ["compute", "disks", "delete", disk, "--zone", disk_zone, "--project", project, "--quiet"],
            timeout=timeout,
            echo=False,
        )
        if rc == 0 or _classify_cli_output(out) == "not_found":
            return True
        if _is_transient_cleanup_error(out) and attempt < retries:
            time.sleep(5)
            continue
        log(f"warning: disk {disk} in {disk_zone} not reclaimed (rc={rc}): {fold_tail(out, limit=400)}")
        return False
    return False


def _resolve_probe_mig_zone(project: str, mig_name: str, *, timeout: int) -> tuple[str | None, str | None]:
    """Resolve the zone of an EXACT-named probe MIG, or classify its absence.

    Returns ``(zone, None)`` when the exact MIG is live in a readable zone,
    ``(None, None)`` when it is CONFIRMED absent (a clean exact-name list with no
    matching row), and ``(None, error)`` when the list itself could not be read
    (fail closed — an unreadable list is never treated as absence). The filter is an
    EXACT ``name=`` equality on the full persisted probe name (never a truncated
    ``name~^prefix`` regex), so a concurrent run whose id shares this run's 8-char
    scope can never be selected.
    """
    rc, out = gcloud(
        [
            "compute",
            "instance-groups",
            "managed",
            "list",
            "--project",
            project,
            "--filter",
            f"name={mig_name}",
            "--format=value(name,zone.basename())",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        return None, fold_tail(out, limit=600)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, zone = parts[0].strip(), parts[1].strip()
        if name == mig_name and _ZONE_RE.match(zone):
            return zone, None
    return None, None


def delete_orphan_gpu_probes(project: str, *, timeout: int = 180, retries: int = 2) -> dict[str, Any]:
    """Backstop: reclaim any GPU capacity-preflight probe resource THIS run left
    behind, keyed by the EXACT persisted probe names.

    select_gpu_zone stands up throwaway size-1 GPU Managed Instance Groups (+ their
    instance templates) and deletes them inline best-effort; an unconfirmed inline
    delete would otherwise leak a billable size-1 GPU MIG that no teardown step
    consumes (the probe MIG is a standalone Compute resource, NOT part of any
    Terraform state and NOT carrying the cluster label delete_orphan_pds scopes on).

    ``mark_probes_pending`` fails CLOSED before EVERY probe create, so the retained-
    probe marker holds the exact full names of every probe whose inline delete is not
    yet confirmed. Reclaim is driven off THOSE exact names — never an ``isv-gpumig-
    <8-char>-`` prefix sweep. The run-scope id is truncated to 8 chars for the GKE
    name cap, so two runs sharing the first 8 chars of RUN_ID share that prefix; a
    prefix sweep could then delete a CONCURRENT run's billable probe. Exact-name
    reclaim (each name carries its own per-probe random discriminator) cannot select
    another run's probes, satisfying the full-scoped-identity cleanup requirement.

    Returns ``{"deleted": [names], "failed": [names], "list_error": str | None}``
    with the same contract as delete_orphan_pds, so the caller emits
    ``cleanup_errors`` + ``success=False`` whenever a probe cannot be confirmed
    reclaimed. An UNREADABLE marker or a failed exact-name LIST (distinct from a
    clean confirmed-absent) surfaces as ``list_error`` so an unverifiable probe never
    presents as a clean teardown.
    """
    try:
        marker_path = _retained_probes_marker_path()
    except LifecycleError as exc:  # run-scope id unset
        return {"deleted": [], "failed": [], "list_error": exc.detail}
    try:
        pending = _read_pending_probe_names(marker_path)
    except OSError as exc:
        # An EXISTING-but-unreadable marker must never be read as "nothing pending":
        # fail closed so the caller surfaces an unverified probe rather than a clean
        # teardown.
        return {"deleted": [], "failed": [], "list_error": f"retained-probe marker {marker_path} unreadable: {exc}"}

    # Partition the persisted names by their resource stem. Every recorded name is a
    # zonal probe MIG or a global instance template (mark_probes_pending records the
    # pair together at create time).
    wanted_migs = [n for n in pending if n.startswith("isv-gpumig-")]
    wanted_tmpls = [n for n in pending if n.startswith("isv-gpuprobe-")]

    deleted: list[str] = []
    failed: list[str] = []

    # 1) Zonal Managed Instance Groups, reclaimed by EXACT persisted name. Resolve
    #    each name's zone with an exact-name list (fail closed on an unreadable list),
    #    then delete; a confirmed-absent MIG is already reclaimed.
    for mig in wanted_migs:
        zone, list_error = _resolve_probe_mig_zone(project, mig, timeout=timeout)
        if list_error is not None:
            return {"deleted": deleted, "failed": failed, "list_error": list_error}
        if zone is None:
            continue  # confirmed absent — nothing billable to reclaim
        if _delete_probe_resource(
            [
                "compute",
                "instance-groups",
                "managed",
                "delete",
                mig,
                "--zone",
                zone,
                "--project",
                project,
                "--quiet",
            ],
            kind="orphan probe MIG",
            name=f"{mig} (zone {zone})",
            retries=retries,
        ):
            deleted.append(mig)
        else:
            failed.append(mig)

    # 2) Global instance templates, reclaimed by EXACT persisted name (a not_found
    #    delete is confirmed-absent, so _delete_probe_resource returns True).
    for tmpl in wanted_tmpls:
        if _delete_probe_resource(
            ["compute", "instance-templates", "delete", tmpl, "--project", project, "--quiet"],
            kind="orphan probe instance-template",
            name=tmpl,
            retries=retries,
        ):
            deleted.append(tmpl)
        else:
            failed.append(tmpl)

    return {"deleted": deleted, "failed": failed, "list_error": None}


# --------------------------------------------------------------------------- #
# Zone derivation helper                                                       #
# --------------------------------------------------------------------------- #

_ZONE_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+-[a-z]$")
_REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")


def _as_zone(value: str) -> str:
    """Coerce one location token to a zonal value the Compute probe accepts.

    An explicit zone (``us-central1-a``) is preserved unchanged; a REGIONAL value
    (``us-central1``) is derived to its ``-a`` zone. This matters because the
    config forwards GCP_K8S_LOCATION as the GPU-zones fallback (so the
    ``--gpu-node-locations`` token is never dropped), and that documented
    location is commonly a REGION — a standalone-MIG probe runs ``--zone``, which
    GCE deterministically rejects for a bare region. Anything else is passed
    through so ``select_gpu_zone`` surfaces the real invalid-zone error.
    """
    token = value.strip()
    if _ZONE_RE.match(token):
        return token
    if _REGION_RE.match(token):
        return f"{token}-a"
    return token


def candidate_gpu_zones(gpu_node_locations: str, location: str) -> list[str]:
    """Resolve the ordered candidate zone list for the GPU capacity preflight.

    Uses the operator-supplied comma-separated GCP_K8S_GPU_ZONES when present;
    otherwise falls back to the single cluster location. EACH token is coerced to
    a zone (an explicit zone is preserved unchanged; a regional value — including
    a regional forwarded location — is derived to its ``-a`` zone), so a zonal
    Compute probe never runs with a bare region.
    """
    raw = gpu_node_locations.split(",") if gpu_node_locations and gpu_node_locations.strip() else [location]
    zones: list[str] = []
    for token in raw:
        zone = _as_zone(token)
        if zone and zone not in zones:
            zones.append(zone)
    return zones


def zone_for_location(location: str) -> str:
    """Return a SINGLE zone for a cluster location (region -> its ``-a`` zone; an
    explicit zone preserved unchanged).

    Used to pin the CPU / system / secondary node pools to ONE zone the same way
    the GPU pools already pin theirs. ``google_container_node_pool.node_count`` is
    PER-ZONE, so a pool that inherits a REGIONAL cluster's node locations spreads
    ``node_count`` across every zone in the region (node_count x #zones). Pinning
    to one zone keeps the actual Ready node count equal to the emitted
    ``expected_replicas`` and holds the CPU/system pools single-zone (cheap), even
    when the operator supplies a region.
    """
    return _as_zone(location)


def resolve_project_id() -> str:
    """Thin passthrough to the shared ADC-aware project resolver."""
    return resolve_project(None)


# --------------------------------------------------------------------------- #
# Multi-cluster (shared-VPC) helpers                                          #
# --------------------------------------------------------------------------- #


def gke_cluster_status_active(cluster_name: str, location: str, project: str) -> str:
    """Return the contract sentinel 'ACTIVE' when the GKE cluster is up.

    GKE reports a ready cluster's lifecycle state as 'RUNNING' (its Status enum
    has no 'ACTIVE'); K8sMultiClusterSameVpcCheck exact-matches the upper-cased
    value against 'ACTIVE'. Map the GKE up-state to the contract sentinel —
    never pass the raw 'RUNNING' through (the check would fail on it). A FAILED
    describe RAISES a classified LifecycleError (it is never emitted as status
    text or 'UNKNOWN' alongside a success=True step); a SUCCESSFUL describe
    reporting a non-RUNNING state is surfaced verbatim so a not-yet-ready cluster
    stays visible.
    """
    rc, out = gcloud(
        [
            "container",
            "clusters",
            "describe",
            cluster_name,
            "--location",
            location,
            "--project",
            project,
            "--format=value(status)",
        ],
        timeout=120,
        echo=False,
    )
    if rc != 0:
        bucket = _classify_cli_output(out)
        raise LifecycleError(
            bucket,
            f"[bucket={bucket}] `gcloud container clusters describe` failed for "
            f"{cluster_name} in {location}: {fold_tail(out)}",
        )
    # Same merged-stderr hazard as delete_orphan_pds: gcloud may prepend a
    # WARNING/"Updates are available ..." diagnostic to the single `value(status)`
    # line, so `out.strip()` is not guaranteed to be the bare status. GKE reports
    # status as one all-caps token (RUNNING/PROVISIONING/RECONCILING/...); pick
    # the first line that is exactly such a token and ignore any chatter.
    status = ""
    for candidate in out.splitlines():
        token = candidate.strip().upper()
        if token and re.fullmatch(r"[A-Z_]+", token):
            status = token
            break
    if status == "RUNNING":
        return "ACTIVE"
    return status or "UNKNOWN"


def ready_node_count_isolated(
    cluster_name: str, location: str, project: str, *, timeout: int = 180
) -> tuple[int, LifecycleError | None]:
    """Return ``(ready_node_count, read_failure)`` for a cluster, using an ISOLATED
    temp kubeconfig.

    Never mutates the ambient ~/.kube/config, so checking the secondary cluster
    does not switch the current context away from the primary (the test-phase
    in-cluster checks run against the primary via ambient kubectl).

    ``read_failure`` is None on a clean read — a count of 0 then means the cluster
    is reachable but has no Ready node YET, a legitimate keep-waiting signal. A
    FAILED credential fetch or ``kubectl`` call, or a malformed JSON response, is
    returned as a classified LifecycleError (constructed, never raised here) so the
    readiness wait can surface a terminal auth/permission/not-found failure
    IMMEDIATELY and retain a transient one for its timeout diagnostic, instead of
    collapsing every failure into an indistinguishable zero count waited out for
    the whole budget.
    """
    import tempfile

    fd, kubeconfig = tempfile.mkstemp(suffix=f"-{run_scope_id()}.kubeconfig")
    os.close(fd)
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    try:
        rc, cred_out = _run(
            [
                "gcloud",
                "container",
                "clusters",
                "get-credentials",
                cluster_name,
                "--location",
                location,
                "--project",
                project,
            ],
            env=env,
            timeout=timeout,
            echo=False,
        )
        if rc != 0:
            bucket = _classify_cli_output(cred_out)
            return 0, LifecycleError(
                bucket,
                f"[bucket={bucket}] `gcloud container clusters get-credentials` failed for "
                f"secondary cluster {cluster_name} in {location}: {fold_tail(cred_out)}",
            )
        rc2, out = _run(["kubectl", "get", "nodes", "-o", "json"], env=env, timeout=timeout, echo=False)
        if rc2 != 0:
            bucket = _classify_cli_output(out)
            return 0, LifecycleError(
                bucket,
                f"[bucket={bucket}] `kubectl get nodes` failed for secondary cluster {cluster_name}: {fold_tail(out)}",
            )
        if not out.strip():
            return 0, LifecycleError(
                "transient",
                f"[bucket=transient] `kubectl get nodes` returned no output for secondary cluster {cluster_name}.",
            )
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return 0, LifecycleError(
                "transient",
                f"[bucket=transient] `kubectl get nodes` returned malformed JSON for secondary "
                f"cluster {cluster_name}: {fold_tail(out)}",
            )
        count = 0
        for node in data.get("items", []):
            conds = node.get("status", {}).get("conditions", []) or []
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds):
                count += 1
        return count, None
    finally:
        try:
            os.remove(kubeconfig)
        except OSError:
            pass


def wait_secondary_ready(
    cluster_name: str,
    location: str,
    project: str,
    *,
    timeout: int = 900,
    poll_interval: int = 15,
) -> int:
    """Block until the secondary cluster reports >=1 Ready node; return the count."""
    deadline = time.time() + timeout
    count = 0
    last_read_error: LifecycleError | None = None
    while time.time() < deadline:
        count, read_error = ready_node_count_isolated(cluster_name, location, project)
        if read_error is not None:
            if read_error.bucket in _TERMINAL_READ_BUCKETS:
                # Bad credentials, a missing permission, or a genuinely absent
                # cluster never converges — surface it now instead of polling out
                # the whole budget and losing the diagnostic to a generic timeout.
                raise read_error
            last_read_error = read_error  # transient/unknown: retain, keep polling
        else:
            last_read_error = None  # a clean read clears any retained transient error
            if count >= 1:
                return count
        log(f"  waiting for secondary cluster {cluster_name} to report a Ready node ({count} so far)...")
        time.sleep(poll_interval)
    detail = f"[bucket=transient] secondary cluster {cluster_name} did not report a Ready node within {timeout}s."
    if last_read_error is not None:
        # The budget closed while readiness reads were still failing — carry the
        # retained API failure so the operator sees WHY, not just "timed out".
        detail += f" Last readiness read failure: {last_read_error.detail}"
    raise LifecycleError("transient", detail)
