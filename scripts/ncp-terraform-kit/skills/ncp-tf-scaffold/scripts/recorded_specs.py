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
"""The specifications a coverage records, found on disk and checked against it."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class SpecError(ValueError):
    """A recorded specification cannot be found, or is not the one the coverage read."""


def documents(coverage: dict[str, Any], coverage_path: Path) -> list[Path]:
    """The specifications this coverage was derived from, as it recorded them.

    Read back from the artifact rather than asked for again. A case derived from
    a different set of documents than the analysis was would disagree with it,
    and nothing downstream could tell. So each file found must have the digest
    the coverage recorded.
    """
    declared = coverage.get("spec_documents") or {}
    if not declared:
        raise SpecError(
            f"{coverage_path} records no spec_documents; the coverage step "
            "writes them, so re-run it rather than naming the documents by hand"
        )
    found = []
    for api, entry in sorted(declared.items()):
        entry = entry or {}
        path = _beside(str(entry.get("file") or ""), api, coverage_path)
        recorded = str(entry.get("digest") or "")
        if not recorded:
            raise SpecError(
                f"{coverage_path} records no digest for the {api} specification, so "
                "nothing says which revision the coverage read; re-run the coverage step"
            )
        actual = digest(path)
        if actual != recorded:
            raise SpecError(
                f"read {path} for the {api} specification: its digest is {actual}, and "
                f"{coverage_path} records {recorded}. Remove a stale copy, or re-run "
                "the coverage step on the document you mean"
            )
        found.append(path)
    return found


def digest(path: Path) -> str:
    """A specification's digest, in the form the coverage step records it."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _beside(name: str, api: str, coverage_path: Path) -> Path:
    """A document the coverage names, found from where the coverage itself sits.

    The artifact states repository-relative paths and does not know where the
    repository is, so the search starts beside the coverage and walks up.
    """
    if not name:
        raise SpecError(f"coverage records no file for the {api} specification")
    candidate = coverage_path.parent / Path(name).name
    if candidate.is_file():
        return candidate
    for parent in coverage_path.parents:
        candidate = parent / name
        if candidate.is_file():
            return candidate
    raise SpecError(
        f"the {api} specification, {name}, is not beside {coverage_path} nor "
        "above it; fetch the specifications the coverage was built from"
    )
