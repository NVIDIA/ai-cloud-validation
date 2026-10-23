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

"""Stop a running kubectl sweep and publish the cancellation.

``--cancel`` is a first-class lifecycle verb upstream, alongside ``--status`` and
``--collect``: all three dispatch to ``kubectl_lifecycle_operation``
(``nv-elbencho-sweep.sh:466-471``) and address the attempt *independently of the submitting
process lifetime* (``docs/DESIGN.md:318``). Without it, an abandoned ACV run leaves the
sweep running on the cluster holding the PVC Lease, and the only way to stop it is to go
find the driver by hand.

Three properties of upstream cancellation shape this file, all normative in
``docs/KUBERNETES_ELBENCHO_LIFECYCLE.md``:

1. **Cancellation is two-phase, not atomic.** The local attempt goes
   ``SUBMITTED -> CANCEL_REQUESTED -> TERMINAL`` (:81-83). A client that exits mid-way
   leaves the attempt in ``CANCEL_REQUESTED``, and **repeating ``--cancel`` resumes the
   exact deletion and finalisation** (T-08, :339). So a single call is not sufficient to
   conclude anything -- this step retries rather than reporting a half-finished cancel as
   done.

2. **``CANCEL_REQUESTED`` must never be reported as ordinary running state** (T-09, :340).
   It is reported here as its own state, with the retry named.

3. **Cancelling does NOT release the cluster.** Cancel stops the Job and publishes the
   outcome while *retaining collection data* (T-07, :338); it is ``--collect`` that
   releases the PVC Lease and deletes owned resources (``docs/DESIGN.md:349-352``). So a
   cancelled sweep still owns cluster state until collected, and this step says so rather
   than implying the cluster is now clean.

``CANCELLED`` is an immutable terminal outcome beside ``SUCCESS`` and ``FAILED`` (:112-113),
so a cancelled sweep is *finished*, not failed. Whether that should fail the ACV run is a
policy question, not a fact about the sweep: ``--expect-cancelled`` says the cancellation
was intended and the step passes.
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
    CANCEL_REQUESTED,
    COLLECTION_COMPLETE,
    KUBECTL,
    WORKLOAD_IO,
    emit,
    fail,
    lock_contended,
    lock_contention_error,
    log,
    parse_batch_status,
    run,
    workload,
)


def _local_attempt_state(status: dict) -> str | None:
    """The local attempt state, which is NOT in the ordinary status view.

    ``kubectl_emit_status_view`` validates its outcome against
    ``^(PREPARED|SUBMITTED|RUNNING|SUCCESS|FAILED|CANCELLED)$`` and prints a *display*
    value derived from it, so ``CANCEL_REQUESTED`` can never appear as ``STATE``. It
    reaches us only through the diagnostics block, as
    ``STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE``, which ``parse_batch_status`` normalises
    to ``DIAGNOSTIC_LOCAL_STATE``. There is no bare ``LOCAL_STATE`` key anywhere upstream
    -- do not reintroduce a lookup for one; it will silently never match.
    """
    return status.get("DIAGNOSTIC_LOCAL_STATE")


def lifecycle(base: str, verb: str, sweep_dir: str, timeout: int, kind: str = WORKLOAD_IO) -> tuple[int, dict, str]:
    """Run one lifecycle verb; return (exit code, parsed status keys, raw stdout).

    A contended lock is reported through the parsed dict rather than the exit code: every
    caller already branches on the exit code to mean "the driver refused", and contention
    is not a refusal -- nothing was attempted at all.
    """
    proc = run(["bash", workload(kind).driver_path(base), f"--{verb}", sweep_dir], timeout=timeout)
    sys.stderr.write(proc.stderr)
    parsed = parse_batch_status(proc.stdout)
    if lock_contended(proc):
        parsed["LOCK_CONTENDED"] = "1"
    return proc.returncode, parsed, proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep-dir", required=True, help="steps.submit_sweep.sweep_dir")
    ap.add_argument("--substrate", required=True, choices=ALL_SUBSTRATES)
    ap.add_argument("--scale-test-base", help="required for --substrate kubectl")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument(
        "--retries",
        type=int,
        default=3,
        help="extra --cancel attempts while the attempt is still "
        "CANCEL_REQUESTED; upstream resumes finalisation on repeat (T-08)",
    )
    ap.add_argument("--retry-interval", type=int, default=15)
    ap.add_argument(
        "--workload",
        default=WORKLOAD_IO,
        choices=ALL_WORKLOADS,
        help="which driver owns this sweep dir; must match the submit step",
    )
    ap.add_argument(
        "--expect-cancelled",
        action="store_true",
        help="treat a reached CANCELLED/terminal state as a PASS. Without it "
        "the step reports what happened but does not call a cancelled "
        "sweep a success, because cancelling is usually a recovery action",
    )
    ap.add_argument(
        "--allow-noop", action="store_true", help="pass when there was nothing to cancel (already terminal)"
    )
    args = ap.parse_args()

    if not args.sweep_dir.strip():
        # The config passes `--sweep-dir={{ steps.submit_sweep.sweep_dir | default('') }}`; an empty
        # value means submit_sweep never produced a sweep. abspath("") would silently become the
        # current directory and report a misleading cluster-access error.
        return fail("no sweep to act on: submit_sweep produced no sweep_dir", substrate=args.substrate)
    sweep_dir = os.path.abspath(args.sweep_dir)
    if not os.path.isdir(sweep_dir):
        return fail(f"no such sweep dir: {sweep_dir}")

    # --status/--cancel/--collect all route to kubectl_lifecycle_operation regardless of
    # the configured substrate (nv-elbencho-sweep.sh:466). On slurm/ssh there is no saved
    # attempt identity for it to act on, so refuse here with the real reason instead of
    # letting the driver fail somewhere less legible.
    if args.substrate != KUBECTL:
        return fail(
            f"--cancel is a kubectl lifecycle operation; substrate is {args.substrate}. "
            "On slurm cancel the coordinator allocation with scancel; on ssh stop the "
            "dispatcher process recorded beside the sweep",
            sweep_dir=sweep_dir,
            substrate=args.substrate,
        )
    if not args.scale_test_base:
        return fail("--scale-test-base is required for --substrate kubectl", sweep_dir=sweep_dir)

    base = os.path.abspath(args.scale_test_base)
    # Seeded so every emit path below has the SAME shape. The noop branch previously
    # omitted cancel_attempts/cancel_exit_code/local_attempt_state, which a FieldExistsCheck
    # would report as missing on exactly the runs where nothing went wrong.
    payload: dict = {
        "sweep_dir": sweep_dir,
        "substrate": args.substrate,
        "workload": args.workload,
        "state_before": None,
        "state": None,
        "local_attempt_state": None,
        "cancelled": False,
        "noop": False,
        "finalised": False,
        "cancel_attempts": 0,
        "cancel_exit_code": None,
        "resources_released": None,
        "release_state_known": False,
        # Same rule as poll_sweep: seeded on every path, because an absent key is not
        # False to a FieldExistsCheck. Only the contention branch sets it True.
        "lock_contended": False,
    }

    # Where are we before touching anything? A sweep that already finished must not be
    # reported as "cancelled by us".
    rc, before, _ = lifecycle(base, "status", sweep_dir, args.timeout, args.workload)
    if before.get("LOCK_CONTENDED"):
        # Nothing was cancelled, and nothing is wrong with the sweep or the cluster.
        return fail(
            lock_contention_error(sweep_dir, "cancel this sweep"),
            sweep_dir=sweep_dir,
            substrate=KUBECTL,
            workload=args.workload,
            lock_contended=True,
            cancelled=False,
            finalised=False,
            state=None,
        )
    if rc != 0:
        return fail(
            f"could not read sweep state before cancelling (--status exited {rc}). This "
            "usually means cluster access was lost; the sweep is unaffected and still "
            f"running. Restore access and re-run cancel against {sweep_dir}",
            **payload,
        )

    payload["state_before"] = before.get("STATE")
    already_terminal = before.get("STATE") in BATCH_STATE_TERMINAL
    if already_terminal:
        payload.update(
            {
                "cancelled": False,
                "state": before.get("STATE"),
                "noop": True,
                # Nothing was in flight, so there is nothing left half-done.
                "finalised": True,
                "note": f"nothing to cancel: the sweep is already {before.get('STATE')}",
            }
        )
        payload["release_state_known"] = "RESULT_COLLECTION" in before
        payload["resources_released"] = before.get("RESULT_COLLECTION") == COLLECTION_COMPLETE
        if not payload["resources_released"]:
            payload["leak_note"] = (
                "the attempt still owns cluster-side state (PVC Lease, Job, DaemonSet); "
                "run the collect step to release it"
            )
        payload["success"] = bool(args.allow_noop)
        if not payload["success"]:
            payload["error"] = (
                f"nothing to cancel: sweep is already {before.get('STATE')}; pass --allow-noop to treat that as a pass"
            )
        return emit(payload)

    attempts = 0
    last_rc, status = None, before
    for attempt in range(args.retries + 1):
        attempts = attempt + 1
        last_rc, _, _ = lifecycle(base, "cancel", sweep_dir, args.timeout, args.workload)
        rc_status, status, _ = lifecycle(base, "status", sweep_dir, args.timeout, args.workload)
        if rc_status != 0:
            payload["status_error"] = f"--status exited {rc_status} after cancel"
            break
        state = status.get("STATE")
        local = _local_attempt_state(status)
        # Finalised: terminal outcome and no cancellation still in flight.
        if state in BATCH_STATE_TERMINAL and local != CANCEL_REQUESTED:
            break
        if attempt < args.retries:
            log(
                f"cancellation not finalised (state={state}, local={local}); "
                f"retrying --cancel in {args.retry_interval}s "
                f"[{attempt + 1}/{args.retries}]"
            )
            time.sleep(args.retry_interval)

    state = status.get("STATE")
    local = _local_attempt_state(status)
    payload.update(
        {
            "state": state,
            "local_attempt_state": local,
            "cancel_exit_code": last_rc,
            "cancel_attempts": attempts,
            "cancelled": state == "CANCELLED",
            "noop": False,
            # The one thing a validation most needs to assert: the cancellation is RESOLVED --
            # a terminal outcome with nothing still in flight. CANCEL_REQUESTED is resumable,
            # not finished, and must never satisfy this.
            "finalised": state in BATCH_STATE_TERMINAL and local != CANCEL_REQUESTED,
        }
    )

    # Cancel retains collection data; only collect releases the cluster. `release_state_known`
    # exists for the same reason `asserted` does on the metrics step: "I could not tell"
    # must be distinguishable from "nothing is held", or a validation passes on ignorance.
    payload["release_state_known"] = "RESULT_COLLECTION" in status
    payload["resources_released"] = status.get("RESULT_COLLECTION") == COLLECTION_COMPLETE
    if not payload["resources_released"]:
        payload["leak_note"] = (
            "cancellation stops the Job but RETAINS collection data: the PVC Lease and the "
            "owned Kubernetes resources are released by --collect, not by --cancel. Run "
            "the collect step against this sweep or the next run will contend for the PVC"
        )

    if local == CANCEL_REQUESTED:
        payload["success"] = False
        payload["error"] = (
            f"cancellation is still in flight after {attempts} attempt(s): the local "
            f"attempt is {CANCEL_REQUESTED}. This is a resumable state, not a failure -- "
            "re-run this step and upstream will resume the exact deletion and "
            "finalisation. Do NOT treat the sweep as still running"
        )
        return emit(payload)

    if state not in BATCH_STATE_TERMINAL:
        payload["success"] = False
        payload["error"] = (
            f"--cancel exited {last_rc} but the sweep is still {state!r} after "
            f"{attempts} attempt(s); it has not reached a terminal outcome"
        )
        return emit(payload)

    payload["success"] = bool(args.expect_cancelled)
    if not payload["success"]:
        payload["error"] = (
            f"the sweep reached {state} after cancellation. Cancelling is normally a "
            "recovery action, so this step does not report it as a pass; use "
            "--expect-cancelled when the cancellation was the intended outcome"
        )
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
