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
"""A layer module's README, written with its entry point.

The render knows how the module's tests run, the module's layout is the kit's
own, and the layer's gap is in the plan that the case records. Two places are
reserved for other writers and keep what those writers put there: the planned
resources that the provider does not implement yet, which `derive` writes from
the export it is given, and the inputs and outputs, which `terraform-docs`
writes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import case_refresh
import provider_readme
import yaml
from runner_parts import RUNNER_NAME, RUNNER_SUBDIR

BEGIN = provider_readme.BEGIN
END = provider_readme.END
#: `ncp-tf-module`'s `module_files.py` holds the same two lines: `derive`
#: writes only between them.
PLANNED_BEGIN = "<!-- BEGIN ncp-tf-module: derive writes this section from the provider's schema export. -->"
PLANNED_END = "<!-- END ncp-tf-module -->"
#: The markers that `terraform-docs` writes between in its `inject` mode.
DOCS_BEGIN = "<!-- BEGIN_TF_DOCS -->"
DOCS_END = "<!-- END_TF_DOCS -->"


def render_section(*, tests: str, cases: str, gap: str) -> str:
    """The section, from the paths relative to the module's directory."""
    runner = f"./{RUNNER_SUBDIR}/{RUNNER_NAME}"
    lines = [
        BEGIN,
        "",
        "## Run the tests",
        "",
        "```sh",
        "cp .env.example .env",
        f"{runner} offline",
        f"{runner} live",
        "```",
        "",
        "Both modes read the provider from a filesystem mirror. Build it first with the"
        " provider's own `run-tests.sh build`; `.env.example` says where the mirror is. An"
        " offline run plans the module's test case with no call to the API, and needs no"
        " account and no credentials. A live run applies the module in a real account,"
        " checks each output, and destroys what it created. When a live test reads a value"
        " that the run supplies, `.env.example` lists it, and"
        f" `{tests}/RUN_VARIABLES.md` says what it names and where it goes. Add"
        " `--json FILE` to write the result stream that"
        " `ncp-tf-verify-report record` reads.",
        "",
        "## What this module holds",
        "",
        "| Path | What it holds | Written by |",
        "|---|---|---|",
        "| `main.tf` | one resource block for each resource type, with `for_each` over its"
        " variable | `ncp-tf-module` |",
        "| `variables.tf` | one map variable for each resource type | `ncp-tf-module` |",
        "| `outputs.tf` | one output for each resource type: the whole resource object, with"
        " the keys of its variable | `ncp-tf-module` |",
        "| `versions.tf` | the provider that the resources need, and the Terraform version | `ncp-tf-module` |",
        "| `examples/default/main.tf` | the wiring of each confirmed reference, as comments;"
        " only with a confirmed reference | `ncp-tf-module` |",
        f"| `{cases}/` | the module's test case, which a person reviews | `ncp-tf-scaffold` |",
        f"| `{tests}/` | the tests that the render writes from the case | `ncp-tf-scaffold` |",
        f"| `{RUNNER_SUBDIR}/{RUNNER_NAME}`, `.env.example` | the entry point and the values it"
        " reads | `ncp-tf-scaffold` |",
        "",
        "A file that `ncp-tf-module` or the render writes is written again at its next run:"
        " change the plan, the case or `references.yaml`, not the file. `ncp-tf-module` lists"
        " below each planned"
        " resource that the provider does not implement yet, and `terraform-docs` writes the"
        " inputs and the outputs below.",
        "",
    ]
    if gap:
        lines += ["## What this layer does not cover", "", gap.strip(), ""]
    return "\n".join([*lines, END]) + "\n"


def updated(existing: str | None, section: str, title: str) -> str:
    """The README with `section` in place, and each reserved place added once."""
    text = provider_readme.updated(existing, section, title)
    for begin, end in ((PLANNED_BEGIN, PLANNED_END), (DOCS_BEGIN, DOCS_END)):
        if begin not in text:
            text = text.rstrip("\n") + f"\n\n{begin}\n{end}\n"
    return text


def layer_gap(path: Path, case: dict[str, Any], cwd: Path) -> str:
    """The `gap` of the case's layer, in the plan that the case records.

    The render's staleness check has found that plan already. Only a case that
    a test builds by hand records none, and it states no gap.
    """
    if "derived_from" not in case:
        return ""
    coverage = yaml.safe_load(case_refresh.recorded_inputs(path, case, cwd)[0].read_text(encoding="utf-8")) or {}
    layers = (coverage.get("module_plan") or {}).get("layers") or {}
    return str((layers.get(case.get("module")) or {}).get("gap") or "")


def write(directory: Path, tests: Path, cases: Path, gap: str) -> Path:
    """Write the section into `directory`'s README.md, and return its path."""
    readme = directory / "README.md"
    existing = readme.read_text(encoding="utf-8") if readme.exists() else None
    shown = {
        name: Path(os.path.relpath(where, directory)).as_posix() for name, where in (("tests", tests), ("cases", cases))
    }
    section = render_section(**shown, gap=gap)
    readme.write_text(updated(existing, section, directory.resolve().name), encoding="utf-8")
    return readme
