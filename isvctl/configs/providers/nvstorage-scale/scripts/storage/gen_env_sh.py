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

"""Translate ai-cloud-validation inputs into a storage-scale-test ``env.sh``.

ACV step ``env:`` values (prefix ``SST_``) in, a complete ``env.sh`` out, plus a JSON
summary on stdout for the next step to template off.

The whole reason this program is strict
---------------------------------------
``env.sh`` is *sourced*, not parsed. ``ELBENCHO_THREAD_LST=("1")`` sets a variable nobody
reads and the sweep silently runs with defaults. Bash cannot catch that and neither can
ACV — ``StepConfig.env`` is ``dict[str, str]``, so its YAML schema validates none of this.
This program is the only validation layer that exists, which is why unknown keys and
wrong-substrate keys are hard errors rather than warnings.

Line numbers below refer to ``storage-scale-test/env.sh.template``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SLURM, SSH, KUBECTL = "slurm", "ssh", "kubectl"
ALL = (SLURM, SSH, KUBECTL)

# Container mount root that the kubectl substrate prepends to every TEST_DIRS path.
# We must emit LOGICAL paths; emitting this prefix ourselves double-prefixes at runtime.
K8S_MOUNT_ROOT = "/mnt/storage-scale-test"

# (env.sh variable, kind, substrates)  keyed by ACV input name.
#   kind: str | int | list | assoc | raw
SPEC: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # core paths
    # Deterministic worker selection. Without it, which nodes run each execution is
    # RANDOM whenever the eligible pool is larger than `--nodes`: the coordinator picks
    # positions with `shuf` (_nv-elbencho-kubectl-coordinator.sh:449) and the coordinator
    # node with `RANDOM %` (_nv-elbencho-kubectl-functions.sh:2690).
    #
    # For a benchmark that is a defensible default -- randomising placement avoids
    # flattering one node. For a VALIDATION harness comparing a run against a threshold or
    # against last week it is a confounder: a regression may be a different node rather
    # than a changed filesystem, and nothing in the result says which.
    #
    # NOTE: env.sh.template:33-36 documents this as affecting SSH and Slurm only. That
    # comment is stale -- both kubectl paths above read ORDER_NODES_ENABLED. Upstream-worthy.
    "SST_ORDER_NODES": ("ORDER_NODES", "int", ALL),
    "SST_RESULTS_DIR": ("RESULTS_DIR", "str", ALL),
    "SST_LOGS_DIR": ("LOGS_DIR", "str", ALL),
    # slurm
    "SST_PARTITION": ("partition", "str", (SLURM,)),
    "SST_ACCOUNT": ("account", "str", (SLURM,)),
    "SST_RESERVATION": ("reservation", "str", (SLURM,)),
    "SST_RUN_TIME": ("run_time", "str", (SLURM,)),
    "SST_JOB_NAME_PREFIX": ("SLURM_JOB_NAME_PREFIX", "str", (SLURM,)),
    "SST_SLURM_EXTRA_ARGS": ("SLURM_EXTRA_ARGS", "list", (SLURM,)),
    "SST_SLURM_EXCLUSIVE_USER": ("SLURM_EXCLUSIVE_USER", "int", (SLURM,)),
    "SST_MODULES": ("MODULES", "list", (SLURM,)),
    "SST_NODE_IGNORES": ("SLURM_NODE_IGNORES", "str", (SLURM,)),
    "SST_NODE_INCLUDES": ("SLURM_NODE_INCLUDES", "str", (SLURM,)),
    # ssh
    "SST_SSH_HOST_LIST": ("SSH_HOST_LIST", "str", (SSH,)),
    "SST_SSH_USER": ("SSH_USER", "str", (SSH,)),
    "SST_SSH_HOMEDIR_SHARED": ("SSH_HOMEDIR_SHARED", "str", (SSH,)),
    # kubectl
    "SST_KUBECTL_NAMESPACE": ("KUBECTL_NAMESPACE", "str", (KUBECTL,)),
    # KUBECTL_PV / KUBECTL_PVC were REMOVED in storage-scale-test v2.0.0
    # (lib/_kubectl_claims.sh:105). A claim is now named inside each TEST_DIRS key as
    # "[namespace/]claim:path", and the PersistentVolume it is bound to is discovered and
    # recorded automatically. Emitting the old names is a hard error upstream, so there is
    # deliberately no SST_ input for them: put the claim in SST_TEST_DIRS instead, e.g.
    #     SST_TEST_DIRS="my-claim:scale-test:1"
    # The "assoc" kind splits on the LAST colon, so the claim-qualified key survives intact.
    "SST_KUBECTL_NODE_SELECTOR": ("KUBECTL_NODE_SELECTOR", "str", (KUBECTL,)),
    "SST_KUBECTL_ELBENCHO_IMAGE": ("KUBECTL_ELBENCHO_IMAGE", "str", (KUBECTL,)),
    # All optional upstream, with defaults IfNotPresent / 2000 / 2000.
    "SST_KUBECTL_IMAGE_PULL_POLICY": ("KUBECTL_IMAGE_PULL_POLICY", "str", (KUBECTL,)),
    "SST_KUBECTL_RUN_AS_USER": ("KUBECTL_RUN_AS_USER", "int", (KUBECTL,)),
    "SST_KUBECTL_RUN_AS_GROUP": ("KUBECTL_RUN_AS_GROUP", "int", (KUBECTL,)),
    # client / targets
    "SST_CLIENT_TYPE": ("client_type", "str", ALL),
    "SST_CLIENT_ARCH": ("client_arch", "str", ALL),
    "SST_TEST_DIRS": ("TEST_DIRS", "assoc", ALL),
    # thresholds  (NOTE: AGG is GB/s, NODE_THROUGHPUT is Gbps — 8x apart)
    "SST_FS_MAX_AGG_THROUGHPUT": ("FS_MAX_AGG_THROUGHPUT", "int", ALL),
    "SST_FS_MAX_NODE_THROUGHPUT_GBPS": ("FS_MAX_NODE_THROUGHPUT_GBPS", "int", ALL),
    "SST_FS_MAX_NODE_IOPS": ("FS_MAX_NODE_IOPS", "int", ALL),
    # elbencho filesystem sweep
    "SST_THREADS": ("ELBENCHO_SCALE_THREAD_LIST", "list", ALL),
    "SST_IO_SIZES": ("ELBENCHO_SCALE_IO_SIZES", "list", ALL),
    "SST_IODEPTH": ("ELBENCHO_IODEPTH_LIST", "list", ALL),
    "SST_DURATION": ("ELBENCHO_SCALE_READ_WRITE_DURATION", "int", ALL),
    "SST_FILE_SIZE_MULTIPLIER": ("ELBENCHO_FILE_SIZE_MULTIPLIER", "int", ALL),
    "SST_FILE_LAYOUT": ("ELBENCHO_FILE_LAYOUT", "str", ALL),
    "SST_FILES_PER_NODE": ("ELBENCHO_FILES_PER_NODE", "str", ALL),
    "SST_FILE_SIZE": ("ELBENCHO_FILE_SIZE", "str", ALL),
    "SST_READ_AFTER_WRITE_PAUSE": ("ELBENCHO_READ_AFTER_WRITE_PAUSE", "int", ALL),
    "SST_LIVE_CSV_EXTENDED": ("ELBENCHO_LIVE_CSV_EXTENDED", "int", ALL),
    "SST_LIVEINT": ("ELBENCHO_LIVEINT", "int", ALL),
    "SST_SINGLE_BIG_FILE": ("ELBENCHO_SINGLE_BIG_FILE", "int", ALL),
    "SST_SINGLE_BIG_FILE_BASENAME": ("ELBENCHO_SINGLE_BIG_FILE_BASENAME", "str", ALL),
    "SST_SINGLE_BIG_FILE_SIZE": ("ELBENCHO_SINGLE_BIG_FILE_SIZE", "str", ALL),
    "SST_ALL_NODES_ACCESS_ALL_DATA": ("ELBENCHO_ALL_NODES_ACCESS_ALL_DATA", "int", ALL),
    # mdtest-elbencho metadata sweep (env.sh.template:188-190). The target tree is
    # <TEST_DIRS>/mdtest-elbencho-target-N/b{0..BRANCH_FACTOR-1}/r{rank}/d{dir}/, so the
    # per-task work is BRANCH_FACTOR**2 directories holding BRANCH_FACTOR**2 *
    # ITEMS_PER_DIR files (env.sh.template:178-179). Both grow QUADRATICALLY in
    # BRANCH_FACTOR -- the template's default of 7 is already 49 dirs and 4900 files per
    # task, so raising it is not a small change. MDTEST_LAYOUT and
    # MDTEST_SINGLE_DIR_{TARGET_FILES,FILES_PER_WORKER} are deliberately absent: the driver
    # derives them from --single-dir-file-target, and setting them by hand would contradict
    # the flag.
    "SST_MDTEST_BRANCH_FACTOR": ("MDTEST_BRANCH_FACTOR", "int", ALL),
    "SST_MDTEST_ITEMS_PER_DIR": ("MDTEST_ITEMS_PER_DIR", "int", ALL),
    "SST_MDTEST_ITERATIONS": ("MDTEST_ITERATIONS", "int", ALL),
}

# Reverse map for --from-yaml (round-trip fixture replay).
BY_ENV_VAR = {v[0]: k for k, v in SPEC.items()}

# env.sh variables that are exported in the template (vs plain assignment).
# Assigned without `export` in env.sh.template. TEST_DIRS matters: bash cannot export an
# associative array, so `export TEST_DIRS=(...)` is wrong even though `bash -n` accepts it.
_PLAIN = {
    "partition",
    "account",
    "reservation",
    "run_time",
    "SLURM_JOB_NAME_PREFIX",
    "SLURM_EXTRA_ARGS",
    "SLURM_EXCLUSIVE_USER",
    "MODULES",
    "client_type",
    "client_arch",
    "TEST_DIRS",
}


class InputError(Exception):
    pass


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render(var: str, kind: str, value: str) -> str:
    if kind == "list":
        elems = [e.strip() for e in value.split(",") if e.strip()]
        body = f"({' '.join(_q(e) for e in elems)})"
    elif kind == "assoc":
        pairs = []
        for item in (p.strip() for p in value.split(",") if p.strip()):
            if ":" not in item:
                raise InputError(f"{var}: expected 'path:weight' pairs, got {item!r}")
            path, _, weight = item.rpartition(":")
            pairs.append(f"[{_q(path)}]={weight.strip()}")
        body = f"({' '.join(pairs)})"
    elif kind == "int":
        if not re.fullmatch(r"-?\d+", value.strip()):
            raise InputError(f"{var}: expected an integer, got {value!r}")
        body = value.strip()
    else:
        body = _q(value)
    prefix = "" if var in _PLAIN else "export "
    decl = f"declare -A {var}\n" if kind == "assoc" else ""
    return f"{decl}{prefix}{var}={body}"


# SST_* keys that are run metadata, not env.sh variables. They are accepted and ignored rather
# than rejected: ACV's output schema requires a cluster_name, so the operator has to set it
# somewhere, and making the one namespaced place for it a fatal error is hostile.
# Read by other steps, not composed into env.sh: the substrate and cluster labels, and the
# Slurm overlay's opt-in cleanup step inputs. Every step inherits the shell environment, so
# rejecting these here would fail the whole run the moment cleanup was enabled.
_METADATA = {"SST_SUBSTRATE", "SST_CLUSTER_NAME", "SST_TEST_DATA_PATH", "SST_TEST_ROOT"}


def collect(env: dict[str, str], substrate: str) -> dict[str, str]:
    """Validate SST_* inputs and return {env_var: rendered_line} for those that were set."""
    supplied = {k: v for k, v in env.items() if k.startswith("SST_") and k not in _METADATA}

    unknown = sorted(set(supplied) - set(SPEC))
    if unknown:
        raise InputError(
            "unknown input key"
            + ("s" if len(unknown) > 1 else "")
            + ": "
            + ", ".join(unknown)
            + "\n  env.sh is sourced, not parsed — an unrecognised key would silently become a "
            "default, so this is fatal rather than a warning."
        )

    wrong = sorted(k for k in supplied if substrate not in SPEC[k][2])
    if wrong:
        detail = ", ".join(f"{k} (valid for: {'/'.join(SPEC[k][2])})" for k in wrong)
        raise InputError(
            f"key(s) not valid for SST_SUBSTRATE={substrate}: {detail}"
            "\n  storage-scale-test silently ignores out-of-substrate variables, so passing "
            "them through would look configured and do nothing."
        )

    out: dict[str, str] = {}
    for key, value in sorted(supplied.items()):
        var, kind, _ = SPEC[key]
        if kind == "assoc" and substrate == KUBECTL and K8S_MOUNT_ROOT in value:
            raise InputError(
                f"{key}: paths must be logical on the kubectl substrate; found "
                f"{K8S_MOUNT_ROOT!r}.\n  The tool prepends the mount root itself — emitting it "
                "here double-prefixes at runtime."
            )
        out[var] = _render(var, kind, value)
    return out


def _fail(reason: str) -> int:
    """Report a failure the way every other step does: stderr for people, JSON for ACV.

    Without the JSON, a typo'd SST_* key surfaced as a step that produced no output at all
    rather than as a failed step that says which key was wrong.
    """
    print(f"error: {reason}", file=sys.stderr)
    json.dump(
        {
            "success": False,
            "platform": "storage",
            "cluster_name": os.environ.get("SST_CLUSTER_NAME") or os.environ.get("CLUSTER_NAME") or "unknown",
            "error": reason,
        },
        sys.stdout,
        indent=2,
    )
    print()
    return 1


def from_yaml(path: str) -> dict[str, str]:
    """Replay a recorded env_used.yaml as SST_* inputs (round-trip fixture)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
    from common.compare_env import parse_env_used_yaml

    rec, env = parse_env_used_yaml(path), {}
    for var, val in rec.items():
        key = BY_ENV_VAR.get(var)
        if not key:
            continue  # derived runtime field, or a variable env_used does not own
        if val.startswith("[") and val.endswith("]"):
            val = ",".join(val[1:-1].split())
        elif val.startswith("{") and val.endswith("}"):
            val = ",".join(p.replace("=", ":") for p in val[1:-1].split(",") if p)
        if val != "":
            env[key] = val
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="path to write env.sh")
    ap.add_argument("--substrate", choices=ALL, help="overrides SST_SUBSTRATE")
    ap.add_argument("--from-yaml", help="replay a recorded env_used.yaml instead of reading SST_* env vars")
    ap.add_argument("--config", help="file of SST_KEY=value lines instead of env vars")
    args = ap.parse_args()

    env = dict(os.environ)
    if args.config:
        for raw in open(args.config, encoding="utf-8"):
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    if args.from_yaml:
        env.update(from_yaml(args.from_yaml))

    substrate = args.substrate or env.get("SST_SUBSTRATE", "")
    if substrate not in ALL:
        return _fail(f"SST_SUBSTRATE must be one of {', '.join(ALL)}; got {substrate or '<unset>'}")

    try:
        rendered = collect(env, substrate)
    except InputError as exc:
        return _fail(str(exc))

    body = "\n".join(rendered[v] for v in sorted(rendered))
    text = (
        "# Generated by gen_env_sh.py — do not edit.\n"
        f"# substrate: {substrate}\n"
        "# Only variables supplied by the caller are emitted; the rest keep\n"
        "# env.sh.template's defaults so the diff stays readable.\n\n"
        f"export EXECUTION_SUBSTRATE={_q(substrate)}\n\n{body}\n"
    )
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        hint = ""
        if os.path.dirname(os.path.abspath(args.out)) == os.path.abspath(os.sep):
            # The config writes to "{{ env.SST_RESULTS_DIR }}/env.acv.sh"; unset, that is "/env.acv.sh".
            hint = " -- is SST_RESULTS_DIR set? The configs write this file under it."
        return _fail(f"cannot write {args.out}: {exc}{hint}")

    digest = hashlib.sha256(
        json.dumps({k: env[k] for k in sorted(env) if k.startswith("SST_")}, sort_keys=True).encode()
    ).hexdigest()[:16]
    json.dump(
        # success/platform/cluster_name are all required by
        # isvctl/schemas/command_output.schema.json; omitting any one fails the step after it
        # has already done its work. The other steps get these from sweeplib.emit().
        {
            "success": True,
            "platform": "storage",
            "cluster_name": os.environ.get("SST_CLUSTER_NAME") or os.environ.get("CLUSTER_NAME") or "unknown",
            "env_sh_path": os.path.abspath(args.out),
            "config_hash": digest,
            "substrate": substrate,
            "resolved": {v: rendered[v].split("=", 1)[1] for v in sorted(rendered)},
        },
        sys.stdout,
        indent=2,
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
