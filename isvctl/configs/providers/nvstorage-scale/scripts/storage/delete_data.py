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

"""Remove a sweep's data directory from the filesystem under test (teardown).

Without this, sweep data accumulates under ``TEST_DIRS`` between runs. That is not a tidiness
concern: a 60 s sweep at 2 nodes x 4 threads can fill a 100 GB scratch filesystem in
~30 s and fail the execution. The next run
fails for a reason that has nothing to do with the change being tested.

Deletion runs **on a compute node** -- via ``sbatch`` under Slurm or over SSH -- because the
login node may not even mount the filesystem, and because deleting millions of files is itself
a timed, distributed operation in this repo.

Safety
------
This deletes data on a shared filesystem, so the guards are layered and this step refuses by
default rather than proceeding on a guess:

* ``--path`` must be a **strict** subdirectory of ``--test-root``: under it, and not equal to
  it. The driver enforces its own version of this rule, but a wrong path caught here never
  reaches a compute node.
* ``--test-root`` is **required**. Deriving it by parsing ``env.sh`` would make the blast
  radius depend on a file this step does not control.
* Obvious catastrophes (``/``, ``~``, a bare relative path, anything with ``..``) are refused
  outright.
* ``--dry-run`` prints exactly what would be removed and touches nothing.

The driver also requires a **single** ``TEST_DIRS`` entry for this mode
(``validate_elbencho_sweep_single_test_dirs_key``) and ``SLURM_ENABLED`` or ``SSH_ENABLED``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import ALL_SUBSTRATES, KUBECTL, emit, fail, find_sweep_dirs, log, run

DRIVER = os.path.join("storage-tests", "fs", "nv-elbencho-sweep.sh")


def check_path(path: str, test_root: str) -> str | None:
    """Return a refusal reason, or ``None`` if the path is safe to hand to the driver."""
    if not path or not test_root:
        return "both --path and --test-root are required"
    if not os.path.isabs(path) or not os.path.isabs(test_root):
        return f"--path and --test-root must be absolute (got {path!r}, {test_root!r})"
    if ".." in path.split(os.sep):
        return f"--path contains '..': {path}"

    # normpath only: deliberately NOT realpath. Resolving symlinks here would let a symlink
    # inside the test root make an outside target look contained.
    p = os.path.normpath(path).rstrip(os.sep) or os.sep
    root = os.path.normpath(test_root).rstrip(os.sep) or os.sep
    if p in ("/", os.path.expanduser("~")):
        return f"refusing to delete {p}"
    if p == root:
        return (
            f"--path is the test root itself ({root}); --delete-only takes a strict "
            "subdirectory so the root's mount point and any siblings survive"
        )
    if not p.startswith(root + os.sep):
        return f"--path {p} is not under --test-root {root}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", required=True, help="absolute path to remove; a strict subdir of --test-root")
    ap.add_argument("--test-root", required=True, help="the TEST_DIRS entry --path must live under")
    ap.add_argument("--substrate", required=True, choices=ALL_SUBSTRATES)
    ap.add_argument("--scale-test-base", required=True)
    ap.add_argument("--results-dir", help="RESULTS_DIR, so the dir this run creates can be reported")
    ap.add_argument("--bash", default="bash")
    ap.add_argument(
        "--timeout", type=int, default=3600, help="deleting a large tree is a real distributed operation; budget for it"
    )
    ap.add_argument("--dry-run", action="store_true", help="report the decision, delete nothing")
    args = ap.parse_args()

    base = os.path.abspath(args.scale_test_base)
    refusal = check_path(args.path, args.test_root)
    if refusal:
        return fail(refusal, path=args.path, test_root=args.test_root)
    if not os.path.isfile(os.path.join(base, DRIVER)):
        return fail(f"no {DRIVER} under {base}")
    if args.substrate == KUBECTL:
        # NOT an unexercised path -- an upstream design constraint. `_run_delete_only_path`
        # ends with "Error: --delete-only requires SLURM_ENABLED or SSH_ENABLED"
        # (nv-elbencho-sweep.sh:631): it dispatches through sbatch or ssh and has no kubectl
        # branch at all. Wiring it here would ship a step that cannot run. Cleaning a PVC
        # needs a cluster-side job upstream does not provide, so this says what is actually
        # true and names the alternative instead of implying it merely needs testing.
        return fail(
            "--delete-only does not support kubectl: upstream requires SLURM_ENABLED or "
            "SSH_ENABLED and dispatches through sbatch/ssh (nv-elbencho-sweep.sh:631). "
            "Data written to the PVC must be removed by a cluster-side job -- e.g. "
            f"`kubectl -n <ns> run ... --rm` mounting the PVC and removing {args.path} -- "
            "or from a host that mounts the same filesystem. Note that --collect already "
            "releases the Lease and deletes the owned Job/DaemonSet; what remains is the "
            "benchmark DATA, not Kubernetes objects",
            path=args.path,
            test_root=args.test_root,
            substrate=KUBECTL,
        )

    target = os.path.normpath(args.path)
    if args.dry_run:
        log(f"dry run: would remove {target} (under {os.path.normpath(args.test_root)})")
        return emit({"success": True, "dry_run": True, "path": target, "deleted": False})

    before = set(find_sweep_dirs(args.results_dir)) if args.results_dir else set()
    proc = run([args.bash, os.path.join(base, DRIVER), "--delete-only", target], cwd=base, timeout=args.timeout)
    sys.stderr.write(proc.stderr)

    # --delete-only mints its own elbencho-<DS> directory for its logs
    # (the ``delete_output_dir=`` assignment in nv-elbencho-sweep.sh, :515 as of b3ab646).
    # Report it, because it has no executions/ and would
    # otherwise look like a broken sweep to poll_sweep or collect_sweep.
    created = None
    if args.results_dir:
        new = [d for d in find_sweep_dirs(args.results_dir) if d not in before]
        created = new[-1] if new else None
        if created:
            log(f"note: --delete-only created {created} for its logs; it has no executions/ and is not a sweep")

    payload = {
        "success": proc.returncode == 0,
        "path": target,
        "deleted": proc.returncode == 0,
        "exit_code": proc.returncode,
        "delete_log_dir": created,
    }
    if proc.returncode != 0:
        payload["error"] = (
            f"--delete-only exited {proc.returncode}. Common causes: TEST_DIRS has more than "
            "one entry (this mode requires exactly one), neither SLURM_ENABLED nor SSH_ENABLED "
            "is set, or the path is not under the test root as the driver computes it."
        )
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
