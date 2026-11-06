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
"""The files `derive` owns in a layer module, and their write.

`derive` renders the whole file set and runs every refusal before it calls
`write`, so a refused run changes no file. The module directory also holds
the scaffold's `tests/cases/`, which nothing here touches: the files are
written one by one, not swapped in as a directory. The `README.md` is the
scaffold's too, and only the section that its render reserves is written here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import terraform_floor

#: Where the worked wiring goes, relative to the module directory.
EXAMPLE = Path("examples/default/main.tf")
#: The first line of a generated example. A file without it is the operator's.
EXAMPLE_HEADER = "# Worked cross-resource wiring, generated from references.yaml."
#: The module's README is the scaffold's. Its render reserves the text between
#: these two lines for `derive`, and `ncp-tf-scaffold`'s `module_readme.py`
#: holds the same two lines.
PLANNED_BEGIN = "<!-- BEGIN ncp-tf-module: derive writes this section from the provider's schema export. -->"
PLANNED_END = "<!-- END ncp-tf-module -->"

Runner = Callable[..., Any]


def render_versions(header: str, local_name: str, address: str, version: str) -> str:
    return (
        header + "terraform {\n"
        f'  required_version = "{terraform_floor.REQUIRED_VERSION}"\n'
        "  required_providers {\n"
        f'    {local_name} = {{\n      source  = "{address}"\n      version = "{version}"\n    }}\n'
        "  }\n}\n"
    )


def render_planned(not_yet_implemented: list[str]) -> str:
    """The text of the README section that `derive` owns; empty when nothing is missing."""
    if not not_yet_implemented:
        return ""
    return (
        "## Planned but not yet generated\n\n"
        "module_plan names these for this layer; the provider does not implement "
        "them yet, so this module does not wrap them. Rerun `derive` once it does.\n\n"
        + "\n".join(f"- `{t}`" for t in not_yet_implemented)
        + "\n"
    )


def filled_readme(existing: str, planned: str) -> str | None:
    """`existing` with `planned` between the markers; None when it has none."""
    start = existing.find(PLANNED_BEGIN)
    stop = existing.find(PLANNED_END, start) if start >= 0 else -1
    if stop < 0:
        return None
    return existing[: start + len(PLANNED_BEGIN)] + "\n" + planned + existing[stop:]


def _write_readme(out: Path, planned: str) -> list[str]:
    """Fill the section of the scaffold's README; a note when it has none."""
    readme = out / "README.md"
    filled = filled_readme(readme.read_text(encoding="utf-8"), planned) if readme.is_file() else None
    if filled is not None:
        readme.write_text(filled, encoding="utf-8")
        return []
    return [
        f"note: wrote no README: {readme} holds no section between {PLANNED_BEGIN} and "
        f"{PLANNED_END}. The scaffold's render writes the module's README: render the "
        "module's case with --runner-out, then run derive again"
    ]


def write(out: Path, files: dict[str, str], example: str | None, not_yet_implemented: list[str]) -> list[str]:
    """Write `files`, the example and the README section into `out`; return notes for stderr.

    With no example, a generated one from an earlier run is removed: its
    wiring no longer matches `main.tf`.
    """
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    notes = _write_readme(out, render_planned(not_yet_implemented))
    return notes + _write_example(out, example)


def _write_example(out: Path, example: str | None) -> list[str]:
    target = out / EXAMPLE
    if example is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example, encoding="utf-8")
        return []
    if not target.is_file():
        return []
    if target.read_text(encoding="utf-8").splitlines()[:1] != [EXAMPLE_HEADER]:
        return [f"note: kept {target}: no reference is confirmed, and the file is not a generated example"]
    target.unlink()
    if not any(target.parent.iterdir()):
        target.parent.rmdir()
    return []


def format_module(out: Path, run: Runner = subprocess.run) -> str | None:
    """Run `terraform fmt` on `out`; return a note when it cannot run."""
    try:
        result = run(["terraform", "fmt", str(out)], capture_output=True, text=True)
    except FileNotFoundError:
        reason = "terraform is not on the path"
    else:
        if result.returncode == 0:
            return None
        reason = result.stderr.strip()
    return f"note: terraform fmt could not run ({reason}); output is unformatted"
