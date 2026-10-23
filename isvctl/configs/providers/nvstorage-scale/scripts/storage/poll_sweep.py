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

"""Report how far a detached sweep has got, without attaching to it.

Reads only durable on-disk state, so it works from a session that never saw the submit ---
which is the entire point of the async design. ``executions/<NNNN>.status`` is written with
``_atomic_write_sentinel`` (write + rename), so a concurrent read is safe.

Status semantics worth stating plainly, because they are not symmetric:

* **Sentinels are authoritative for what finished.** SUCCESS/FAILED are written by the
  coordinator after the execution ends.
* **Slurm is authoritative for whether anything is still going to happen.** If every
  execution is PENDING and the coordinator job is gone from both ``squeue`` and ``sacct``'s
  running states, the sweep is dead, not queued --- and nothing on disk says so. That
  combination is the one real failure mode this step exists to catch.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import (
    ALL_SUBSTRATES,
    ALL_WORKLOADS,
    BATCH_STATE_TERMINAL,
    DRAFT,
    FAILED,
    KUBECTL,
    PENDING,
    RUNNING,
    SLURM,
    SSH,
    SUCCESS,
    WORKLOAD_IO,
    batch_is_terminal,
    batch_value,
    emit,
    fail,
    is_batch_dir,
    lock_contended,
    lock_contention_error,
    log,
    parse_batch_status,
    read_coordinator_jobid,
    read_statuses,
    run,
    workload,
)

# sacct states that mean the allocation is finished, one way or another.
_TERMINAL_SLURM = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
    "REVOKED",
}


# Jinja2 renders a JSON null as the literal "None", and _render_args only drops a rendered
# arg when it is *empty*. So `{{ steps.submit_sweep.coordinator_jobid }}` arrives here as the
# four-character string "None" whenever submit could not find a job id -- which is exactly the
# case where we must fall back to reading the handle off disk, not run `squeue --jobs None`.
_NOT_A_JOBID = {"", "none", "null", "nil", "~"}


def clean_jobid(raw: str) -> str | None:
    value = raw.strip()
    if value.lower() in _NOT_A_JOBID or not value.isdigit():
        return None
    return value


def slurm_job_state(jobid: str) -> str | None:
    """Best-effort coordinator state. ``None`` means Slurm could not be reached at all.

    squeue first (cheap, and the only source while the job is live), then sacct (the only
    source once it has been purged from the queue). Distinguishing "Slurm says gone" from
    "I could not ask Slurm" matters: the first is terminal, the second must not be reported
    as one, or a working sweep reads as dead the moment the access session lapses.
    """
    proc = run(["squeue", "--noheader", "--jobs", jobid, "--format=%T"])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().split()[0]
    proc = run(["sacct", "-j", jobid, "--noheader", "--parsable2", "--format=State"])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().splitlines()[0].split("|")[0].strip()
    if proc.returncode != 0:
        return None
    return "UNKNOWN"


def ssh_dispatcher_alive(sweep_dir: str) -> bool | None:
    """Is the SSH dispatcher still running? ``None`` means "cannot tell".

    SSH dispatch has no scheduler to ask and writes no ``.coordinator.jobid`` -- it loops over
    executions on the launching host. Without some liveness signal, a dead dispatcher on SSH is
    indistinguishable from a slow one, and ``abandoned`` can never be detected. So
    ``submit_sweep`` records its launcher PID as a sidecar and this checks it.

    This proxy is **weaker than the Slurm one** in two ways, and the caller must not treat it
    as equivalent:

    * A PID is only meaningful **on the host that launched it**. Polling from a different
      machine would read some unrelated process, so an unknown PID yields ``None``, never
      ``False``.
    * PIDs are recycled. A false *alive* is possible; a false *dead* is not, which is the
      safer direction for a check that would otherwise declare a healthy sweep abandoned.
    """
    pid_file = os.path.join(sweep_dir, "executions", ".acv-launcher.pid")
    try:
        with open(pid_file, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # signal 0: existence check only, delivers nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return None
    return True


def tally(sweep_dir: str) -> dict:
    statuses = read_statuses(sweep_dir)
    counts = {PENDING: 0, RUNNING: 0, SUCCESS: 0, FAILED: 0}
    other = 0
    for value in statuses.values():
        if value in counts:
            counts[value] += 1
        else:
            other += 1
    return {
        "total": len(statuses),
        "pending": counts[PENDING],
        "running": counts[RUNNING],
        "succeeded": counts[SUCCESS],
        "failed": counts[FAILED],
        "unrecognised": other,
    }


# The count keys upstream publishes. BARE, in both emitters: `kubectl_emit_status_view`
# (_nv-elbencho-kubectl-functions.sh:722-729) and `_elbencho_batch_status`
# (lib/_batch_functions.sh:526-530) print the identical `EXECUTIONS_*` names with no
# namespace prefix at all. Only ONE view is ever emitted for a given --status
# (KUBERNETES_ELBENCHO_LIFECYCLE.md:188), and `EXECUTION_SCOPE` names which one it was:
# `CURRENT_ATTEMPT` for the kubectl attempt view, `BATCH` for the batch ledger. That is
# upstream's own discriminator -- integration-tests/lib/filesystem_integration.py:5384
# tests exactly this field for exactly this purpose.
_STATUS_COUNTS = (
    "EXECUTIONS_TOTAL",
    "EXECUTIONS_PENDING",
    "EXECUTIONS_RUNNING",
    "EXECUTIONS_SUCCEEDED",
    "EXECUTIONS_FAILED",
)


def status_view(parsed: dict) -> dict:
    """The progress view fields, normalised, from either emitter.

    Shared deliberately. A plain sweep and a sealed kubectl batch produce the SAME keys
    from the same code path upstream, and reading them in two places is how the batch path
    came to be a round behind the plain one: it kept inferring count trustworthiness from
    ``NEXT_ACTION`` long after the plain path started reading the published
    ``PROGRESS_SOURCE``, and it never learned about lock contention at all.
    """
    out: dict = {}
    if parsed.get("PROGRESS_SOURCE"):
        out["progress_source"] = parsed["PROGRESS_SOURCE"]
    if parsed.get("EXECUTION_SCOPE"):
        out["execution_scope"] = parsed["EXECUTION_SCOPE"]
    # Parsed as INTEGERS. `--status` emits text, and a string survives into the payload
    # looking like a number while silently failing every FieldValueCheck min/max
    # comparison -- the check neither passes nor errors usefully, it just never matches.
    for k in _STATUS_COUNTS:
        if k not in parsed:
            continue
        raw = parsed[k]
        try:
            out["kubectl_" + k.lower()] = int(raw)
        except (TypeError, ValueError):
            # Keep the unparsable value visible rather than dropping it; a format change
            # upstream should be legible here, not an absent key.
            out["kubectl_" + k.lower() + "_raw"] = raw
    return out


def kubectl_status(base: str, sweep_dir: str, timeout: int, kind: str = WORKLOAD_IO) -> dict:
    """Ask the driver for the authoritative state of a plain (non-batch) kubectl sweep.

    This MUST parse, not merely attach stdout. On kubectl the per-cell ``<NNNN>.status``
    sentinels are not published into the local tree until ``--collect`` runs, so
    :func:`tally` reads ``PENDING`` for cells the cluster has already finished, and
    :func:`assess` derives ``done`` from that tally. Attaching the driver's output without
    reading it therefore left ``--wait`` blocking on a sweep that was already done.

    The driver reports ``STATE=SUCCESS`` / ``NEXT_ACTION=COLLECT`` while the tally still
    says pending, so without this the step runs to its full ``--wait`` deadline.

    The driver emits BARE keys here (``STATE=``, ``EXECUTIONS_*``, ``NEXT_ACTION=``), the
    same shape :func:`parse_batch_status` already normalises, so it is reused.
    """
    driver = workload(kind).driver_path(base)
    proc = run(["bash", driver, "--status", sweep_dir], timeout=timeout)
    sys.stderr.write(proc.stderr)
    # Set on EVERY path, including the failure ones. An absent key is not the same as
    # False to a FieldExistsCheck: the validation that asserts "this run was not
    # serialised behind another operation" would report a missing field on a genuine
    # driver failure, hiding the real diagnosis behind a bookkeeping complaint.
    out: dict = {
        "native_status_exit_code": proc.returncode,
        "native_status_stdout": proc.stdout,
        "lock_contended": False,
    }
    if proc.returncode != 0:
        # Contention is a DIFFERENT thing from a failed query, and the difference decides
        # what an operator does next. The lock is non-blocking, so another lifecycle
        # operation against this sweep exits us immediately -- a transient, local,
        # self-resolving condition, not lost cluster access.
        if lock_contended(proc):
            out["lock_contended"] = True
            return out
        # A failed query is not evidence the sweep failed; say so and let tally stand.
        out["kubectl_status_error"] = f"--status exited {proc.returncode}"
        return out
    parsed = parse_batch_status(proc.stdout)
    if parsed.get("STATE"):
        out["kubectl_execution_state"] = parsed["STATE"]
    if parsed.get("NEXT_ACTION"):
        out["kubectl_next_action"] = parsed["NEXT_ACTION"]
    # PROGRESS_SOURCE is upstream STATING where its counts came from, and it is the whole
    # reason this block exists. `PVC` means they were read live from the cluster this
    # moment; `LOCAL` means they came from the published manifest after a collect
    # (kubectl_emit_status_view, _nv-elbencho-kubectl-functions.sh:695-728, and the three
    # call sites at :5239 LOCAL/COMPLETE, :5265 LOCAL/CLEANUP_PENDING, :5456 PVC/PENDING).
    # ACV used to INFER trustworthiness from `NEXT_ACTION != COLLECT`; reading the
    # published fact replaces a heuristic with an answer.
    out.update(status_view(parsed))
    return out


# The count keys upstream publishes, mapped onto the payload names ACV reports.
_DRIVER_COUNTS = {
    "total": "kubectl_executions_total",
    "pending": "kubectl_executions_pending",
    "running": "kubectl_executions_running",
    "succeeded": "kubectl_executions_succeeded",
    "failed": "kubectl_executions_failed",
}


def adopt_driver_counts(payload: dict, kstatus: dict) -> bool:
    """Replace the local tally with the driver's counts when the driver read the cluster.

    On kubectl the local ``<NNNN>.status`` sentinels stay PENDING until ``--collect``
    publishes them, so :func:`tally` reports 0 succeeded for the entire life of a running
    sweep: one that is 1/8, then 6/8, then 8/8 complete reads ``succeeded: 0, pending: 8``
    at every probe, while ``--status`` returns the true counts from the PVC each time.

    The local tally is not discarded -- it moves to ``local_*`` so a disagreement between
    what the cluster says and what has been published locally stays visible. That gap is
    expected before a collect and is itself diagnostic.
    """
    if kstatus.get("progress_source") != "PVC":
        return False
    counts = {name: kstatus[key] for name, key in _DRIVER_COUNTS.items() if isinstance(kstatus.get(key), int)}
    # All or nothing: a partial adoption would mix cluster and local numbers into a tally
    # that adds up to nothing coherent.
    if len(counts) != len(_DRIVER_COUNTS):
        return False
    for name in _DRIVER_COUNTS:
        payload["local_" + name] = payload.get(name)
        payload[name] = counts[name]
    return True


def batch_status(base: str, root: str, timeout: int, kind: str = WORKLOAD_IO) -> dict:
    """Ask the driver for a prepared batch's lifecycle state.

    A batch root carries the same ``executions/<id>.status`` sentinels a plain sweep
    does, so ``tally`` already counts it correctly. What tally cannot see is whether
    the batch is DRAFT or SEALED and whether a dispatcher still holds it -- there is
    no Slurm job or SSH pid to ask. ``--status`` is the only authority for that.

    A SEALED kubectl batch takes the same non-blocking ``flock`` every other kubectl
    lifecycle verb takes, so this call is contendable exactly like :func:`kubectl_status`
    -- which is why ``lock_contended`` is seeded on every return path here too.
    """
    driver = workload(kind).driver_path(base)
    result = run(["bash", driver, "--status", root], timeout=timeout)
    # Seeded on EVERY path, for the same reason as kubectl_status: an absent key is not
    # False, and the qos_throughput gate requires `lock_contended is False` from poll_sweep
    # for batch and plain runs alike.
    out: dict = {"batch": True, "lock_contended": False}
    if result.returncode != 0:
        if lock_contended(result):
            out["lock_contended"] = True
            return out
        out["batch_status_error"] = (result.stderr or result.stdout).strip()[:300] or f"rc={result.returncode}"
        return out
    parsed = parse_batch_status(result.stdout)
    out.update(
        {
            "batch_state": parsed.get("BATCH"),
            "batch_execution_state": batch_value(parsed, "STATE"),
            "batch_next_action": batch_value(parsed, "NEXT_ACTION"),
            "batch_done": batch_is_terminal(parsed),
        }
    )
    # The same progress view a plain sweep publishes. A sealed kubectl batch with a live
    # attempt emits EXECUTION_SCOPE=CURRENT_ATTEMPT and PROGRESS_SOURCE=PVC -- real counts
    # read from the cluster this moment -- while its local sentinels stay PENDING until
    # --collect. Without this the batch path reported the stale local tally and, worse,
    # labelled it authoritative.
    out.update(status_view(parsed))
    if "KUBECTL_STATE" in parsed:
        out["batch_kubectl_state"] = parsed["KUBECTL_STATE"]
    return out


def assess(sweep_dir: str, substrate: str, jobid: str | None, args) -> dict:
    counts = tally(sweep_dir)
    payload: dict = {"sweep_dir": sweep_dir, "substrate": substrate, "coordinator_jobid": jobid, **counts}

    if counts["total"] == 0:
        payload.update(success=False, state="unreified", error=f"no <NNNN>.status files under {sweep_dir}/executions")
        return payload

    job_state = None
    coordinator_gone = False
    if substrate == SLURM and jobid:
        job_state = slurm_job_state(jobid)
        payload["coordinator_state"] = job_state
        coordinator_gone = job_state is not None and job_state.split()[0] in _TERMINAL_SLURM
    elif substrate == SSH:
        alive = ssh_dispatcher_alive(sweep_dir)
        payload["dispatcher_alive"] = alive
        payload["liveness_source"] = "launcher-pid (same host only)" if alive is not None else None
        coordinator_gone = alive is False

    outstanding = counts["pending"] + counts["running"]

    if outstanding == 0:
        payload["state"] = "complete" if counts["failed"] == 0 else "complete_with_failures"
        payload["done"] = True
        payload["success"] = counts["failed"] == 0
    elif coordinator_gone:
        payload["state"] = "abandoned"
        payload["done"] = True
        payload["success"] = False
        gone = f"coordinator job {jobid} is {job_state}" if substrate == SLURM else "the SSH dispatcher process is gone"
        payload["error"] = (
            f"{gone} but {outstanding} execution(s) are still {PENDING}/{RUNNING}; the sweep "
            f"stopped without finishing. Re-dispatch with: nv-elbencho-sweep.sh --resume "
            f"{sweep_dir}"
        )
    else:
        payload["state"] = "running"
        payload["done"] = False
        # Not an error --- but the step must not report success on an unfinished sweep, or a
        # `collect` bound to it will parse a partial result set as a complete one.
        payload["success"] = bool(args.allow_incomplete)
        if not args.allow_incomplete:
            payload["error"] = (
                f"{outstanding} of {counts['total']} execution(s) still outstanding; "
                "pass --allow-incomplete to treat a progress report as a passing step"
            )
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep-dir", required=True, help="steps.submit_sweep.sweep_dir")
    ap.add_argument("--substrate", required=True, choices=ALL_SUBSTRATES)
    ap.add_argument(
        "--coordinator-jobid", default="", help="steps.submit_sweep.coordinator_jobid; re-read from disk if omitted"
    )
    ap.add_argument("--scale-test-base", help="required for --substrate kubectl")
    ap.add_argument(
        "--wait", type=int, default=0, help="seconds to keep polling until the sweep is done (0 = report once)"
    )
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls when --wait")
    ap.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="report progress without failing the step while work is outstanding",
    )
    ap.add_argument("--native-timeout", type=int, default=300)
    ap.add_argument(
        "--workload",
        default=WORKLOAD_IO,
        choices=ALL_WORKLOADS,
        help="which driver owns this sweep dir; must match the submit step. "
        "The lifecycle verbs are identical across workloads, but each "
        "driver refuses a results dir the other one created",
    )
    args = ap.parse_args()

    sweep_dir = os.path.abspath(args.sweep_dir)
    if not os.path.isdir(sweep_dir):
        return fail(f"no such sweep dir: {sweep_dir}")

    # Enforce what --scale-test-base's help has always claimed. Without it the kubectl
    # branch below is skipped entirely, so `done` falls back to tally(), whose per-cell
    # sentinels stay PENDING until --collect runs -- and --wait then blocks for its full
    # deadline on a sweep that already succeeded. A silent degrade into a wait that can
    # never finish is worse than a refusal.
    if args.substrate == KUBECTL and not args.scale_test_base:
        return fail(
            "--scale-test-base is required with --substrate kubectl: without it "
            "poll_sweep cannot ask the driver for the authoritative state, and "
            "would wait on local sentinels that only --collect publishes"
        )

    jobid = clean_jobid(args.coordinator_jobid) or read_coordinator_jobid(sweep_dir)

    deadline = time.time() + args.wait
    while True:
        payload = assess(sweep_dir, args.substrate, jobid, args)
        if is_batch_dir(sweep_dir) and args.scale_test_base:
            status = batch_status(os.path.abspath(args.scale_test_base), sweep_dir, args.native_timeout, args.workload)
            payload.update(status)
            if status.get("lock_contended"):
                # Identical treatment to the plain-sweep path below, and for the identical
                # reason: the holder is another lifecycle operation and will finish, so
                # keep waiting rather than failing. Previously this fell through to
                # assess()'s tally-shaped verdict, which told the operator to pass
                # --allow-incomplete over a local file lock, after waiting out the full
                # --wait deadline for a condition that clears in seconds.
                payload["state"] = "lock-contended"
                payload["done"] = False
                payload["success"] = False
                payload["counts_authoritative"] = False
                payload["error"] = lock_contention_error(sweep_dir, "read batch state")
            else:
                # --status is authoritative for a batch: a DRAFT batch has every
                # execution PENDING and no dispatcher, which the sweep-shaped tally
                # would otherwise read as "running forever".
                if "batch_done" in status:
                    payload["done"] = status["batch_done"]
                # tally() counts a DRAFT batch's 90 PENDING sentinels and concludes
                # "running", but nothing has been dispatched -- the batch has not been
                # sealed yet. Let the lifecycle state name what is actually true.
                if status.get("batch_state") == DRAFT:
                    payload["state"] = "draft"
                elif status.get("batch_execution_state"):
                    payload["state"] = status["batch_execution_state"].lower()
                # Take the driver's counts whenever it read them from the PVC -- the same
                # rule the plain-sweep path uses, now actually applied here. A sealed
                # kubectl batch delegates to the attempt view, which reports live cluster
                # counts with PROGRESS_SOURCE=PVC while the local sentinels stay PENDING
                # until --collect. This branch used to infer authority from
                # `batch_next_action != "COLLECT"`, which is True for the whole life of a
                # RUNNING batch -- so it published `succeeded: 0` and called it
                # authoritative for the entire run.
                adopted = adopt_driver_counts(payload, status)
                payload["counts_authoritative"] = adopted
                if not adopted:
                    payload["counts_note"] = (
                        "counts are the LOCAL sentinels, which stay PENDING on kubectl "
                        "until --collect publishes them; the driver did not report "
                        "PROGRESS_SOURCE=PVC for this query. batch_execution_state is "
                        "authoritative for whether the batch is finished"
                    )
                # ...and `success` must follow the authority too, not the stale tally. On
                # kubectl the per-cell sentinels stay PENDING until --collect runs, so
                # assess() sees every cell outstanding and reports success=False on a batch
                # the driver has already called SUCCESS. The plain-sweep kubectl branch
                # below fixed exactly this; the batch branch did not, so a finished mixed
                # batch failed its poll step while reporting state=success, done=true.
                bstate = status.get("batch_execution_state")
                if bstate in BATCH_STATE_TERMINAL:
                    payload["success"] = bstate == SUCCESS
                    if payload["success"]:
                        payload.pop("error", None)
                    else:
                        payload["error"] = {
                            "CANCELLED": "the batch was CANCELLED; this is a terminal "
                            "outcome, not a stall. Collect it to release "
                            "the cluster",
                            "FAILED": "the driver reports the batch FAILED; collect it to publish whatever did finish",
                        }.get(bstate, f"the batch reached {bstate}")
        # NOT for a batch dir -- but not for the reason this comment used to give. It
        # claimed a sealed kubectl batch's --status carries two namespaces whose bare
        # `STATE=` keys collide on print order. Upstream emits only ONE progress view per
        # --status (KUBERNETES_ELBENCHO_LIFECYCLE.md:188), so there is no collision.
        #
        # The real reason is simply that the batch branch above has already run --status
        # once and interpreted it. Running it a second time here would double every
        # driver call, re-take the lifecycle lock, and let two interpretations of the same
        # output fight over `state`/`success`. The batch branch now does everything this
        # one does -- contention, PROGRESS_SOURCE, count adoption -- via the shared
        # `status_view`, so there is nothing left for a second pass to add.
        if args.substrate == KUBECTL and args.scale_test_base and not is_batch_dir(sweep_dir):
            kstatus = kubectl_status(
                os.path.abspath(args.scale_test_base), sweep_dir, args.native_timeout, args.workload
            )
            payload.update(kstatus)
            # Take the driver's counts whenever it read them from the PVC, INCLUDING while
            # the sweep is still running -- that is the case the local tally gets wrong for
            # the whole duration, and the case an operator is actually watching.
            adopted = adopt_driver_counts(payload, kstatus)
            payload["counts_authoritative"] = adopted
            if not adopted:
                payload["counts_note"] = (
                    "counts are the LOCAL sentinels, which stay PENDING on kubectl until "
                    "--collect publishes them; the driver did not report "
                    "PROGRESS_SOURCE=PVC for this query"
                )
            # The driver is authoritative on kubectl; the local tally is not until
            # --collect publishes the sentinels. Only OVERRIDE on a terminal state --
            # a non-terminal driver state must not mask `abandoned` from assess().
            kstate = kstatus.get("kubectl_execution_state")
            if kstate in BATCH_STATE_TERMINAL:
                payload["state"] = kstate.lower()
                payload["done"] = True
                payload["success"] = kstate == "SUCCESS"
                if kstate == "SUCCESS":
                    payload.pop("error", None)
                else:
                    # REPLACE, never setdefault: assess() has already written a tally-shaped
                    # error ("N of M still outstanding; pass --allow-incomplete"), and on a
                    # terminal non-SUCCESS outcome that advice is actively wrong --
                    # --allow-incomplete cannot rescue a sweep that was cancelled or failed,
                    # and the outstanding count is just the stale local sentinels.
                    detail = {
                        "CANCELLED": "the sweep was CANCELLED; this is a terminal outcome, "
                        "not an unfinished run. Collect it to retrieve whatever "
                        "completed before cancellation and to release the PVC "
                        "Lease",
                        "FAILED": "the driver reports the sweep FAILED; collect it to "
                        "retrieve the cells that did finish",
                    }.get(kstate, f"the driver reports the sweep {kstate}")
                    payload["error"] = detail
                # Authority was already settled above from PROGRESS_SOURCE. The previous
                # version re-derived it here as `NEXT_ACTION != COLLECT`, which is a
                # heuristic standing in for a fact upstream publishes -- and one that said
                # nothing at all while the sweep was running, which is when the counts are
                # most misleading.
            elif kstatus.get("lock_contended"):
                # Transient by construction: the holder is another lifecycle operation,
                # which will finish. Keep waiting rather than failing -- a poll that gave
                # up here would turn a few seconds of overlap into a failed run. Only if
                # it is still contended at the deadline does it become an error, below.
                payload["state"] = "lock-contended"
                payload["done"] = False
                payload["success"] = False
                payload["error"] = lock_contention_error(sweep_dir, "read sweep state")
            elif kstatus.get("kubectl_status_error") or not kstate:
                # We could not read the driver's state. That is NOT evidence the sweep is
                # running -- it is evidence we cannot see. assess() would otherwise report
                # `running` off local sentinels that are PENDING until --collect, and
                # suggest --allow-incomplete, which would turn a blind poll into a pass.
                # Losing cluster access mid-sweep is the expected case here (a short-lived
                # credential outlives neither a long sweep nor a laptop suspend), so say so
                # and name the recovery, rather than guessing.
                payload["state"] = "unknown"
                payload["done"] = False
                payload["success"] = False
                payload["counts_authoritative"] = False
                why = kstatus.get("kubectl_status_error") or "the driver reported no STATE"
                payload["error"] = (
                    f"cannot determine sweep state: {why}. This usually means cluster "
                    "access was lost (expired credential, or a changed kubeconfig/context). "
                    "The sweep is detached and is NOT affected by this; it keeps running on "
                    f"the cluster. Restore access and poll again against {sweep_dir}"
                )
        if payload.get("done") or time.time() >= deadline:
            break
        source = payload.get("progress_source", "local").lower()
        log(
            f"{payload['state']}: {payload['succeeded']}/{payload['total']} done, "
            f"{payload['pending'] + payload['running']} outstanding "
            f"(counts from {source}); sleeping {args.interval}s"
        )
        time.sleep(args.interval)

    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
