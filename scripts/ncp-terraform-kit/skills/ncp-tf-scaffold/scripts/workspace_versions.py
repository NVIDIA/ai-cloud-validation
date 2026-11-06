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
"""The `versions.tf` of a provider test workspace.

The Terraform floor is read from the module skill, which owns it for every
Terraform file the kit writes, so a workspace and the module it tests ask for
the same Terraform.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _module_skill_scripts() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        scripts = parent / "ncp-tf-module" / "scripts"
        if scripts.is_dir():
            return scripts
    raise SystemExit("case_builder: no ncp-tf-module skill beside this one; install both skills")


sys.path.insert(0, str(_module_skill_scripts()))

import terraform_floor  # noqa: E402


def render(local_name: str, source: str, version: str) -> str:
    return (
        "terraform {\n"
        f'  required_version = "{terraform_floor.REQUIRED_VERSION}"\n'
        "  required_providers {\n"
        f"    {local_name} = {{\n"
        f'      source  = "{source}"\n'
        f'      version = "{version}"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
