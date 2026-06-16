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

"""Render a source-requirements YAML to a publishable Markdown listing.

YAML is the source of record (queryable, key-clean); Markdown is the published,
Google-Docs-friendly view. Handles both the `offtake` and `reference` sources.

Usage:
    python3 scripts/requirements_source_to_md.py docs/requirements/offtake-requirements.yaml
    python3 scripts/requirements_source_to_md.py            # render both known sources
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REQ_DIR = REPO_ROOT / "docs" / "requirements"
DEFAULT_SOURCES = [REQ_DIR / "offtake-requirements.yaml", REQ_DIR / "software-reference-requirements.yaml"]

GENERATED_BANNER = "<!-- GENERATED FILE - DO NOT EDIT BY HAND. Source: {src}. Run `make plan`. -->"


def cell(val: Any) -> str:
    """Escape a value for a Markdown table cell."""
    return str(val if val is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def heading(out: list[str], text: str) -> None:
    """Append a heading with a blank line before and after."""
    if out and out[-1] != "":
        out.append("")
    out += [text, ""]


def render_offtake(doc: dict[str, Any], src_name: str) -> str:
    """Render the offtake listing, grouped by section -> subsection."""
    out = [
        GENERATED_BANNER.format(src=src_name),
        "",
        f"# {doc.get('title', 'Offtake Requirements')}",
        "",
        f"> Structured source of record: `{src_name}` (version {doc.get('version', 'n/a')}).",
        "> Curated, in-repo copy of the publicly-published *NVIDIA Requirements Guide",
        "> for AI Clouds*. Edit the YAML, not this file.",
        "",
    ]
    section = subsection = None
    for r in doc.get("requirements", []):
        if r.get("section") != section:
            section = r.get("section")
            heading(out, f"## {section}")
            subsection = None
        if r.get("subsection") != subsection:
            subsection = r.get("subsection")
            if subsection:
                heading(out, f"### {subsection}")
            out += [
                "| Req ID | Requirement Area | Description | Test details | Status |",
                "| :----- | :--------------- | :---------- | :----------- | :----- |",
            ]
        out.append(
            f"| {cell(r.get('req_id'))} | {cell(r.get('area'))} | {cell(r.get('description'))} "
            f"| {cell(r.get('test_details'))} | {cell(r.get('status', 'active'))} |"
        )
    return "\n".join(out) + "\n"


def render_reference(doc: dict[str, Any], src_name: str) -> str:
    """Render the reference listing, grouped by domain -> component."""
    out = [GENERATED_BANNER.format(src=src_name), "", f"# {doc.get('title', 'Software Reference Requirements')}", ""]
    if doc.get("preamble"):
        out += [doc["preamble"], ""]
    domain = component = None
    for r in doc.get("requirements", []):
        if r.get("domain") != domain:
            domain = r.get("domain")
            heading(out, f"## {domain}")
            component = None
        if r.get("component") != component:
            component = r.get("component")
            heading(out, f"### {component}")
            out += [
                "| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |",
                "| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |",
            ]
        out.append(
            f"| {cell(r.get('req_id'))} | {cell(r.get('component'))} | {cell(r.get('description'))} "
            f"| {cell(r.get('reference_mapping'))} | `{cell(r.get('covers_test'))}` | {cell(r.get('status'))} |"
        )
    return "\n".join(out) + "\n"


RENDERERS = {"offtake": render_offtake, "reference": render_reference}


def render(path: Path) -> None:
    """Render a single source YAML to its sibling `.md`."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path} must contain a single mapping document")
    source = doc.get("source")
    renderer = RENDERERS.get(source)
    if renderer is None:
        raise ValueError(f"{path} has unsupported source {source!r} (expected one of {sorted(RENDERERS)})")
    out_path = path.with_suffix(".md")
    out_path.write_text(renderer(doc, path.name), encoding="utf-8")
    print(f"Wrote {out_path}")


def main() -> None:
    """Render the given source YAML(s), or both known sources by default."""
    targets = [Path(a) for a in sys.argv[1:]] or DEFAULT_SOURCES
    for t in targets:
        if t.exists():
            render(t)
        else:
            print(f"skip (not found): {t}", file=sys.stderr)


if __name__ == "__main__":
    main()
