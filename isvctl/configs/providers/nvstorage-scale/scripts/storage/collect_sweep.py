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

"""Turn a finished sweep directory into a result ACV validations can assert on.

Everything here is read-only and works from a session that never saw the submit.

The one rule that is easy to get wrong
--------------------------------------
On the kubectl substrate a *failed sweep is still collectible*: ``--collect`` exits nonzero
while leaving the directory complete and resumable. If that exit code fails the ACV step,
every finished cell is thrown away because one cell did not finish. So the native collect's
exit code is recorded as data, never propagated --- and the step's own success comes from the
status sentinels. Bind this step with ``continue_on_failure: true`` regardless, so a sweep
that genuinely failed still publishes its partial results to later validations.

Slurm's ``--resume`` has the same trap in mirror image: it exits 1 both when it refuses
(coordinator still live) and when the sweep failed. Match on ``refusing to reset or
redispatch`` to tell those apart.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import (
    ALL_SUBSTRATES,
    ALL_WORKLOADS,
    COLLECTION_COMPLETE,
    FAILED,
    INFRASTRUCTURE_FAILURES,
    KUBECTL,
    SUCCESS,
    WORKLOAD_IO,
    WORKLOADS,
    describe_failure,
    emit,
    fail,
    is_batch_dir,
    lock_contended,
    lock_contention_error,
    log,
    parse_batch_status,
    read_failure_evidence,
    read_placement,
    read_statuses,
    run,
    workload,
)

# Every workload's DERIVED roll-up name. These are outputs of metrics.py, not results, and
# must never be counted as collected CSVs. Both patterns are excluded regardless of the
# workload being collected, because a results tree can hold either and an excluded name that
# is simply absent costs nothing. `mdtest-elbencho-metrics-<stamps>.csv` is interpolated,
# not fixed, so this has to be a glob match and not a name comparison.
DERIVED_CSV_PATTERNS = tuple(w.metrics_glob for w in WORKLOADS.values())


def read_exitcode(executions_dir: str, exec_id: str) -> int | None:
    path = os.path.join(executions_dir, f"{exec_id}.exitcode")
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def find_csvs(sweep_dir: str) -> list[str]:
    """elbencho writes ``elbencho-<size>-c_NNN-s_NNN-d_NNN_<DS>.csv`` per execution.

    Two names are excluded deliberately:
      * ``*-live.csv`` -- progress samples, not results; counting them would double every
        execution in any aggregate a validation computes.
      * the *derived* roll-up that ``metrics.py`` asks the
        upstream extractor to write into this same directory
        upstream extractor to write into this same directory (``elbencho-metrics.csv`` for
        io; ``mdtest-elbencho-metrics-<stamps>.csv`` for mdtest). It is an output of
        analysis, not an input to it, so re-running collect after metrics would otherwise
        report one extra "result".
    """
    found = sorted(glob.glob(os.path.join(sweep_dir, "**", "*.csv"), recursive=True))
    out = []
    for path in found:
        name = os.path.basename(path)
        if name.endswith("-live.csv"):
            continue
        if any(fnmatch.fnmatch(name, pat) for pat in DERIVED_CSV_PATTERNS):
            continue
        out.append(path)
    return out


def summarise_csv(path: str) -> dict:
    """Header + row count only. Parsing elbencho's numbers is a validation's job, not a step's."""
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            rows = sum(1 for _ in reader)
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    return {"path": path, "columns": header, "rows": rows}


def native_collect(base: str, sweep_dir: str, timeout: int, kind: str = WORKLOAD_IO, retries: int = 2) -> dict:
    """Publish the cluster-side results into the local sweep tree.

    Its nonzero exit is captured, never raised: a sweep whose cells FAILED also makes
    --collect exit nonzero, and that must still publish the cells that did finish. The
    caller distinguishes that case from "we could not reach the cluster at all" by looking
    at what actually landed -- see the guard in main().
    """
    driver = workload(kind).driver_path(base)
    proc = run(["bash", driver, "--collect", sweep_dir], timeout=timeout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        log(
            f"note: --collect exited {proc.returncode}; this is expected for a sweep that "
            "failed and does NOT mean collection failed. Not propagating."
        )
    out = {
        "native_collect_exit_code": proc.returncode,
        "native_collect_stdout": proc.stdout,
        "lock_contended": lock_contended(proc),
    }
    # --collect is the step that releases the PVC Lease and deletes the owned Job and
    # DaemonSet (docs/DESIGN.md:349-352), so "did collect finish?" is also "is the cluster
    # clean?". This step is deliberately continue_on_failure in the config -- one bad cell
    # must not discard good data -- which means a collect that did NOT release can pass the
    # phase and leave the sweep owning the PVC. The next run then fails on lease contention
    # a long way from the cause. Ask the driver what it
    # still owns and surface it, rather than leaving the leak silent.
    rc, status = _collection_state(base, sweep_dir, timeout, kind)
    # One --collect can publish the results yet leave its own cleanup unfinished
    # (RESULT_COLLECTION=CLEANUP_PENDING, NEXT_ACTION=COLLECT), with the PVC Lease still
    # held. The driver's instruction is to collect again, and a second --collect completes
    # it. Follow that instruction a bounded number of times rather than reporting a leak the
    # driver says how to fix.
    attempts = 1
    while (
        rc == 0
        and attempts <= retries
        and status.get("NEXT_ACTION") == "COLLECT"
        and status.get("RESULT_COLLECTION") != COLLECTION_COMPLETE
    ):
        attempts += 1
        log(f"note: cleanup still pending (RESULT_COLLECTION={status.get('RESULT_COLLECTION')}); collecting again")
        run(["bash", driver, "--collect", sweep_dir], timeout=timeout)
        rc, status = _collection_state(base, sweep_dir, timeout, kind)
    out["collect_attempts"] = attempts
    if rc == 0:
        # Symmetric with cancel_sweep: "I could not tell" must be distinguishable from
        # "nothing is held", or a validation passes on ignorance.
        out["release_state_known"] = "RESULT_COLLECTION" in status
        out["result_collection"] = status.get("RESULT_COLLECTION")
        out["next_action"] = status.get("NEXT_ACTION")
        out["resources_released"] = status.get("RESULT_COLLECTION") == COLLECTION_COMPLETE
        if not out["resources_released"]:
            out["leak_note"] = (
                "this attempt still owns cluster-side state: RESULT_COLLECTION="
                f"{status.get('RESULT_COLLECTION')!r} (expected {COLLECTION_COMPLETE!r}). "
                "The PVC Lease and the owned Job/DaemonSet are NOT released, so the next "
                f"sweep against this PVC will contend. Re-run collect against {sweep_dir}"
            )
    else:
        out["release_state_known"] = False
        out["resources_released"] = None
        out["leak_note"] = (
            f"could not confirm whether cluster resources were released (--status exited "
            f"{rc}); treat the PVC Lease as still held until a successful collect"
        )
    return out


def _collection_state(base: str, sweep_dir: str, timeout: int, kind: str = WORKLOAD_IO) -> tuple[int, dict]:
    """Read RESULT_COLLECTION back from the driver after a collect attempt."""
    driver = workload(kind).driver_path(base)
    proc = run(["bash", driver, "--status", sweep_dir], timeout=timeout)
    return proc.returncode, parse_batch_status(proc.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep-dir", required=True, help="steps.submit_sweep.sweep_dir")
    ap.add_argument("--substrate", required=True, choices=ALL_SUBSTRATES)
    ap.add_argument("--scale-test-base", help="required for --substrate kubectl")
    ap.add_argument(
        "--require-complete", action="store_true", help="fail the step unless every execution reached SUCCESS"
    )
    ap.add_argument("--native-timeout", type=int, default=1800)
    ap.add_argument(
        "--collect-retries",
        type=int,
        default=2,
        help="re-run --collect while the driver reports cleanup still pending (NEXT_ACTION=COLLECT)",
    )
    ap.add_argument(
        "--workload",
        default=WORKLOAD_IO,
        choices=ALL_WORKLOADS,
        help="which driver owns this sweep dir; must match the submit step",
    )
    args = ap.parse_args()

    sweep_dir = os.path.abspath(args.sweep_dir)
    if not os.path.isdir(sweep_dir):
        return fail(f"no such sweep dir: {sweep_dir}")

    payload: dict = {
        "sweep_dir": sweep_dir,
        "substrate": args.substrate,
        "workload": args.workload,
        "batch": is_batch_dir(sweep_dir),
    }

    # Must run before the sentinels are read: on kubectl, per-cell results are not published
    # into the sweep dir until --collect does it, so parsing first would see an empty run.
    if args.substrate == KUBECTL:
        if not args.scale_test_base:
            return fail("--scale-test-base is required for --substrate kubectl")
        payload.update(
            native_collect(
                os.path.abspath(args.scale_test_base),
                sweep_dir,
                args.native_timeout,
                args.workload,
                retries=args.collect_retries,
            )
        )

    executions_dir = os.path.join(sweep_dir, "executions")
    statuses = read_statuses(sweep_dir)
    if not statuses:
        return fail(f"no <NNNN>.status files under {executions_dir}", **payload)

    executions = [
        {
            "id": exec_id,
            "status": status,
            "exit_code": read_exitcode(executions_dir, exec_id),
            "log": (
                os.path.join(executions_dir, f"{exec_id}.log")
                if os.path.exists(os.path.join(executions_dir, f"{exec_id}.log"))
                else None
            ),
        }
        for exec_id, status in sorted(statuses.items())
    ]
    succeeded = [e for e in executions if e["status"] == SUCCESS]
    failed = [e for e in executions if e["status"] == FAILED]
    incomplete = [e for e in executions if e["status"] not in (SUCCESS, FAILED)]

    csvs = find_csvs(sweep_dir)
    payload.update(
        {
            "executions": executions,
            "total": len(executions),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "incomplete": len(incomplete),
            "complete": not incomplete,
            "results_csv": [summarise_csv(p) for p in csvs],
            # Countable form of the above. ACV's FieldValueCheck compares numbers via min/max and
            # has no way to take the length of a list, so a validation that wants "at least one
            # result exists" needs this to exist as a scalar.
            "results_csv_count": len(csvs),
            "env_used_yaml": (
                os.path.join(sweep_dir, "env_used.yaml")
                if os.path.exists(os.path.join(sweep_dir, "env_used.yaml"))
                else None
            ),
            "env_used_sh": (
                os.path.join(sweep_dir, "env_used.sh")
                if os.path.exists(os.path.join(sweep_dir, "env_used.sh"))
                else None
            ),
        }
    )

    # WHERE the results were measured, not just that they exist.
    #
    # Collect is the right home for this: placement is per-ATTEMPT, and collect is what
    # publishes an attempt. After a resume there are several attempts in the tree, and only
    # the one that produced these results is worth reporting -- `current-attempt` names it.
    #
    # The pool and the selection are different sets, and they diverge silently: a sweep can
    # stand up workers on both eligible nodes and run every execution on one of them --
    # node_coverage 0.5, reported nowhere, while the run passes every throughput
    # assertion. A number with no record of where it came from cannot be compared
    # against the same number from last week.
    placement = read_placement(sweep_dir)
    payload["placement"] = placement
    # Flattened for validations: FieldExistsCheck does NOT walk dotted paths
    # (generic.py:52 does a plain `f not in step_output`), so anything a validation must
    # assert on has to exist at the top level.
    payload["nodes_used_count"] = placement["nodes_used_count"]
    payload["nodes_available_count"] = placement["nodes_available_count"]
    payload["node_coverage"] = placement["node_coverage"]

    payload["worker_selection"] = placement["worker_selection"]
    if placement["nodes_available_count"] and placement["nodes_used_count"] < placement["nodes_available_count"]:
        # The advice depends on what was already in force. Telling an operator to set a
        # knob they have already set reads as a bug in the report.
        if placement["worker_selection"] == "deterministic":
            advice = (
                "selection was deterministic, so this is the same subset every "
                "run; raise --nodes to measure more of the pool"
            )
        else:
            advice = (
                "selection was RANDOM, so a different subset may be measured next "
                "run; set SST_ORDER_NODES=1 to make it repeatable"
            )
        log(
            f"warning: measured {placement['nodes_used_count']} of "
            f"{placement['nodes_available_count']} eligible node(s) "
            f"({', '.join(placement['nodes_used'])}); {advice}"
        )

    if succeeded and not csvs:
        # Worth flagging loudly: a sweep can mark executions SUCCESS and still leave nothing
        # to measure, which is exactly what a throughput validation would silently pass on.
        log(f"warning: {len(succeeded)} execution(s) SUCCESS but no result CSV found under {sweep_dir}")

    # A collect that reached nothing is not a success. The lenient rule below exists so a
    # sweep with some FAILED cells still publishes the good ones -- but with no cluster
    # access there are no cells to publish, nothing is marked FAILED, and `failed` is
    # therefore 0, so the rule would hand back success=True on an empty result set.
    # Losing access mid-run is the expected case for a detached sweep (credentials expire,
    # laptops suspend), so name it instead of passing vacuously.
    # Contention first: it produces exactly the shape the access-loss branch below looks
    # for -- non-zero exit, no CSVs, everything still outstanding -- and that branch would
    # tell the operator to re-authenticate over a local file lock.
    if args.substrate == KUBECTL and payload.get("lock_contended"):
        return fail(lock_contention_error(sweep_dir, "collect this sweep"), **payload)

    if (
        args.substrate == KUBECTL
        and payload.get("native_collect_exit_code") not in (None, 0)
        and not csvs
        and incomplete
    ):
        return fail(
            f"--collect exited {payload['native_collect_exit_code']} and published no "
            f"results: {len(incomplete)} of {len(executions)} execution(s) are still "
            "unresolved locally. This usually means cluster access was lost (expired "
            "credential, or a changed kubeconfig/context) -- the results are still on the "
            "PVC and the sweep is unaffected. Restore access and re-run collect against "
            f"{sweep_dir}",
            **payload,
        )

    # WHY it failed, and whether "it failed" is a statement about the storage at all.
    #
    # Until now this step reported `N execution(s) FAILED` plus a --resume command: what
    # happened and what to type, never why. Worse, it could not tell a benchmark failure
    # from a LOST COORDINATOR -- an evicted or preempted pod leaves FAILED cells behind
    # exactly like a storage fault does. Calling that "storage validation failed" is a
    # false finding, and acting on it sends someone to debug a filesystem that was never
    # the problem. Upstream publishes the distinction; this reads it.
    evidence = read_failure_evidence(sweep_dir)
    payload["failure"] = evidence
    # Flattened for validations: FieldExistsCheck does not walk dotted paths
    # (generic.py:52), so anything asserted on must live at the top level.
    payload["failure_kind"] = evidence["failure_kind"]
    payload["is_storage_finding"] = evidence["is_storage_finding"]
    payload["failure_exit_code"] = evidence["exit_code"]
    # Countable form. FieldValueCheck compares numbers (min/max/expected/contains/
    # operator) and has NO length comparison, so "at least one evidence file was read"
    # has to exist as a scalar or it cannot be asserted at all.
    payload["evidence_source_count"] = len(evidence["evidence_sources"])

    payload["success"] = (not failed) and (not incomplete or not args.require_complete)
    if failed:
        detail = describe_failure(evidence)
        if evidence["failure_kind"] in INFRASTRUCTURE_FAILURES:
            # Deliberately NOT recommending --resume first. Resuming into the same
            # preemption reproduces the eviction, not the measurement.
            payload["error"] = (
                f"{len(failed)} of {len(executions)} execution(s) FAILED, but this is NOT a storage result: {detail}"
            )
        else:
            payload["error"] = (
                f"{len(failed)} of {len(executions)} execution(s) FAILED: {detail} "
                f"Re-dispatch the unfinished ones with: nv-elbencho-sweep.sh --resume "
                f"{sweep_dir}"
            )
    elif incomplete and args.require_complete:
        payload["error"] = f"{len(incomplete)} execution(s) never reached a terminal status"

    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
