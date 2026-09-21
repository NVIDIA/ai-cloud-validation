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

"""Tests for the structured-requirements Markdown renderer."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parent.parent / "requirements_source_to_md.py"
_spec = importlib.util.spec_from_file_location("requirements_source_to_md", _SCRIPT)
assert _spec and _spec.loader
requirements_source_to_md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(requirements_source_to_md)


def test_project_prd_format_renders_a_uniquely_named_source() -> None:
    """A project PRD keeps a unique trace source while sharing a generic renderer."""
    source_path = requirements_source_to_md.REQ_DIR / "network-operator-readiness-requirements.yaml"
    document = yaml.safe_load(source_path.read_text(encoding="utf-8"))

    rendered = requirements_source_to_md.RENDERERS[document["format"]](document, source_path.name)

    assert source_path in requirements_source_to_md.DEFAULT_SOURCES
    assert document["source"] == "network-operator-prd"
    assert document["format"] == "project-prd"
    assert "# Enterprise RA Network Operator Self-Validation Integration PRD" in rendered
    assert "> Owner: NVIDIA Network Operator team." in rendered
    assert "## Network Validation" in rendered
    assert "| ENT-REQ-008 | GPUDirect RDMA |" in rendered


def test_render_rejects_a_non_string_source(tmp_path: Path) -> None:
    """A malformed source fails clearly before renderer dispatch."""
    source_path = tmp_path / "malformed-requirements.yaml"
    source_path.write_text("source: null\nrequirements: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must declare a string source"):
        requirements_source_to_md.render(source_path)


def test_render_rejects_an_unknown_document_format(tmp_path: Path) -> None:
    """A unique source may select only a documented reusable renderer format."""
    source_path = tmp_path / "malformed-requirements.yaml"
    source_path.write_text(
        "source: team-prd\nformat: not-a-renderer\nrequirements: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported format 'not-a-renderer'"):
        requirements_source_to_md.render(source_path)
