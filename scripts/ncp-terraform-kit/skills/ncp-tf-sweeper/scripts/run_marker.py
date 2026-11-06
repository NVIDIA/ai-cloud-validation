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
"""Where a listed resource carries the run id, read from the documents.

A run writes its id into what the client chooses at create: the name, another
field such as an email, or a label. The reviewed case says which create value
holds `{run_id}`; the documents say whether the listed resource returns that
value, and under which field. The case's keys are the documents' field names in
snake case, so each key is matched to the field whose snake case it is. A
top-level key that Terraform reserves has the provider's trailing underscore.
"""

from __future__ import annotations

import re
from typing import Any

import attribute_names
import server_assigned

RUN_ID = "{run_id}"
_VERBS = frozenset({"get", "put", "post", "delete", "patch"})


def run_values(values: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Each key path of a create whose value holds the run id. A list item is
    not a field that the list returns as one value, so lists are not read."""
    found: list[tuple[str, ...]] = []
    for key, value in values.items():
        here = (*prefix, str(key))
        if isinstance(value, dict):
            found += run_values(value, here)
        elif isinstance(value, str) and RUN_ID in value:
            found.append(here)
    return found


def dependency_values(
    values: dict[str, Any], variables: Any, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], str]]:
    """Each key path of a create whose whole value is the placeholder of a
    declared dependency resource, with its variable. The test creates that
    resource with the run id in its name field when the variable is unset."""
    found: list[tuple[tuple[str, ...], str]] = []
    for key, value in values.items():
        here = (*prefix, str(key))
        if isinstance(value, dict):
            found += dependency_values(value, variables, here)
        elif isinstance(value, str) and (named := re.fullmatch(r"\{([a-z_][a-z0-9_]*)\}", value)):
            if named.group(1) in variables:
                found.append((here, named.group(1)))
    return found


def _json(content: Any) -> dict[str, Any]:
    for media in (content or {}).values():
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"]
    return {}


def _resolve(node: Any, schemas: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """A schema and its name, through a `$ref` of either language."""
    if not isinstance(node, dict):
        return {}, ""
    reference = str(node.get("$ref") or "")
    if not reference:
        return node, str(node.get("title") or "")
    name = reference.rsplit("/", 1)[-1]
    return schemas.get(name) or {}, name


class Documents:
    """The request and the answer of each operation, by the index's reference."""

    def __init__(self, documents: list[Any]) -> None:
        self._methods: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
        self._parameters: dict[str, dict[str, dict[str, Any]]] = {}
        for document in documents:
            if not isinstance(document, dict):
                continue
            if document.get("resources") and document.get("name"):
                self._discovery(document)
            elif document.get("paths"):
                self._openapi(document)

    def _discovery(self, document: dict[str, Any]) -> None:
        api, schemas = str(document["name"]), document.get("schemas") or {}

        def walk(resources: Any, path: str) -> None:
            for name, body in (resources or {}).items():
                here = f"{path}.{name}" if path else name
                for method, meta in (body.get("methods") or {}).items():
                    self._methods[f"op:{api}.{here}.{method}"] = (
                        meta.get("request") or {},
                        meta.get("response") or {},
                        schemas,
                    )
                    self._parameters[f"op:{api}.{here}.{method}"] = {
                        str(n): s for n, s in (meta.get("parameters") or {}).items() if isinstance(s, dict)
                    }
                walk(body.get("resources"), here)

        walk(document.get("resources"), "")

    def _openapi(self, document: dict[str, Any]) -> None:
        schemas = (document.get("components") or {}).get("schemas") or {}
        for path, item in (document.get("paths") or {}).items():
            for verb, meta in (item or {}).items():
                if verb.lower() not in _VERBS or not isinstance(meta, dict):
                    continue
                answers = meta.get("responses") or {}
                answer = next(
                    (
                        _json((answers[code] or {}).get("content"))
                        for code in sorted(answers)
                        if str(code).startswith("2")
                    ),
                    {},
                )
                ref = f"op:{meta.get('operationId') or f'{verb}{path}'}"
                self._methods[ref] = (_json((meta.get("requestBody") or {}).get("content")), answer, schemas)
                self._parameters[ref] = {
                    str(s.get("name")): s
                    for s in list(meta.get("parameters") or []) + list(item.get("parameters") or [])
                    if isinstance(s, dict) and s.get("in") in ("path", "query")
                }

    def _listed(self, list_ref: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
        """The schema of one resource in the list's answer: the answer's one
        array of objects. Empty when the answer declares none."""
        _, answer, schemas = self._methods.get(list_ref, ({}, {}, {}))
        schema, _ = _resolve(answer, schemas)
        if schema.get("type") == "array":
            item, name = _resolve(schema.get("items"), schemas)
            return item, name, schemas
        arrays = [
            _resolve(p.get("items"), schemas)
            for p in (schema.get("properties") or {}).values()
            if isinstance(p, dict) and p.get("type") == "array"
        ]
        arrays = [found for found in arrays if found[0].get("properties")]
        return (*arrays[0], schemas) if len(arrays) == 1 else ({}, "", schemas)

    def _placed(self, create_ref: str, list_ref: str, keys: tuple[str, ...]) -> tuple[list[str] | None, bool]:
        """The request field that a create value goes to, as a path of the
        listed resource, and whether the listed resource has that path. None
        when the request has no such field. A request that wraps the resource
        names the wrapper first, and the listed resource has none."""
        item, item_name, schemas = self._listed(list_ref)
        request, _, create_schemas = self._methods.get(create_ref, ({}, {}, {}))
        schema, request_name = _resolve(request, create_schemas)
        path: list[str] = []
        wrapped = request_name == item_name
        for depth, key in enumerate(keys):
            properties = schema.get("properties") or {}
            spelled = attribute_names.terraform_name if depth else attribute_names.root_name
            field = next((p for p in properties if spelled(p) == key), None)
            if field is None:
                return None, False
            schema, name = _resolve(properties[field], create_schemas)
            if not path and not wrapped and name and name == item_name:
                wrapped = True
                continue
            path.append(field)
        here = item
        for field in path:
            here = _resolve((here.get("properties") or {}).get(field), schemas)[0]
            if not here:
                return path, False
        return path, True

    def run_field(self, create_ref: str, list_ref: str, keys: tuple[str, ...]) -> str:
        """The listed resource's field that a create value goes to, as a dotted
        path, or "" when the list does not return it or it is the resource's
        name, which the name rule reads already."""
        path, listed = self._placed(create_ref, list_ref, keys)
        return ".".join(path) if path is not None and listed and path not in ([], ["name"]) else ""

    def names_the_resource(self, create_ref: str, list_ref: str, keys: tuple[str, ...]) -> bool:
        """Whether a create value becomes the listed resource's name: the create
        takes it in a field or a parameter that the listed resource does not
        have, as a client-chosen id (`roleId`, `accountId`) that the API makes
        the last segment of the name. False when the list declares no resource
        schema, because then nothing says what it returns."""
        if not self._listed(list_ref)[0]:
            return False
        path, listed = self._placed(create_ref, list_ref, keys)
        if path is not None:
            return bool(path) and not listed
        return len(keys) == 1 and any(
            attribute_names.root_name(name) == keys[0] for name in self._parameters.get(create_ref, {})
        )

    def optional_parameters(self, ref: str) -> dict[str, str]:
        """Each parameter that the document does not mark required, with its description."""
        return {
            name: str(spec.get("description") or "")
            for name, spec in self._parameters.get(ref, {}).items()
            if not spec.get("required")
        }

    def has_labels(self, list_ref: str) -> bool:
        """Whether the listed resource has labels that the client sets."""
        labels = ((self._listed(list_ref)[0].get("properties")) or {}).get("labels")
        return isinstance(labels, dict) and not server_assigned.is_server_assigned(labels)
