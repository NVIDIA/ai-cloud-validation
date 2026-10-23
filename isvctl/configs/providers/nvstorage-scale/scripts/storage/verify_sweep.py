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

"""Verify a collected sweep ran everything it was configured to run.

The gap this closes
-------------------
Every other step in this provider reasons forward from what the sweep produced.
``poll_sweep`` reads execution sentinels, ``collect_sweep`` counts published CSVs and
checks each reified execution reached a terminal status, ``metrics`` parses whatever rows
the extractor emitted. All of them answer "is what exists healthy?". None of them answers
"is what exists *all of it*?" -- because none reads the configuration back.

``utils/summarize-elbencho.py`` is the only upstream tool that closes that loop. It
rebuilds the expected cell matrix from the sweep's own ``env_used.yaml``
(``nodes_spec`` x ``ELBENCHO_SCALE_IO_SIZES`` x ``ELBENCHO_SCALE_THREAD_LIST`` x
``ELBENCHO_IODEPTH_LIST``), renders the filename each cell must produce, and diffs that
set against the ``elbencho-*-c_*-s_*-d_*_<DS>.out`` files on disk. A sweep that
reified fewer executions than it was configured for -- so every execution that exists is
SUCCESS and every CSV that exists parses -- is invisible to collect and to metrics, and
is exactly what this catches.

Why the exit code is worthless here
-----------------------------------
``summarize-elbencho.py`` **always returns 0** (``main()``, last line). Not a directory,
no ``env_used.yaml``, an mdtest snapshot, an unparsable ``nodes_spec`` -- each is a
``_eprint`` warning and a ``continue``, and an INCOMPLETE sweep is a *banner in stdout*.
So ``returncode == 0`` means only "the interpreter started"; it is equally true of a run
that examined the sweep and found it whole and of a run that skipped it entirely.

This step therefore requires POSITIVE evidence: a summary block must come back naming the
directory, and the absence of a block is a failure, not a pass. That inversion is the
whole point of the file -- the obvious wrapper (``success = proc.returncode == 0``) would
report green on every sweep in existence, including ones it never looked at.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import (
    ALL_WORKLOADS,
    SUMMARIZER,
    WORKLOAD_IO,
    WORKLOAD_MDTEST,
    emit,
    fail,
    find_group_dirs,
    is_batch_dir,
    log,
    run,
    sst_version,
)

# Upstream's banners, verbatim (utils/summarize-elbencho.py:_INCOMPLETE_TAG and
# _INCOMPLETE_LINE23_PREFIX). The tag is prepended to line 1; the prefix marks lines 2-3.
INCOMPLETE_TAG = "*** INCOMPLETE RUN ***"

# The two independent reasons upstream calls a run incomplete. They are checked in this
# order and only ONE is ever reported -- `_completeness_note` returns the execution-status
# note first and never reaches the file diff -- so a sweep can be both and say only the
# first. Both shapes are parsed here so whichever arrives becomes a number.
_MISSING_RE = re.compile(r"missing (\d+)/(\d+) ")
_EXECUTIONS_RE = re.compile(r"(\d+)/(\d+) executions not SUCCESS")

# The basename is NOT the first token of line 1: upstream PREPENDS the banner, so an
# incomplete run reads `*** INCOMPLETE RUN *** missing 1/2 <glob> elbencho-<DS> dur: ...`
# and taking the first word yields "missing". The basename is the token immediately before
# the duration field, which `_format_three_lines` always emits as `dur: ` or, for a
# completion-based shared-directory run, `configured_dur: `.
_NAME_RE = re.compile(r"(\S+) (?:configured_)?dur: ")

# The benchmark output files the completeness check counts, named by
# `_BENCH_OUT_BASENAME_RE` upstream. Counted here only to learn how many cells DID produce
# output: the expected total appears in the banner, and the banner exists only when
# something is missing, so a complete sweep would otherwise report "0 expected" and make a
# non-emptiness assertion fail on precisely the healthy case.
_BENCH_OUT_RE = re.compile(r"^elbencho-.+-c_\d+-s_\d+-d_\d+_\d{8}Z\d{6}\.out$")


def count_bench_outs(result_dir: str) -> int:
    """How many benchmark .out files a result directory actually holds."""
    try:
        return sum(1 for n in os.listdir(result_dir) if _BENCH_OUT_RE.match(n))
    except OSError:
        return 0


def expected_cells(block: dict, result_dir: str) -> int:
    """How many cells this result directory was CONFIGURED to run.

    Upstream never prints the configured total on its own; it appears only inside an
    INCOMPLETE banner, and which banner fires decides which number is available.
    `_completeness_note` returns the execution-status note FIRST and never reaches the
    file diff, so the three cases are genuinely different and none can stand in for
    another:

    * file-diff banner    -> `missing N/M`, and M is the configured total outright.
    * execution banner    -> `N/M executions not SUCCESS`, and M is the reified execution
      count. `outputs_missing` is 0 here even though cells ARE missing, because the file
      diff never ran -- so deriving the total as present+missing understates it. A sweep
      cancelled after 5 of 8 cells reports 3/8 executions not SUCCESS with 6 .out files
      present, which that arithmetic would call a matrix of 6.
    * no banner (complete) -> every configured cell produced a file, so the files on disk
      ARE the matrix.
    """
    if "outputs_expected" in block:
        return block["outputs_expected"]
    if "executions_total" in block:
        return block["executions_total"]
    return count_bench_outs(result_dir)


def parse_summary(stdout: str) -> list[dict]:
    """Split the summarizer's three-line blocks into one record per result directory.

    The format is positional: line 1 is ``[banner ]<basename> dur: ... nodes: ...``, lines
    2 and 3 are indented and carry an ``[INCOMPLETE]`` prefix when line 1 had the banner.
    Only line 1 is parsed for meaning; lines 2-3 are kept verbatim as the human-readable
    shape of the run, because they are the part an operator actually wants to read.
    """
    blocks: list[dict] = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("    "):
            if blocks:
                blocks[-1]["detail"].append(raw.strip())
            continue
        line = raw.strip()
        complete = not line.startswith(INCOMPLETE_TAG)
        banner = ""
        if not complete:
            line = line[len(INCOMPLETE_TAG) :].strip()
            banner = raw.strip()
        name_match = _NAME_RE.search(line)
        record: dict = {
            "name": name_match.group(1) if name_match else line.split(" ", 1)[0],
            "complete": complete,
            "summary": line,
            "detail": [],
        }
        if not complete:
            record["banner"] = banner
            missing = _MISSING_RE.search(banner)
            if missing:
                record["outputs_missing"] = int(missing.group(1))
                record["outputs_expected"] = int(missing.group(2))
            execs = _EXECUTIONS_RE.search(banner)
            if execs:
                record["executions_not_success"] = int(execs.group(1))
                record["executions_total"] = int(execs.group(2))
        blocks.append(record)
    return blocks


def parse_warnings(stderr: str) -> list[str]:
    """The summarizer's skip reasons, which are the ONLY trace of a directory it ignored.

    Every skip in `_summarize_directory` prints ``Warning: <path>: <reason>; skipping`` and
    returns None, leaving no gap in stdout to notice. Carried into the payload so that a
    sweep this step could not verify says why rather than merely saying nothing.
    """
    return [line.strip() for line in stderr.splitlines() if line.strip().startswith("Warning:")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--sweep-dir",
        required=True,
        help="a collected result directory, or a batch root (its io groups are then verified individually)",
    )
    ap.add_argument("--scale-test-base", required=True)
    ap.add_argument("--workload", default=WORKLOAD_IO, choices=ALL_WORKLOADS)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="report what is missing but pass the step anyway. For a sweep "
        "deliberately cut short -- a cancel/resume cycle leaves cells "
        "unrun by design -- where the metrics are still wanted",
    )
    args = ap.parse_args()

    base = os.path.abspath(args.scale_test_base)
    sweep_dir = os.path.abspath(args.sweep_dir)

    # Refused rather than attempted: the summarizer would skip an mdtest snapshot with a
    # stderr warning and exit 0, which this step would then have to report as "verified
    # nothing". Saying so here names the real constraint instead of a missing block.
    if args.workload == WORKLOAD_MDTEST:
        return fail(
            "completeness verification is IO-only: summarize-elbencho.py rebuilds the "
            "expected matrix from an nv-elbencho IO sweep's env_used.yaml and rejects an "
            "mdtest snapshot outright ('looks like mdtest-elbencho, not nv-elbencho IO "
            "sweep'). Upstream ships no mdtest equivalent, so there is nothing to call. "
            "mdtest completeness is covered only by the execution sentinels that "
            "collect already checks",
            sweep_dir=sweep_dir,
            workload=args.workload,
            verified=False,
        )

    script = os.path.join(base, SUMMARIZER)
    if not os.path.isfile(script):
        return fail(f"no {SUMMARIZER} under --scale-test-base {base}")
    if not os.path.isdir(sweep_dir):
        return fail(f"--sweep-dir {sweep_dir} is not a directory")

    batch = is_batch_dir(sweep_dir)
    if batch:
        # A batch root holds no env_used.yaml of its own; the per-group result dirs do.
        targets = find_group_dirs(sweep_dir, WORKLOAD_IO)
        if not targets:
            return fail(
                f"{sweep_dir} is a batch root with no io group directories under "
                "groups/NNNN/. A batch of only mdtest groups has nothing this step can "
                "verify; drop the verify step from an mdtest-only batch config",
                sweep_dir=sweep_dir,
                batch=True,
                verified=False,
            )
    else:
        targets = [sweep_dir]

    proc = run(["python3", script] + targets, cwd=base, timeout=args.timeout)
    sys.stderr.write(proc.stderr)
    blocks = parse_summary(proc.stdout)
    warnings = parse_warnings(proc.stderr)

    payload: dict = {
        "sweep_dir": sweep_dir,
        "scale_test_base": base,
        "sst_version": sst_version(base),
        "workload": WORKLOAD_IO,
        "batch": batch,
        "exit_code": proc.returncode,
        "directories_requested": len(targets),
        "directories_summarized": len(blocks),
        "warnings": warnings,
        "results": blocks,
    }

    # The inversion this step exists for. A directory the summarizer declined to read
    # produces no block and no non-zero exit; without this, that is indistinguishable
    # from a clean bill of health.
    if len(blocks) != len(targets):
        skipped = sorted({os.path.basename(t) for t in targets} - {b["name"] for b in blocks})
        reason = warnings[0] if warnings else "no reason was printed"
        payload["verified"] = False
        payload["success"] = False
        payload["error"] = (
            f"summarize-elbencho.py returned no summary for {len(targets) - len(blocks)} "
            f"of {len(targets)} directory(ies) ({', '.join(skipped) or 'unknown'}) and "
            f"still exited 0 -- it skips what it cannot read. First reason: {reason}. "
            "This is NOT a pass: nothing was verified. If env_used.yaml is simply absent, "
            "upstream's utils/reconstruct_elbencho_env_used.py can rebuild one from the "
            "*.out/*.log files -- but run it by hand and read the result, because a "
            "configuration derived FROM the outputs and then checked AGAINST those same "
            "outputs is circular and would report complete no matter what ran"
        )
        return emit(payload)

    payload["verified"] = True
    incomplete = [b for b in blocks if not b["complete"]]
    payload["complete"] = not incomplete
    payload["incomplete_count"] = len(incomplete)
    payload["outputs_missing"] = sum(b.get("outputs_missing", 0) for b in incomplete)
    payload["outputs_present"] = sum(count_bench_outs(t) for t in targets)
    # Paired by NAME, not by position. `find_group_dirs` returns targets in group order
    # (groups/0001, 0002, ...); the summarizer RE-SORTS its argv by basename
    # (`_sort_key`, summarize-elbencho.py:471), which is the `<prefix>-<DS>` datestamp.
    # Those two orders agree only while result-dir datestamps happen to ascend with group
    # number. When they do not, a positional zip hands `expected_cells` a block from one
    # group and a directory from another -- and its no-banner fallback then counts the
    # wrong directory's .out files. That bites a mixed batch where some groups are
    # complete and some are not, which is exactly the batch worth verifying.
    by_name = {b["name"]: b for b in blocks}
    if len(by_name) == len(blocks) and all(os.path.basename(t) in by_name for t in targets):
        pairs = [(by_name[os.path.basename(t)], t) for t in targets]
    else:
        # A block whose basename could not be parsed back to a target. Positional order is
        # then the only pairing available; say so rather than reporting a silent guess.
        log(
            "warning: could not match every summary block to its result directory by "
            "name; falling back to positional pairing for outputs_expected"
        )
        payload["pairing"] = "positional"
        pairs = list(zip(blocks, targets))
    payload.setdefault("pairing", "by-name")
    payload["outputs_expected"] = sum(expected_cells(b, t) for b, t in pairs)
    payload["success"] = payload["complete"] or args.allow_incomplete

    if incomplete:
        first = incomplete[0]
        payload["error"] = (
            f"{len(incomplete)} of {len(blocks)} result directory(ies) did not run the "
            f"full configured matrix: {first.get('banner', first['summary'])}. Every "
            "execution that exists may still be SUCCESS -- this compares the output "
            "files against the cell matrix in env_used.yaml, which is the only check "
            "that reads the configuration back"
        )
        if args.allow_incomplete:
            payload["allow_incomplete_override"] = True
            log(f"warning: sweep INCOMPLETE but --allow-incomplete was given: {payload['error']}")
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
