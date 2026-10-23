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

"""Run storage-scale-test's own environment validation before anything is dispatched.

``validate_env.sh`` is upstream's pre-flight. On kubectl it checks the launcher
prerequisites, the kube context, the namespace/PV/PVC *identity* (by UID, so a recreated
claim reusing a name is caught), the worker node selector, and -- the expensive one -- an
actual read/write of the PVC from the elbencho image. It exits 0 or 1 and prints its errors
under ``Validation failed with the following errors:``.

Why this is worth a step
------------------------
Every one of those failures is otherwise found *after* a submit, by the cluster, in a
diagnostics bundle. A ``TEST_DIRS`` entry that does not exist on the PVC produces a full
submit, a Job, a prepare failure, and a diagnostics directory -- reported as
``STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=API_UNAVAILABLE``, which sends you to look at the
Kubernetes API when the real problem was a missing directory.
``validate_env.sh`` names that class of problem in seconds, before anything is created.

Why it installs env.sh itself
-----------------------------
``validate_env.sh`` sources the checkout's ``env.sh`` -- the same hard-coded path the driver
uses -- so it validates *whatever env.sh is currently installed*. Run before
:mod:`submit_sweep` without installing, it would validate the PREVIOUS run's configuration
and report a confident pass on settings this run will never use. So this step installs the
composed env.sh through the very same :func:`submit_sweep.install_env_sh`, which is
deterministic in ``--config-hash``: submit then installs byte-identical content to the same
path, and the two steps cannot disagree.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import emit, fail, log, run, sst_version
from submit_sweep import install_env_sh

VALIDATOR = "validate_env.sh"
DIAG_PREFIX = "STORAGE_SCALE_TEST_DIAGNOSTIC_"
FAILURE_HEADER = "Validation failed with the following errors:"
WARNING_HEADER = "Validation notes / warnings:"
SUCCESS_LINE = "All validation checks passed successfully"


def parse_report(stdout: str) -> dict:
    """Split validate_env.sh's output into per-check results, warnings and errors.

    The format is positional, not keyed: a two-space-indented ``name: detail`` line per
    check, and two optional blocks introduced by fixed headers whose entries are indented
    further. Nothing here is a contract upstream publishes, so the parse is deliberately
    forgiving -- the authoritative signal is the EXIT CODE, and these fields exist to make a
    failure readable rather than to be matched on.
    """
    checks: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    diagnostics: dict[str, str] = {}
    section = "checks"
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith(FAILURE_HEADER):
            section = "errors"
            continue
        if line.startswith(WARNING_HEADER):
            section = "warnings"
            continue
        if line.strip() == SUCCESS_LINE:
            continue
        if line.strip().startswith(DIAG_PREFIX):
            # Upstream's diagnostics block is ~18 KEY=VALUE lines, most of them empty. It
            # belongs in a structured field, not inlined into `error` -- ACV repeats a
            # step's error verbatim in every failing validation's text, so a 2 KB error
            # becomes 4 KB of terminal and buries the one line that says what to do.
            key, _, value = line.strip().partition("=")
            value = value.strip().strip("'").replace("\\ ", " ")
            if value and value != "unknown":
                diagnostics[key[len(DIAG_PREFIX) :].lower()] = value
            continue
        if section == "errors":
            errors.append(line.strip())
        elif section == "warnings":
            warnings.append(line.strip())
        elif line.startswith("  ") and ":" in line:
            name, _, detail = line.strip().partition(":")
            checks.append({"name": name.strip(), "detail": detail.strip()})
    return {"checks": checks, "warnings": warnings, "errors": errors, "diagnostics": diagnostics}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scale-test-base", required=True)
    ap.add_argument(
        "--env-sh",
        required=True,
        help="overlay from gen_env_sh.py (steps.gen_env.env_sh_path). Installed "
        "before validating, so the checks see THIS run's configuration",
    )
    ap.add_argument("--config-hash", required=True, help="steps.gen_env.config_hash")
    ap.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="validate_env.sh runs a real PVC read/write from the elbencho "
        "image, including an image pull on a cold node",
    )
    ap.add_argument(
        "--warn-only",
        action="store_true",
        help="report what failed but pass the step anyway. For a cluster where "
        "a known check is expected to fail and the sweep is still worth "
        "attempting; NOT a default, because the whole point is to fail "
        "before anything is created",
    )
    args = ap.parse_args()

    base = os.path.abspath(args.scale_test_base)
    script = os.path.join(base, VALIDATOR)
    if not os.path.isfile(script):
        return fail(f"no {VALIDATOR} under --scale-test-base {base}")

    try:
        composed = install_env_sh(base, os.path.abspath(args.env_sh), args.config_hash)
    except (OSError, ValueError) as exc:
        return fail(f"could not install env.sh before validating: {exc}")
    log(f"installed env.sh -> {composed}")

    # --verbose is REQUIRED for checks_run, not a debugging nicety. storage-scale-test
    # v2.0.0 moved the per-check detail lines behind it (validate_env.sh:60 "Set by
    # --verbose: print a detail line for every check that passed"); without it only the
    # register_summary lines print, parse_report() finds no checks, and this step reports
    # checks_run=0 on a PASSING validation -- which the qos_throughput gate then
    # refuses (it requires `checks_run >= 1`). Before v2 the detail lines printed unconditionally.
    proc = run(["bash", script, "--verbose"], cwd=base, timeout=args.timeout)
    sys.stderr.write(proc.stderr)
    report = parse_report(proc.stdout)

    payload: dict = {
        "scale_test_base": base,
        "composed_env_sh": composed,
        "sst_version": sst_version(base),
        "exit_code": proc.returncode,
        "validated": True,
        "checks_run": len(report["checks"]),
        **report,
    }
    # The exit code is the authority; the parsed text is for humans. A validator that
    # changes its wording must not silently start passing.
    passed = proc.returncode == 0
    payload["preflight_passed"] = passed
    payload["success"] = passed or args.warn_only

    if not passed:
        # ONE line of cause plus the two diagnostics that tell an operator what to do.
        # The full text stays in `errors`/`diagnostics` for anyone who wants it.
        cause = (
            next((e for e in report["errors"] if not e.startswith(DIAG_PREFIX)), "")
            or (proc.stdout.strip().splitlines() or [""])[-1]
        )
        diag = report["diagnostics"]
        hint = diag.get("safe_next_action") or "fix the configuration and re-run"
        reason = f" [{diag['reason']}]" if diag.get("reason") else ""
        payload["error"] = (
            f"{VALIDATOR} failed{reason}: {cause[:200]}. Nothing was created on the cluster -- next: {hint}"
        )
        if args.warn_only:
            # Keep the reason visible even though the step passes, or --warn-only turns
            # into "the preflight silently does nothing".
            payload["warn_only_override"] = True
            log(f"warning: preflight FAILED but --warn-only was given: {payload['error']}")
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
