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

"""Turn a collected sweep into headline numbers and check them against thresholds.

`collect_sweep` stops at CSV *inventory* -- path, columns, row count -- because parsing
elbencho's output is a big job that upstream already did. This step reuses that work rather
than reimplementing it: it shells out to ``utils/extract-elbencho.py`` (the .out grammar, the
.csv/.out pairing, per-client outlier analysis, dedup of repeated operations).

Why shell out instead of import
-------------------------------
``extract-elbencho.py`` imports **matplotlib and numpy at module scope**. The ACV venv has
neither, and adding a plotting stack to ACV to read two numbers would be absurd. Upstream
0af71d8 removed the ``.sh`` wrappers and made the tool executable: its shebang plus built-in
bootstrap create/validate storage-scale-test's own venv and re-exec under it, so we invoke
the script directly and must NOT run it under ACV's interpreter. Neither upstream tool emits
JSON today, so we read back the ``elbencho-metrics.csv`` that ``--to-csv`` writes; a
``--json`` mode upstream would make this direct, and is worth asking for.

What the thresholds here are NOT
--------------------------------
**``FS_MAX_AGG_THROUGHPUT`` / ``FS_MAX_NODE_THROUGHPUT_GBPS`` / ``FS_MAX_NODE_IOPS`` are not
acceptance criteria.** They are *sizing inputs*: ``compute_target_file_count_per_thread``
(``lib/_elbencho_functions.sh:940-975``) uses them to pick a file count that keeps a run
roughly bounded in time, and ``env.sh.template:113-118`` says so outright. Asserting against
them would be circular -- raising the "threshold" changes the file count, which changes the
benchmark you are measuring.

So acceptance thresholds are **ACV-owned, separate, and expressed as floors**: the sweep must
reach *at least* this. They are named in **GB/s (decimal bytes)** throughout, deliberately,
because ``FS_MAX_NODE_THROUGHPUT_GBPS`` is in **Gbps (bits)** while ``FS_MAX_AGG_THROUGHPUT``
is in **GB/s (bytes)** -- an 8x trap sitting in two adjacent lines of the same file.
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
    ALL_WORKLOADS,
    BATCH_EXTRACTOR,
    BATCH_REPORTS_SUBDIR,
    WORKLOAD_IO,
    WORKLOAD_MDTEST,
    WORKLOADS,
    emit,
    fail,
    is_batch_dir,
    log,
    run,
    workload,
)

# extract-elbencho.py:4636 -- fixed name, written into the first input directory. Kept as a
# module constant because the io workload is the default and the name appears in operator-
# facing error text; the authoritative per-workload value is Workload.metrics_glob.
METRICS_CSV = "elbencho-metrics.csv"
EXTRACTOR = os.path.join("utils", "extract-elbencho.py")

MIB = 1024 * 1024


def mib_s_to_gb_s(mib_s: float) -> float:
    """MiB/s -> GB/s (decimal). Kept explicit: the repo mixes GB/s, Gbps and MiB/s."""
    return mib_s * MIB / 1_000_000_000


def extract(base: str, sweep_dir: str, timeout: int, kind: str = WORKLOAD_IO) -> str | None:
    """Run the workload's extractor over *sweep_dir*; return the CSV it wrote.

    A nonzero exit is not automatically fatal: the extractor can fail on plotting while
    still having written the CSV, and the CSV is all we want.

    The result is located by GLOB, not by name. ``extract-elbencho.py`` writes a fixed
    ``elbencho-metrics.csv``, but ``extract-mdtest-elbencho.py`` interpolates the sweep's
    datestamps into ``mdtest-elbencho-metrics-<stamps>.csv``
    (extract-mdtest-elbencho.py:2410-2415), so a fixed name would never match and this step
    would report "the extractor produced nothing" on a perfectly good run.
    """
    wl = workload(kind)
    script = wl.extractor_path(base)
    if not os.path.isfile(script):
        log(f"error: no extractor at {script}")
        return None
    proc = run([script, "--to-csv", sweep_dir], timeout=timeout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        log(f"note: extractor exited {proc.returncode}; checking for {wl.metrics_glob} anyway")
    found = sorted(glob.glob(os.path.join(sweep_dir, wl.metrics_glob)))
    if not found:
        return None
    if len(found) > 1:
        # Re-running the extractor after a --resume adds a second datestamp and so a second
        # filename; the newest is the one that saw every execution.
        found.sort(key=os.path.getmtime)
        log(
            f"note: {len(found)} roll-up CSVs match {wl.metrics_glob}; using the newest ({os.path.basename(found[-1])})"
        )
    return found[-1]


def extract_batch(base: str, batch_root: str, timeout: int, kind: str, groups: str | None) -> list[str]:
    """Report a prepared batch and return the roll-up CSVs, one per reported group.

    A batch is NOT a results directory. Its executions live under
    ``groups/<NNNN>/<kind>-<DS>/`` and which of them are committed is defined by
    ``batch-manifest.tsv``, never by walking the tree (``lib/_batch_functions.sh:19-20``).
    The per-workload engines have no idea about any of that, so pointing one at a batch
    root reports nothing and this step would say "the extractor produced no CSV" about a
    batch full of good results.

    ``--kind all`` is passed for a mixed batch so BOTH engines run, each over its own
    groups. Per-group CSVs are returned separately rather than merged: an io group's rows
    and an mdtest group's rows have no columns in common, and concatenating them would
    produce a table where every row is mostly empty.
    """
    script = os.path.join(base, BATCH_EXTRACTOR)
    if not os.path.isfile(script):
        log(f"error: no batch reporter at {script}")
        return []
    argv = [script, batch_root, "--to-csv", "--kind", kind]
    if groups:
        argv += ["--groups", groups]
    proc = run(argv, timeout=timeout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        log(f"note: batch reporter exited {proc.returncode}; checking for CSVs anyway")
    found: list[str] = []
    for wl in WORKLOADS.values():
        found += glob.glob(os.path.join(batch_root, BATCH_REPORTS_SUBDIR, "*", wl.metrics_glob))
    return sorted(found)


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except ValueError:
        return 0.0


def _i(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except ValueError:
        return 0


def load_metrics(path: str) -> list[dict]:
    """Read the extractor's CSV into the handful of fields a validation cares about.

    Field names come from ``ElbenchoMetrics`` (``extract-elbencho.py:157``), written via
    ``DictWriter(fieldnames=[f.name for f in fields(ElbenchoMetrics)])``, so the header is
    the dataclass. Throughput is recomputed from ``throughput_mib_s`` rather than trusting
    ``throughput_gbps``, which is Gb/s (bits) despite sitting beside MB/s and MiB/s columns.
    """
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for raw in csv.DictReader(fh):
            nodes = _i(raw, "nodes")
            mib_s = _f(raw, "throughput_mib_s")
            gb_s = mib_s_to_gb_s(mib_s)
            rows.append(
                {
                    "operation": (raw.get("operation") or "").upper(),
                    "nodes": nodes,
                    "threads": _i(raw, "threads"),
                    "io_size": raw.get("io_size") or "",
                    "io_depth": _i(raw, "io_depth"),
                    "iops": _f(raw, "iops"),
                    "throughput_mib_s": mib_s,
                    "agg_gb_s": round(gb_s, 3),
                    "per_node_gb_s": round(gb_s / nodes, 3) if nodes else 0.0,
                }
            )
    return rows


def headline(rows: list[dict]) -> dict:
    """Best observed result per operation.

    "Best" is the point of a sweep: it walks node counts and thread counts precisely to find
    where the filesystem tops out, so the max is the meaningful summary, not the mean. The
    winning row is reported alongside so a regression can be attributed to a shape.
    """
    out: dict = {}
    for op in ("WRITE", "READ"):
        subset = [r for r in rows if r["operation"] == op]
        if not subset:
            continue
        best_agg = max(subset, key=lambda r: r["agg_gb_s"])
        best_node = max(subset, key=lambda r: r["per_node_gb_s"])
        best_iops = max(subset, key=lambda r: r["iops"])
        out[op.lower()] = {
            "samples": len(subset),
            "best_agg_gb_s": best_agg["agg_gb_s"],
            "best_agg_at": {k: best_agg[k] for k in ("nodes", "threads", "io_size", "io_depth")},
            "best_per_node_gb_s": best_node["per_node_gb_s"],
            "best_iops": round(best_iops["iops"], 1),
        }
    return out


# ---------------------------------------------------------------- mdtest
#
# A metadata sweep measures a different thing from an IO sweep, and the difference is not
# cosmetic: elbencho reports BANDWIDTH, mdtest reports OPERATION RATES for three distinct
# phases (create, stat, delete) plus their latency distributions. There is no GB/s here at
# all, so the io thresholds are not merely unset for mdtest -- they are meaningless, and
# silently accepting them would let a config "assert" something that can never be measured.
#
# Column names come from `write_csv_export` (extract-mdtest-elbencho.py:1474-1510).
MDTEST_PHASES = ("create", "stat", "delete")


def load_mdtest_metrics(path: str) -> list[dict]:
    """Read the mdtest roll-up into per-(node,thread) rows."""
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for raw in csv.DictReader(fh):
            row = {
                "nodes": _i(raw, "node_count"),
                "threads": _i(raw, "thread_count"),
                "iterations": _i(raw, "iteration_count"),
            }
            for phase in MDTEST_PHASES:
                row[f"{phase}_ops_s"] = _f(raw, f"{phase}_rate_avg")
                # p99 is the tail an operator actually feels; p50 alone hides a stall.
                row[f"{phase}_lat_p99_us"] = _f(raw, f"{phase}_lat_p99")
            rows.append(row)
    return rows


def mdtest_headline(rows: list[dict]) -> dict:
    """Best observed rate per phase, with the shape that achieved it.

    Max, not mean, for the same reason as the io sweep: walking node and task counts is how
    a sweep finds where metadata tops out, so the peak is the summary. The latency reported
    beside it is the latency OF THAT ROW, not the best latency seen anywhere -- pairing a
    peak rate with someone else's tail would describe a run that never happened.
    """
    out: dict = {}
    for phase in MDTEST_PHASES:
        key = f"{phase}_ops_s"
        subset = [r for r in rows if r.get(key)]
        if not subset:
            continue
        best = max(subset, key=lambda r: r[key])
        out[phase] = {
            "samples": len(subset),
            "best_ops_s": round(best[key], 1),
            "best_at": {"nodes": best["nodes"], "threads": best["threads"]},
            "lat_p99_us_at_best": round(best[f"{phase}_lat_p99_us"], 1),
        }
    return out


def ceiling(name: str, observed: float | None, cap: float | None) -> dict | None:
    """One upper-bound comparison, for latency. Mirror image of :func:`check`.

    Latency is the one metric here where bigger is worse, so it needs its own comparison.
    Expressing it as a floor on 1/latency would be arithmetic cleverness at the cost of an
    error message nobody can read.
    """
    if cap is None:
        return None
    if observed is None:
        return {
            "name": name,
            "passed": False,
            "cap": cap,
            "observed": None,
            "detail": "no measurement for this phase -- the sweep produced no such rows",
        }
    return {
        "name": name,
        "passed": observed <= cap,
        "cap": cap,
        "observed": observed,
        "detail": f"{observed} us vs ceiling {cap} us",
    }


def _threshold(value: str) -> float | None:
    """A ``--min-*``/``--max-*`` value. Empty means "not configured", not zero.

    Configs pass thresholds from ``tests.settings`` (``"{{ min_write_iops }}"``), and a
    setting left blank -- a substrate with no baseline yet -- renders as ``""``. Treating that
    as unset lets qos_throughput fail closed with "no minimum configured" instead of this step
    crashing on ``float("")``.
    """
    value = value.strip()
    return float(value) if value else None


def check(name: str, observed: float | None, floor: float | None) -> dict | None:
    """One threshold comparison. ``None`` floor means "not configured", not "passes"."""
    if floor is None:
        return None
    if observed is None:
        return {
            "name": name,
            "passed": False,
            "floor": floor,
            "observed": None,
            "detail": "no measurement for this operation -- the sweep produced no such rows",
        }
    return {
        "name": name,
        "passed": observed >= floor,
        "floor": floor,
        "observed": observed,
        "detail": f"{observed} GB/s vs floor {floor} GB/s" if "gb_s" in name else f"{observed} vs floor {floor}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep-dir", required=True, help="steps.submit_sweep.sweep_dir")
    ap.add_argument("--scale-test-base", help="storage-scale-test checkout (omit with --from-csv)")
    ap.add_argument("--from-csv", help="skip extraction and read this roll-up CSV")
    ap.add_argument(
        "--workload",
        default=WORKLOAD_IO,
        choices=ALL_WORKLOADS,
        help="io = elbencho bandwidth/IOPS; mdtest = metadata create/stat/"
        "delete rates. Selects both the extractor and the thresholds; the "
        "two sets are not interchangeable",
    )
    ap.add_argument("--extractor-timeout", type=int, default=1800)
    ap.add_argument(
        "--groups",
        help="prepared-batch group ids to report, comma separated (e.g. "
        "'0001,0003'). Batch inputs only; ignored for a plain sweep",
    )
    ap.add_argument(
        "--substrate",
        choices=("slurm", "ssh", "kubectl"),
        help="substrate this sweep ran on; sets `platform` in the output. "
        "Without it the payload falls back to 'unknown', because ACV's "
        "`env:` is per-step and this step inherits no SST_SUBSTRATE.",
    )
    # Acceptance thresholds -- floors, ACV-owned. Deliberately NOT the FS_MAX_* sizing knobs.
    ap.add_argument("--min-write-gb-s", type=_threshold)
    ap.add_argument("--min-read-gb-s", type=_threshold)
    ap.add_argument("--min-node-write-gb-s", type=_threshold)
    ap.add_argument("--min-node-read-gb-s", type=_threshold)
    ap.add_argument("--min-write-iops", type=_threshold)
    ap.add_argument("--min-read-iops", type=_threshold)
    # mdtest thresholds. Rates are ops/s floors; latencies are microsecond ceilings.
    ap.add_argument("--min-create-ops-s", type=_threshold)
    ap.add_argument("--min-stat-ops-s", type=_threshold)
    ap.add_argument("--min-delete-ops-s", type=_threshold)
    ap.add_argument("--max-create-lat-p99-us", type=_threshold)
    ap.add_argument("--max-stat-lat-p99-us", type=_threshold)
    ap.add_argument("--max-delete-lat-p99-us", type=_threshold)
    args = ap.parse_args()

    io_thresholds = (
        "min_write_gb_s",
        "min_read_gb_s",
        "min_node_write_gb_s",
        "min_node_read_gb_s",
        "min_write_iops",
        "min_read_iops",
    )
    mdtest_thresholds = (
        "min_create_ops_s",
        "min_stat_ops_s",
        "min_delete_ops_s",
        "max_create_lat_p99_us",
        "max_stat_lat_p99_us",
        "max_delete_lat_p99_us",
    )
    # Applicability is enforced AFTER extraction, against what was actually measured --
    # see the `unmeasurable` check below. A prepared batch can hold both kinds, so
    # --workload alone cannot decide which thresholds are legitimate. What must never
    # happen is a threshold being silently IGNORED: that leaves `asserted: true` with
    # nothing about the operator's number ever evaluated, which is the vacuous pass this
    # step's `asserted` flag exists to prevent.

    sweep_dir = os.path.abspath(args.sweep_dir)
    if not os.path.isdir(sweep_dir):
        return fail(f"no such sweep dir: {sweep_dir}")

    batch = is_batch_dir(sweep_dir)
    if batch and args.from_csv:
        # Upstream refuses this combination outright (filesystem_batch.py:435-438) because
        # one CSV cannot stand for a batch's several groups. Say so here rather than
        # reporting a batch's thresholds from a single group's numbers.
        return fail(
            "--from-csv cannot report a prepared batch: a batch has one roll-up "
            "per group. Point --sweep-dir at a single group's results directory, "
            "or drop --from-csv",
            sweep_dir=sweep_dir,
            batch=True,
        )

    csvs: list[str] | None = None
    if args.from_csv:
        csv_path: str | None = args.from_csv
        if not os.path.isfile(csv_path or ""):
            return fail(f"no such CSV: {args.from_csv}")
    else:
        if not args.scale_test_base:
            return fail("--scale-test-base is required unless --from-csv is given")
        wl = workload(args.workload)
        abs_base = os.path.abspath(args.scale_test_base)
        if batch:
            # `--kind all` reports every committed group with its own engine. Narrowing to
            # the run's --workload here would silently drop the other half of a mixed batch
            # and then report thresholds over a partial result set.
            csvs = extract_batch(abs_base, sweep_dir, args.extractor_timeout, "all", args.groups)
            if not csvs:
                return fail(
                    f"the batch reporter produced no roll-up CSV under "
                    f"{os.path.join(sweep_dir, BATCH_REPORTS_SUBDIR)}. Run it by hand to "
                    f"see why: {os.path.join(abs_base, BATCH_EXTRACTOR)} {sweep_dir} "
                    "--to-csv --kind all",
                    sweep_dir=sweep_dir,
                    workload=args.workload,
                    batch=True,
                )
            # Each group is reported by its own engine, so the WORKLOAD of a CSV is told by
            # which engine's filename pattern it matches -- not by this run's --workload,
            # which in a mixed batch describes only the first group.
            csv_path = None
        else:
            csvs = None
            csv_path = extract(abs_base, sweep_dir, args.extractor_timeout, args.workload)
            if csv_path is None:
                return fail(
                    f"the extractor produced no {wl.metrics_glob} under {sweep_dir}. Run it "
                    f"by hand to see why: {wl.extractor_path(args.scale_test_base)} "
                    f"--to-csv {sweep_dir}",
                    sweep_dir=sweep_dir,
                    workload=args.workload,
                )

    # ------------------------------------------------------------------ measurements
    # Keyed by workload, because a prepared batch can hold BOTH kinds and their rows share
    # no columns. A single `rows` list was fine while one step meant one workload; it
    # cannot describe a mixed batch without silently dropping half of it.
    LOADERS = {WORKLOAD_IO: load_metrics, WORKLOAD_MDTEST: load_mdtest_metrics}
    measured: dict[str, list[dict]] = {}
    csv_by_kind: dict[str, str] = {}

    def _load(kind: str, path: str) -> str | None:
        try:
            loaded = LOADERS[kind](path)
        except OSError as exc:
            return f"could not read {path}: {exc}"
        if loaded:
            measured.setdefault(kind, []).extend(loaded)
            csv_by_kind.setdefault(kind, path)
        return None

    if csvs is not None:
        # Batch: the engine that produced a CSV is identified by its filename pattern, not
        # by this run's --workload -- which in a mixed batch describes only the first group.
        for path in csvs:
            name = os.path.basename(path)
            for kind, wl_ in WORKLOADS.items():
                if fnmatch.fnmatch(name, wl_.metrics_glob):
                    problem = _load(kind, path)
                    if problem:
                        return fail(problem, sweep_dir=sweep_dir, batch=True)
                    break
            else:
                log(f"note: ignoring {name}: it matches no known roll-up pattern")
    else:
        problem = _load(args.workload, csv_path or "")
        if problem:
            return fail(problem)

    if not measured:
        where = csv_path or os.path.join(sweep_dir, BATCH_REPORTS_SUBDIR)
        return fail(
            f"{where} parsed to zero metrics -- the run produced no usable .out/.csv pairs",
            sweep_dir=sweep_dir,
            workload=args.workload,
            batch=batch,
        )

    # ------------------------------------------------------------------ thresholds
    # Applicability is decided by WHAT WAS MEASURED, not by --workload. On a plain sweep
    # those are the same thing; on a mixed batch both threshold sets are legitimate, and on
    # a single-kind batch the other set must still be refused rather than ignored.
    configured = {
        WORKLOAD_IO: [n for n in io_thresholds if getattr(args, n, None) is not None],
        WORKLOAD_MDTEST: [n for n in mdtest_thresholds if getattr(args, n, None) is not None],
    }
    unmeasurable = [
        ("--" + n.replace("_", "-")) for kind, names in configured.items() if kind not in measured for n in names
    ]
    if unmeasurable:
        return fail(
            f"{', '.join(unmeasurable)} cannot be measured here: this run produced "
            f"{', '.join(sorted(measured))} results only. An io sweep reports bandwidth and "
            "IOPS, an mdtest sweep reports operation rates and latencies; neither has a "
            "value for the other's thresholds",
            sweep_dir=sweep_dir,
            workloads_measured=sorted(measured),
        )

    by_workload: dict[str, dict] = {}
    checks: list[dict] = []
    phases_measured: list[str] | None = None

    if WORKLOAD_IO in measured:
        rows_io = measured[WORKLOAD_IO]
        head_io = headline(rows_io)
        w = head_io.get("write", {})
        r = head_io.get("read", {})
        checks += [
            c
            for c in (
                check("write_agg_gb_s", w.get("best_agg_gb_s"), args.min_write_gb_s),
                check("read_agg_gb_s", r.get("best_agg_gb_s"), args.min_read_gb_s),
                check("write_per_node_gb_s", w.get("best_per_node_gb_s"), args.min_node_write_gb_s),
                check("read_per_node_gb_s", r.get("best_per_node_gb_s"), args.min_node_read_gb_s),
                check("write_iops", w.get("best_iops"), args.min_write_iops),
                check("read_iops", r.get("best_iops"), args.min_read_iops),
            )
            if c is not None
        ]
        by_workload[WORKLOAD_IO] = {
            "metrics_csv": csv_by_kind[WORKLOAD_IO],
            "samples": len(rows_io),
            "headline": head_io,
        }

    if WORKLOAD_MDTEST in measured:
        rows_md = measured[WORKLOAD_MDTEST]
        head_md = mdtest_headline(rows_md)
        # Flat, first-class fields rather than leaving a validation to walk into `headline`.
        # `FieldExistsCheck` does a plain `f not in step_output` (generic.py:52) -- it has
        # NO dotted-path support, unlike `FieldValueCheck` (_get_field_value). A validation
        # written as `headline.create.best_ops_s` therefore reports every phase missing even
        # when all three were measured.
        phases_measured = [p for p in MDTEST_PHASES if p in head_md]
        floors = {"create": args.min_create_ops_s, "stat": args.min_stat_ops_s, "delete": args.min_delete_ops_s}
        caps = {
            "create": args.max_create_lat_p99_us,
            "stat": args.max_stat_lat_p99_us,
            "delete": args.max_delete_lat_p99_us,
        }
        checks += [
            c
            for c in [
                x
                for phase in MDTEST_PHASES
                for x in (
                    check(f"{phase}_ops_s", head_md.get(phase, {}).get("best_ops_s"), floors[phase]),
                    ceiling(f"{phase}_lat_p99_us", head_md.get(phase, {}).get("lat_p99_us_at_best"), caps[phase]),
                )
            ]
            if c is not None
        ]
        by_workload[WORKLOAD_MDTEST] = {
            "metrics_csv": csv_by_kind[WORKLOAD_MDTEST],
            "samples": len(rows_md),
            "headline": head_md,
            "phases_measured": phases_measured,
        }

    # ------------------------------------------------------------------ payload
    kinds = sorted(measured)
    mixed = len(kinds) > 1
    all_rows = [row for kind in kinds for row in measured[kind]]
    failed = [c for c in checks if not c["passed"]]
    payload = {
        "success": not failed,
        "sweep_dir": sweep_dir,
        "workload": args.workload,
        "batch": batch,
        "workloads_measured": kinds,
        "mixed_workloads": mixed,
        # emit() derives `platform` from this; without it the payload says "unknown".
        **({"substrate": args.substrate} if args.substrate else {}),
        # Single-workload runs keep the EXACT top-level shape every existing validation
        # already asserts on. A mixed batch has no single headline to put here, so it gets
        # an empty one and the real numbers live under `by_workload` -- which FieldValueCheck
        # can reach, since it walks dotted paths.
        "metrics_csv": csv_by_kind[kinds[0]] if not mixed else None,
        "samples": len(all_rows),
        "headline": by_workload[kinds[0]]["headline"] if not mixed else {},
        "by_workload": by_workload,
        **(
            {
                "phases_measured": phases_measured,
                # A partial sweep can produce create without stat or delete. A floor on create
                # alone would pass that run, so the completeness of the phase set is asserted
                # as its own boolean.
                "all_phases_measured": phases_measured == list(MDTEST_PHASES),
            }
            if phases_measured is not None
            else {}
        ),
        "checks": checks,
        "measurements": all_rows,
    }
    if not checks:
        # Loud, because a run with no thresholds configured passes no matter how bad it was.
        log(
            "warning: no thresholds configured -- this step measured the run but asserted "
            "nothing. Pass --min-* to make it a real check."
        )
        payload["asserted"] = False
    else:
        payload["asserted"] = True
    if failed:
        payload["error"] = "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
