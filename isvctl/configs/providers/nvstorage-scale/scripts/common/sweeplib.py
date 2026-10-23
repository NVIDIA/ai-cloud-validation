#!/usr/bin/env python3
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

"""Shared helpers for the nvstorage-scale ACV provider steps.

Two ACV mechanics constrain everything in here, both confirmed by reading
``isvctl/src/isvctl/orchestrator/step_executor.py``:

1. **stdout must be JSON and nothing else.** ``_parse_output`` does
   ``json.loads(result.stdout.strip())`` on the *whole* stream and, on failure, logs at
   DEBUG and moves on. A stray ``print()`` does not raise — it silently produces a step
   with no output, and the next step's ``{{ steps.x.y }}`` then resolves to a *skip*, not
   an error. Every diagnostic in these scripts goes to stderr via :func:`log`.
2. **``StepConfig.env`` is never templated.** ``_execute_step`` renders ``step.args``
   through Jinja2 and then does a plain ``env.update(step.env)`` with the raw dict. So
   static sweep configuration can travel in ``env:``, but anything produced by an earlier
   step (the env.sh path, the sweep dir, the job id) *must* travel in ``args:``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SLURM, SSH, KUBECTL = "slurm", "ssh", "kubectl"
ALL_SUBSTRATES = (SLURM, SSH, KUBECTL)

# The four sentinel values _atomic_write_sentinel ever writes to <NNNN>.status.
PENDING, RUNNING, SUCCESS, FAILED = "PENDING", "RUNNING", "SUCCESS", "FAILED"
TERMINAL = (SUCCESS, FAILED)

# --------------------------------------------------------------------------
# Workloads.
#
# storage-scale-test has two *filesystem* front doors that are structurally the same
# program: `nv-elbencho-sweep.sh` (bandwidth/IOPS) and `nv-mdtest-elbencho.sh` (metadata
# create/stat/delete). Both route through `elbencho_batch_cli_route`, both source the same
# `kubectl/_nv-elbencho-kubectl-functions.sh`, and both accept the identical lifecycle verbs
# `--resume/--status/--cancel/--collect`. Only three things differ, and they are exactly the
# three recorded here: the driver path, the output directory name, and the report extractor.
#
# The other two front doors are deliberately absent:
#   * `nv-warp-sweep.sh`   (object/S3)  -- refuses kubectl outright, nv-warp-sweep.sh:110
#   * `nv-netbench.sh`     (network)    -- refuses kubectl outright, nv-netbench.sh:124
# Both are slurm/ssh only, and neither can be exercised on a Kubernetes dev cluster, so they
# are not registered here rather than shipped as code nothing can run.

WORKLOAD_IO, WORKLOAD_MDTEST = "io", "mdtest"
ALL_WORKLOADS = (WORKLOAD_IO, WORKLOAD_MDTEST)


class Workload:
    """Where a workload's driver, output directories and extractor live."""

    def __init__(self, name: str, driver: str, dir_re: str, extractor: str, metrics_glob: str) -> None:
        self.name = name
        self.driver = driver
        self.dir_re = re.compile(dir_re)
        self.extractor = extractor
        self.metrics_glob = metrics_glob

    def driver_path(self, base: str) -> str:
        return os.path.join(base, self.driver)

    def extractor_path(self, base: str) -> str:
        return os.path.join(base, self.extractor)


WORKLOADS: dict[str, Workload] = {
    WORKLOAD_IO: Workload(
        WORKLOAD_IO,
        os.path.join("storage-tests", "fs", "nv-elbencho-sweep.sh"),
        # Anchored, so it can never match `mdtest-elbencho-<DS>`. The two workloads write
        # into the same RESULTS_DIR, and an unanchored match would hand an io poll an
        # mdtest sweep directory.
        r"^elbencho-\d{8}Z\d{6}$",
        os.path.join("utils", "extract-elbencho.py"),
        # extract-elbencho.py:4636 -- a FIXED name, written into the first input directory.
        "elbencho-metrics.csv",
    ),
    WORKLOAD_MDTEST: Workload(
        WORKLOAD_MDTEST,
        os.path.join("storage-tests", "fs", "nv-mdtest-elbencho.sh"),
        r"^mdtest-elbencho-\d{8}Z\d{6}$",
        os.path.join("utils", "extract-mdtest-elbencho.py"),
        # NOT a fixed name: extract-mdtest-elbencho.py:2410-2415 interpolates the sweep's
        # datestamps, so the only safe way to find it is a glob. Hard-coding
        # "mdtest-elbencho-metrics.csv" would never match anything.
        "mdtest-elbencho-metrics-*.csv",
    ),
}


# The ONLY reporter that understands a prepared batch. `extract-elbencho.py` and
# `extract-mdtest-elbencho.py` take result directories; pointed at a batch root they see a
# tree of `groups/NNNN/<kind>-<DS>` and find nothing to parse. `extract-filesystem.py`
# reads `batch-manifest.tsv`, selects groups by `--kind`/`--groups`, dispatches each to its
# own engine, and writes per-group reports under `<root>/reports/groups/<NNNN>/`
# (lib/filesystem_batch.py:444-494). It is also the only way to report a MIXED batch, via
# `--kind all`.
BATCH_EXTRACTOR = os.path.join("utils", "extract-filesystem.py")
BATCH_REPORTS_SUBDIR = os.path.join("reports", "groups")

# The completeness reporter. `utils/summarize-elbencho.py` is the only tool upstream ships
# that answers "did this sweep run everything it was configured to run" -- it rebuilds the
# expected cell matrix from `env_used.yaml` (nodes_spec x IO sizes x threads x iodepth) and
# diffs it against the `elbencho-*-c_*-s_*-d_*_<DS>.out` files on disk. Nothing else in the
# toolchain crosses from "what was produced" back to "what was asked for".
#
# It is IO-ONLY by construction: `_validate_io_sweep_env` rejects an mdtest `env_used.yaml`
# ("looks like mdtest-elbencho, not nv-elbencho IO sweep") and skips the directory
# (utils/summarize-elbencho.py:_summarize_directory).
SUMMARIZER = os.path.join("utils", "summarize-elbencho.py")

# Batch group directories live at `groups/NNNN/<workload prefix>-<DS>`
# (lib/_batch_functions.sh, group dir regex). The group number is zero-padded to four.
_GROUP_NUM_RE = re.compile(r"^\d{4}$")


def find_group_dirs(batch_root: str, kind: str = WORKLOAD_IO) -> list[str]:
    """Every `groups/NNNN/<kind>-<DS>` directory under a batch root, in group order.

    A batch root holds no result directory of its own, so any tool that takes result
    directories -- the summarizer among them -- has to be handed the group dirs instead.
    Returns [] for a non-batch path, which lets callers treat "plain sweep" as the
    zero-group case without a separate branch.
    """
    groups_dir = os.path.join(batch_root, "groups")
    if not os.path.isdir(groups_dir):
        return []
    pattern = workload(kind).dir_re
    found: list[str] = []
    for num in sorted(os.listdir(groups_dir)):
        if not _GROUP_NUM_RE.match(num):
            continue
        group = os.path.join(groups_dir, num)
        if not os.path.isdir(group):
            continue
        for name in sorted(os.listdir(group)):
            if pattern.match(name) and os.path.isdir(os.path.join(group, name)):
                found.append(os.path.join(group, name))
    return found


def workload(name: str) -> Workload:
    try:
        return WORKLOADS[name]
    except KeyError:
        raise ValueError(f"unknown workload {name!r}; known: {', '.join(ALL_WORKLOADS)}") from None


def sst_version(base: str, timeout: int = 60) -> str | None:
    """The storage-scale-test version the sweep actually ran against.

    Every front door accepts ``--version`` (``lib/project_version.sh``). Recording it makes a
    result attributable to a checkout; without it a regression between two runs is
    indistinguishable from an upstream change. The value carries a ``-modified`` suffix when
    the working tree is dirty -- which it always is under this provider, because
    :func:`submit_sweep.install_env_sh` replaces ``env.sh``. That is informative, not a bug,
    and the suffix is left in place deliberately rather than scrubbed.
    """
    driver = WORKLOADS[WORKLOAD_IO].driver_path(base)
    if not os.path.isfile(driver):
        return None
    proc = run(["bash", driver, "--version"], timeout=timeout)
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip().splitlines()
    return value[0].strip() if value else None


# The validation domain this provider reports under, matching the other storage providers.
PLATFORM = "storage"


def log(msg: str) -> None:
    """Diagnostics go to stderr; stdout is reserved for the JSON contract."""
    print(msg, file=sys.stderr, flush=True)


def emit(payload: dict) -> int:
    """Write the step's JSON result and return the process exit code.

    ``success`` is part of the payload *and* drives the exit code, because ACV consumes
    both: ``StepResult.success`` comes from the return code, while validations read the
    parsed output. Keeping them consistent avoids a step that "passed" with a failure body.

    ``platform`` and ``cluster_name`` are injected because ACV validates every step's stdout
    against ``isvctl/schemas/command_output.schema.json``, whose top level requires them
    (plus ``success``). A step that omits any of the three does all of its work and *then*
    fails with "Validation error at $: '<field>' is a required property" -- the work is done,
    the sweep is running, and the step still reports failure.
    """
    # `platform` is the validation domain, the same "storage" every storage provider's
    # scripts report. Where the sweep ran is a separate fact, `substrate`.
    payload.setdefault("platform", PLATFORM)
    # The substrate fallback is "unknown", NOT a substrate name. ACV's `env:` is per-step,
    # so SST_SUBSTRATE is only set for steps that declare it; a step that does not (metrics)
    # would otherwise label every run with whatever name sat here. Defaulting to "slurm"
    # silently mislabelled kubectl runs as Slurm -- wrong provenance on real results, which
    # is worse than an obviously-absent one. Pass --substrate to make this accurate.
    payload.setdefault("substrate", os.environ.get("SST_SUBSTRATE") or "unknown")
    payload.setdefault(
        "cluster_name", os.environ.get("SST_CLUSTER_NAME") or os.environ.get("CLUSTER_NAME") or "unknown"
    )
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if payload.get("success") else 1


def fail(reason: str, **extra) -> int:
    log(f"error: {reason}")
    return emit({"success": False, "error": reason, **extra})


def find_sweep_dirs(results_dir: str, kind: str = WORKLOAD_IO) -> list[str]:
    """Every output directory of workload *kind* under *results_dir*, oldest first.

    The driver names its output dir ``${RESULTS_DIR}/<prefix>-$(date -u +%Y%m%dZ%H%M%S)``
    (the ``OUTPUT_DIR=`` assignment in ``nv-elbencho-sweep.sh``, :692 as of b3ab646) and
    prints it nowhere machine-readable, so the only way
    to learn it is to look. Give each ACV run its own ``RESULTS_DIR`` and this is
    unambiguous; share one and it is a race.
    """
    try:
        names = os.listdir(results_dir)
    except OSError:
        return []
    pattern = workload(kind).dir_re
    return [os.path.join(results_dir, n) for n in sorted(names) if pattern.match(n)]


def read_statuses(sweep_dir: str) -> dict[str, str]:
    """Map execution id -> status sentinel, read from ``executions/<NNNN>.status``."""
    ex = os.path.join(sweep_dir, "executions")
    out: dict[str, str] = {}
    try:
        names = os.listdir(ex)
    except OSError:
        return out
    for name in sorted(names):
        if not name.endswith(".status"):
            continue
        path = os.path.join(ex, name)
        try:
            with open(path, encoding="utf-8") as fh:
                out[name[: -len(".status")]] = fh.read().strip()
        except OSError:
            # A sentinel mid-rename is normal: _atomic_write_sentinel writes then renames.
            continue
    return out


def read_coordinator_jobid(sweep_dir: str) -> str | None:
    """The durable Slurm handle written by the coordinator once sbatch returns.

    ``lib/env_functions.sh:2101`` writes it via ``_atomic_write_sentinel``, which is what
    makes it safe to read from another process while the sweep runs. This is the whole
    reason a detached submit can be re-attached from a fresh login session.
    """
    path = os.path.join(sweep_dir, "executions", ".coordinator.jobid")
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value if value.isdigit() else None


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run *cmd*, turning "the tool is not installed" into an exit code rather than a raise.

    ``squeue``/``sacct`` being absent is an ordinary state, not a bug: these steps are meant
    to be re-run from wherever the operator is, including a laptop with no Slurm client. An
    uncaught ``FileNotFoundError`` there would print a traceback to stderr and leave stdout
    empty, and ACV reads an unparseable stdout as *no output at all* -- so the step would
    look like it produced nothing rather than like it failed.
    """
    log(f"+ {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  ({cmd[0]}: {exc})")
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))


# --------------------------------------------------------------------------
# Prepared batches.
#
# A batch is a directory holding `batch-manifest.tsv` plus `executions/<id>.status`
# sentinels. It is DRAFT until `--start` writes `batch-sealed.sha256`, after which
# its execution set is frozen -- appends are rejected and the saved group settings,
# not today's env.sh, drive execution.
#
# `--status` emits bare `KEY=VALUE` lines (`BATCH=`, `STATE=`, `EXECUTIONS_*`,
# `NEXT_ACTION=`). On a sealed kubectl batch with a live attempt it instead
# delegates to kubectl_lifecycle_operation, which emits `STORAGE_SCALE_TEST_KUBECTL_*`
# keys. parse_batch_status normalises both into one dict.
# --------------------------------------------------------------------------

BATCH_MANIFEST = "batch-manifest.tsv"
BATCH_SEALED = "batch-sealed.sha256"

# ALL FOUR markers upstream tests, in upstream's order (`elbencho_batch_is_batch`,
# lib/_batch_functions.sh:23-29). Its comment says why the set matters: "Exact batch
# markers keep a damaged batch on the validating lifecycle path; a missing manifest must
# never downgrade it to an ordinary results tree." Testing only the manifest -- which this
# did -- means a batch that lost or never wrote one is handled as a plain sweep: poll takes
# the sweep-shaped tally instead of asking --status, and collect skips the batch path. The
# wrong code path on a damaged batch is precisely the case the markers exist to catch.
BATCH_MARKERS = (BATCH_MANIFEST, "batch-profile.tsv", BATCH_SEALED, "batch-sealed-environment.sha256")

DRAFT, SEALED = "DRAFT", "SEALED"

# NEXT_ACTION values that mean "the batch is finished, stop polling".
BATCH_NEXT_TERMINAL = ("NONE",)
# STATE values that mean terminal, regardless of NEXT_ACTION. These are the PVC run
# lifecycle's immutable terminal outcomes (docs/KUBERNETES_ELBENCHO_LIFECYCLE.md:112) --
# CANCELLED belongs here with the other two: a cancelled sweep is finished, and omitting
# it made every poll wait out its full deadline on work that had already stopped.
BATCH_STATE_TERMINAL = ("SUCCESS", "FAILED", "CANCELLED")

# Local attempt state meaning "cancellation is in flight but not finalised". Upstream is
# explicit that status must NOT report ordinary running state here, and must direct the
# operator to retry --cancel (KUBERNETES_ELBENCHO_LIFECYCLE.md:171, T-09). Repeating
# --cancel resumes the exact deletion and finalisation (T-08).
CANCEL_REQUESTED = "CANCEL_REQUESTED"

# RESULT_COLLECTION values. Until this reads COMPLETE the attempt still owns cluster-side
# state: --collect is what releases the PVC Lease and deletes the owned Job/DaemonSet
# (docs/DESIGN.md:349-352). Anything else means resources are still held.
COLLECTION_COMPLETE = "COMPLETE"

_BATCH_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def is_batch_dir(path: str) -> bool:
    """True when ``path`` is a prepared batch root.

    Mirrors upstream exactly: ANY of :data:`BATCH_MARKERS` present -- as a file OR a
    symlink -- makes it a batch. ``os.path.exists`` is deliberately not used alone here;
    upstream's ``[[ ! -e ... && ! -L ... ]]`` treats a DANGLING symlink as present, and
    ``os.path.exists`` follows symlinks and would answer False for one. A dangling marker
    is a damaged batch, which is the case that most needs the batch path.
    """
    return any(os.path.exists(os.path.join(path, m)) or os.path.islink(os.path.join(path, m)) for m in BATCH_MARKERS)


def batch_is_sealed(path: str) -> bool:
    """True once ``--start`` has frozen the batch's execution set."""
    return os.path.exists(os.path.join(path, BATCH_SEALED))


# --------------------------------------------------------------------------
# Placement: WHICH nodes a sweep actually measured.
#
# On kubectl the worker pool and the per-execution selection are different sets, and
# upstream records both:
#
#   kubernetes/attempts/<id>/nodes.tsv              node, node UID, arch -- the ELIGIBLE pool
#   kubernetes/attempts/<id>/worker-endpoints.tsv   the worker pods actually stood up
#   executions/<NNNN>.workers.tsv                   node, pod, pod UID, endpoint -- SELECTED
#
# They diverge whenever the pool is larger than `--nodes`, and the choice between them is
# RANDOM unless ORDER_NODES is set: `_coordinator_select_workers` picks positions with
# `shuf` and `kubectl_choose_coordinator_node` with `RANDOM %`
# (_nv-elbencho-kubectl-coordinator.sh:449, _nv-elbencho-kubectl-functions.sh:2690).
#
# This matters for a validation harness specifically. A throughput figure with no record
# of where it was measured cannot be compared across runs: a regression may be a different
# node rather than a changed filesystem. A sweep can stand up workers on both eligible
# nodes and measure only one of them in every execution -- half the pool provisioned,
# never exercised, and nothing says so.

# --------------------------------------------------------------------------
# Failure evidence: WHY a sweep failed, and whether that is a storage finding at all.
#
# Upstream writes three structured artefacts on the failure paths and publishes all of
# them into the local tree at --collect (they are in the ledger allow-list,
# _nv-elbencho-kubectl-functions.sh:5522):
#
#   run-summary.tsv       schema-versioned ledger: run_status, per-state counts,
#                         `failed_execution`, and `exit_code`
#                         (_coordinator_write_summary, coordinator:586-616)
#   startup-error.txt     a message when the coordinator failed BEFORE or OUTSIDE any
#                         cell -- startup failure, or "coordinator interrupted outside a
#                         failed cell" (coordinator:709-715 and :1101)
#   coordinator-loss.tsv  schema 1: observed_status, execution_observed_status,
#                         recovered_status, exit_code -- written when the coordinator
#                         itself was LOST and the run was recovered (coordinator:1259-1270)
#
# Why this matters for a VALIDATION harness rather than a benchmark: these distinguish a
# failure OF the storage from a failure of the thing measuring it. A FAILED cell is a
# result. A lost coordinator at exit code 143 is SIGTERM -- a preempted or evicted pod --
# and the measurement is void, not negative. Reporting the second as "storage validation
# failed" would be a false finding, and acting on it would send someone to debug a
# filesystem that was never the problem.

# --------------------------------------------------------------------------
# The lifecycle lock.
#
# Every kubectl lifecycle operation -- status, collect, cancel, resume -- takes an
# exclusive `flock` on `<sweep>/kubernetes/lifecycle.lock` before it does anything
# (`kubectl_local_lock_acquire`, _nv-elbencho-kubectl-functions.sh:285-306). The flock is
# `-n`: NON-BLOCKING. A contended operation does not wait its turn, it exits 1 immediately
# with a message on stderr.
#
# From ACV's side that is indistinguishable, by exit code alone, from the driver failing.
# And ACV's existing reading of "driver exited non-zero" is "cluster access was lost
# (expired credential, or a changed kubeconfig/context)". Holding the lock and polling
# produces exactly that message, sending an operator to re-authenticate over a local file
# lock when another operation simply holds it.
#
# This is not a rare state. The provider's own configs run poll -> collect -> resume
# against one results directory, and a second isvctl invocation, or a human polling from
# another terminal while a collect runs, contends immediately.
#
# The message is a tested upstream contract, not incidental text: upstream's own
# tests/test_elbencho_kubectl_lifecycle_contract.py:71 greps for this exact string.
LOCK_CONTENDED_MARKER = "another Kubernetes lifecycle operation is active"


def lock_contended(proc) -> bool:
    """True when a driver call failed only because another operation holds the lock."""
    return proc.returncode != 0 and LOCK_CONTENDED_MARKER in (proc.stderr or "")


def lock_contention_error(sweep_dir: str, operation: str) -> str:
    """Why this failed, and the one thing that actually resolves it.

    Deliberately says what it is NOT: the standing advice for a non-zero driver exit is to
    restore cluster access, and following that here wastes time on credentials that were
    never the problem.
    """
    return (
        f"cannot {operation}: another Kubernetes lifecycle operation is already running "
        f"against {sweep_dir}. The driver takes a non-blocking exclusive lock on "
        "kubernetes/lifecycle.lock and exits rather than queueing. This is NOT a "
        "credential or connectivity problem and re-authenticating will not help -- the "
        "sweep itself is unaffected. Wait for the other operation (a collect, cancel, "
        "resume or status against the same results directory, possibly from another "
        "terminal or a second isvctl run) to finish, then retry"
    )


RUN_SUMMARY = "run-summary.tsv"
STARTUP_ERROR = "startup-error.txt"
COORDINATOR_LOSS = "coordinator-loss.tsv"

# What kind of thing went wrong. Only `benchmark` is a statement about storage.
FAILURE_NONE = "none"
FAILURE_BENCHMARK = "benchmark"
FAILURE_COORDINATOR_LOSS = "coordinator_loss"
FAILURE_TERMINATED = "terminated"
FAILURE_STARTUP = "startup"
FAILURE_UNKNOWN = "unknown"
INFRASTRUCTURE_FAILURES = (FAILURE_COORDINATOR_LOSS, FAILURE_TERMINATED, FAILURE_STARTUP)

# SIGTERM. The exit code a preempted or evicted pod leaves behind, and upstream's own
# recovery value for a lost coordinator (coordinator:1257).
SIGTERM_EXIT = 143


def read_kv_tsv(path: str) -> dict[str, str]:
    """A two-column `key<TAB>value` file as a dict. Missing or unreadable -> {}."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.rstrip("\n").partition("\t")
                if sep and key:
                    out[key] = value
    except OSError:
        return {}
    return out


def _as_int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def read_failure_evidence(sweep_dir: str) -> dict:
    """Upstream's own account of why a sweep failed, and what kind of failure it was.

    Everything here is read from files already on disk after a collect; nothing talks to
    the cluster, so a sweep can be diagnosed from a fresh session long after it ended.

    The classification order is deliberate and is NOT the order the files appear in. A
    lost coordinator also leaves FAILED cells behind, so testing cells first would label
    every eviction a benchmark failure -- the exact misreading this exists to prevent.
    """
    summary = read_kv_tsv(os.path.join(sweep_dir, RUN_SUMMARY))
    loss = read_kv_tsv(os.path.join(sweep_dir, COORDINATOR_LOSS))
    startup = ""
    try:
        with open(os.path.join(sweep_dir, STARTUP_ERROR), encoding="utf-8") as fh:
            startup = fh.read().strip()
    except OSError:
        startup = ""

    evidence: dict = {
        "run_status": summary.get("run_status"),
        "attempt_id": summary.get("attempt_id") or loss.get("attempt_id"),
        "exit_code": _as_int(loss.get("exit_code") or summary.get("exit_code")),
        "failed_execution": summary.get("failed_execution") or loss.get("execution"),
        "startup_error": startup or None,
        "coordinator_loss": loss or None,
        "evidence_sources": sorted(
            name
            for name, present in (
                (RUN_SUMMARY, bool(summary)),
                (STARTUP_ERROR, bool(startup)),
                (COORDINATOR_LOSS, bool(loss)),
            )
            if present
        ),
    }

    failed_count = _as_int(summary.get("failed")) or 0
    status = summary.get("run_status")
    exit_code = evidence["exit_code"]
    if loss:
        kind = FAILURE_COORDINATOR_LOSS
    elif startup:
        kind = FAILURE_STARTUP
    elif status == FAILED and exit_code == SIGTERM_EXIT:
        # Exit 143 is SIGTERM: the process was KILLED, which a benchmark failing for
        # storage reasons does not do.
        #
        # The original version keyed infrastructure failures on coordinator-loss.tsv
        # alone. That file is written only on upstream's RECOVERY path, when a coordinator
        # detects it lost a prior incarnation and reconstructs the outcome
        # (coordinator:1259). Kill the coordinator outright and no such file appears: the
        # cell it was running is simply marked FAILED with exit code 143, which is
        # indistinguishable from a storage fault by cell count alone. The live kill was
        # therefore reported as a benchmark failure and `is_storage_finding: True` -- the
        # exact false finding this code exists to prevent.
        #
        # A deliberate cancel also SIGTERMs, but upstream records that as run_status
        # CANCELLED, not FAILED, so FAILED+143 is specific to an unplanned termination.
        kind = FAILURE_TERMINATED
    elif failed_count or status == FAILED:
        kind = FAILURE_BENCHMARK
    elif status in (SUCCESS, "CANCELLED") or not summary:
        kind = FAILURE_NONE
    else:
        kind = FAILURE_UNKNOWN
    evidence["failure_kind"] = kind
    # The single field a caller needs to decide whether a failure says anything about
    # storage. Kept explicit rather than left to each caller to re-derive from `kind`.
    evidence["is_storage_finding"] = kind == FAILURE_BENCHMARK
    return evidence


def describe_failure(evidence: dict) -> str:
    """One sentence naming what went wrong and whether it concerns the storage."""
    kind = evidence.get("failure_kind")
    code = evidence.get("exit_code")
    if kind == FAILURE_COORDINATOR_LOSS:
        loss = evidence.get("coordinator_loss") or {}
        signal = (
            " (SIGTERM -- the pod was terminated, typically preemption or node eviction)"
            if code == SIGTERM_EXIT
            else ""
        )
        return (
            f"the COORDINATOR was lost, not the benchmark: observed "
            f"{loss.get('observed_status', '?')}, recovered as "
            f"{loss.get('recovered_status', '?')}, exit code {code}{signal}. This is an "
            "infrastructure event and says NOTHING about the storage -- the measurement "
            "is void, not negative. Re-run it rather than filing a storage defect"
        )
    if kind == FAILURE_TERMINATED:
        cell = evidence.get("failed_execution")
        where = f" while running execution {cell}" if cell and cell != "none" else ""
        return (
            f"the run was TERMINATED{where}: exit code {SIGTERM_EXIT} is SIGTERM, so the "
            "process was killed rather than failing on its own. A benchmark failing for "
            "storage reasons does not exit 143. If this was not a deliberate cancel, "
            "suspect pod preemption, node eviction or an OOM kill. The measurement is "
            "void, not negative -- re-run it rather than filing a storage defect"
        )
    if kind == FAILURE_STARTUP:
        return (
            f"the coordinator failed outside any benchmark cell: "
            f"{evidence.get('startup_error')}. Nothing was measured, so this is not a "
            "storage result; it is an environment or dispatch problem"
        )
    if kind == FAILURE_BENCHMARK:
        cell = evidence.get("failed_execution")
        where = f" at execution {cell}" if cell and cell != "none" else ""
        tail = f" (exit code {code})" if code is not None else ""
        return f"a benchmark cell FAILED{where}{tail}. This IS a result about the storage, not an infrastructure event"
    if kind == FAILURE_UNKNOWN:
        return (
            "the sweep failed and upstream left no diagnosis: no "
            f"{COORDINATOR_LOSS}, no {STARTUP_ERROR}, and {RUN_SUMMARY} names no "
            "failed execution"
        )
    return "no failure was recorded"


ATTEMPTS_SUBDIR = os.path.join("kubernetes", "attempts")
CURRENT_ATTEMPT = os.path.join("kubernetes", "current-attempt")
NODES_TSV = "nodes.tsv"
WORKER_ENDPOINTS_TSV = "worker-endpoints.tsv"


def _read_tsv_column(path: str, column: int = 0) -> list[str]:
    """One column of a TSV, skipping blanks. Missing file -> []."""
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [line.rstrip("\n").split("\t") for line in fh if line.strip()]
    except OSError:
        return []
    return [r[column] for r in rows if len(r) > column and r[column]]


def current_attempt_dir(sweep_dir: str) -> str | None:
    """The attempt directory `--collect` last published, or None.

    Read rather than globbed: a resumed sweep holds several attempts, and the placement of
    the one that produced the current results is the only one worth reporting.
    """
    try:
        with open(os.path.join(sweep_dir, CURRENT_ATTEMPT), encoding="utf-8") as fh:
            attempt = fh.read().strip()
    except OSError:
        return None
    if not attempt:
        return None
    path = os.path.join(sweep_dir, ATTEMPTS_SUBDIR, attempt)
    return path if os.path.isdir(path) else None


def read_placement(sweep_dir: str) -> dict:
    """Which nodes were eligible, which had workers, and which actually ran executions.

    Everything is derived from files already on disk after a collect; nothing here talks
    to the cluster, so it works from a fresh session long after the sweep ended.
    """
    executions_dir = os.path.join(sweep_dir, "executions")
    used: list[str] = []
    pods: list[str] = []
    try:
        names = sorted(os.listdir(executions_dir))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".workers.tsv"):
            continue
        path = os.path.join(executions_dir, name)
        used.extend(_read_tsv_column(path, 0))
        pods.extend(_read_tsv_column(path, 1))

    attempt = current_attempt_dir(sweep_dir)
    available: list[str] = []
    workers: list[str] = []
    if attempt:
        available = _read_tsv_column(os.path.join(attempt, NODES_TSV), 0)
        workers = _read_tsv_column(os.path.join(attempt, WORKER_ENDPOINTS_TSV), 0)

    nodes_used = sorted(set(used))
    nodes_available = sorted(set(available))

    # Whether the choice of nodes was deterministic, read back from the snapshot the run
    # actually used rather than from the current environment -- a sweep collected days
    # later from a different shell must report what IT ran under, not what is set now.
    # `ORDER_NODES_ENABLED` is upstream's derived flag (empty or 1), which is what both
    # kubectl paths actually test; `ORDER_NODES` itself accepts `yes`/`off`/`1` and
    # friends, so reading the raw value would mean reimplementing upstream's truthiness.
    ordered = None
    try:
        with open(os.path.join(sweep_dir, "env_used.sh"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("export ORDER_NODES_ENABLED="):
                    ordered = line.split("=", 1)[1].strip().strip("'\"") == "1"
                    break
    except OSError:
        ordered = None
    placement: dict = {
        "attempt_dir": attempt,
        "nodes_available": nodes_available,
        "nodes_available_count": len(nodes_available),
        "nodes_with_workers_count": len(set(workers)),
        "nodes_used": nodes_used,
        "nodes_used_count": len(nodes_used),
        "pods_used": sorted(set(pods)),
        "executions_with_placement": sum(1 for n in names if n.endswith(".workers.tsv")),
        # What makes node_coverage interpretable: a coverage of 0.5 under random
        # selection may be a different half next run; under ordered selection it is the
        # same half every time.
        "worker_selection": ("deterministic" if ordered else "random" if ordered is False else "unknown"),
    }
    # Guard against a pool of 0, which would make any ratio either a crash or a
    # meaningless 1.0. `None` is honest where a fraction would not be.
    if nodes_available:
        placement["node_coverage"] = round(len(nodes_used) / len(nodes_available), 4)
    else:
        placement["node_coverage"] = None
    return placement


def parse_batch_status(text: str) -> dict[str, str]:
    """Parse ``--status`` output into a dict, normalising the key dialects.

    What ``--status`` ACTUALLY emits, verified against upstream rather than assumed: both
    emitters print BARE, unprefixed keys. ``kubectl_emit_status_view``
    (_nv-elbencho-kubectl-functions.sh:722-729) and ``_elbencho_batch_status``
    (lib/_batch_functions.sh:526-530) use the identical names -- ``BATCH=``, ``STATE=``,
    ``EXECUTIONS_*``, ``PROGRESS_SOURCE=``, ``NEXT_ACTION=``.

    Crucially they do NOT co-occur: **only one progress view is emitted** per ``--status``
    (docs/KUBERNETES_ELBENCHO_LIFECYCLE.md:188), and ``EXECUTION_SCOPE`` says which one --
    ``CURRENT_ATTEMPT`` for the kubectl attempt view, ``BATCH`` for the batch ledger. So
    there is no key collision to arbitrate; an earlier version of this docstring claimed a
    sealed kubectl batch reports both ``BATCH_STATE`` and ``KUBECTL_STATE`` in one block,
    and it does not. The ``STORAGE_SCALE_TEST_`` prefix appears on ``--status`` output
    nowhere at all; it is used only by ``--batch``/``--append`` prepare output
    (``STORAGE_SCALE_TEST_BATCH_RESULTS``/``_GROUP``/``_EXECUTIONS``,
    lib/_batch_functions.sh:393).

    The prefix handling below is therefore forward-defence, not a live code path: it keeps
    the namespaces apart IF upstream ever starts emitting them, rather than silently
    letting a prefixed key overwrite a bare one.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = _BATCH_KV_RE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key.startswith("STORAGE_SCALE_TEST_KUBECTL_"):
            key = "KUBECTL_" + key[len("STORAGE_SCALE_TEST_KUBECTL_") :]
        elif key.startswith("STORAGE_SCALE_TEST_BATCH_"):
            # Its OWN namespace, for the same reason KUBECTL_ has one. A sealed kubectl
            # batch delegates to the kubectl lifecycle view, which prints its own BARE
            # `STATE=`/`NEXT_ACTION=` AFTER these -- so stripping the prefix here let the
            # delegated display value (e.g. PREPARING/WAIT) overwrite the batch's real
            # SUCCESS/NONE, and a finished batch polled until its deadline. Last-write-wins
            # over a shared key is not a namespace.
            key = "BATCH_" + key[len("STORAGE_SCALE_TEST_BATCH_") :]
        elif key.startswith("STORAGE_SCALE_TEST_"):
            key = key[len("STORAGE_SCALE_TEST_") :]
        out[key] = value
    return out


def batch_value(status: dict[str, str], name: str) -> str | None:
    """Read a lifecycle key, preferring the batch namespace over a bare one.

    A plain sweep emits bare keys; a sealed batch emits ``BATCH_``-prefixed ones and may
    ALSO carry bare keys from the kubectl view it delegates to. When both exist the batch
    value is the one that describes the thing being polled.
    """
    return status.get("BATCH_" + name, status.get(name))


def batch_is_terminal(status: dict[str, str]) -> bool:
    """True when a parsed ``--status`` says there is nothing left to wait for."""
    if batch_value(status, "STATE") in BATCH_STATE_TERMINAL:
        return True
    return batch_value(status, "NEXT_ACTION") in BATCH_NEXT_TERMINAL
