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

"""Check the storage meets its minimum bandwidth and IOPS on a valid sweep (HSS02-01).

The storage suite's ``HssQosThroughputCheck`` (suites/storage.yaml, ``step: qos_throughput``)
judges this step. It reads ``tests.bandwidth_meets_min`` and ``tests.iops_meets_min``, the
same shape my-isv's reference ``qos_throughput_test.py`` emits.

The numbers come from the ``metrics`` step, which owns the thresholds (``--min-*``) and the
comparisons. This step translates its verdicts into the HSS02-01 shape -- and refuses to give
one at all unless the sweep behind them is valid.

WHY IT GATES
------------
Every test-phase step before this one runs with ``continue_on_failure`` so that ``collect``
always releases the PVC Lease. The orchestrator does not fail a run for such a step
(step_executor.py: ``if not step_result.success and not step.continue_on_failure``), and
like every provider this one defines no checks of its own. So nothing else would notice a
poll that lost the cluster, a sweep that ran half its matrix, or a two-claim run that wrote
to one claim -- and a throughput number from such a sweep is not a QoS result. This step
receives each earlier step's output (``{{ steps.<name> | tojson }}``) and enforces the
invariants on them. Any failure fails both HSS02-01 tests with the reason, rather than
letting a valid-looking bandwidth figure through.

A step is part of the workflow when its argument is passed. Passed but empty (``{}``) means
it never reported, which fails -- except for ``cancel`` and ``resume``, which are opt-in
(``skip: true``) and so are empty on an ordinary run.

mdtest measures metadata operations, not bandwidth. ``bandwidth_meets_min`` is reported as
skipped with that reason, so on mdtest HSS02-01 can fail (on operations per second) but is
reported skipped rather than passed when everything else is fine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import KUBECTL, WORKLOAD_IO, WORKLOAD_MDTEST, emit, fail

REQUIRED_STEPS = ("preflight", "poll_sweep", "collect", "verify", "targets", "metrics")
OPTIONAL_STEPS = ("cancel", "resume")


def _present(out: dict, field: str) -> bool:
    return out.get(field) is not None


def _rules(step: str, workload: str, substrate: str, min_claims: int) -> list[tuple[str, callable]]:
    """The invariants for one step, as (label, predicate) pairs.

    Each is one the provider's former per-step checks asserted. The label says what must hold,
    so a failure reads as the reason the sweep cannot be judged.
    """
    kubectl = substrate == KUBECTL
    ok = ("step succeeded", lambda o: o.get("success") is True)
    rules: dict[str, list] = {
        # Environment validated before dispatch, and the validator examined something.
        "preflight": [
            ok,
            # --warn-only lets an operator run past a known validation failure; it is recorded
            # as warn_only_override and honoured here rather than refused at the verdict.
            (
                "preflight passed (or --warn-only override)",
                lambda o: o.get("preflight_passed") is True or o.get("warn_only_override") is True,
            ),
            ("checks_run >= 1", lambda o: (o.get("checks_run") or 0) >= 1),
        ],
        "poll_sweep": [ok]
        + (
            [
                # Not serialised behind another lifecycle operation.
                ("lock_contended is false", lambda o: o.get("lock_contended") is False),
                # Progress read from the cluster, not from local sentinels that stay PENDING.
                ("progress_source is PVC", lambda o: o.get("progress_source") == "PVC"),
                ("counts_authoritative is true", lambda o: o.get("counts_authoritative") is True),
                (
                    "cluster execution counts reported",
                    lambda o: _present(o, "kubectl_executions_pending") and _present(o, "kubectl_executions_running"),
                ),
            ]
            if kubectl
            else []
        ),
        "collect": [
            ok,
            ("every execution reached SUCCESS", lambda o: o.get("complete") is True),
            ("failed == 0", lambda o: o.get("failed") == 0),
            ("results_csv_count >= 1", lambda o: (o.get("results_csv_count") or 0) >= 1),
            ("succeeded count reported", lambda o: _present(o, "succeeded")),
        ]
        + (
            [
                # A failure, had there been one, was classified from real evidence.
                (
                    "failure classification reported",
                    lambda o: _present(o, "failure_kind") and _present(o, "is_storage_finding"),
                ),
                ("evidence_source_count >= 1", lambda o: (o.get("evidence_source_count") or 0) >= 1),
                # Which nodes were actually measured.
                ("placement reported", lambda o: _present(o, "nodes_available_count") and _present(o, "node_coverage")),
                ("nodes_used_count >= 1", lambda o: (o.get("nodes_used_count") or 0) >= 1),
            ]
            if kubectl
            else []
        )
        + (
            [("routed to the mdtest driver", lambda o: o.get("workload") == WORKLOAD_MDTEST)]
            if workload == WORKLOAD_MDTEST
            else []
        ),
        # The sweep produced an output for every cell its own env_used.yaml configured.
        "verify": [
            ok,
            ("verified is true", lambda o: o.get("verified") is True),
            ("full configured matrix ran", lambda o: o.get("complete") is True),
            ("outputs_expected >= 1", lambda o: (o.get("outputs_expected") or 0) >= 1),
            ("directories_summarized >= 1", lambda o: (o.get("directories_summarized") or 0) >= 1),
        ],
        # Every configured claim was written to, at its configured weight.
        "targets": [
            ok,
            ("weights_match is true", lambda o: o.get("weights_match") is True),
            (f"claims_configured >= {min_claims}", lambda o: (o.get("claims_configured") or 0) >= min_claims),
            (
                f"claims_exercised >= {max(min_claims, 1)}",
                lambda o: (o.get("claims_exercised") or 0) >= max(min_claims, 1),
            ),
        ],
        "metrics": [
            ("samples >= 1", lambda o: (o.get("samples") or 0) >= 1),
            # A run with no thresholds configured would pass however bad it was.
            ("thresholds were applied", lambda o: o.get("asserted") is True),
        ]
        + (
            [("all mdtest phases measured", lambda o: o.get("all_phases_measured") is True)]
            if workload == WORKLOAD_MDTEST
            else []
        ),
        "cancel": [
            ok,
            ("cancellation finalised", lambda o: o.get("finalised") is True),
            ("release state known", lambda o: o.get("release_state_known") is True),
            (
                "release fields reported",
                lambda o: all(_present(o, f) for f in ("resources_released", "state", "cancel_attempts")),
            ),
        ],
        "resume": [
            ok,
            ("a new attempt was dispatched", lambda o: o.get("new_attempt") is True),
            (
                "attempt fields reported",
                lambda o: all(_present(o, f) for f in ("previous_attempt", "attempt", "resumed")),
            ),
        ],
    }
    return rules[step]


def evaluate_prerequisites(
    outputs: dict[str, dict | None], workload: str, substrate: str, min_claims: int
) -> tuple[dict, list[str]]:
    """Check every supplied step against its invariants. Returns (report, failures)."""
    report, failures = {}, []
    for step in REQUIRED_STEPS + OPTIONAL_STEPS:
        out = outputs.get(step)
        if out is None:
            continue  # not part of this workflow
        if not out:
            if step in OPTIONAL_STEPS:
                report[step] = {"passed": True, "detail": "not run (opt-in step)"}
                continue
            report[step] = {"passed": False, "detail": "step did not report"}
            failures.append(f"{step}: step did not report")
            continue
        broken = [label for label, pred in _rules(step, workload, substrate, min_claims) if not pred(out)]
        report[step] = {"passed": not broken, "detail": "; ".join(broken) or "ok"}
        failures += [f"{step}: {label}" for label in broken]
    # Floors that are neither bandwidth nor operations (e.g. latency ceilings) still decide
    # whether the run is acceptable; they just have no HSS02-01 field of their own.
    other = [
        c for c in (outputs.get("metrics") or {}).get("checks", []) if _kind(c["name"]) is None and not c.get("passed")
    ]
    if other:
        report["metrics_other_thresholds"] = {"passed": False, "detail": "; ".join(c["detail"] for c in other)}
        failures += [f"metrics: {c['name']}: {c['detail']}" for c in other]
    return report, failures


def _kind(check_name: str) -> str | None:
    if check_name.endswith("_gb_s"):
        return "bandwidth"
    if check_name.endswith(("_iops", "_ops_s")):
        return "iops"
    return None


def _verdict(checks: list[dict], kind: str) -> dict:
    """Fold metrics' threshold checks of one kind into an HSS02-01 test entry."""
    mine = [c for c in checks if _kind(c["name"]) == kind]
    flag = "--min-write-gb-s/--min-read-gb-s" if kind == "bandwidth" else "--min-*-iops/--min-*-ops-s"
    if not mine:
        # Fail closed: an unconfigured minimum is not a met one.
        return {"passed": False, "error": f"no {kind} minimum configured ({flag} on the metrics step)"}
    failed = [c for c in mine if not c.get("passed")]
    entry = {
        "passed": not failed,
        "by_check": {
            c["name"]: {"passed": c.get("passed"), "observed": c.get("observed"), "floor": c.get("floor")} for c in mine
        },
    }
    # The headline pair comes from ONE check -- the one with the least headroom -- so the
    # measured value and the minimum it is compared against describe the same operation.
    # Taking the lowest measurement and the highest floor would pair write with read.
    tightest = min(mine, key=lambda c: (c.get("observed") or 0) / c["floor"] if c["floor"] else 0)
    entry["binding_check"] = tightest["name"]
    observed = tightest.get("observed")
    if kind == "bandwidth":
        # metrics reports decimal GB/s; HSS02-01 speaks MB/s.
        entry["measured_mbps"] = round(observed * 1000, 1) if observed is not None else None
        entry["min_mbps"] = round(tightest["floor"] * 1000, 1)
    else:
        entry["measured_iops"] = observed
        entry["min_iops"] = tightest["floor"]
    detail = "; ".join(c["detail"] for c in (failed or mine))
    entry["error" if failed else "message"] = detail
    return entry


def _load(name: str, raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        out = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(fail(f"--{name.replace('_', '-')} is not JSON: {exc}"))
    return out if isinstance(out, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workload", default=WORKLOAD_IO, choices=(WORKLOAD_IO, WORKLOAD_MDTEST))
    ap.add_argument("--substrate", required=True, help="steps.gen_env.substrate")
    ap.add_argument(
        "--min-claims", type=int, default=1, help="fewest claims the sweep must have been spread across (targets step)"
    )
    # Declared one by one, not in a loop: the repo's stub-contract test reads argparse flags
    # statically, and every flag a config passes must be visible to it.
    ap.add_argument("--preflight", dest="preflight", metavar="JSON", help="{{ steps.preflight | tojson }}")
    ap.add_argument("--poll-sweep", dest="poll_sweep", metavar="JSON", help="{{ steps.poll_sweep | tojson }}")
    ap.add_argument("--collect", dest="collect", metavar="JSON", help="{{ steps.collect | tojson }}")
    ap.add_argument("--verify", dest="verify", metavar="JSON", help="{{ steps.verify | tojson }}")
    ap.add_argument("--targets", dest="targets", metavar="JSON", help="{{ steps.targets | tojson }}")
    ap.add_argument("--metrics", dest="metrics", metavar="JSON", help="{{ steps.metrics | tojson }}")
    ap.add_argument("--cancel", dest="cancel", metavar="JSON", help="{{ steps.cancel | tojson }}")
    ap.add_argument("--resume", dest="resume", metavar="JSON", help="{{ steps.resume | tojson }}")
    args = ap.parse_args()

    outputs = {s: _load(s, getattr(args, s)) for s in REQUIRED_STEPS + OPTIONAL_STEPS}
    if outputs["metrics"] is None:
        return fail(
            "--metrics is required: it carries the measurements and threshold verdicts",
            test_name="qos_throughput",
            substrate=args.substrate,
        )

    prerequisites, failures = evaluate_prerequisites(outputs, args.workload, args.substrate, args.min_claims)
    checks = (outputs["metrics"] or {}).get("checks", [])
    if failures:
        reason = "sweep not valid for a QoS verdict: " + "; ".join(failures)
        tests = {
            "bandwidth_meets_min": {"passed": False, "error": reason},
            "iops_meets_min": {"passed": False, "error": reason},
        }
    else:
        tests = {"iops_meets_min": _verdict(checks, "iops")}
        if args.workload == WORKLOAD_MDTEST:
            tests["bandwidth_meets_min"] = {
                "skipped": True,
                "skip_reason": "mdtest measures metadata operations per second, not bandwidth",
            }
        else:
            tests["bandwidth_meets_min"] = _verdict(checks, "bandwidth")

    # Cluster cleanup is not a QoS question -- a held Lease does not make the measurement
    # wrong -- so it never touches HSS02-01's verdict. But a run that leaves the PVC Lease,
    # Job or DaemonSet behind blocks the next run on that claim, so it fails this step (and
    # with it the run) and says what is still held. collect already retried the driver.
    collect = outputs.get("collect") or {}
    cleanup_errors = []
    if collect and collect.get("resources_released") is False:
        cleanup_errors.append(collect.get("leak_note") or "collect did not release cluster resources")
    elif collect and args.substrate == KUBECTL and collect.get("resources_released") is None:
        cleanup_errors.append(collect.get("leak_note") or "could not confirm cluster resources were released")
    ok = all(t.get("passed") or t.get("skipped") for t in tests.values()) and not cleanup_errors
    payload = {
        "success": ok,
        "test_name": "qos_throughput",
        "substrate": args.substrate,
        "workload": args.workload,
        "tests": tests,
        "prerequisites": prerequisites,
    }
    if not ok:
        # Both tests carry the same reason when a prerequisite failed; say it once.
        payload["error"] = "; ".join(
            dict.fromkeys([*(t["error"] for t in tests.values() if t.get("error")), *cleanup_errors])
        )
    if cleanup_errors:
        payload["cleanup_errors"] = cleanup_errors
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
