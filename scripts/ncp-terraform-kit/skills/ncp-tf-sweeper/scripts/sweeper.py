#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
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
"""Render the sweeper for one NCP: `sweep.yaml` for review, and its Go core.

    uv run scripts/sweeper.py render --coverage <coverage.yaml> \
        --cases <the provider's cases directory> --out <directory> \
        [--provider-export <provider-schema-export.json>] \
        [--account-scope 'projects/{project}']

The documents are those that the coverage records. The specification index is
the scaffold's own, read from the `ncp-tf-scaffold` skill installed beside this
one, so the two skills read one document the same way. The account scope is
read the provider's way, from the `ncp-tf-provider` skill beside this one, so a
parameter that the provider fills from the scope is listed in the same scope.
The render writes every file but `adapter.go`, which the agent writes and the
render never touches. It writes `go.mod` only where none is: the sweeper is a
Go module of its own, and the agent adds the NCP's SDK to it.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _skill_scripts(skill: str) -> Path:
    """The scripts of another kit skill, installed beside this one."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        sibling = parent / skill / "scripts"
        if sibling.is_dir():
            return sibling
    raise SystemExit(f"sweeper: no {skill} skill beside this one; install the kit's skills together")


# The sweep plan matches a case's configuration path by the provider's
# conversion of a document's field name.
sys.path.append(str(_skill_scripts("ncp-tf-provider")))

import sweep_plan  # noqa: E402
import sweeper_go  # noqa: E402

ADAPTER = "adapter.go"
#: The sweeper's own module. Its `main` package is then not a second one in the
#: provider's module, whose build takes exactly one, and the adapter cannot
#: import a package of the provider. Nothing imports the sweeper, so its module
#: path is never resolved.
GO_MOD = "go.mod"
MODULE = "sweeper"
#: The Go language version the rendered core is written to. `go get` raises it
#: when the NCP's SDK needs a newer one.
GO_VERSION = "1.21"


def _sibling(skill: str, module: str) -> Any:
    """A script of another kit skill, installed beside this one."""
    sys.path.insert(0, str(_skill_scripts(skill)))
    return importlib.import_module(module)


def _account_scope(template: str | None) -> Any:
    """The provider's account scope, refused as the provider refuses it."""
    if not template:
        return None
    provider = _sibling("ncp-tf-provider", "provider_schema")
    try:
        return provider.AccountScope.parse(template)
    except provider.InputError as error:
        raise ValueError(str(error)) from error


def _cases(directory: Path, tc: Any) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Each type's reviewed cases by their name, the unnamed case first, by
    Terraform type. A named case can create its resource in another scope."""
    found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for path in sorted(directory.rglob("*.yaml")):
        case = tc.load_document(path)
        if isinstance(case, dict) and case.get("kind") == "case" and case.get("contract") == "resource":
            found.setdefault(tc.case_identity.terraform_type(case), []).append((tc.case_identity.case_name(case), case))
    return {kind: sorted(entries, key=lambda entry: entry[0]) for kind, entries in found.items()}


def _settings(export_path: str | None, tc: Any) -> dict[str, str]:
    """Each provider setting's name, with the first variable it falls back to."""
    export = tc.fixture_values.read_export(export_path, None)
    found = {}
    for _, schema in sorted((export.get("provider_schemas") or {}).items()):
        block = ((schema or {}).get("provider") or {}).get("block") or {}
        for name, attribute in (block.get("attributes") or {}).items():
            stated = tc.provider_settings.FALLBACK.search(str((attribute or {}).get("description") or ""))
            if stated:
                found[name] = tc.provider_settings.ENVIRONMENT_VARIABLE.findall(stated.group(1))[0]
    return found


def command_render(args: argparse.Namespace) -> int:
    tc = _sibling("ncp-tf-scaffold", "case_builder")
    scope = _account_scope(args.account_scope)
    coverage_path = Path(args.coverage)
    coverage = tc.load_document(coverage_path)
    sources = tc.spec_documents(coverage, coverage_path)
    try:
        types, not_built = sweep_plan.derive(
            coverage,
            _cases(Path(args.cases), tc),
            [tc.load_document(p) for p in sources],
            tc,
            _settings(args.provider_export, tc),
            scope,
        )
    except sweep_plan.PlanError as error:
        print(f"sweeper: {error}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "sweep.yaml").write_text(
        yaml.safe_dump(sweep_plan.document(types, not_built, sources), sort_keys=False, width=100), encoding="utf-8"
    )
    for name, text in sweeper_go.files(types).items():
        (out / name).write_text(text, encoding="utf-8")
    if not (out / GO_MOD).exists():
        (out / GO_MOD).write_text(f"module {MODULE}\n\ngo {GO_VERSION}\n", encoding="utf-8")
    print(f"{out}: {len(types)} types; write {ADAPTER} if it is not there", file=sys.stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sweeper", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    child = subparsers.add_parser("render", help="write sweep.yaml and the sweeper's Go core")
    child.add_argument("--coverage", required=True, help="path to coverage.yaml")
    child.add_argument("--cases", required=True, help="the provider's resource cases directory")
    child.add_argument("--out", required=True, help="the directory the sweeper lives in")
    child.add_argument("--provider-export", help="the provider's schema export, for its settings")
    child.add_argument(
        "--account-scope",
        help="the account scope the provider configuration fills, as the API writes it, e.g. 'projects/{project}'",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return command_render(args)
    except (OSError, ValueError) as error:
        print(f"sweeper: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
