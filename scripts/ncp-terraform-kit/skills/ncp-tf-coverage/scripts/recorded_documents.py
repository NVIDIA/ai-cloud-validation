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
"""Where a `spec_documents` entry points, by the rule the scaffold step reads it.

The coverage gate and the scaffold must agree on which file an entry names.
If the gate matched a looser rule, it could pass an analysis that the scaffold
then refuses one step later, where the agent that wrote the entry is gone.

Pure path logic: the file system is the only input.
"""

from __future__ import annotations

from pathlib import Path


def locate(name: str, coverage_path: Path) -> Path | None:
    """The file that `name` names, or None when no file is there.

    The artifact states repository-relative paths and does not know where the
    repository is, so the search starts beside the coverage and walks up. Keep
    this rule the same as `recorded_specs._beside` in the scaffold skill.
    """
    if not name:
        return None
    candidate = coverage_path.parent / Path(name).name
    if candidate.is_file():
        return candidate.resolve()
    for parent in coverage_path.resolve().parents:
        candidate = parent / name
        if candidate.is_file():
            return candidate.resolve()
    return None
