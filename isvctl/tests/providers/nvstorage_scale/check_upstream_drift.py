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

"""Detect drift between ``gen_env_sh.py``'s SPEC and the upstream storage-scale-test.

Why this exists
---------------
``SPEC`` is a hand-maintained copy of upstream's variable names. If upstream renames a
variable, the generator keeps emitting the old name: ``env.sh`` sets something nobody reads
and **the sweep silently runs with defaults**. That is the exact failure the generator's own
docstring warns about, turned back on the generator itself -- and neither of the two strongest
offline tests can see it:

* the **golden file** compares the generator against *itself*;
* the **round-trip fixture** is a frozen recording, so it only catches a rename
  if somebody re-captures it, and nothing prompts that.

So this is the only check that looks at upstream at all. Run it whenever the vendored checkout
moves. The kubectl substrate merged in PR #5 and batch mode in PR #7, both of which
touched ``env.sh.template``.

What counts as "present upstream"
---------------------------------
A variable is present if it is assigned *or documented* anywhere in ``env.sh.template`` or
``lib/env_base.sh``, including **commented-out** declarations. That matters: four
``ELBENCHO_SINGLE_BIG_FILE*`` variables ship commented at ``env.sh.template:200-203`` and are
real, settable configuration. A checker that only counted live assignments would report them
as drift on every run and be ignored within a week.

Exit codes: 0 clean, 1 drift found, 2 could not check (missing checkout).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# Files that between them define or document every configurable variable.
UPSTREAM_FILES = ("env.sh.template", os.path.join("lib", "env_base.sh"))

# Assignment or documented-assignment, with or without `export`, `declare -A`, or a leading `#`.
_ASSIGN = re.compile(r"^\s*#?\s*(?:export\s+|declare\s+-A\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*=", re.M)
_DECLARE = re.compile(r"^\s*#?\s*declare\s+-A\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
# SPEC rows look like:  "SST_FOO": ("FOO", "str", ALL),   or  ("FOO", "str", (KUBECTL,)),
_SPEC_ROW = re.compile(r'"(SST_[A-Z0-9_]+)"\s*:\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,\s*"\w+"\s*,\s*([^)]*\)?)')


def _read_upstream(base: str, rel: str, ref: str | None) -> str | None:
    """File contents from the working tree, or from a git ref when one is given."""
    if ref:
        proc = subprocess.run(["git", "-C", base, "show", f"{ref}:{rel}"], capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else None
    try:
        with open(os.path.join(base, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def upstream_names(base: str, ref: str | None = None) -> tuple[set[str], list[str]]:
    """Every variable name upstream assigns or documents, and the files actually read."""
    names: set[str] = set()
    read: list[str] = []
    for rel in UPSTREAM_FILES:
        text = _read_upstream(base, rel, ref)
        if text is None:
            continue
        read.append(rel)
        names |= set(_ASSIGN.findall(text)) | set(_DECLARE.findall(text))
    return names, read


def spec_rows(gen_path: str) -> list[tuple[str, str, bool]]:
    """Return ``(sst_key, env_var, kubectl_only)`` per SPEC entry.

    The substrate tuple matters: entries gated to ``(KUBECTL,)`` *may* target variables that
    exist only on a newer upstream ref than the one checked. Reporting those as drift against a
    main checkout would make the tool cry wolf on every run, which is how checkers get ignored.

    That exemption is conditional, not permanent. ``main`` reaches the probe in ``main()``:
    once upstream defines any ``KUBECTL_*`` name the substrate has landed, and a kubectl-only
    entry that is still absent is a **removal**, which is drift.
    """
    with open(gen_path, encoding="utf-8") as fh:
        rows = _SPEC_ROW.findall(fh.read())
    out = []
    for key, var, subs in rows:
        subs = subs.strip().rstrip(",)")
        out.append((key, var, "KUBECTL" in subs and "ALL" not in subs and "SLURM" not in subs and "SSH" not in subs))
    return out


# The provider this guards. This file lives with its tests in
# isvctl/tests/providers/nvstorage_scale/.
PROVIDER = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "configs", "providers", "nvstorage-scale"
    )
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--scale-test-base",
        default=os.path.abspath(os.path.join(PROVIDER, "..", "..", "..", "..", "..", "storage-scale-test")),
        help="storage-scale-test checkout to check against",
    )
    ap.add_argument("--generator", default=os.path.join(PROVIDER, "scripts", "storage", "gen_env_sh.py"))
    ap.add_argument("--ref", help="check against this git ref instead of the working tree (e.g. origin/main)")
    ap.add_argument(
        "--strict-kubectl", action="store_true", help="treat kubectl-only entries as drift even without --ref"
    )
    ap.add_argument(
        "--list-unmapped",
        action="store_true",
        help="also list upstream ELBENCHO_*/FS_MAX_* names the SPEC does not map",
    )
    args = ap.parse_args()

    base = os.path.abspath(args.scale_test_base)
    names, read = upstream_names(base, args.ref)
    if not read:
        print(f"CANNOT CHECK: no {' or '.join(UPSTREAM_FILES)} under {base}", file=sys.stderr)
        print("  pass --scale-test-base <checkout>", file=sys.stderr)
        return 2

    rows = spec_rows(os.path.abspath(args.generator))
    if not rows:
        print("CANNOT CHECK: parsed zero SPEC rows -- the SPEC literal's shape changed", file=sys.stderr)
        return 2

    absent = [(k, v, kube) for k, v, kube in rows if v not in names]
    # Has the kubectl substrate actually landed in the thing we are checking? If upstream
    # defines or documents ANY KUBECTL_* name, it has -- and "pending upstream merge" stops
    # being a valid excuse for a kubectl-only SPEC entry being absent. Before this probe the
    # tool could not tell "not added yet" from "deliberately removed", so it filed both under
    # pending: storage-scale-test v2.0.0 deleted KUBECTL_PVC and KUBECTL_PV
    # (lib/_kubectl_claims.sh) and this check reported them as "expected absent from main"
    # while the generator kept emitting them. Only a live cluster run caught it.
    kubectl_landed = any(n.startswith("KUBECTL_") for n in names)
    exempt = not (args.ref or args.strict_kubectl or kubectl_landed)
    pending = [r for r in absent if r[2]] if exempt else []
    missing = [(k, v) for k, v, kube in absent if (k, v, kube) not in pending]

    where = f"{args.ref}:" if args.ref else "working tree "
    print(f"checked {len(rows)} SPEC entries against {where}{', '.join(read)} ({len(names)} names upstream)")

    if pending:
        print(f"\npending upstream merge ({len(pending)}) -- kubectl-only, expected absent from main:")
        for key, var, _ in pending:
            print(f"  {key:34s} -> {var}")
        print("  re-check them with: --ref origin/main")

    if missing:
        print(f"\nDRIFT: {len(missing)} SPEC target(s) no longer exist upstream:", file=sys.stderr)
        if kubectl_landed:
            print(
                "  (upstream defines KUBECTL_* names, so the kubectl substrate HAS landed:\n"
                "   an absent kubectl-only entry is a removal, not a pending merge.)",
                file=sys.stderr,
            )
        for key, var in missing:
            print(f"  {key:34s} -> {var}", file=sys.stderr)
        print(
            "\nEach one means the generator emits a variable nothing reads, so the sweep runs\n"
            "with that setting at its default and reports success. Re-map or remove them.",
            file=sys.stderr,
        )
        return 1

    if args.list_unmapped:
        # Informational only: upstream knobs ACV cannot currently reach. Never an error --
        # most are intentionally out of scope.
        interesting = sorted(
            n
            for n in names
            if n.startswith(("ELBENCHO_", "FS_MAX_", "KUBECTL_", "SSH_")) and n not in {v for _, v, _ in rows}
        )
        print(f"\nunmapped upstream knobs ({len(interesting)}) -- informational:")
        for n in interesting:
            print(f"  {n}")

    print("OK: every SPEC target still exists upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
