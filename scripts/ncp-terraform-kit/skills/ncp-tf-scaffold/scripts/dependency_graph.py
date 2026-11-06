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
"""The dependency resources that the documents state, for each planned create.

Two rules, each a fact that a document states:

- path nesting: the create posts under the item of another resource, so that
  resource must exist first;
- a stated name format: a request field's description gives the name format
  of another resource in code marks, such as `groups/{group}`.

A field name alone makes no dependency: `keyAlgorithm` is not a key. Prose
such as "Create one first." states a dependency that no rule reads, so the
rules are a floor, and the agent still reads each create.

Each rule ends at a name shape: the item path of a resource, with `{}` for
each value, as `groups/{}`. A planned resource whose read has that shape is
the dependency. A name with no host is a name of the create's own API, so
when the create's own document reads an item of that shape, only a type of
that document is the dependency. Two APIs can read items of one shape. The
account scope is the provider's configuration, not a dependency.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import attribute_names
from case_names import NAME_MAX

#: The rule names a derived dependency records.
PATH_NESTING = "path_nesting"
STATED_FORMAT = "stated_format"

#: One segment of a Discovery name pattern: a literal collection, or one value.
_VALUE = "[^/]+"
_LITERAL = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
#: A name format in code marks: collections and values in turn, as
#: `groups/{group}` or `projects/{project}/serviceAccounts/{account}`.
FORMAT = re.compile(r"`((?:[A-Za-z][A-Za-z0-9-]*/\{[^{}`/]+\})(?:/[A-Za-z][A-Za-z0-9-]*/\{[^{}`/]+\})*)`")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
#: A length both languages state in prose, as `4-32 characters`, with a hyphen or an en dash.
LIMIT = re.compile(r"(\d+)[-–](\d+)\s+characters", re.IGNORECASE)  # noqa: RUF001


@dataclass(frozen=True)
class Method:
    """One operation, reduced to what the two rules read."""

    ref: str
    key: str
    #: The position of the method's document in the documents read.
    document: int = 0
    #: The item path that a read of one item takes, as `groups/{}`; "" otherwise.
    shape: str = ""
    #: Each path parameter that holds another item: (parameter, shape, evidence).
    nested: tuple[tuple[str, str, str], ...] = ()
    body: dict[str, Any] = field(default_factory=dict)
    resolve: Callable[[str], dict[str, Any]] = lambda _name: {}
    #: Each path and query parameter, with what the document says about it.
    params: tuple[tuple[str, str], ...] = ()


def pattern_shape(pattern: str) -> str:
    """`^groups/[^/]+$` as `groups/{}`, or "" when the pattern is not a name."""
    parts = pattern.removeprefix("^").removesuffix("$").replace(_VALUE, "{}").split("/")
    if len(parts) % 2 or not all(_LITERAL.match(p) if n % 2 == 0 else p == "{}" for n, p in enumerate(parts)):
        return ""
    return "/".join(parts)


def template_shape(template: str) -> str:
    """A path or a format with `{}` for each value: `v1/rulesets/{id}` as `v1/rulesets/{}`."""
    return _PLACEHOLDER.sub("{}", template.strip("/"))


def names(shape: str, wanted: str) -> bool:
    """Whether an item path is the one a shape names: the same, or ending in it.

    A format states a name from its first collection, and an OpenAPI path
    carries the API's own prefix before it.
    """
    return bool(shape and wanted) and (shape == wanted or shape.endswith("/" + wanted))


# --- reading the documents ----------------------------------------------------


def index(documents: list[Any]) -> dict[str, Method]:
    """Each operation of the documents, under its reference and its `METHOD /path`."""
    found: list[Method] = []
    for position, document in enumerate(documents):
        if not isinstance(document, dict):
            continue
        if document.get("resources") and document.get("name"):
            found += _discovery(document, position)
        elif document.get("paths"):
            found += _openapi(document, position)
    return {key: method for method in found for key in (method.ref, method.key)}


def _discovery(document: dict[str, Any], position: int) -> list[Method]:
    api, schemas = str(document.get("name")), document.get("schemas") or {}
    service = str(document.get("servicePath") or "")
    found: list[Method] = []

    def walk(resources: Any, path: str) -> None:
        for name, body in (resources or {}).items():
            here = f"{path}.{name}" if path else name
            for method_name, meta in (body.get("methods") or {}).items():
                patterns = {
                    p: str(s["pattern"])
                    for p, s in (meta.get("parameters") or {}).items()
                    if isinstance(s, dict) and s.get("location") == "path" and s.get("pattern")
                }
                shapes = [pattern_shape(p) for p in patterns.values()]
                found.append(
                    Method(
                        ref=f"op:{api}.{here}.{method_name}",
                        key=_key(str(meta.get("httpMethod")), service + str(meta.get("flatPath") or meta.get("path"))),
                        document=position,
                        # A list takes its parent's name, so only a get names its own item.
                        shape=shapes[0] if method_name == "get" and len(shapes) == 1 else "",
                        nested=tuple((p, pattern_shape(v), v) for p, v in sorted(patterns.items()) if pattern_shape(v)),
                        body=schemas.get(str((meta.get("request") or {}).get("$ref") or "")) or {},
                        resolve=lambda ref: schemas.get(ref) or {},
                        params=_described(p for p in (meta.get("parameters") or {}).items()),
                    )
                )
            walk(body.get("resources"), here)

    walk(document.get("resources"), "")
    return found


def _openapi(document: dict[str, Any], position: int) -> list[Method]:
    schemas = (document.get("components") or {}).get("schemas") or {}
    found: list[Method] = []
    for path, item in (document.get("paths") or {}).items():
        for verb, meta in (item or {}).items():
            if verb.lower() not in {"get", "put", "post", "delete", "patch"}:
                continue
            shape = template_shape(path)
            found.append(
                Method(
                    ref=f"op:{meta.get('operationId') or f'{verb}{path}'}",
                    key=_key(verb, path),
                    document=position,
                    # A list has no value at the end of its path, so only a read of one item does.
                    shape=shape if verb.lower() == "get" and shape.endswith("{}") else "",
                    nested=_openapi_nested(path) if verb.lower() == "post" else (),
                    body=_openapi_body(meta, schemas),
                    resolve=lambda ref: schemas.get(ref.rsplit("/", 1)[-1]) or {},
                    params=_described(
                        (str(p.get("name")), {**p, "location": p.get("in")})
                        for p in [*(meta.get("parameters") or []), *(item.get("parameters") or [])]
                        if isinstance(p, dict)
                    ),
                )
            )
    return found


def _described(parameters: Iterable[tuple[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Each path and query parameter in attribute spelling, with its description."""
    return tuple(
        (attribute_names.root_name(str(name)), str(spec.get("description") or ""))
        for name, spec in parameters
        if isinstance(spec, dict) and spec.get("location") in ("path", "query")
    )


def _openapi_nested(path: str) -> tuple[tuple[str, str, str], ...]:
    """Each item path that a POST's path passes through, with its parameter."""
    parts = path.strip("/").split("/")
    return tuple(
        (part.strip("{}"), template_shape("/".join(parts[: n + 1])), path)
        for n, part in enumerate(parts[:-1])
        if _PLACEHOLDER.fullmatch(part)
    )


def _openapi_body(meta: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    for media in ((meta.get("requestBody") or {}).get("content") or {}).values():
        declared = (media or {}).get("schema") or {}
        if declared.get("$ref"):
            return schemas.get(str(declared["$ref"]).rsplit("/", 1)[-1]) or {}
        if declared:
            return declared
    return {}


def _key(verb: str, path: str) -> str:
    return f"{verb.upper()} /{path.lstrip('/')}"


def body_fields(method: Method) -> Iterator[tuple[str, str]]:
    """Each request body field at any depth, in attribute spelling, with its description."""

    def walk(schema: dict[str, Any], prefix: str, seen: frozenset[str]) -> Iterator[tuple[str, str]]:
        for name, prop in (schema.get("properties") or {}).items():
            if not isinstance(prop, dict):
                continue
            here = (
                f"{prefix}.{attribute_names.terraform_name(str(name))}"
                if prefix
                else attribute_names.root_name(str(name))
            )
            yield here, str(prop.get("description") or "")
            inner = prop.get("items") if prop.get("type") == "array" else prop
            ref = str((inner or {}).get("$ref") or "")
            if ref and ref not in seen:
                yield from walk(method.resolve(ref), here, seen | {ref})
            elif not ref and isinstance(inner, dict):
                yield from walk(inner, here, seen)

    yield from walk(method.body, "", frozenset())


def stated_formats(method: Method) -> Iterator[tuple[str, str, str]]:
    """Each request field that states a name format: (field, shape, evidence)."""
    for here, description in body_fields(method):
        for stated in FORMAT.findall(description):
            yield here, template_shape(stated), f"`{stated}`"


def stated_limits(method: Method) -> dict[str, int]:
    """The longest value each parameter and body field takes, where the document states one."""
    found: dict[str, int] = {}
    for here, description in [*method.params, *body_fields(method)]:
        if hit := LIMIT.search(description):
            found[here] = int(hit.group(2))
    return found


# --- the derivation -----------------------------------------------------------


def derive(
    documents: list[Any],
    coverage: dict[str, Any],
    account_scope: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """Each planned type's derived dependencies, by type.

    `account_scope` is the provider's, as `projects/{project}`: a value of that
    shape is the provider's configuration, so it is no dependency.
    """
    methods = index(documents)
    planned = _planned(methods, coverage)
    reads = [(kind, read) for kind, _, read in planned if read and read.shape]
    items = {(method.document, method.shape) for method in methods.values() if method.shape}
    scope = template_shape(account_scope) if account_scope else ""
    return {kind: _dependencies(kind, create, reads, items, scope) for kind, create, _ in planned if create}


@dataclass(frozen=True)
class Derived:
    """What the documents state about one planned type that its case records."""

    dependencies: list[dict[str, Any]]
    #: The longest value each create field takes, where the document states one.
    field_limits: dict[str, int]


def derived(documents: list[Any], coverage: dict[str, Any], account_scope: str = "") -> dict[str, Derived]:
    """Each planned type's derived dependencies and stated field limits, by type."""
    dependencies = derive(documents, coverage, account_scope)
    return {
        kind: Derived(dependencies[kind], stated_limits(create))
        for kind, create, _ in _planned(index(documents), coverage)
        if create
    }


def _planned(methods: dict[str, Method], coverage: dict[str, Any]) -> list[tuple[str, Method | None, Method | None]]:
    return [
        (
            str(entry.get("terraform_type") or ""),
            _found(methods, (entry.get("operations") or {}).get("create")),
            _found(methods, (entry.get("operations") or {}).get("read")),
        )
        for layer in (((coverage or {}).get("module_plan") or {}).get("layers") or {}).values()
        for entry in (layer or {}).get("resources") or []
    ]


def _found(methods: dict[str, Method], reference: Any) -> Method | None:
    if not reference:
        return None
    text = str(reference)
    verb, _, path = text.partition(" ")
    return methods.get(text) or (methods.get(_key(verb, path)) if path else None)


def _dependencies(
    kind: str,
    create: Method,
    reads: list[tuple[str, Method]],
    items: set[tuple[int, str]],
    scope: str,
) -> list[dict[str, Any]]:
    """Each planned type, other than `kind`, whose item a value of the create names.

    `items` holds each document's item shapes, planned or not. A name that the
    create's own document reads names that document's item, also when the plan
    holds no type for it.
    """
    candidates = [(attribute_names.root_name(p), shape, evidence, PATH_NESTING) for p, shape, evidence in create.nested]
    candidates += [(f, shape, evidence, STATED_FORMAT) for f, shape, evidence in stated_formats(create)]
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for name, shape, evidence, rule in candidates:
        if scope and shape == scope:
            continue
        own = any(document == create.document and names(item, shape) for document, item in items)
        for other, read in reads:
            if other != kind and names(read.shape, shape) and (read.document == create.document or not own):
                found.setdefault((name, other), {"field": name, "resource": other, "rule": rule, "evidence": evidence})
    return list(found.values())


# --- the case check -----------------------------------------------------------


#: The case field the derivation writes.
FIELD = "derived_dependencies"
_WHOLE = re.compile(r"^\{([a-z_][a-z0-9_]*)\}$")


#: Where the case records the limits of the fields other than its name field.
LIMITS = "field_limits"


def placed(case: dict[str, Any], found: Derived | None) -> dict[str, Any]:
    """The case with its derived dependencies after `naming`, beside the fixture they
    constrain, and the stated limit of each field other than the name field in `naming`.

    A case with none holds no field, so a reviewer reads an entry as a fact.
    `naming.name_limit` already holds the name field's limit.
    """
    if found is None:
        return case
    naming = dict(case.get("naming") or {})
    named = attribute_names.configuration_path(str(naming.get("name_field") or ""))
    # The kit caps every name at NAME_MAX, so a longer limit constrains nothing.
    limits = {
        field_name: limit
        for field_name, limit in sorted(found.field_limits.items())
        if field_name != named and limit < NAME_MAX
    }
    if limits:
        naming[LIMITS] = limits
    shaped: dict[str, Any] = {}
    for key, value in case.items():
        shaped[key] = naming if key == "naming" else value
        if key == "naming" and found.dependencies:
            shaped[FIELD] = [dict(entry) for entry in found.dependencies]
    return shaped


def refusals(case: dict[str, Any]) -> list[str]:
    """Each derived dependency that `fixture.create` sets without declaring it.

    A field the fixture does not set is not refused: the provider fills an
    account parameter, and a body field can be optional.
    """
    declared = {
        str(e.get("variable")): str(e.get("resource") or "")
        for e in case.get("dependencies") or []
        if isinstance(e, dict)
    }
    create = (case.get("fixture") or {}).get("create")
    found = []
    for entry in case.get(FIELD) or []:
        kind, field_name = str(entry.get("resource")), str(entry.get("field"))
        for value in _values_at(create, field_name.split(".")):
            whole = _WHOLE.match(value) if isinstance(value, str) else None
            if whole and declared.get(whole.group(1)) == kind:
                continue
            found.append(_refusal(entry, value))
            break
    return found


def _values_at(node: Any, parts: list[str]) -> Iterator[Any]:
    if isinstance(node, list):
        for item in node:
            yield from _values_at(item, parts)
    elif not parts:
        yield node
    elif isinstance(node, dict) and parts[0] in node:
        yield from _values_at(node[parts[0]], parts[1:])


def _refusal(entry: dict[str, Any], value: Any) -> str:
    kind = entry.get("resource")
    message = (
        f"fixture.create.{entry.get('field')} is {value!r}, which names a {kind} "
        f"({entry.get('rule')}: {entry.get('evidence')}), and no `dependencies` entry with "
        f"`resource: {kind}` gives it. The provider can create a {kind}, so declare it: write "
        "the value as the entry's placeholder, and name the created resource with `reference` "
        "or `attribute` from its reviewed case (references/dependencies.md)"
    )
    if entry.get("rule") == PATH_NESTING:
        message += (
            f". When {kind} is the account that the provider configuration names, run "
            "`instantiate` again with the provider's `--account-scope` instead"
        )
    return message
