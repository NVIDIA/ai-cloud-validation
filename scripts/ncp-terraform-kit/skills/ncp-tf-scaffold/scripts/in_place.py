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

"""The fields the plan updates in place, as the provider derives them.

A field that the provider does not mark as replacing is one that a change
updates in place. When no sentence of the documents says so, only a live
update shows a wrong one before a real user does, so the live update owes
each of them. The provider's derivation decides; this reads its result and
keeps no rule of its own. A map, such as `labels`, is owed whole; an object is
owed by each settable field inside it, so a field fixed at the create inside
an object stays out while the rest of the object is changed.

A field that the update request sends at another path is left to the agent,
which records its request path under the provider data's `update_mappings`.
The provider updates such a field in place, so the live update owes each
mapped one. Without that file the derivation owes none of them: no document
says which request field changes it.

The provider's reviewed schema holds the review's `naming` answers. A field
that an answer says names the object replaces it, so the update does not owe
it. Without that file the derivation applies no answer: none is guessed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

#: The flags of an attribute that a configuration sets.
SETTABLE = ("required", "optional")


def provider_scripts() -> Path:
    """The sibling skill's scripts. The skills install together, so the
    sibling is always beside this one."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        sibling = parent / "ncp-tf-provider" / "scripts"
        if sibling.is_dir():
            return sibling
    raise SystemExit(
        f"case_builder: no ncp-tf-provider skill beside {here.parents[1]}. The live update owes the "
        f"fields that the provider updates in place; install both skills"
    )


class ReviewError(ValueError):
    """The named provider schema cannot give the review's answers."""


def review_answers(path: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """The `naming` answers of the provider's reviewed schema, by type and parameter."""
    if path is None:
        return {}
    sys.path.insert(0, str(provider_scripts()))
    import reviewed

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewError(f"cannot read the provider schema {path}: {error}") from error
    if not reviewed.is_derivation(document):
        raise ReviewError(
            f"{path} is not a schema derivation; name the provider's reviewed "
            f"provider-schema.json, which its `schema` command writes"
        )
    return reviewed.naming_answers(document)


def update_mappings(path: Path | None) -> dict[str, tuple[str, ...]]:
    """The paths that the provider data's `update_mappings` maps, by type: each
    one a field's path in the object that the read returns."""
    if path is None:
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewError(f"cannot read the provider data {path}: {error}") from error
    resources = document.get("resources") if isinstance(document, dict) else None
    if not isinstance(resources, list):
        raise ReviewError(
            f"{path} has no `resources` list; name the provider's per-resource data, "
            f"whose entries hold `update_mappings`"
        )
    return {
        str(entry["terraform_type"]): tuple(entry.get("update_mappings") or {})
        for entry in resources
        if isinstance(entry, dict) and entry.get("terraform_type")
    }


def derive(
    coverage: dict[str, Any],
    spec_files: tuple[Path, ...],
    provider_schema_file: Path | None = None,
    provider_data_file: Path | None = None,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Each planned type's fields that a change updates in place, as pairs of
    the document's path and the configuration's path, with the review's
    answers of `provider_schema_file` and the update mappings of
    `provider_data_file` when they are given."""
    answers = review_answers(provider_schema_file)
    mapped = update_mappings(provider_data_file)
    sys.path.insert(0, str(provider_scripts()))
    import attribute_names
    import provider_schema
    from operation_facts import steers

    methods, schemas = provider_schema.load_documents(list(spec_files))
    found: dict[str, tuple[tuple[str, str], ...]] = {}
    for resource in provider_schema.derive(coverage, methods, schemas, answers=answers):
        left = set(resource.replacement_left_to_agent)
        wrapper = ((resource.operations.get("create") or {}).get("wraps_resource_under") or {}).get("field", "")
        keys = [f"{wrapper}.{key}" if wrapper else key for key in mapped.get(resource.terraform_type, ())]
        top = [a for a in resource.attributes if a.source != provider_schema.FROM_PARAMETER]
        paths = [
            path for attribute in top if attribute.name not in left for path in _paths(attribute, "", left, steers)
        ]
        paths += [path for attribute in top for key in keys for path in _mapped(attribute, key.split("."), "", steers)]
        found[resource.terraform_type] = tuple(
            (path, attribute_names.configuration_path(path)) for path in dict.fromkeys(paths)
        )
    return found


def _mapped(attribute: Any, parts: list[str], prefix: str, steers: Any) -> list[str]:
    """The paths that a change updates in place under the mapped path `parts`,
    which starts at `attribute`."""
    if attribute.name != parts[0] or not _changes(attribute, steers):
        return []
    if len(parts) == 1:
        return _paths(attribute, prefix, set(), steers)
    path = f"{prefix}{attribute.name}."
    return [found for child in attribute.attributes for found in _mapped(child, parts[1:], path, steers)]


def _changes(attribute: Any, steers: Any) -> bool:
    """A configuration sets it, and a change of it does not replace the object."""
    return (
        bool(set(SETTABLE) & set(attribute.terraform_flags))
        and not attribute.forces_replacement
        and not steers(attribute.name, attribute.description)
    )


def _paths(attribute: Any, prefix: str, left: set[str], steers: Any) -> list[str]:
    """The path of each field under `attribute` that a change updates in place."""
    if not _changes(attribute, steers) or attribute.name in left:
        return []
    path = f"{prefix}{attribute.name}"
    if not attribute.attributes:
        return [path]
    return [found for child in attribute.attributes for found in _paths(child, f"{path}.", left, steers)]
