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

"""Install the generated env.sh and start a sweep asynchronously.

Why this is not just ``subprocess.run(driver)``
-----------------------------------------------
ACV runs every step with ``capture_output=True`` and waits. A 12-hour elbencho sweep under
that shape buffers twelve hours of output to nothing, holds a short-lived cluster credential well past
its ~2h lifetime, and loses everything if the laptop sleeps. So this step *starts* the
sweep, waits only for the durable handle to appear, and returns. ``poll_sweep`` and
``collect_sweep`` re-attach later from a fresh session.

Two things tracing the driver turned up that shaped this file:

* **``nv-elbencho-sweep.sh`` sources a hard-coded path** --- ``"${SCRIPT_DIR}/../../env.sh"``
  (the validation subshell and the top-level ``source``, :435 and :441 as of b3ab646). There
  is no flag to point it elsewhere. So a generated env.sh has to *become*
  the repo's ``env.sh``; see :func:`install_env_sh`.
* **``gen_env_sh.py`` emits an overlay, not a whole env.sh.** By design it writes only the
  variables the caller supplied, so it has no ``SCALE_TEST_BASE`` and never chains to
  ``lib/env_base.sh``. Sourced on its own it would leave the driver with no library at all.
  :func:`compose_env_sh` splices it into ``env.sh.template`` instead.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import (
    ALL_SUBSTRATES,
    ALL_WORKLOADS,
    KUBECTL,
    SLURM,
    TERMINAL,
    WORKLOAD_IO,
    WORKLOAD_MDTEST,
    batch_is_sealed,
    emit,
    fail,
    find_sweep_dirs,
    log,
    read_coordinator_jobid,
    read_statuses,
    run,
    sst_version,
    workload,
)

# The last line of env.sh.template; everything the caller overrides must land *before* it,
# so that lib/env_base.sh derives its values from the overridden ones.
CHAIN_LINE = 'source "${SCALE_TEST_BASE}/lib/env_base.sh"'

# Kept as a module constant because the io sweep is still the default workload and several
# call sites read it; `--workload mdtest` resolves its own driver through sweeplib.WORKLOADS.
DRIVER = os.path.join("storage-tests", "fs", "nv-elbencho-sweep.sh")

# Flags that exist ONLY on nv-elbencho-sweep.sh. nv-mdtest-elbencho.sh accepts exactly
# --nodes/--tasks/--single-dir-file-target/--env-override plus the lifecycle verbs
# (nv-mdtest-elbencho.sh:89) and rejects anything else with "unexpected argument". Passing
# one of these through would fail inside a detached process, where the only evidence is a
# launch log nobody reads -- so they are refused here, by name, before anything starts.
IO_ONLY_FLAGS = ("--mode", "--read-from", "--bio", "--rand", "--single", "--run-to-completion")


def compose_env_sh(template_path: str, overlay_path: str) -> str:
    """Splice the generator's overlay into the template, ahead of the env_base.sh chain."""
    with open(template_path, encoding="utf-8") as fh:
        template = fh.read()
    with open(overlay_path, encoding="utf-8") as fh:
        overlay = fh.read()
    if CHAIN_LINE not in template:
        raise ValueError(
            f"{template_path} no longer ends by sourcing lib/env_base.sh; the splice point "
            "moved and this script must be re-checked against it"
        )
    head, _, tail = template.partition(CHAIN_LINE)
    return (
        head
        + "\n########### ACV overlay (generated; do not edit) ###########\n"
        + overlay
        + "\n########### end ACV overlay ###########\n\n"
        + CHAIN_LINE
        + tail
    )


def install_env_sh(base: str, overlay_path: str, config_hash: str) -> str:
    """Make the composed env.sh the one the driver will source.

    The composed file is written per-run under ``.acv/`` and ``env.sh`` becomes a symlink to
    it, so the artifact that actually ran is immutable and identifiable after the fact. A
    pre-existing *regular* ``env.sh`` is backed up once rather than destroyed --- on a shared
    login node that file is somebody's hand-tuned configuration.

    This does mean ``env.sh`` is single-slot: two ACV runs against the same checkout will
    fight over it. That is the same cluster-level serialization the kubectl substrate already
    imposes with its "no active labeled attempt" gate, so it is a constraint to document
    rather than a bug to fix here.
    """
    acv_dir = os.path.join(base, ".acv")
    os.makedirs(acv_dir, exist_ok=True)
    composed_path = os.path.join(acv_dir, f"env-{config_hash}.sh")
    composed = compose_env_sh(os.path.join(base, "env.sh.template"), overlay_path)
    with open(composed_path, "w", encoding="utf-8") as fh:
        fh.write(composed)

    link = os.path.join(base, "env.sh")
    if os.path.islink(link):
        os.unlink(link)
    elif os.path.exists(link):
        backup = f"{link}.acv-backup-{int(time.time())}"
        os.rename(link, backup)
        log(f"note: preserved the existing env.sh at {backup}")
    # Symlink, not copy: bash sets BASH_SOURCE[0] to the path used to source, so
    # SCALE_TEST_BASE still resolves to the repo root (env.sh.template:19).
    tmp = f"{link}.acv-tmp-{os.getpid()}"
    os.symlink(composed_path, tmp)
    os.replace(tmp, link)
    return composed_path


def check_bash(bash: str) -> str | None:
    """Reject a bash that cannot source the composed env.sh.

    ``declare -A`` is bash >= 4.0 and the template uses it for ``TEST_DIRS``. macOS ships
    3.2, so a laptop-side smoke test fails deep inside the driver with
    ``declare: -A: invalid option`` in a log the operator has not thought to look at yet.
    Checking up front costs one subprocess and names the real problem.
    """
    try:
        proc = subprocess.run([bash, "-c", "echo $BASH_VERSINFO"], capture_output=True, text=True)
    except OSError as exc:
        # Must not propagate: an uncaught exception writes a traceback to stderr and leaves
        # stdout empty, which ACV reads as a step with no output rather than a failure.
        return f"cannot run {bash!r}: {exc}"
    if proc.returncode != 0:
        return f"cannot run {bash!r}: {proc.stderr.strip()}"
    major = proc.stdout.strip()
    if not major.isdigit() or int(major) < 4:
        return (
            f"{bash} is bash {major}.x; the generated env.sh needs >= 4.0 for 'declare -A' "
            "(TEST_DIRS). macOS ships 3.2 -- run this on the cluster login node, or pass "
            "--bash /opt/homebrew/bin/bash."
        )
    return None


# Sidecars we own inside the sweep dir. The driver knows nothing about them; they exist so a
# later invocation -- possibly from a different session -- can recognise its own prior work.
HASH_FILE = ".acv-config-hash"
PID_FILE = ".acv-launcher.pid"


def _sidecar(sweep_dir: str, name: str) -> str:
    return os.path.join(sweep_dir, "executions", name)


def write_sidecars(sweep_dir: str, config_hash: str, pid: int) -> None:
    for name, value in ((HASH_FILE, config_hash), (PID_FILE, str(pid))):
        path = _sidecar(sweep_dir, name)
        tmp = f"{path}.tmp-{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(value + "\n")
            os.replace(tmp, path)  # rename, so a concurrent reader never sees a partial file
        except OSError as exc:
            log(f"warning: could not write {name}: {exc}")


def read_sidecar(sweep_dir: str, name: str) -> str | None:
    try:
        with open(_sidecar(sweep_dir, name), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def find_reattachable(results_dir: str, config_hash: str, kind: str = WORKLOAD_IO) -> str | None:
    """An existing sweep of this same config that still has work outstanding.

    Without this, re-running the step after a dropped connection starts a **second**
    coordinator against the same configuration -- two sweeps writing to the same TEST_DIRS,
    each unaware of the other. A sweep whose executions are all terminal is deliberately NOT
    re-attachable: that is a finished run, and asking for it again means asking for a new one.
    """
    for candidate in reversed(find_sweep_dirs(results_dir, kind)):
        if read_sidecar(candidate, HASH_FILE) != config_hash:
            continue
        statuses = read_statuses(candidate)
        if statuses and any(st not in TERMINAL for st in statuses.values()):
            return candidate
    return None


def build_driver_args(args: argparse.Namespace) -> list[str]:
    """The workload flags for one submission, in the order the driver documents them.

    ``--env-override`` is included here rather than at the call sites because upstream
    accepts it on a new submission and on ``--batch``/``--append`` and nowhere else
    (nv-elbencho-sweep.sh usage; nv-mdtest-elbencho.sh:121-125) -- which is exactly the set
    of places this function feeds.
    """
    kind = getattr(args, "workload", WORKLOAD_IO)
    env_override = getattr(args, "env_override", None)
    if kind == WORKLOAD_MDTEST:
        argv = ["--nodes", args.nodes, "--tasks", args.tasks]
        if getattr(args, "single_dir_file_target", None):
            argv += ["--single-dir-file-target", str(args.single_dir_file_target)]
        if env_override:
            argv += ["--env-override", env_override]
        return argv
    argv = []
    if args.mode == "write-only":
        argv.append("--write-only")
    elif args.mode == "write-no-read":
        argv.append("--write-no-read")
    if args.read_from:
        argv += ["--read-from", args.read_from]
    if args.bio:
        argv.append("--bio")
    if args.rand:
        argv.append("--rand")
    if args.single:
        argv.append("--single")
    if args.run_to_completion:
        argv.append("--run-to-completion")
    if env_override:
        argv += ["--env-override", env_override]
    argv += ["--nodes", args.nodes]
    return argv


def stamp_provenance(payload: dict, base: str, kind: str) -> dict:
    """Record which checkout and which workload produced this result.

    A sweep directory names its workload and nothing else; two runs a month apart against
    different upstream commits are otherwise indistinguishable in the result tree. The
    version is read once, here, because every later step inherits it through
    ``{{ steps.submit_sweep.sst_version }}`` rather than shelling out again.
    """
    payload.setdefault("workload", kind)
    payload.setdefault("sst_version", sst_version(base))
    return payload


def incompatible_workload_flags(args: argparse.Namespace) -> list[str]:
    """io-only flags the caller set while asking for a workload that rejects them."""
    if getattr(args, "workload", WORKLOAD_IO) != WORKLOAD_MDTEST:
        return []
    set_flags = []
    if args.mode != "default":
        set_flags.append("--mode")
    for flag in ("read_from", "bio", "rand", "single", "run_to_completion"):
        if getattr(args, flag, None):
            set_flags.append("--" + flag.replace("_", "-"))
    return set_flags


def launch_detached(base: str, argv: list[str], launch_log: str, bash: str, driver: str = DRIVER) -> int:
    """Start the driver in its own session so it outlives this step and the ACV process."""
    os.makedirs(os.path.dirname(launch_log), exist_ok=True)
    fh = open(launch_log, "ab", buffering=0)
    cmd = [bash, os.path.join(base, driver)] + argv
    log(f"+ (detached) {' '.join(shlex.quote(c) for c in cmd)}  >> {launch_log}")
    proc = subprocess.Popen(
        cmd,
        cwd=base,
        stdout=fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    fh.close()
    return proc.pid


def wait_for_sweep_dir(results_dir: str, known: set[str], timeout: int, kind: str = WORKLOAD_IO) -> str | None:
    """Wait for the driver to mkdir its ``elbencho-<DS>`` output directory.

    It does this before reification and before any dispatch (the ``OUTPUT_DIR=`` assignment
    in ``nv-elbencho-sweep.sh``, :692 as of b3ab646),
    so this resolves in seconds --- long before the sweep itself does anything expensive.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for candidate in find_sweep_dirs(results_dir, kind):
            if candidate not in known:
                return candidate
        time.sleep(1)
    return None


def wait_for_jobid(sweep_dir: str, timeout: int) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobid = read_coordinator_jobid(sweep_dir)
        if jobid:
            return jobid
        time.sleep(2)
    return None


def submit_kubectl(base: str, args: argparse.Namespace) -> dict:
    """The kubectl substrate is async natively --- no detach, no jobid polling.

    Fails loudly if the checkout has no kubectl implementation, rather than silently
    reporting a submit that did not happen.
    """
    # Probe for the kubectl implementation directory, NOT for the string
    # "EXECUTION_SUBSTRATE". That string is part of the substrate *selector* in env_base.sh,
    # which is present without any kubectl execution code -- so such a check would pass on a
    # checkout that cannot run a kubectl sweep, and
    # this Slurm-shaped path would run against it and fail confusingly. The functions file
    # defining kubectl_submit_sweep is the thing that actually has to exist.
    kubectl_impl = os.path.join(base, "storage-tests", "fs", "kubectl", "_nv-elbencho-kubectl-functions.sh")
    if not os.path.isfile(kubectl_impl):
        return {
            "success": False,
            "error": (
                f"this checkout has no kubectl substrate: {kubectl_impl} is missing. "
                "This is a stale checkout: pull NVIDIA/storage-scale-test main before using --substrate kubectl."
            ),
        }

    # kubectl_submit_sweep is natively async: it creates the Job, journals it, transitions
    # the attempt to SUBMITTED and returns, so unlike Slurm there is nothing to detach from
    # and no job id to poll.
    kind = getattr(args, "workload", WORKLOAD_IO)
    driver = workload(kind).driver_path(base)
    # sweeplib.run, not subprocess.run: a driver that outlives --submit-timeout must come
    # back as a failed result with a reason, not an uncaught TimeoutExpired that leaves the
    # step with no JSON at all.
    proc = run([args.bash, driver] + build_driver_args(args), cwd=base, timeout=args.submit_timeout)
    sys.stderr.write(proc.stderr)
    dirs = find_sweep_dirs(args.results_dir, kind)
    return {
        "success": proc.returncode == 0,
        "substrate": KUBECTL,
        "sweep_dir": dirs[-1] if dirs else None,
        "coordinator_jobid": None,
        "exit_code": proc.returncode,
        "workload": kind,
    }


_BATCH_RESULTS_KEY = "STORAGE_SCALE_TEST_BATCH_RESULTS="


def _parse_batch_results_dir(stdout: str) -> str | None:
    """Pull the batch root out of ``--batch``/``--append`` stdout.

    The driver prints ``STORAGE_SCALE_TEST_BATCH_RESULTS=<root>`` on every prepare.
    It is the only machine-readable handle to the batch, so a missing line is a
    hard error rather than something to guess around.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(_BATCH_RESULTS_KEY):
            return line[len(_BATCH_RESULTS_KEY) :].strip() or None
    return None


def _run_driver(base: str, argv: list[str], bash: str, timeout: int, driver: str = DRIVER):
    """Run the driver synchronously from ``base`` and return the CompletedProcess.

    A timeout comes back as a failed CompletedProcess (sweeplib.run), never a raise.
    """
    return run([bash, driver, *argv], cwd=base, timeout=timeout)


def parse_group_spec(spec: str, default_kind: str) -> tuple[str, list[str]]:
    """Split one ``--batch-group`` value into (workload, driver flags).

    A batch may MIX workloads: the manifest's group rows carry a ``kind`` column that is
    literally ``io`` or ``mdtest`` (``lib/_batch_functions.sh:81`` rejects anything else),
    and upstream's usage says "Either filesystem launcher can --start or --resume the mixed
    batch". Mixing is how you measure bandwidth and metadata against one filesystem under
    one sealed execution set, which is the only way the two numbers are comparable.

    The prefix is optional and the bare form keeps its old meaning, so existing configs are
    unaffected::

        "--nodes 2 --write-no-read"          -> the run's --workload
        "io: --nodes 2 --write-no-read"      -> io, explicitly
        "mdtest: --nodes 1 --tasks 4"        -> mdtest

    A colon is only treated as a prefix when what precedes it is a known workload name, so
    a flag value containing one (``--read-from host:/path``) is never mistaken for a prefix.
    """
    head, sep, rest = spec.partition(":")
    kind = head.strip()
    if sep and kind in ALL_WORKLOADS:
        return kind, shlex.split(rest)
    return default_kind, shlex.split(spec)


def prepare_batch(base: str, args: argparse.Namespace) -> dict:
    """Build a prepared batch: ``--batch`` for group 1, ``--append`` for the rest.

    Returns the batch root without starting it. Nothing here dispatches work, so
    a failure midway leaves a DRAFT batch that can be inspected or discarded --
    the execution set only freezes at ``--start``.
    """
    default_kind = getattr(args, "workload", WORKLOAD_IO)
    first = build_driver_args(args)
    driver = workload(default_kind).driver
    result = _run_driver(base, ["--batch", *first], args.bash, args.submit_timeout, driver)
    if result.returncode != 0:
        return {
            "success": False,
            "error": f"--batch failed (rc={result.returncode}): {(result.stderr or result.stdout).strip()[:400]}",
        }
    root = _parse_batch_results_dir(result.stdout)
    if not root:
        return {
            "success": False,
            "error": "--batch printed no STORAGE_SCALE_TEST_BATCH_RESULTS line; "
            f"cannot locate the batch root. stdout: {result.stdout.strip()[:300]}",
        }
    log(f"batch prepared at {root}")

    groups = 1
    kinds = [default_kind]
    for spec in args.batch_group or []:
        kind, extra = parse_group_spec(spec, default_kind)
        if not extra:
            return {"success": False, "error": f"--batch-group {spec!r} is empty"}
        # Each group is appended through ITS OWN launcher. The batch root is shared and the
        # manifest records the kind, so the two drivers cooperate rather than compete.
        appended = _run_driver(base, ["--append", root, *extra], args.bash, args.submit_timeout, workload(kind).driver)
        if appended.returncode != 0:
            return {
                "success": False,
                "error": f"--append {spec!r} ({kind}) failed (rc={appended.returncode}): "
                f"{(appended.stderr or appended.stdout).strip()[:400]}",
                "batch_results_dir": root,
                "groups_prepared": groups,
                "group_workloads": kinds,
            }
        groups += 1
        kinds.append(kind)
        log(f"appended group {groups} ({kind}): {spec}")
    return {
        "success": True,
        "batch_results_dir": root,
        "groups_prepared": groups,
        "group_workloads": kinds,
        # A downstream metrics step needs this: a mixed batch cannot be reported by a
        # single engine, only by extract-filesystem.py with --kind all.
        "mixed_workloads": len(set(kinds)) > 1,
    }


def start_batch(base: str, root: str, args: argparse.Namespace) -> dict:
    """Seal the batch and dispatch it.

    kubectl is natively async -- ``--start`` returns once the Job exists. slurm and
    ssh block for the whole sweep, which is exactly the shape ACV cannot hold, so
    those detach through the same helper a non-batch submit uses.
    """
    if args.substrate == KUBECTL:
        started = _run_driver(
            base,
            ["--start", root],
            args.bash,
            args.submit_timeout,
            workload(getattr(args, "workload", WORKLOAD_IO)).driver,
        )
        if started.returncode != 0:
            return {
                "success": False,
                "error": f"--start failed (rc={started.returncode}): "
                f"{(started.stderr or started.stdout).strip()[:400]}",
                "batch_results_dir": root,
            }
        if not batch_is_sealed(root):
            return {
                "success": False,
                "error": f"--start returned 0 but {root} has no batch-sealed.sha256; "
                "refusing to report a start that did not seal the batch",
                "batch_results_dir": root,
            }
        return {"success": True, "batch_results_dir": root, "sealed": True, "detached": False}
    launch_log = os.path.join(root, "acv-batch-start.log")
    pid = launch_detached(base, ["--start", root], launch_log, args.bash)
    return {
        "success": True,
        "batch_results_dir": root,
        "detached": True,
        "dispatcher_pid": pid,
        "launch_log": launch_log,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-sh", required=True, help="overlay from gen_env_sh.py (steps.gen_env.env_sh_path)")
    ap.add_argument("--config-hash", required=True, help="steps.gen_env.config_hash")
    ap.add_argument("--substrate", required=True, choices=ALL_SUBSTRATES)
    ap.add_argument(
        "--results-dir",
        required=True,
        help="RESULTS_DIR the overlay sets; must be unique per run for discovery to be sound",
    )
    ap.add_argument("--scale-test-base", required=True, help="storage-scale-test checkout root")
    ap.add_argument("--nodes", required=True, help="driver --nodes spec, e.g. '1,2,4'")
    ap.add_argument(
        "--workload",
        default=WORKLOAD_IO,
        choices=ALL_WORKLOADS,
        help="io = nv-elbencho-sweep.sh (bandwidth/IOPS); "
        "mdtest = nv-mdtest-elbencho.sh (metadata create/stat/delete). "
        "Both share the batch library, the kubectl lifecycle and env.sh; "
        "they differ in driver, output directory name and extractor",
    )
    ap.add_argument(
        "--tasks", help="mdtest --tasks spec (tasks per node); required with --workload mdtest, rejected otherwise"
    )
    ap.add_argument(
        "--single-dir-file-target",
        type=int,
        help="mdtest --single-dir-file-target: one flat directory of about N "
        "zero-byte files. Upstream requires a single node count and a "
        "single task count with it",
    )
    ap.add_argument(
        "--env-override",
        help="file sourced AFTER env.sh so its workload settings (TEST_DIRS, "
        "FS_MAX_*, MDTEST_*, ELBENCHO_*) win. Saved into the sweep's "
        "env_used.sh by upstream. NOTE: it lands after lib/env_base.sh has "
        "already derived values, so it is for leaf workload knobs only -- "
        "anything env_base.sh derives from must go through --env-sh",
    )
    ap.add_argument("--mode", default="default", choices=("default", "write-only", "write-no-read"))
    ap.add_argument("--read-from", help="driver --read-from path")
    ap.add_argument("--bio", action="store_true")
    ap.add_argument("--rand", action="store_true")
    ap.add_argument("--single", action="store_true")
    ap.add_argument(
        "--run-to-completion",
        action="store_true",
        help="write each cell's full dataset rather than stopping on a timer. "
        "Upstream requires completion mode when ELBENCHO_FILES_PER_NODE is set "
        "with the worker-directories layout (buffered IO or multiple TEST_DIRS "
        "roots also satisfy it)",
    )
    ap.add_argument(
        "--dir-timeout", type=int, default=120, help="seconds to wait for the elbencho-<DS> directory to appear"
    )
    ap.add_argument(
        "--jobid-timeout", type=int, default=600, help="seconds to wait for .coordinator.jobid (slurm only)"
    )
    ap.add_argument("--submit-timeout", type=int, default=900, help="kubectl submit timeout")
    ap.add_argument(
        "--no-reattach",
        action="store_true",
        help="launch a new sweep even if one with this config_hash is still running",
    )
    ap.add_argument("--bash", default="bash", help="bash used to run the driver; must be >= 4.0 (see check_bash)")
    ap.add_argument(
        "--batch",
        action="store_true",
        help="prepare a batch (--batch/--append) and --start it, instead of dispatching a single sweep",
    )
    ap.add_argument(
        "--batch-group",
        action="append",
        metavar="FLAGS",
        help="extra batch group as one shell-quoted flag string, e.g. "
        "'--nodes 2 --write-no-read'; repeatable. Requires --batch. "
        "Prefix with 'io:' or 'mdtest:' to MIX workloads in one sealed "
        "batch, e.g. 'mdtest: --nodes 1 --tasks 4'; without a prefix the "
        "group uses the run's --workload",
    )
    ap.add_argument("--no-start", action="store_true", help="with --batch, leave the batch DRAFT instead of sealing it")
    args = ap.parse_args()
    # Before anything touches the checkout: with SST_RESULTS_DIR unset the config renders
    # --results-dir as "", and every later path would be relative to wherever isvctl ran.
    if not args.results_dir.strip():
        return fail("--results-dir is empty: set SST_RESULTS_DIR to the directory for this run's results")

    base = os.path.abspath(args.scale_test_base)
    wl = workload(args.workload)
    if not os.path.isfile(wl.driver_path(base)):
        return fail(f"no {wl.driver} under --scale-test-base {base}")

    if args.workload == WORKLOAD_MDTEST and not args.tasks:
        return fail(
            "--tasks is required with --workload mdtest: upstream rejects a "
            "submission without it (nv-mdtest-elbencho.sh:133)"
        )
    if args.workload != WORKLOAD_MDTEST and (args.tasks or args.single_dir_file_target):
        return fail(f"--tasks/--single-dir-file-target are mdtest-only; workload is {args.workload}")
    bad_flags = incompatible_workload_flags(args)
    if bad_flags:
        return fail(
            f"{', '.join(bad_flags)} do(es) not exist on {wl.driver}; nv-mdtest-elbencho.sh "
            "accepts only --nodes/--tasks/--single-dir-file-target/--env-override. Passing "
            "one would fail inside the detached driver, where the error reaches nothing"
        )
    if args.env_override and not os.path.isfile(args.env_override):
        return fail(f"no such --env-override file: {args.env_override}")

    problem = check_bash(args.bash)
    if problem:
        return fail(problem)

    try:
        composed = install_env_sh(base, os.path.abspath(args.env_sh), args.config_hash)
    except (OSError, ValueError) as exc:
        return fail(f"could not install env.sh: {exc}")
    log(f"installed env.sh -> {composed}")

    os.makedirs(args.results_dir, exist_ok=True)

    if args.batch_group and not args.batch:
        return fail("--batch-group requires --batch")

    if args.batch:
        prepared = stamp_provenance(prepare_batch(base, args), base, args.workload)
        prepared["composed_env_sh"] = composed
        if not prepared["success"]:
            return emit(prepared)
        root = prepared["batch_results_dir"]
        if args.no_start:
            prepared["sealed"] = False
            prepared["note"] = "batch left DRAFT; --start it to freeze and dispatch"
            return emit(prepared)
        started = stamp_provenance(start_batch(base, root, args), base, args.workload)
        started["composed_env_sh"] = composed
        # Carry EVERY shape fact forward from the prepare. start_batch() describes the
        # dispatch, not the batch, so without this a STARTED mixed batch reports
        # group_workloads/mixed_workloads as null -- and a downstream metrics step that
        # keys off mixed_workloads would report only half of it. Only `--no-start` runs
        # returned the prepare dict directly.
        for key in ("groups_prepared", "group_workloads", "mixed_workloads"):
            if key in prepared:
                started[key] = prepared[key]
        return emit(started)

    if args.substrate == KUBECTL:
        payload = stamp_provenance(submit_kubectl(base, args), base, args.workload)
        payload["composed_env_sh"] = composed
        return emit(payload)

    if not args.no_reattach:
        existing = find_reattachable(args.results_dir, args.config_hash, args.workload)
        if existing:
            log(
                f"re-attaching to {existing}: same config_hash and work still outstanding. "
                "Pass --no-reattach to force a second sweep."
            )
            return emit(
                stamp_provenance(
                    {
                        "success": True,
                        "substrate": args.substrate,
                        "sweep_dir": existing,
                        "coordinator_jobid": read_coordinator_jobid(existing),
                        "launch_log": None,
                        "launcher_pid": read_sidecar(existing, PID_FILE),
                        "composed_env_sh": composed,
                        "config_hash": args.config_hash,
                        "reattached": True,
                    },
                    base,
                    args.workload,
                )
            )

    known = set(find_sweep_dirs(args.results_dir, args.workload))
    if known:
        log(
            f"note: {len(known)} pre-existing sweep dir(s) under {args.results_dir}; "
            "a per-run RESULTS_DIR makes discovery unambiguous"
        )

    launch_log = os.path.join(args.results_dir, f"acv-launch-{args.config_hash}.log")
    pid = launch_detached(base, build_driver_args(args), launch_log, args.bash, wl.driver)

    sweep_dir = wait_for_sweep_dir(args.results_dir, known, args.dir_timeout, args.workload)
    if sweep_dir is None:
        return fail(
            f"no new {wl.dir_re.pattern} directory under {args.results_dir} within "
            f"{args.dir_timeout}s; the driver most likely failed while sourcing env.sh",
            launch_log=launch_log,
            pid=pid,
            composed_env_sh=composed,
        )
    log(f"sweep dir: {sweep_dir}")
    write_sidecars(sweep_dir, args.config_hash, pid)

    jobid = None
    if args.substrate == SLURM:
        jobid = wait_for_jobid(sweep_dir, args.jobid_timeout)
        if jobid is None:
            # Not fatal: the sweep dir and its executions/ are already durable, so
            # poll_sweep can still work off .status files. But without the jobid we
            # cannot distinguish "queued" from "coordinator died".
            log(
                f"warning: no .coordinator.jobid within {args.jobid_timeout}s; "
                "poll_sweep will fall back to status sentinels only"
            )

    return emit(
        stamp_provenance(
            {
                "success": True,
                "substrate": args.substrate,
                "sweep_dir": sweep_dir,
                "coordinator_jobid": jobid,
                "launch_log": launch_log,
                "launcher_pid": pid,
                "composed_env_sh": composed,
                "config_hash": args.config_hash,
                "reattached": False,
            },
            base,
            args.workload,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
