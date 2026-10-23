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

"""Compare two storage-scale-test ``env.sh``-shaped files over their shared keys.

Why this exists
---------------
The obvious round-trip test — "generate an ``env.sh`` and diff it against a recorded
``env_used.sh``" — cannot pass, for two reasons discovered by reading
``lib/env_functions.sh:1275`` (``write_elbencho_env_used``):

1. ``env_used.{sh,yaml}`` is a *sweep-relevant subset*. It records ``TEST_DIRS``,
   ``FS_MAX_*`` and the ``ELBENCHO_*`` family, and omits ``RESULTS_DIR``, ``LOGS_DIR``,
   ``partition``, ``run_time``, ``account``, ``client_type``, ``client_arch`` and every
   ``SLURM_*``.
2. It also records values *derived at run time* from CLI flags — ``dio_or_bio``,
   ``rand_option``, ``single_option``, ``sweep_*``, ``nodes_spec``, ``treefile_cache`` —
   which a config generator must never emit.

So the round-trip is a subset comparison over the generator-owned intersection, and this
tool is that comparison. The Slurm/path half is covered separately by a golden file.

Values are normalised before comparison so that cosmetic bash differences (quoting,
spacing, ``export`` prefix, array element quoting) do not produce false failures.
"""

from __future__ import annotations

import argparse
import re
import sys

# Derived-at-runtime fields that appear in env_used.* but are never generator output.
DEFAULT_IGNORE = (
    "dio_or_bio",
    "rand_option",
    "single_option",
    "sweep_write_only",
    "sweep_write_no_read",
    "sweep_read_from",
    "nodes_spec",
    "treefile_cache",
)

_ASSIGN = re.compile(r"^\s*(?:export\s+)?(?:declare\s+-A\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _strip_inline_comment(raw: str) -> str:
    """Drop a trailing ``# ...`` comment that is outside quotes and parentheses."""
    out, quote, depth = [], None, 0
    for ch in raw:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "#" and depth == 0:
            break
        out.append(ch)
    return "".join(out).strip()


def normalise(value: str) -> str:
    """Reduce a bash rvalue to a comparable canonical form.

    Arrays become space-joined bare elements; assoc arrays become sorted ``k=v`` pairs;
    scalars are unquoted. This makes ``("1" "4")`` and ``( 1 4 )`` compare equal, which is
    what we want — we are testing configuration equivalence, not byte formatting.
    """
    v = _strip_inline_comment(value)
    if v.startswith("(") and v.endswith(")"):
        inner = v[1:-1].strip()
        # Associative: ["key"]=val
        pairs = re.findall(r"\[\s*(\"[^\"]*\"|'[^']*'|[^\]\s]+)\s*\]\s*=\s*(\S+)", inner)
        if pairs:
            norm = sorted(f"{_unquote(k)}={_unquote(val)}" for k, val in pairs)
            return "{" + ",".join(norm) + "}"
        elems = re.findall(r"\"[^\"]*\"|'[^']*'|\S+", inner)
        return "[" + " ".join(_unquote(e) for e in elems) + "]"
    return _unquote(v)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def parse_env_sh(path: str) -> dict[str, str]:
    """Parse assignments from an env.sh-shaped file. Multi-line arrays are joined."""
    text = open(path, encoding="utf-8", errors="replace").read()
    # Join arrays that span lines so the line-based scan below sees one assignment.
    text = re.sub(r"=\(\s*\n", "=(", text)
    text = re.sub(r"\n\s*\)", ")", text)
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if m:
            out[m.group(1)] = normalise(m.group(2))
    return out


def parse_env_used_yaml(path: str) -> dict[str, str]:
    """Minimal parser for the specific shapes ``write_elbencho_env_used`` emits.

    Deliberately not PyYAML: this must run with no third-party deps (and PyYAML is not
    installed in this checkout). Handles ``KEY: value``, ``KEY: [a, b]``, and the two
    nested blocks (``TEST_DIRS:`` mapping, ``treefile_cache:``).
    """
    out: dict[str, str] = {}
    block: str | None = None
    assoc: dict[str, str] = {}
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            if block == "TEST_DIRS" and assoc:
                out["TEST_DIRS"] = "{" + ",".join(sorted(f"{k}={v}" for k, v in assoc.items())) + "}"
                assoc = {}
            block = None
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if val == "":
                block = key
                if key != "TEST_DIRS":
                    out.setdefault(key, "")
                continue
            if val.startswith("[") and val.endswith("]"):
                elems = [e.strip() for e in val[1:-1].split(",") if e.strip()]
                out[key] = "[" + " ".join(_unquote(e) for e in elems) + "]"
            else:
                out[key] = _unquote(val)
        else:
            if block == "TEST_DIRS" and ":" in line:
                k, _, v = line.strip().partition(":")
                assoc[_unquote(k)] = _unquote(v)
    if block == "TEST_DIRS" and assoc:
        out["TEST_DIRS"] = "{" + ",".join(sorted(f"{k}={v}" for k, v in assoc.items())) + "}"
    return out


def load(path: str) -> dict[str, str]:
    return parse_env_used_yaml(path) if path.endswith((".yaml", ".yml")) else parse_env_sh(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("generated", help="env.sh produced by gen_env_sh.py")
    ap.add_argument("recorded", help="recorded env_used.sh or env_used.yaml")
    ap.add_argument("--keys-from", help="restrict comparison to keys present in this file")
    ap.add_argument(
        "--ignore",
        default=",".join(DEFAULT_IGNORE),
        help="comma-separated keys to skip (default: derived runtime fields)",
    )
    ap.add_argument(
        "--require-min", type=int, default=1, help="fail if fewer than this many keys were actually compared"
    )
    args = ap.parse_args()

    ignore = {k.strip() for k in args.ignore.split(",") if k.strip()}
    gen, rec = load(args.generated), load(args.recorded)
    keys = set(load(args.keys_from)) if args.keys_from else set(gen) & set(rec)
    keys = (keys & set(gen) & set(rec)) - ignore

    if len(keys) < args.require_min:
        print(f"FAIL: only {len(keys)} comparable key(s); expected >= {args.require_min}", file=sys.stderr)
        print(
            "      (a near-empty intersection usually means the generator emitted nothing, "
            "not that everything matched)",
            file=sys.stderr,
        )
        return 2

    bad = [(k, gen[k], rec[k]) for k in sorted(keys) if gen[k] != rec[k]]
    for k, g, r in bad:
        print(f"MISMATCH {k}\n  generated: {g}\n  recorded : {r}", file=sys.stderr)

    only_rec = sorted((set(rec) - set(gen)) - ignore)
    if only_rec:
        print(
            f"note: {len(only_rec)} recorded key(s) not emitted by the generator "
            f"(expected for derived/substrate-specific values): {', '.join(only_rec)}"
        )

    if bad:
        print(f"FAIL: {len(bad)}/{len(keys)} shared keys differ", file=sys.stderr)
        return 1
    print(f"OK: {len(keys)}/{len(keys)} shared keys match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
