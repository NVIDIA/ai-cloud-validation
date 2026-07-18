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
        return 124, f"{partial}\n[timeout after {timeout}s running: {' '.join(args)}]"
    if echo and proc.stdout:
        sys.stderr.write(proc.stdout)
        sys.stderr.flush()
    return proc.returncode, proc.stdout or ""


def _classify_cli_output(output: str) -> str:
    """Map a failing CLI output to a disposition bucket (best-effort)."""
    low = output.lower()
    if any(tok.lower() in low for tok in _STOCKOUT_TOKENS):
        return "transient"
    if "quota_exceeded" in low or "quota exceeded" in low:
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
    if "not found" in low or "notfound" in low or "404" in low:
        return "not_found"
    if "already exists" in low or "alreadyexists" in low or "409" in low:
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


def normalize_authorized_cidrs(raw: str) -> list[str]:
    """Parse the comma-separated control-plane authorized CIDR list.

    The `none` sentinel or a blank value returns [] (authorized networks left
    unconfigured). A bare IPv4 normalizes to /32. Every entry must be a valid
    CIDR and MUST NOT be world-open (0.0.0.0/0 or ::/0) — a world-open entry
    defeats the ACL, so it is a hard config_error, never a silent pass."""
    v = normalize_sentinel(raw)
    if not v:
        return []
    out: list[str] = []
    for token in v.split(","):
        token = token.strip()
        if not token:
            continue
        candidate = token if "/" in token else f"{token}/32"
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


def _note_retained_probes(retained: list[str]) -> None:
    """Surface any probe resource whose inline delete was not confirmed.

    The names are run-scoped (``isv-gpumig-<run_id>-*`` / ``isv-gpuprobe-<run_id>-*``),
    so teardown's delete_orphan_gpu_probes backstop reclaims them deterministically
    even though select_gpu_zone stays best-effort here (it must not fail a found
    capacity zone on a transient cleanup hiccup)."""
    if retained:
        log(
            f"note: GPU capacity preflight left {len(retained)} probe resource(s) "
            f"unconfirmed-deleted ({'; '.join(retained)}); teardown's run-scoped probe "
            "backstop will reclaim them."
        )


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
            network or "default",
            "--image-family",
            "debian-12",
            "--image-project",
            "debian-cloud",
            "--boot-disk-size",
            "50GB",
        ]
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
                _note_retained_probes(_delete_probe(project, zone, mig_name, template_name))
                bucket = _classify_cli_output(out)
                raise LifecycleError(
                    bucket,
                    f"[bucket={bucket}] GPU probe instance-template create failed in "
                    f"{zone} with a NON-stockout error (config/policy/quota, not "
                    f"capacity): {fold_tail(out)}",
                )
            _note_retained_probes(_delete_probe(project, zone, mig_name, template_name))
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
            _note_retained_probes(_delete_probe(project, zone, mig_name, template_name))

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


def label_gpu_nodes(*, timeout: int = 120) -> None:
    """Apply nvidia.com/gpu.present=true to every GKE GPU node (honest bridge).

    GKE's managed-driver path does NOT set nvidia.com/gpu.present (a
    GPU-Operator/GFD label); GKE GPU nodes carry cloud.google.com/gke-accelerator
    and advertise the nvidia.com/gpu resource. The released GPU checks discover
    GPU nodes via `kubectl get nodes -l nvidia.com/gpu.present=true`, so map GKE's
    native GPU labeling to the label those checks select on. The gke-accelerator
    selector covers every GPU node (baseline + test pools) in one pass.
    """
    rc, out = kubectl(
        [
            "label",
            "nodes",
            "-l",
            "cloud.google.com/gke-accelerator",
            "nvidia.com/gpu.present=true",
            "--overwrite",
        ],
        timeout=timeout,
    )
    if rc != 0:
        # Non-fatal if there are simply no GPU nodes yet; the two-gate preflight
        # is the authoritative readiness gate. Surface as a warning only.
        log(f"warning: labeling GPU nodes returned rc={rc}: {fold_tail(out, limit=400)}")


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


def wait_two_gate_gpu_ready(*, timeout: int = 900, poll_interval: int = 15) -> str | None:
    """Block until a GPU node reaches the provider-native GPU-ready state, using
    NO-image-pull signals only: the node is Ready, advertises nonzero allocatable
    ``nvidia.com/gpu``, AND hosts a Ready GKE-managed GPU device-plugin DaemonSet
    pod (which reports Ready only after the managed driver install completes).

    Returns the driver version read best-effort from that already-running managed
    pod (``kubectl exec nvidia-smi``, no new pull), or None when it cannot be read
    — driver_version is OPTIONAL inventory (K8sDriverVersionCheck skips on empty),
    and readiness NEVER depends on reading it. Never returns while no GPU node has
    reached the provider-native ready state.
    """
    deadline = time.time() + timeout
    label_gpu_nodes()
    while time.time() < deadline:
        nodes = _kubectl_json(["get", "nodes", "-l", "nvidia.com/gpu.present=true", "-o", "json"])
        items = (nodes or {}).get("items", []) if isinstance(nodes, dict) else []
        for node in items:
            node_name = (node.get("metadata", {}) or {}).get("name")
            status = node.get("status", {}) or {}
            allocatable = status.get("allocatable", {}) or {}
            gpu = allocatable.get("nvidia.com/gpu")
            conditions = status.get("conditions", []) or []
            is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            if node_name and is_ready and gpu and str(gpu) not in ("", "0"):
                if _gpu_device_plugin_pod_ready_on_node(node_name):
                    # Provider-native GPU readiness satisfied. Read the driver
                    # version best-effort from the managed pod (no image pull).
                    return _probe_driver_version()
        log(
            "  waiting for a Ready GPU node with allocatable nvidia.com/gpu and a Ready "
            "GKE-managed device-plugin pod..."
        )
        label_gpu_nodes()
        time.sleep(poll_interval)
    raise LifecycleError(
        "transient",
        "[bucket=transient] GPU readiness preflight timed out: no GPU node reached "
        "Ready with allocatable nvidia.com/gpu and a Ready GKE-managed device-plugin "
        f"pod within {timeout}s.",
    )


# --------------------------------------------------------------------------- #
# Pool-scoped GPU completion gate (create_test_gpu_node_pool)                  #
# --------------------------------------------------------------------------- #


def _ready_gpu_node_names(label_selector: str) -> list[str]:
    """Names of nodes matching ``label_selector`` that satisfy all three
    provider-native GPU-ready signals: Ready=True, nonzero allocatable
    ``nvidia.com/gpu``, AND a Ready GKE-managed GPU device-plugin DaemonSet pod
    on the node (the no-image-pull driver-ready signal).

    Scoped to ONE pool's own selector (``cloud.google.com/gke-nodepool=<pool>``),
    so the setup baseline GPU pool's nodes never satisfy THIS pool's readiness
    count — the exact distinction the cluster-wide readiness helper cannot make.
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

    Why this is NOT ``wait_two_gate_gpu_ready`` (cluster-wide): the setup baseline
    GPU pool could already satisfy a cluster-wide gate while THIS test pool is
    still unregistered or driver-less, so a cluster-wide gate would let the create
    step emit success before the new nodes are Ready or discoverable — the exact
    false-success this pool-scoped gate prevents. A timeout, labeling error,
    missing node, or readback mismatch RAISES so the create step's success stays
    false and the released GPU checks never run against an unready or
    undiscoverable test pool.
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

    nodes = _kubectl_json_required(["get", "nodes", "-o", "json"], "node inventory")
    node_items = nodes.get("items", []) if isinstance(nodes, dict) else []
    node_count = len(node_items)
    node_names = [n.get("metadata", {}).get("name") for n in node_items]

    gpu_nodes = _kubectl_json_required(
        ["get", "nodes", "-l", "nvidia.com/gpu.present=true", "-o", "json"], "GPU node inventory"
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
        "cluster_autoscaler_namespace": "kube-system",
        "cluster_autoscaler_deployment": "cluster-autoscaler",
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
        if _delete_disk_with_retry(project, disk, disk_zone, timeout=timeout, retries=retries):
            deleted.append(disk)
        else:
            failed.append(disk)
    return {"deleted": deleted, "failed": failed, "list_error": None}


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


def delete_orphan_gpu_probes(project: str, *, timeout: int = 180, retries: int = 2) -> dict[str, Any]:
    """Backstop: reclaim any GPU capacity-preflight probe resource THIS run left
    behind.

    select_gpu_zone stands up throwaway size-1 GPU Managed Instance Groups (+ their
    instance templates), named ``isv-gpumig-<run_id>-*`` / ``isv-gpuprobe-<run_id>-*``,
    and deletes them inline best-effort; an unconfirmed inline delete would
    otherwise leak a billable size-1 GPU MIG that no teardown step consumes (the
    probe MIG is a standalone Compute resource, NOT part of any Terraform state and
    NOT carrying the cluster label delete_orphan_pds scopes on). Reclaim by THIS
    run's probe-name prefix (exact run-ownership via the run-scope id — never a
    broad ``isv-gpumig-*`` sweep across other runs).

    Returns ``{"deleted": [names], "failed": [names], "list_error": str | None}``
    with the same contract as delete_orphan_pds, so the caller emits
    ``cleanup_errors`` + ``success=False`` whenever a probe cannot be confirmed
    reclaimed. A failed LISTING (distinct from an empty listing) surfaces as
    ``list_error`` so an unverifiable probe never presents as a clean teardown.
    """
    try:
        sid = run_scope_id()
    except LifecycleError as exc:
        return {"deleted": [], "failed": [], "list_error": exc.detail}
    mig_prefix = normalize_gke_name(f"isv-gpumig-{sid}")
    tmpl_prefix = normalize_gke_name(f"isv-gpuprobe-{sid}")

    deleted: list[str] = []
    failed: list[str] = []

    # 1) Zonal Managed Instance Groups whose name is one of this run's probe MIGs.
    rc, out = gcloud(
        [
            "compute",
            "instance-groups",
            "managed",
            "list",
            "--project",
            project,
            "--filter",
            f"name~^{mig_prefix}-",
            "--format=value(name,zone.basename())",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        return {"deleted": [], "failed": [], "list_error": fold_tail(out, limit=600)}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        mig, mig_zone = parts[0].strip(), parts[1].strip()
        # Drop merged-stderr chatter: a real probe MIG row starts with THIS run's
        # prefix and carries a valid zone basename in the second column.
        if not mig.startswith(mig_prefix) or not _ZONE_RE.match(mig_zone):
            continue
        if _delete_probe_resource(
            [
                "compute",
                "instance-groups",
                "managed",
                "delete",
                mig,
                "--zone",
                mig_zone,
                "--project",
                project,
                "--quiet",
            ],
            kind="orphan probe MIG",
            name=f"{mig} (zone {mig_zone})",
            retries=retries,
        ):
            deleted.append(mig)
        else:
            failed.append(mig)

    # 2) Global instance templates whose name is one of this run's probe templates.
    rc, out = gcloud(
        [
            "compute",
            "instance-templates",
            "list",
            "--project",
            project,
            "--filter",
            f"name~^{tmpl_prefix}-",
            "--format=value(name)",
        ],
        timeout=timeout,
        echo=False,
    )
    if rc != 0:
        return {"deleted": deleted, "failed": failed, "list_error": fold_tail(out, limit=600)}
    for line in out.splitlines():
        tmpl = line.strip()
        if not tmpl or not tmpl.startswith(tmpl_prefix):
            continue
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


def ready_node_count_isolated(cluster_name: str, location: str, project: str, *, timeout: int = 180) -> int:
    """Count Ready nodes in a cluster using an ISOLATED temp kubeconfig.

    Never mutates the ambient ~/.kube/config, so checking the secondary cluster
    does not switch the current context away from the primary (the test-phase
    in-cluster checks run against the primary via ambient kubectl).
    """
    import tempfile

    fd, kubeconfig = tempfile.mkstemp(suffix=f"-{run_scope_id()}.kubeconfig")
    os.close(fd)
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    try:
        rc, _ = _run(
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
            return 0
        rc2, out = _run(["kubectl", "get", "nodes", "-o", "json"], env=env, timeout=timeout, echo=False)
        if rc2 != 0 or not out.strip():
            return 0
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return 0
        count = 0
        for node in data.get("items", []):
            conds = node.get("status", {}).get("conditions", []) or []
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds):
                count += 1
        return count
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
    while time.time() < deadline:
        count = ready_node_count_isolated(cluster_name, location, project)
        if count >= 1:
            return count
        log(f"  waiting for secondary cluster {cluster_name} to report a Ready node ({count} so far)...")
        time.sleep(poll_interval)
    raise LifecycleError(
        "transient",
        f"[bucket=transient] secondary cluster {cluster_name} did not report a Ready node within {timeout}s.",
    )
