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

"""Report which storage targets an elbencho sweep actually wrote to, per claim.

storage-scale-test v2.0.0 names the claim inside each ``TEST_DIRS`` key, and the weight is
how many target slots that claim gets: ``claim-a:scale-test=2`` places
``target-1`` and ``target-2`` under that claim's mount. Nothing else in this provider looks
at that distribution. ``collect`` counts CSVs and ``verify`` counts cells, and both pass a
two-claim sweep that silently ran everything on one claim, because every cell that exists
is healthy.

This step compares two independent records:

* **configured**: ``env_used.yaml``. ``TEST_DIRS`` is what was asked for, and
  ``KUBECTL_MAPPED_TEST_DIRS`` is the in-pod mount the driver resolved for each claim, with
  its weight.
* **observed**: the ``.out`` files. elbencho echoes its ``COMMAND LINE``, which contains
  the target paths it was actually handed. On kubectl they are
  ``<mount>/elbencho-sweep-target-<N>-<ts>-e<NNNN>``.

The observed weight of a mount is the number of DISTINCT ``target-<N>`` indexes under it.
The same slot repeats once per cell and per phase, so it is counted once.

It reports. It does not judge: whether ``weights_match`` must be true, or how many claims
must be exercised, belongs to the config's validations. Runs after ``collect``, because on
kubectl the ``.out`` files stay on the PVC until the driver publishes them.

Kubectl-only, and io-only. mdtest's ``.out`` files do not carry the same target layout.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # nvstorage-scale/scripts/ (for common.*)
from common.sweeplib import emit, fail, log

TARGET_RE = re.compile(r"(/\S+?)/elbencho-sweep-target-(\d+)-")


def _unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" else s


def read_mappings(path: str) -> dict[str, dict[str, int]]:
    """Return the ``TEST_DIRS`` and ``KUBECTL_MAPPED_TEST_DIRS`` blocks as ``{key: weight}``.

    Deliberately not PyYAML, for the same reason as ``compare_env.parse_env_used_yaml``:
    the step scripts run on a bare interpreter. That parser flattens ``TEST_DIRS`` and drops
    ``KUBECTL_MAPPED_TEST_DIRS`` entirely, so it cannot be reused here.
    """
    blocks: dict[str, dict[str, int]] = {"TEST_DIRS": {}, "KUBECTL_MAPPED_TEST_DIRS": {}}
    current: str | None = None
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            key, _, val = line.partition(":")
            current = key.strip() if key.strip() in blocks and not val.strip() else None
            continue
        if current:
            # rpartition: the key itself contains a colon ("claim:path").
            k, _, v = line.strip().rpartition(":")
            try:
                blocks[current][_unquote(k)] = int(_unquote(v))
            except ValueError:
                log(f"warning: unparseable weight in {current}: {line.strip()!r}")
    return blocks


def mount_for(key: str, mounts: list[str]) -> str | None:
    """Match a ``[namespace/]claim:path`` key to the mount the driver resolved for it."""
    claim, _, sub = key.rpartition(":")
    claim = claim.rpartition("/")[2]  # drop an explicit namespace
    suffix = "/" + "/".join(p for p in (claim, sub.strip("/")) if p)
    hits = [m for m in mounts if m.rstrip("/").endswith(suffix)]
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sweep-dir", required=True)
    ap.add_argument("--substrate", default=None, help="sets `platform` in the output")
    args = ap.parse_args()
    base = {"test_name": "storage_targets", "substrate": args.substrate}

    env_used = os.path.join(args.sweep_dir, "env_used.yaml")
    if not os.path.isfile(env_used):
        return fail(f"no env_used.yaml in {args.sweep_dir} -- did collect publish this sweep?", **base)
    blocks = read_mappings(env_used)
    configured, mapped = blocks["TEST_DIRS"], blocks["KUBECTL_MAPPED_TEST_DIRS"]
    if not configured:
        return fail(f"{env_used} records no TEST_DIRS", **base)
    if not mapped:
        return fail(f"{env_used} records no KUBECTL_MAPPED_TEST_DIRS -- kubectl substrate only", **base)

    outs = sorted(glob.glob(os.path.join(args.sweep_dir, "*.out")))
    if not outs:
        return fail(f"no .out files in {args.sweep_dir} -- nothing to observe", **base)
    seen: dict[str, set[int]] = {}
    for path in outs:
        for line in open(path, encoding="utf-8", errors="replace"):
            if not line.startswith("COMMAND LINE:"):
                continue
            for mount, idx in TARGET_RE.findall(line):
                seen.setdefault(mount, set()).add(int(idx))

    targets, unresolved = {}, []
    for key, weight in sorted(configured.items()):
        mount = mount_for(key, list(mapped))
        if mount is None:
            unresolved.append(key)
        observed = len(seen.get(mount, ())) if mount else 0
        targets[key] = {
            "mount": mount,
            "weight_configured": weight,
            "weight_mapped": mapped.get(mount) if mount else None,
            "weight_observed": observed,
        }
    # A target the sweep wrote to that no configured claim accounts for.
    stray = sorted(m for m in seen if m not in {t["mount"] for t in targets.values()})

    exercised = [k for k, t in targets.items() if t["weight_observed"] > 0]
    return emit(
        {
            **base,
            "success": True,
            "claims_configured": len(configured),
            "claims_exercised": len(exercised),
            "unexercised_claims": sorted(set(configured) - set(exercised)),
            "unresolved_claims": unresolved,
            "stray_targets": stray,
            "weights_match": not unresolved
            and not stray
            and all(t["weight_configured"] == t["weight_mapped"] == t["weight_observed"] for t in targets.values()),
            "targets": targets,
            "out_files_scanned": len(outs),
        }
    )


if __name__ == "__main__":
    sys.exit(main())
