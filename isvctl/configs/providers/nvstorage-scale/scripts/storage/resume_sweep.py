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

"""Re-dispatch the executions of a sweep that stopped short.

This is the automated form of what ``poll_sweep`` and ``collect_sweep`` currently tell the
operator to run by hand. It is the recovery path for the ``abandoned`` state -- coordinator
terminal, executions still PENDING/RUNNING.

Three things about ``--resume`` that shape this file, all from reading
the ``resume_dir`` branches of ``nv-elbencho-sweep.sh`` (:321, :417, :579 as of b3ab646):

1. **It blocks exactly like a fresh submit.** The resume branch ends in
   ``dispatch_slurm_executions``/``dispatch_ssh_executions``, the same blocking call a normal
   run makes. So resume is detached the same way submit is -- a resumed 12h sweep is still a
   12h sweep.

2. **``.coordinator.jobid`` already exists from the prior attempt.** Waiting for "a job id"
   would return the *stale* one instantly and report success before Slurm had accepted
   anything. This waits for the id to **change**.

3. **Exit 1 is ambiguous.** ``--resume`` exits 1 both when it *refuses* (the recorded
   coordinator is still live -- ``lib/env_functions.sh:1895``) and when the sweep genuinely
   failed. Those need opposite responses: the first means "leave it alone, it is working",
   the second means "something is broken". We match the refusal text to tell them apart.

It also re-reads the prior ``env_used.sh`` rather than the current ``env.sh``
(``_resume_load_env_used``), so **editing env.sh and resuming changes nothing**.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import (
    ALL_SUBSTRATES,
    ALL_WORKLOADS,
    COLLECTION_COMPLETE,
    KUBECTL,
    SUCCESS,
    WORKLOAD_IO,
    emit,
    fail,
    lock_contended,
    lock_contention_error,
    log,
    parse_batch_status,
    read_coordinator_jobid,
    read_statuses,
    run,
    workload,
)

DRIVER = os.path.join("storage-tests", "fs", "nv-elbencho-sweep.sh")
REFUSAL = "refusing to reset or redispatch"


def outstanding(sweep_dir: str) -> list[str]:
    """Execution ids that ``--resume`` would re-dispatch (anything not SUCCESS)."""
    return [i for i, st in sorted(read_statuses(sweep_dir).items()) if st != SUCCESS]


def launch_detached(base: str, sweep_dir: str, log_path: str, bash: str, driver: str = DRIVER) -> int:
    fh = open(log_path, "ab", buffering=0)
    cmd = [bash, os.path.join(base, driver), "--resume", sweep_dir]
    log(f"+ (detached) {' '.join(cmd)}  >> {log_path}")
    proc = subprocess.Popen(
        cmd, cwd=base, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
    )
    fh.close()
    return proc.pid


def wait_for_new_jobid(sweep_dir: str, previous: str | None, log_path: str, timeout: int) -> tuple[str | None, str]:
    """Wait for a *different* coordinator id, or for the refusal to show up in the log.

    Returns ``(jobid, outcome)`` where outcome is one of ``dispatched`` / ``refused`` /
    ``timeout``. Checking the log each pass is what makes a refusal fast rather than a wait
    for the full timeout -- refusal is the common case when someone resumes a healthy sweep.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = read_coordinator_jobid(sweep_dir)
        if current and current != previous:
            return current, "dispatched"
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                if REFUSAL in fh.read():
                    return None, "refused"
        except OSError:
            pass
        time.sleep(2)
    return None, "timeout"


def lifecycle_status(base: str, sweep_dir: str, wl, timeout: int) -> tuple[int, dict]:
    """One ``--status`` call, parsed. A contended lock is flagged in the parsed dict."""
    proc = run(["bash", wl.driver_path(base), "--status", sweep_dir], timeout=timeout)
    sys.stderr.write(proc.stderr)
    parsed = parse_batch_status(proc.stdout)
    if lock_contended(proc):
        parsed["LOCK_CONTENDED"] = "1"
    return proc.returncode, parsed


def resume_kubectl(base: str, sweep_dir: str, wl, args: argparse.Namespace, outstanding_count: int) -> int:
    """Resume a collected kubectl attempt by creating a new one.

    Two properties of upstream resume shape this, both in
    ``kubectl_resume_collected_sweep`` (``_nv-elbencho-kubectl-functions.sh:6155``):

    1. **It is collection-gated.** Unless the attempt is ``COLLECTED`` the driver refuses
       with ``INVALID_LIFECYCLE_OPERATION`` / ``collection-gate`` (:6176-6186). Shelling
       ``--resume`` without checking first turns an ordering mistake into a page of
       diagnostics that names an internal state machine; checking here names the *step* the
       operator has to run instead.
    2. **It creates a NEW attempt**, it does not re-enter the old one. So the attempt id is
       the observable that proves a resume actually happened -- a driver that exits 0 while
       leaving the id unchanged has not resumed anything, and reporting that as success
       would hide a no-op behind a green step.

    Unlike slurm this is NOT detached: kubectl submission is natively async, so ``--resume``
    returns once the new Job exists.
    """
    rc, before = lifecycle_status(base, sweep_dir, wl, args.jobid_timeout)
    if rc != 0 and before.get("LOCK_CONTENDED"):
        # Not lost access: another lifecycle operation holds the lock. The advice below
        # would send the operator to re-authenticate over a local flock.
        return fail(
            lock_contention_error(sweep_dir, "resume this sweep"),
            sweep_dir=sweep_dir,
            substrate=KUBECTL,
            workload=args.workload,
            lock_contended=True,
            outcome="contended",
            resumed=0,
        )
    if rc != 0:
        return fail(
            f"could not read sweep state before resuming (--status exited {rc}). This "
            "usually means cluster access was lost; the sweep is unaffected. Restore "
            f"access and re-run resume against {sweep_dir}",
            sweep_dir=sweep_dir,
            substrate=KUBECTL,
        )

    previous_attempt = before.get("ATTEMPT")
    collection = before.get("RESULT_COLLECTION")
    if collection != COLLECTION_COMPLETE:
        return fail(
            f"kubectl resume is collection-gated: RESULT_COLLECTION={collection!r} "
            f"(needs {COLLECTION_COMPLETE!r}). Run the collect step against {sweep_dir} "
            "first -- collect is also what releases the PVC Lease, so resuming before it "
            "would contend with the attempt it is trying to replace",
            sweep_dir=sweep_dir,
            substrate=KUBECTL,
            attempt=previous_attempt,
            result_collection=collection,
            resumed=0,
        )

    proc = run(["bash", wl.driver_path(base), "--resume", sweep_dir], cwd=base, timeout=args.timeout)
    sys.stderr.write(proc.stderr)

    rc_after, after = lifecycle_status(base, sweep_dir, wl, args.jobid_timeout)
    attempt = after.get("ATTEMPT") if rc_after == 0 else None
    payload = {
        "sweep_dir": sweep_dir,
        "substrate": KUBECTL,
        "workload": args.workload,
        "resumed": outstanding_count,
        "previous_attempt": previous_attempt,
        "attempt": attempt,
        # Seeded on every path for the same reason as poll_sweep/cancel_sweep; the
        # contention branches below overwrite it.
        "lock_contended": False,
        # The proof. Upstream mints a new attempt id on resume; an unchanged id means
        # nothing was dispatched, whatever the exit code said.
        "new_attempt": bool(attempt and previous_attempt and attempt != previous_attempt),
        "resume_exit_code": proc.returncode,
        "state": after.get("STATE") if rc_after == 0 else None,
        "next_action": after.get("NEXT_ACTION") if rc_after == 0 else None,
    }
    if lock_contended(proc):
        # Nothing was attempted, so this is not a refusal: the driver never got past the
        # lock. Calling it "refused" would imply the sweep was in a state that declined a
        # resume -- a different problem with a different remedy.
        payload["success"] = False
        payload["outcome"] = "contended"
        payload["lock_contended"] = True
        payload["error"] = lock_contention_error(sweep_dir, "resume this sweep")
        return emit(payload)
    if proc.returncode != 0:
        payload["success"] = False
        payload["outcome"] = "refused"
        payload["error"] = (
            f"--resume exited {proc.returncode}; the sweep was not resumed. "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
        return emit(payload)
    if not payload["new_attempt"]:
        payload["success"] = False
        payload["outcome"] = "noop"
        payload["error"] = (
            f"--resume exited 0 but the attempt id did not change ({previous_attempt!r} -> "
            f"{attempt!r}); upstream mints a NEW attempt on resume, so nothing was "
            "dispatched. Do not treat this as a resumed sweep"
        )
        return emit(payload)
    payload["success"] = True
    payload["outcome"] = "dispatched"
    # The new attempt is asynchronous like any other submission: poll it.
    payload["note"] = f"new attempt {attempt}; poll this sweep dir again to follow it"
    return emit(payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep-dir", required=True)
    ap.add_argument("--substrate", required=True, choices=ALL_SUBSTRATES)
    ap.add_argument("--scale-test-base", required=True)
    ap.add_argument("--bash", default="bash")
    ap.add_argument(
        "--workload",
        default=WORKLOAD_IO,
        choices=ALL_WORKLOADS,
        help="which driver owns this sweep dir; must match the submit step",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="kubectl --resume is synchronous (it returns once the new Job exists); slurm/ssh detach and ignore this",
    )
    ap.add_argument(
        "--jobid-timeout", type=int, default=600, help="seconds to wait for a NEW coordinator id, or for a refusal"
    )
    ap.add_argument(
        "--allow-noop", action="store_true", help="succeed instead of failing when there is nothing to resume"
    )
    args = ap.parse_args()

    if not args.sweep_dir.strip():
        # The config passes `--sweep-dir={{ steps.submit_sweep.sweep_dir | default('') }}`; an empty
        # value means submit_sweep never produced a sweep. abspath("") would silently become the
        # current directory and report a misleading cluster-access error.
        return fail("no sweep to act on: submit_sweep produced no sweep_dir", substrate=args.substrate)
    sweep_dir = os.path.abspath(args.sweep_dir)
    base = os.path.abspath(args.scale_test_base)
    if not os.path.isdir(sweep_dir):
        return fail(f"no such sweep dir: {sweep_dir}")
    wl = workload(args.workload)
    if not os.path.isfile(wl.driver_path(base)):
        return fail(f"no {wl.driver} under {base}")
    if not os.path.isfile(os.path.join(sweep_dir, "env_used.sh")):
        # Without it the driver cannot rehydrate; better to say so than to let it exit 1.
        return fail(
            f"{sweep_dir}/env_used.sh is missing -- --resume rehydrates the prior "
            "configuration from it and cannot run without it"
        )

    todo = outstanding(sweep_dir)
    if not todo:
        msg = f"nothing to resume: every execution in {sweep_dir} is already {SUCCESS}"
        if args.allow_noop:
            log(f"note: {msg}")
            return emit({"success": True, "sweep_dir": sweep_dir, "outcome": "noop", "resumed": 0})
        return fail(msg, sweep_dir=sweep_dir, outcome="noop")

    if args.substrate == KUBECTL:
        return resume_kubectl(base, sweep_dir, wl, args, len(todo))

    previous = read_coordinator_jobid(sweep_dir)
    log(f"resuming {len(todo)} execution(s); prior coordinator id = {previous or 'none'}")
    log_path = os.path.join(sweep_dir, f"acv-resume-{int(time.time())}.log")
    pid = launch_detached(base, sweep_dir, log_path, args.bash, wl.driver)

    jobid, outcome = wait_for_new_jobid(sweep_dir, previous, log_path, args.jobid_timeout)

    payload = {
        "sweep_dir": sweep_dir,
        "substrate": args.substrate,
        "outcome": outcome,
        "resume_log": log_path,
        "launcher_pid": pid,
        "resumed": len(todo),
        "previous_coordinator_jobid": previous,
        "coordinator_jobid": jobid,
    }

    if outcome == "dispatched":
        payload["success"] = True
        return emit(payload)
    if outcome == "refused":
        # Deliberately not a hard failure in wording: the sweep is fine, the resume was
        # unnecessary. The step still exits nonzero so nobody reads it as work done.
        payload["success"] = False
        payload["error"] = (
            f"the recorded coordinator ({previous}) is still active, so --resume refused to "
            "redispatch. This means the sweep is still running, NOT that it failed -- poll it "
            "instead of resuming."
        )
        return emit(payload)
    payload["success"] = False
    payload["error"] = (
        f"no new coordinator id within {args.jobid_timeout}s and no refusal in {log_path}; "
        "the resume most likely failed during validation -- read that log"
    )
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
