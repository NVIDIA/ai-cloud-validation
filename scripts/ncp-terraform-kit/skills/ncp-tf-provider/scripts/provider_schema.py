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
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
"""Derive a provider's resource schemas, and measure its asynchrony.

The resource schema is the API schema. Nothing here selects fields: every
property the specification defines becomes an attribute, and whether it is
`Required`, `Optional` or `Computed` follows the document rather than a
judgement. A schema decided here cannot drift between runs, which is the whole
reason it is derived instead of curated.

`measure` answers the other question the generator needs: which operations are
long-running, so the provider knows it must poll rather than trust the create
response.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

# The sibling modules, found beside this file however the script is started.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import address_pattern
import any_of
import attribute_names
import call_conditions
import deprecated
import description_text
import go_names
import operation_facts
import operator_overrides
import read_fill
import replacement
import resource_identifier
import reviewed
import scope_parameters
import server_assigned
import settings_check
import terraform_flags

#: Schemas reference each other and can recurse, and a single resource can reach
#: hundreds of properties within a few levels. The bound is a stated policy, not
#: a guess: expansion stops at a repeated schema name on the current path, and at
#: this depth. Both stops are reported, never silent.
DEFAULT_MAX_DEPTH = 4

REQUIRED, OPTIONAL, COMPUTED = "required", "optional", "computed"

#: Where an attribute came from, when it was not the create request body.
#: The body is the ordinary source and is left unmarked, so the field appears
#: only on the two that a reviewer has to treat differently: a value that
#: travels in the URL, and a value the practitioner cannot send at all.
FROM_PARAMETER, FROM_RESPONSE = "parameter", "response"

#: Discovery's JSON-Schema-ish types, mapped to what a provider attribute is.
TYPES = {
    "string": "string",
    "boolean": "bool",
    "integer": "int64",
    "number": "float64",
    "array": "list",
    "object": "object",
}


class InputError(Exception):
    """An input is missing or unreadable."""


def refuse_shared_names(terraform_type: str, attributes: list[Attribute]) -> None:
    """Refuse two top-level attributes that get one Terraform name.

    A schema declares only one of them, so the other field is lost without a
    word.
    """
    seen: dict[str, str] = {}
    for attribute in attributes:
        name = attribute_names.root_name(attribute.name)
        if name in seen:
            raise InputError(
                f"{terraform_type}: the fields {seen[name]!r} and {attribute.name!r} both get the "
                f"Terraform name {name!r}, and a schema can declare only one of them"
            )
        seen[name] = attribute.name


@dataclass(frozen=True)
class Attribute:
    name: str
    type: str
    behaviour: str
    description: str = ""
    attributes: tuple[Attribute, ...] = ()
    #: Set when expansion stopped here rather than because the schema ended.
    truncated: str = ""
    #: `FROM_PARAMETER` or `FROM_RESPONSE`, and empty for the request body.
    source: str = ""
    #: What the runtime sets, which `terraform_flags.assign` derives.
    terraform_flags: tuple[str, ...] = ()
    #: Set when no planned call can change the field, so a change replaces
    #: the resource; `replacement.mark` derives it.
    forces_replacement: bool = False
    #: The change of a one-way field that its call cannot make, `increase` or
    #: `decrease`; that change, and no other, replaces the resource.
    replaces_when: str = ""
    #: Set when the document marks the field read-only and an update call
    #: changes it: the configuration sets it, and only that call sends it.
    set_by_calls: bool = False
    #: `operator` when a `field_overrides` entry of the plan decided the field,
    #: so a later step does not take it for a reading of the documents.
    decided_by: str = ""

    def as_dict(self, *, root: bool = False) -> dict[str, Any]:
        """The attribute as the schema writes it; `root` for a top-level one."""
        out: dict[str, Any] = {
            # What the provider declares, and what the document called it. They
            # differ for every camelCase field, and both are needed: the first
            # is what a configuration writes, the second is what the request
            # carries.
            "name": attribute_names.root_name(self.name) if root else attribute_names.terraform_name(self.name),
            "api_name": self.name,
            "type": self.type,
            "behaviour": self.behaviour,
            "terraform_flags": list(self.terraform_flags),
        }
        if self.description:
            out["description"] = self.description
        if self.attributes:
            out["attributes"] = [a.as_dict() for a in self.attributes]
        if self.truncated:
            out["truncated"] = self.truncated
        if self.source:
            out["source"] = self.source
        if self.forces_replacement:
            out["forces_replacement"] = True
        if self.replaces_when:
            out["replaces_when"] = self.replaces_when
        if self.set_by_calls:
            out["set_by_calls"] = True
        if self.decided_by:
            out["decided_by"] = self.decided_by
        return out


@dataclass
class ResourceSchema:
    terraform_type: str
    logical_kind: str
    layer: str
    create_operation: str
    request_schema: str
    response_schema: str = ""
    attributes: list[Attribute] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Each planned operation, by verb, as `operation_facts` describes it.
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Where the provider finds the value that addresses the resource.
    identifier: dict[str, Any] = field(default_factory=dict)
    #: The settable attributes that `replacement.mark` leaves to the agent's
    #: off-path record. The schema file states them in a note; the coverage
    #: report reads them here.
    replacement_left_to_agent: list[str] = field(default_factory=list)
    #: Where `replacement.mark` took each decision from, by field path, for
    #: the coverage report; the schema file does not carry it.
    replacement_sources: dict[str, str] = field(default_factory=dict)
    #: What terraform destroy does with an object that the API never deletes.
    on_destroy: dict[str, Any] = field(default_factory=dict)
    #: Each parameter that may name the object through a field that only a
    #: review can tell, open or answered: `replacement.naming_entries`.
    naming: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "terraform_type": self.terraform_type,
            "logical_kind": self.logical_kind,
            "layer": self.layer,
            "create_operation": self.create_operation,
            "request_schema": self.request_schema,
            "response_schema": self.response_schema,
            "attributes": [a.as_dict(root=True) for a in self.attributes],
            "operations": self.operations,
            "identifier": self.identifier,
            "notes": self.notes,
        }
        if self.on_destroy:
            out["on_destroy"] = self.on_destroy
        if self.naming:
            out["naming"] = self.naming
        return out


def behaviour_of(prop: dict[str, Any], *, required_names: frozenset[str], name: str, operation: str = "") -> str:
    """Required, Optional or Computed -- read from the document, never chosen.

    A field the API marks required is `Required`; a field it marks output-only is
    `Computed`; everything else is `Optional`. This is the document's fact. The
    flags the runtime sets are derived from it in `terraform_flags`.

    Both specification languages are read, because each says these two things
    its own way. Required arrives as a method parameter flag, as a schema's own
    `required` list, or as the methods a property's `annotations.required`
    names: one schema serves several methods, and a field one create requires
    is optional everywhere else. Output-only arrives as the `readOnly` flag, or
    as a marker in the description text where a document has no flag to set.
    """
    if name in required_names:
        return REQUIRED
    if operation and operation in ((prop.get("annotations") or {}).get("required") or ()):
        return REQUIRED
    if server_assigned.is_server_assigned(prop):
        return COMPUTED
    return OPTIONAL


def type_of(prop: dict[str, Any]) -> str:
    if prop.get("$ref") or (prop.get("items") or {}).get("$ref"):
        return "list" if prop.get("type") == "array" else "object"
    if _inline_object(prop) or _inline_object(prop.get("items") or {}):
        return "list" if prop.get("type") == "array" else "object"
    return TYPES.get(str(prop.get("type", "")), "unknown")


def _referenced(prop: dict[str, Any]) -> str | None:
    """The schema this property points at, by bare name.

    Discovery writes `Instance`; OpenAPI writes `#/components/schemas/Instance`.
    The last segment is the name in both, so one table serves both languages.
    """
    reference = prop.get("$ref") or (prop.get("items") or {}).get("$ref")
    return reference.rsplit("/", 1)[-1] if reference else None


def _inline_object(node: dict[str, Any]) -> bool:
    """True for an object schema written in place, with no name a $ref could carry.

    Discovery inlines small nested objects rather than giving them a top-level
    name -- a firewall rule's allow-list entries, for instance, have their own
    `properties` right where `allowed`'s `items` sits, never a schema of their
    own. Without this, such a property falls through to a bare `list`/`unknown`
    type with no children, which is silently narrower than the document: the
    fields exist and are simply unreachable through the generated schema.
    """
    return bool(isinstance(node, dict) and node.get("type") == "object" and isinstance(node.get("properties"), dict))


def _inline_child(prop: dict[str, Any]) -> dict[str, Any] | None:
    """The inline object a property or its array items resolve to, if either does."""
    if _referenced(prop):
        return None
    if _inline_object(prop):
        return prop
    items = prop.get("items") or {}
    return items if _inline_object(items) else None


def expand(
    schema_name: str,
    schemas: dict[str, Any],
    *,
    required_names: frozenset[str] = frozenset(),
    max_depth: int = DEFAULT_MAX_DEPTH,
    operation: str = "",
    keep: frozenset[tuple[str, ...]] = frozenset(),
    _path: tuple[str, ...] = (),
    _names: tuple[str, ...] = (),
) -> tuple[list[Attribute], list[str]]:
    """Expand one named schema into attributes, bounded by cycle and by depth.

    `operation` is the method the attributes are sent to, which decides the
    fields a document marks required for some methods only.

    `keep` holds the attribute paths of the deprecated fields that the
    operator keeps; `_names` is the attribute path of this schema.

    `_path` is the chain of schema names already open. A name reappearing on it
    is a cycle, and the attribute is emitted without children rather than
    dropped: the field exists in the API, so hiding it would make the provider
    silently narrower than the document.

    A name is what makes a cycle detectable at all, so this is only the entry
    point for schemas that have one ($ref'd schemas). An inline object -- one
    written in place, with no name of its own -- is expanded by
    `_expand_properties` directly; see `_inline_child`.
    """
    schema = schemas.get(schema_name)
    if not isinstance(schema, dict):
        return [], [f"{schema_name}: not defined in the specification"]

    # The schema being expanded is itself open. Without it here, a directly
    # self-referencing schema recurses one extra level before the cycle is seen.
    open_path = (*_path, schema_name)
    # OpenAPI states required-ness on the object, not the property, and it does
    # so at every level. Discovery has no equivalent, so this is empty there.
    required_here = required_names | frozenset(schema.get("required") or ())
    return _expand_properties(schema, schemas, required_here, max_depth, open_path, operation, keep, _names)


def _expand_properties(
    schema: dict[str, Any],
    schemas: dict[str, Any],
    required_here: frozenset[str],
    max_depth: int,
    path: tuple[str, ...],
    operation: str = "",
    keep: frozenset[tuple[str, ...]] = frozenset(),
    names: tuple[str, ...] = (),
) -> tuple[list[Attribute], list[str]]:
    """The recursive core `expand` and inline-object expansion both run.

    `path` bounds depth for both kinds of child and additionally holds named
    schemas for `$ref` cycle detection -- an inline object carries no name a
    later property could point back at, so it only ever contributes to depth.
    """
    notes: list[str] = []
    attributes: list[Attribute] = []
    here = frozenset(p[-1] for p in keep if p[:-1] == names)
    for name, prop in deprecated.kept(schema.get("properties") or {}, path, notes, here):
        if not isinstance(prop, dict):
            continue
        prop = any_of.merged(prop, schemas) or prop
        behaviour = behaviour_of(prop, required_names=required_here, name=name, operation=operation)
        description = description_text.copied((prop.get("description") or "").strip().replace("\n", " "))
        reference = _referenced(prop)
        inline = _inline_child(prop)
        children: tuple[Attribute, ...] = ()
        truncated = ""

        if reference:
            if reference in path:
                truncated = f"cycle: {reference} already open"
                notes.append(f"{'.'.join((*path, name))}: {truncated}")
            elif len(path) >= max_depth:
                truncated = f"depth: stopped at {max_depth}"
                notes.append(f"{'.'.join((*path, name))}: {truncated}")
            else:
                nested, nested_notes = expand(
                    reference,
                    schemas,
                    max_depth=max_depth,
                    operation=operation,
                    keep=keep,
                    _path=path,
                    _names=(*names, name),
                )
                children = tuple(nested)
                notes.extend(nested_notes)
        elif inline is not None:
            if len(path) >= max_depth:
                truncated = f"depth: stopped at {max_depth}"
                notes.append(f"{'.'.join((*path, name))}: {truncated}")
            else:
                nested, nested_notes = _expand_properties(
                    inline,
                    schemas,
                    frozenset(inline.get("required") or ()),
                    max_depth,
                    (*path, name),
                    operation,
                    keep,
                    (*names, name),
                )
                children = tuple(nested)
                notes.extend(nested_notes)

        attributes.append(
            Attribute(
                name=name,
                type=type_of(prop),
                behaviour=behaviour,
                description=description[:200],
                attributes=children,
                truncated=truncated,
            )
        )
    return attributes, notes


@dataclass(frozen=True)
class Parameter:
    """One declared parameter, with where it goes and whether it repeats."""

    name: str
    location: str
    required: bool = False
    repeated: bool = False
    #: The regular expression the document says a value matches, or "".
    pattern: str = ""


@dataclass(frozen=True)
class Method:
    """One callable operation, kept with the fields the derivation needs."""

    operation_id: str
    http_method: str
    path: str
    request_ref: str
    response_ref: str
    required_parameters: frozenset[str]
    #: The rest of the operation's own parameters. One of them often names the
    #: resource -- `workloadIdentityPoolId` is how a pool is called, and it is
    #: declared optional -- so a schema built from the required ones alone
    #: cannot express what the case asks for.
    optional_parameters: frozenset[str] = frozenset()
    #: Which document declared it. A schema name is unique inside one API and
    #: not between them -- six of the nine documents one NCP publishes declare a
    #: `Policy` -- so a reference is only meaningful against the document that
    #: wrote it, and the method is what says which that was.
    document: str = ""
    #: Why this is believed long-running, or "" when it is not. The two
    #: specification languages say it differently: a Discovery method returns an
    #: operation resource, while an OpenAPI operation declares 202 Accepted,
    #: which is HTTP's own word for "started, not finished".
    async_reason: str = ""
    #: The path as a template, with `{+name}` where a value spans segments. The
    #: `path` above may be a flattened spelling that loses it.
    template: str = ""
    #: What the document puts in front of every path, when it says.
    service_path: str = ""
    parameters: tuple[Parameter, ...] = ()
    #: The operation's own prose. A precondition the API enforces -- a delete
    #: that fails while something still references the resource -- is stated
    #: here and nowhere else in the document.
    description: str = ""


@dataclass(frozen=True)
class AccountScope:
    """The account every call is made in, which the provider configuration names.

    Written as the API writes the scope, `projects/{project}`: the placeholder is
    the provider setting. A path or query parameter holding the setting alone,
    or the whole scope, is one the provider fills. As a resource attribute it
    would be a second place to name the account, and the second place is where
    the two disagree.
    """

    template: str
    setting: str

    @classmethod
    def parse(cls, template: str) -> AccountScope:
        placeholders = re.findall(r"\{([^{}]+)\}", template)
        if len(placeholders) != 1:
            raise InputError(
                f"--account-scope {template!r} must name exactly one provider setting, as in 'projects/{{project}}'"
            )
        return cls(template=template, setting=placeholders[0])

    @property
    def pattern(self) -> str:
        """The expression a document writes for a parameter that is this scope."""
        literal = re.split(r"\{[^{}]+\}", self.template)
        return "^" + "[^/]+".join(re.escape(part) for part in literal) + "$"

    def value_for(self, parameter: Parameter) -> str:
        """What the provider sends for this parameter, or "" when the caller does."""
        if parameter.location not in ("path", "query"):
            return ""
        if parameter.name == self.setting:
            return "{" + self.setting + "}"
        if parameter.pattern == self.pattern:
            return self.template
        return ""


#: HTTP methods that change something, and so may be long-running.
MUTATING_HTTP = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def index_openapi(document: dict[str, Any], source: str = "") -> tuple[dict[str, Method], dict[str, Any]]:
    """Read an OpenAPI 3.x document into methods and schemas.

    Returns the schema table too, because a request body is commonly written
    inline rather than behind a `$ref`, and an inline body defines a schema that
    `components.schemas` never lists. Registering it under a name derived from
    its operation lets everything downstream see one kind of schema, whichever
    way the document spelled it.
    """
    schemas: dict[str, Any] = dict((document.get("components") or {}).get("schemas") or {})
    found: dict[str, Method] = {}

    for path, operations in (document.get("paths") or {}).items():
        if not isinstance(operations, dict):
            continue
        shared = _resolved_all(operations.get("parameters") or [], "parameters", document)
        for verb, operation in operations.items():
            http_method = verb.upper()
            if http_method not in {*MUTATING_HTTP, "GET"} or not isinstance(operation, dict):
                continue
            identifier = operation.get("operationId") or f"{verb.lower()}{path}"
            parameters = _with_shared(shared, _resolved_all(operation.get("parameters") or [], "parameters", document))
            responses = operation.get("responses") or {}

            request_ref = _register_body(
                _resolved(operation.get("requestBody"), "requestBodies", document), identifier, schemas
            )
            response_ref = ""
            for code in ("200", "201", "202"):
                body = _json_schema((_resolved(responses.get(code), "responses", document) or {}).get("content"))
                if body:
                    response_ref = _register(body, f"{identifier}Response", schemas)
                    break

            found[identifier] = Method(
                operation_id=identifier,
                http_method=http_method,
                path=path,
                request_ref=request_ref,
                response_ref=response_ref,
                document=source,
                required_parameters=frozenset(
                    p.get("name", "") for p in parameters if isinstance(p, dict) and p.get("required")
                ),
                optional_parameters=frozenset(
                    p.get("name", "") for p in parameters if isinstance(p, dict) and not p.get("required")
                ),
                # 202 is HTTP's own word for "accepted, not finished", and it is
                # the only portable signal an OpenAPI document gives.
                async_reason="declares a 202 Accepted response" if "202" in responses else "",
                description=description_text.copied(
                    str(operation.get("description") or operation.get("summary") or "")
                ),
                template=path,
                parameters=tuple(
                    Parameter(
                        name=str(p.get("name", "")),
                        location=str(p.get("in", "")),
                        required=bool(p.get("required")),
                        repeated=(p.get("schema") or {}).get("type") == "array",
                        pattern=str((p.get("schema") or {}).get("pattern") or ""),
                    )
                    for p in parameters
                    if isinstance(p, dict) and p.get("name")
                ),
            )
    return found, schemas


#: How many `$ref` steps a parameter, request body or response may take before
#: it reaches its object. A chain longer than this is a loop in the document.
REF_STEPS = 10


def _resolved(node: Any, section: str, document: dict[str, Any]) -> Any:
    """`node`, or the object under `components.<section>` that its `$ref` names.

    A parameter, a request body or a response may be written in place or by
    `$ref`. A reference that does not resolve is refused: reading it as absent
    would drop a required parameter or a whole body without a word.
    """
    for _ in range(REF_STEPS):
        if not (isinstance(node, dict) and "$ref" in node):
            return node
        ref = str(node["$ref"])
        prefix = f"#/components/{section}/"
        named = ((document.get("components") or {}).get(section) or {}).get(ref[len(prefix) :])
        if not ref.startswith(prefix) or named is None:
            raise InputError(f"the $ref {ref!r} names no object under components.{section} of the document")
        node = named
    raise InputError(f"a $ref under components.{section} takes more than {REF_STEPS} steps")


def _resolved_all(nodes: list[Any], section: str, document: dict[str, Any]) -> list[Any]:
    return [_resolved(node, section, document) for node in nodes]


def _with_shared(shared: list[Any], own: list[Any]) -> list[Any]:
    """An operation's parameters, with those its path item declares for all.

    OpenAPI lets a path item declare a parameter once for every operation on
    it, and an operation's own declaration of the same name and location
    replaces it.
    """
    declared = {(p.get("name"), p.get("in")) for p in own if isinstance(p, dict)}
    return [
        *(p for p in shared if isinstance(p, dict) and (p.get("name"), p.get("in")) not in declared),
        *own,
    ]


def _json_schema(content: Any) -> dict[str, Any] | None:
    """The JSON schema out of an OpenAPI content map, whatever the media type."""
    if not isinstance(content, dict):
        return None
    for media, body in content.items():
        if "json" in str(media) and isinstance(body, dict):
            schema = body.get("schema")
            return schema if isinstance(schema, dict) else None
    return None


def _bare_ref(schema: dict[str, Any]) -> str | None:
    reference = schema.get("$ref") or (schema.get("items") or {}).get("$ref")
    return reference.rsplit("/", 1)[-1] if reference else None


def _register_body(request_body: Any, identifier: str, schemas: dict[str, Any]) -> str:
    """Name an operation's request body, registering it when it is inline."""
    return _register(
        _json_schema((request_body or {}).get("content")),
        f"{identifier}Request",
        schemas,
    )


def _register(schema: Any, synthetic: str, schemas: dict[str, Any]) -> str:
    """Name a body, registering it under `synthetic` when it is written inline.

    Both directions need this, and for the same reason. A document is free to
    write a body inline rather than behind a reference, and an inline body
    defines a schema `components.schemas` never lists. Naming it here lets
    everything downstream see one kind of schema whichever way the document
    spelled it -- and a response left unnamed yields no computed attributes at
    all, which reads exactly like an API that assigns nothing.
    """
    if schema is None:
        return ""
    named = _bare_ref(schema)
    if named:
        return named
    schemas[synthetic] = schema
    return synthetic


def index_discovery(document: dict[str, Any], source: str = "") -> dict[str, Method]:
    """Every method in a Google Discovery document, keyed by its own id."""
    found: dict[str, Method] = {}

    def walk(node: dict[str, Any]) -> None:
        for method in (node.get("methods") or {}).values():
            identifier = method.get("id")
            if not identifier:
                continue
            parameters = method.get("parameters") or {}
            response_ref = (method.get("response") or {}).get("$ref", "")
            found[identifier] = Method(
                operation_id=identifier,
                http_method=str(method.get("httpMethod", "")).upper(),
                path=method.get("flatPath") or method.get("path") or "",
                request_ref=(method.get("request") or {}).get("$ref", ""),
                response_ref=response_ref,
                document=source,
                required_parameters=frozenset(name for name, p in parameters.items() if p.get("required")),
                optional_parameters=frozenset(name for name, p in parameters.items() if not p.get("required")),
                async_reason=("returns an operation resource" if response_ref.endswith("Operation") else ""),
                template=method.get("path") or method.get("flatPath") or "",
                description=description_text.copied(str(method.get("description") or "")),
                service_path=str(document.get("servicePath") or ""),
                parameters=tuple(
                    Parameter(
                        name=name,
                        location=str(p.get("location", "")),
                        required=bool(p.get("required")),
                        repeated=bool(p.get("repeated")),
                        pattern=str(p.get("pattern") or ""),
                    )
                    for name, p in sorted(parameters.items())
                ),
            )
        for child in (node.get("resources") or {}).values():
            walk(child)

    walk(document)
    return found


def load_documents(paths: list[Path]) -> tuple[dict[str, Method], dict[str, dict[str, Any]]]:
    """Merge the supplied documents into one method index and one schema table.

    Every operation is indexed under its own identifier and under its method and
    path, because a plan names an operation by whichever form its specification
    makes available.

    **OpenAPI is the format this expects.** Google Discovery is read as well,
    because one NCP this kit targets publishes nothing else. Any other language
    is refused with the format named, so the operator can fall back to reading
    it rather than guessing what went wrong.
    """
    methods: dict[str, Method] = {}
    schemas: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise InputError(f"no such file: {path}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InputError(f"{path} is not valid JSON: {error}") from error

        source = path.name
        if bad := go_names.unholdable(document):
            raise InputError(
                f"{source}: the {bad[0]} name {bad[1]!r} holds a quote, a backtick, a backslash, "
                f"whitespace or a control character, and Go cannot hold it in a struct tag. "
                f"Nothing was derived"
            )
        if isinstance(document, dict) and str(document.get("openapi") or "").startswith("3."):
            found, defined = index_openapi(document, source)
            methods.update(found)
            schemas[source] = defined
            continue
        if isinstance(document, dict) and "schemas" in document and "resources" in document:
            methods.update(index_discovery(document, source))
            # Kept per document rather than merged. A `$ref` is API-local, so
            # one table for all of them lets a later document answer an earlier
            # one's reference: GCP Compute and Filestore both define `Instance`,
            # and Cloud DNS and Resource Manager both define `Project`.
            schemas[source] = document.get("schemas") or {}
            continue

        raise InputError(
            f"{path} is {_format_of(document)}. This reads OpenAPI 3.x, and "
            f"Google Discovery. Derive the schemas and the asynchrony by reading "
            f"the specification yourself, and say the result was produced by "
            f"hand rather than by this script"
        )
    return with_path_keys(methods), schemas


def _format_of(document: Any) -> str:
    """Name the specification format, so a refusal says what it actually found.

    The fallback is a person reading the document, so the message has to be
    enough for them to start: "not a Discovery document" is not.
    """
    if not isinstance(document, dict):
        return "not a JSON object"
    if document.get("openapi"):
        return f"not an OpenAPI 3.x document (openapi: {document['openapi']})"
    if document.get("swagger"):
        return (
            f"not an OpenAPI 3.x document (swagger: {document['swagger']}). It is a "
            f"Swagger {document['swagger']} document, which predates OpenAPI 3"
        )
    if "paths" in document:
        return "an OpenAPI-shaped document with no `openapi` version declared"
    if "schemas" in document:
        return "a document with schemas but no operations this can read"
    return "a JSON document in no recognised specification language"


def _coverage_skill() -> Path:
    """The sibling skill that owns the requirement catalog and its reader.

    Reading it there rather than shipping a second copy is what keeps a
    provider run and a coverage analysis agreeing about what a section is. The
    skills install together, so the sibling is always beside this one.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        sibling = parent / "ncp-tf-coverage"
        if sibling.is_dir():
            return sibling
    raise SystemExit(
        f"provider_schema: no ncp-tf-coverage skill beside {here.parents[1]}. "
        f"This reads the requirement catalog it ships; install both skills"
    )


def _resource_shape() -> Any:
    """The coverage skill's reader of a planned resource's calls, so the
    provider names each update call by the verb that the plan's gate checked."""
    sys.path.insert(0, str(_coverage_skill() / "scripts"))
    import resource_shape

    return resource_shape


def _catalog(path: str | None) -> dict[str, dict[str, Any]]:
    named = Path(path) if path else (_coverage_skill() / "references" / "requirements-for-ai-clouds-v2.4.md")
    sys.path.insert(0, str(_coverage_skill() / "scripts"))
    import requirements_catalog

    return requirements_catalog.load(named)


def requirement_ids(entry: dict[str, Any]) -> list[str]:
    """The requirements a planned resource serves, however the plan spells them.

    A plan writes a requirement as a bare id, or as a mapping carrying a
    `part_id` when the requirement was cut into parts.
    """
    out: list[str] = []
    for item in entry.get("requirements") or []:
        if isinstance(item, dict):
            out.append(str(item.get("req_id") or ""))
        else:
            out.append(str(item))
    return [i for i in out if i]


def narrowed_to_sections(coverage: dict[str, Any], wanted: Sequence[str], requirements: str | None) -> set[str]:
    """The `terraform_type`s filed under a heading the operator named."""
    sys.path.insert(0, str(_coverage_skill() / "scripts"))
    import requirements_catalog

    planned = planned_resources(coverage)
    # A dependency resource names no requirement; it is filed with the resource
    # it serves, so narrowing to that resource's heading keeps both.
    filed = requirements_catalog.filed_requirements(planned)
    try:
        kept = requirements_catalog.narrowed_to_sections(
            planned,
            wanted,
            _catalog(requirements),
            lambda r: filed.get(str(r.get("terraform_type") or "")) or requirement_ids(r),
        )
    except requirements_catalog.CatalogError as error:
        raise InputError(f"no planned resource is filed under: {error}") from error
    return {str(r.get("terraform_type") or "") for r in kept}


def planned_resources(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    """Every resource the module plan declares, with its owning layer."""
    plan = coverage.get("module_plan") or {}
    layers = plan.get("layers") or {}
    out: list[dict[str, Any]] = []
    for layer, body in layers.items():
        for resource in (body or {}).get("resources") or []:
            out.append({**resource, "layer": layer})
    return out


def path_key(method: str, path: str) -> str:
    """The `METHOD /path` form, spelled one way.

    A path template has several legal spellings and the leading slash is the
    common one, so both the index and the lookup pass through here.
    """
    return f"{method.upper()} /{path.lstrip('/')}"


def with_path_keys(methods: dict[str, Method]) -> dict[str, Method]:
    """The index again, with every operation also under its method and path."""
    return methods | {
        path_key(method.http_method, method.path): method
        for method in methods.values()
        if method.http_method and method.path
    }


def resolve_operation(reference: str, methods: dict[str, Method]) -> Method | None:
    """Find an operation by whichever reference form the plan used.

    A plan names an operation by identifier where the specification defines one
    and by method and path where it does not. Indexing only the identifier
    leaves every resource of a specification of the second kind with no create
    operation, and so with no attributes at all -- a provider whose resources
    nobody can configure, derived without a single error.
    """
    if reference.startswith("ext:"):
        raise InputError(
            f"{reference} names an endpoint outside the NCP's own API. A generated "
            "resource cannot call one, so it cannot be a resource's operation."
        )
    identifier = stated_reference(reference)
    if identifier in methods:
        return methods[identifier]
    verb, _, path = identifier.partition(" ")
    return methods.get(path_key(verb, path)) if path else None


def stated_reference(reference: str) -> str:
    """The reference as the plan wrote it, without the optional `op:` marker."""
    return reference[3:] if reference.startswith("op:") else reference


def names_in(attributes: tuple[Attribute, ...] | list[Attribute]) -> set[str]:
    """Every name these attributes carry, at any depth.

    Depth is the point. A response field the request can set further in is not
    server-assigned: one NCP's service-account create takes `displayName` inside
    a request wrapper and returns it at the top, and reading only the top level
    would offer the practitioner the same value twice -- once settable, once
    not.
    """
    found: set[str] = set()
    for attribute in attributes:
        found.add(attribute.name)
        found |= names_in(attribute.attributes)
    return found


def as_computed(attribute: Attribute) -> Attribute:
    """This attribute and everything under it, marked server-assigned.

    All the way down, because a practitioner who cannot send a value cannot
    send a field inside it either. Leaving a child `Optional` would offer a
    value Terraform can never put in a request.
    """
    return replace(
        attribute,
        behaviour=COMPUTED,
        source=FROM_RESPONSE,
        attributes=tuple(as_computed(child) for child in attribute.attributes),
    )


def returned_attributes(
    method: Method, schemas: dict[str, Any], *, max_depth: int = DEFAULT_MAX_DEPTH
) -> tuple[list[Attribute], list[str]]:
    """What the create response describes, or nothing and the reason why.

    A long-running create answers with an operation handle, not the resource, so
    its response describes the operation. Merging that would give every
    asynchronous resource the same few operation fields and none of its own.
    """
    if method.async_reason:
        return [], [
            f"{method.operation_id} {method.async_reason}, so its response "
            f"describes the operation rather than the resource and supplies no "
            f"attributes"
        ]
    if not method.response_ref:
        return [], [
            f"{method.operation_id} declares no response body, so nothing is "
            f"computed here unless the request itself marks it"
        ]
    return expand(method.response_ref, schemas, max_depth=max_depth)


def derive(
    coverage: dict[str, Any],
    methods: dict[str, Method],
    schemas: dict[str, dict[str, Any]],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    scope: AccountScope | None = None,
    answers: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> list[ResourceSchema]:
    """One schema per planned resource, derived from its create operation.

    `answers` holds the review's `naming` answers, by type and parameter.
    """
    derived: list[ResourceSchema] = []

    def scope_value(parameter: Parameter) -> str:
        return scope.value_for(parameter) if scope else ""

    def no_value(parameter: Parameter) -> str:
        return ""

    for resource in planned_resources(coverage):
        create = (resource.get("operations") or {}).get("create", "")
        # The configuration names the account it calls in, never one that the
        # create makes: a new object of the account's kind is addressed by its own name.
        provider_value = (
            no_value
            if resource_identifier.makes_the_account_kind(
                resolve_operation(str(create), methods),
                resolve_operation(str((resource.get("operations") or {}).get("read") or ""), methods),
                scope_value,
            )
            else scope_value
        )
        identifier = stated_reference(str(create))
        schema = ResourceSchema(
            terraform_type=str(resource.get("terraform_type", "")),
            logical_kind=str(resource.get("logical_kind", "")),
            layer=str(resource.get("layer", "")),
            create_operation=identifier,
            request_schema="",
        )
        shape = _resource_shape()
        schema.operations, operation_notes = operation_facts.for_resource(
            shape.verbs(resource),
            lambda reference: resolve_operation(reference, methods),
            schemas,
            reported=frozenset({"create"}),
            provider_value=provider_value,
        )
        for verb, fields in shape.call_fields(resource).items():
            if verb in schema.operations:
                schema.operations[verb]["fields"] = fields
                called = resolve_operation(str(shape.verbs(resource)[verb]), methods)
                operation_notes.extend(
                    operation_facts.mark_call(
                        schema.operations[verb], schemas.get(called.document, {}) if called else {}
                    )
                )
        for verb, value in shape.call_values(resource).items():
            if verb in schema.operations:
                schema.operations[verb]["value"] = value
        schema.notes.extend(operation_notes)
        method = resolve_operation(str(create), methods)
        if method is None:
            schema.notes.append(f"create operation {identifier!r} is not in the supplied documents")
            derived.append(schema)
            continue
        schema.request_schema = method.request_ref
        schema.response_schema = method.response_ref
        if not method.request_ref:
            schema.notes.append(f"{identifier} declares no request body; its attributes are its parameters alone")
        read = resolve_operation(str((resource.get("operations") or {}).get("read") or ""), methods)
        scope_only = scope_parameters.named_like_the_object(method, read)
        wrapper = ((schema.operations.get("create") or {}).get("wraps_resource_under") or {}).get("field", "")
        attributes, notes = (
            expand(
                method.request_ref,
                schemas.get(method.document, {}),
                required_names=method.required_parameters - scope_only,
                max_depth=max_depth,
                operation=method.operation_id,
                keep=operator_overrides.kept_paths(resource, wrapper),
            )
            if method.request_ref
            else ([], [])
        )
        supplied = {p.name: provider_value(p) for p in method.parameters if provider_value(p)}
        for name, value in sorted(supplied.items()):
            notes.append(f"{name} is filled by the provider configuration as {value!r}, so it is not an attribute")
        # A required parameter has no counterpart in the request body, so it is
        # added rather than found.
        body_names = {a.name for a in attributes}
        notes.extend(scope_parameters.unfilled(method.operation_id, (scope_only & body_names) - set(supplied)))
        parameters = sorted(method.required_parameters - body_names - set(supplied))
        for name in parameters:
            attributes.append(
                Attribute(
                    name=name,
                    type="string",
                    behaviour=REQUIRED,
                    description="Required parameter of the create operation.",
                    source=FROM_PARAMETER,
                )
            )
        # An optional parameter is often how the caller names the thing being
        # created -- `workloadIdentityPoolId` is declared optional and is the
        # pool's id -- so leaving them out produced a resource nobody could name.
        taken = body_names | set(parameters) | set(supplied)
        for name in sorted(method.optional_parameters - taken):
            attributes.append(
                Attribute(
                    name=name,
                    type="string",
                    behaviour=OPTIONAL,
                    description="Optional parameter of the create operation.",
                    source=FROM_PARAMETER,
                )
            )
            parameters.append(name)
        # The request says what may be sent; the response says what comes back.
        # A field only the response carries is one the API assigns, and it is
        # where a resource's identifier is stated. Without it the provider has
        # nothing to refresh or destroy with, which the document did say.
        returned, returned_notes = returned_attributes(method, schemas.get(method.document, {}), max_depth=max_depth)
        settable = names_in(attributes)
        returned, envelope_notes = terraform_flags.inside_envelope(returned, settable, identifier)
        notes.extend(returned_notes + envelope_notes)
        for attribute in returned:
            if attribute.name in parameters:
                notes.append(
                    f"{identifier} takes a required parameter named "
                    f"{attribute.name!r} and returns a field of that name. The "
                    f"parameter is kept, so what the response assigns under that "
                    f"name is not an attribute here"
                )
                continue
            if attribute.name in settable:
                continue
            attributes.append(as_computed(attribute))
        # The provider needs the identifier before its first read, so a field
        # that only the read states cannot be where it comes from.
        known_at_create = {a.name for a in attributes}
        if read is not None and read.response_ref and resource_identifier.parameter_of(read, provider_value):
            fresh, fresh_notes = read_fill.only_the_read_states(
                read.operation_id,
                *expand(
                    read.response_ref,
                    schemas.get(read.document, {}),
                    max_depth=max_depth,
                ),
                read_fill.at_the_top(attributes, wrapper),
                set(parameters),
            )
            attributes.extend(as_computed(attribute) for attribute in fresh)
            notes.extend(fresh_notes)
        attributes, flag_notes = terraform_flags.for_resource(
            attributes,
            read,
            schemas.get(read.document, {}) if read else {},
            (schema.operations.get("create") or {}).get("wraps_resource_under") or {},
            {a.name for a in attributes if a.source != FROM_RESPONSE},
            max_depth,
        )
        notes.extend(flag_notes)
        attributes = terraform_flags.settable_through_calls(
            attributes, {f for fields in shape.call_fields(resource).values() for f in fields}, wrapper
        )
        schema.attributes = attributes
        answer, _ = (
            expand(method.response_ref, schemas.get(method.document, {}), max_depth=max_depth)
            if method.response_ref
            else ([], [])
        )
        schema.identifier, identifier_notes = resource_identifier.for_resource(
            {verb: resolve_operation(str(ref), methods) for verb, ref in (resource.get("operations") or {}).items()},
            method,
            known_at_create,
            terraform_flags.inside_envelope(answer, settable, identifier)[0],
            provider_value,
        )
        notes.extend(identifier_notes)
        update = resolve_operation(str((resource.get("operations") or {}).get("update", "")), methods)
        if update and "update" in schema.operations:
            notes.extend(
                operation_facts.mark_update(
                    schema.operations["update"],
                    schemas.get(update.document, {}),
                    attributes,
                    create=schema.operations.get("create"),
                )
            )
        update_entry = schema.operations.get("update") if update else None
        steering = replacement.steering_parameters(
            method, (update, resolve_operation(str((resource.get("operations") or {}).get("delete") or ""), methods))
        )
        wrapped = [c for a in schema.attributes if a.name == wrapper for c in a.attributes]
        planned = {
            verb: resolve_operation(str(ref), methods) for verb, ref in (resource.get("operations") or {}).items()
        }
        identity, wrapped_identity, unnamed = replacement.naming(
            method,
            schema.identifier,
            {a.name for a in schema.attributes},
            {c.name for c in wrapped},
            provider_value,
            resource_identifier.addressing_parameters(planned, method, provider_value),
        )
        stated, pattern_notes = address_pattern.filled_by(
            unnamed,
            resource_identifier.addressed_by(planned, provider_value)[1],
            (schemas.get(method.document, {}).get(method.request_ref) or {}).get("properties") or {},
            {
                a.name
                for a in schema.attributes
                if a.source != FROM_RESPONSE and set(a.terraform_flags) & {"required", "optional"}
            },
        )
        identity, unnamed = identity | frozenset(stated.values()), tuple(p for p in unnamed if p not in stated)
        notes.extend(pattern_notes)
        optional = frozenset(
            a.name
            for a in schema.attributes
            if a.source == FROM_PARAMETER and a.behaviour == OPTIONAL and a.name not in steering
        )
        candidates = (*unnamed, *sorted(optional))
        named, names_nothing, applied, naming_notes = replacement.reviewed_naming(
            (answers or {}).get(schema.terraform_type) or {}, candidates, {a.name for a in schema.attributes}
        )
        schema.attributes, found, schema.replacement_left_to_agent, asked = replacement.mark(
            schema.attributes,
            update=update_entry,
            payload=operation_facts.payload_fields(
                update_entry, schemas.get(update.document, {}), server_assigned.is_server_assigned
            )
            if update and update_entry
            else set(),
            limited=_changes_only(resource, update, methods),
            call_fields={f for fields in shape.call_fields(resource).values() for f in fields},
            steering=steering
            | names_nothing
            | frozenset(
                a.name for a in [*schema.attributes, *wrapped] if operation_facts.steers(a.name, a.description)
            ),
            optional_parameters=optional - set(applied),
            wrapper=wrapper,
            identity=identity | named,
            wrapped_identity=wrapped_identity,
            unnamed=tuple(p for p in unnamed if p not in applied),
            **call_conditions.field_marks(resource),
            sources=schema.replacement_sources,
        )
        notes.extend(naming_notes)
        notes.extend(found)
        notes.extend(
            call_conditions.attach(
                schema.operations,
                resource,
                lambda reference: resolve_operation(reference, methods),
                lambda found: operation_facts.describe(found, schemas.get(found.document, {}), "", provider_value)[0],
            )
        )
        schema.attributes = call_conditions.one_way(schema.attributes, resource)
        schema.on_destroy = call_conditions.on_destroy(resource)
        schema.attributes, override_notes = operator_overrides.apply(schema.attributes, resource, wrapper)
        decided = {str(e.get("field") or "") for e in resource.get("field_overrides") or [] if isinstance(e, dict)}
        schema.replacement_left_to_agent = [n for n in schema.replacement_left_to_agent if n not in decided]
        schema.naming, open_notes = replacement.naming_entries(
            candidates, applied, [p for p in asked if p not in decided]
        )
        notes.extend(open_notes)
        notes.extend(override_notes)
        schema.notes.extend(dict.fromkeys(notes))
        refuse_shared_names(schema.terraform_type, schema.attributes)
        derived.append(schema)
    return derived


def _changes_only(resource: dict[str, Any], update: Method | None, methods: dict[str, Method]) -> set[str] | None:
    """The fields that a `changes_only` condition says the update changes, or
    None when the plan records no such condition."""
    if update is None:
        return None
    found: set[str] | None = None
    for entry in resource.get("conditions") or []:
        if not isinstance(entry, dict) or entry.get("kind") != "changes_only":
            continue
        method = resolve_operation(str(entry.get("operation") or ""), methods)
        if method is not None and (method.operation_id, method.path) == (update.operation_id, update.path):
            found = (found or set()) | {str(f) for f in entry.get("fields") or []}
    return found


def measure_async(coverage: dict[str, Any], methods: dict[str, Method]) -> dict[str, Any]:
    """How much of this provider is long-running, and where it polls.

    The provider must poll whenever a mutating call returns an operation handle
    rather than the resource. Counting it is what tells the generator that
    polling is not optional, and the count is reproducible from the pinned
    documents.

    Every planned verb except `read` is mutating, and so is every update call,
    which the provider sends to change the resource. The HTTP method does not
    decide it: some APIs read with `POST`.
    """
    total = 0
    asynchronous: list[str] = []
    unknown: list[str] = []
    shape = _resource_shape()
    for resource in planned_resources(coverage):
        for verb, reference in shape.verbs(resource).items():
            if verb == "read":
                continue
            total += 1
            identifier = stated_reference(str(reference))
            method = resolve_operation(str(reference), methods)
            if method is None:
                unknown.append(identifier)
            elif method.async_reason:
                asynchronous.append(identifier)
    return {
        "mutating_operations": total,
        "long_running": len(asynchronous),
        "synchronous": total - len(asynchronous) - len(unknown),
        "unresolved": sorted(unknown),
        "polling_required": bool(asynchronous),
        "long_running_operations": sorted(asynchronous),
    }


def _load_coverage(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InputError(f"no such file: {path}") from error
    except (yaml.YAMLError, UnicodeDecodeError) as error:
        raise InputError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(loaded, dict):
        raise InputError(f"{path} does not hold a mapping")
    if not loaded.get("module_plan"):
        raise InputError(f"{path} has no 'module_plan' section to generate from")
    return loaded


def command_schema(args: argparse.Namespace) -> int:
    coverage = _load_coverage(Path(args.coverage))
    methods, schemas = load_documents([Path(p) for p in args.spec])
    scope = AccountScope.parse(args.account_scope) if args.account_scope else None
    try:
        previous, indent = reviewed.previous(args.out)
    except ValueError as error:
        raise InputError(str(error)) from error
    derived = derive(
        coverage, methods, schemas, max_depth=args.max_depth, scope=scope, answers=reviewed.naming_answers(previous)
    )
    if args.section:
        wanted = narrowed_to_sections(coverage, args.section, args.requirements)
        derived = [d for d in derived if d.terraform_type in wanted]
    if args.resource:
        derived = [d for d in derived if d.terraform_type == args.resource]
        if not derived:
            raise InputError(f"no planned resource named {args.resource!r}")
    payload = {
        "max_depth": args.max_depth,
        "resources": [d.as_dict() for d in derived],
    }
    if previous is not None and (args.section or args.resource):
        # A narrowed run replaces only the resources that it derived. The file
        # keeps every other resource as reviewed, `sensitive` flags included.
        derived_types = {d.terraform_type for d in derived}
        payload["resources"] += [
            entry
            for entry in previous.get("resources") or []
            if isinstance(entry, dict) and str(entry.get("terraform_type")) not in derived_types
        ]
    if previous is not None:
        kept, lost = reviewed.carry(previous, payload)
        for where in kept:
            print(f"kept {where}", file=sys.stderr)
        for where in lost:
            print(f"warning: {where} was reviewed, and this derivation has no place for it now", file=sys.stderr)
    text = json.dumps(payload, indent=indent, sort_keys=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    for item in derived:
        counts = _counts(item.attributes)
        print(
            f"{item.terraform_type}: {counts['total']} attributes "
            f"({counts[REQUIRED]} required, {counts[OPTIONAL]} optional, "
            f"{counts[COMPUTED]} computed)",
            file=sys.stderr,
        )
    for item in derived:
        for parameter in replacement.open_naming(item.naming):
            print(
                f"open: {item.terraform_type} naming {parameter}: no review answer says which field names the object",
                file=sys.stderr,
            )
    return 0


def _counts(attributes: tuple[Attribute, ...] | list[Attribute]) -> dict[str, int]:
    out = {"total": 0, REQUIRED: 0, OPTIONAL: 0, COMPUTED: 0}
    for attribute in attributes:
        out["total"] += 1
        out[attribute.behaviour] += 1
        for key, value in _counts(attribute.attributes).items():
            out[key] += value
    return out


def command_measure(args: argparse.Namespace) -> int:
    coverage = _load_coverage(Path(args.coverage))
    methods, _ = load_documents([Path(p) for p in args.spec])
    result = measure_async(coverage, methods)
    print(json.dumps(result, indent=2))
    if result["polling_required"]:
        print(
            f"\n{result['long_running']} of {result['mutating_operations']} mutating "
            f"operations are long-running. The provider must poll to a terminal "
            f"state, read the operation's error, and take the resource identifier "
            f"from the resource the operation names.",
            file=sys.stderr,
        )
    return 0


def command_settings(args: argparse.Namespace) -> int:
    return settings_check.check(Path(args.export))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="provider_schema")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("schema", "derive the resource schemas from the specification"),
        ("measure", "count the long-running operations the provider must poll"),
    ):
        child = subparsers.add_parser(name, help=help_text)
        child.add_argument("--coverage", required=True, help="path to coverage.yaml")
        child.add_argument(
            "--spec",
            required=True,
            action="append",
            help="a pinned OpenAPI 3 or Google Discovery document; repeatable",
        )
        if name == "schema":
            child.add_argument("--out", help="write JSON here instead of stdout")
            child.add_argument("--resource", help="one terraform_type only")
            child.add_argument(
                "--section",
                action="append",
                help="only resources filed under this requirement section or "
                "subsection, matched as a case-insensitive substring; repeatable",
            )
            child.add_argument(
                "--requirements",
                help="the requirement catalog; the bundled one by default",
            )
            child.add_argument(
                "--account-scope",
                help="the account scope the provider configuration fills, as the API "
                "writes it, e.g. 'projects/{project}'",
            )
            child.add_argument(
                "--max-depth",
                type=int,
                default=DEFAULT_MAX_DEPTH,
                help=f"expansion bound (default {DEFAULT_MAX_DEPTH})",
            )
    child = subparsers.add_parser("settings", help="check every provider setting has an environment fallback")
    child.add_argument(
        "--export",
        required=True,
        help="path to the provider's own `terraform providers schema -json` output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "schema":
            return command_schema(args)
        if args.command == "settings":
            return command_settings(args)
        return command_measure(args)
    except (InputError, settings_check.UnreadableExport) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
