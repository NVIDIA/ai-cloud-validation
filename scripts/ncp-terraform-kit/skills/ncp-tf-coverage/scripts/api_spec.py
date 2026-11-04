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

"""One view of an NCP API specification, whatever form the NCP publishes it in.

Two forms reach us. OpenAPI 3.x names each operation with `operationId` and
keys everything under `paths`. Google publishes Discovery documents instead,
which key operations by a dotted `id` and carry no `paths` object at all. Both
describe the same thing, so both reduce to the same `Spec` here and the gates
upstream never branch on which one arrived.

**Why the operation identifier is the preferred evidence form.** A REST path is
three unstable things at once: an HTTP verb an analyst can get wrong, a path
template with several legal spellings (`{name=projects/*}` in the REST reference,
`{projectsId}` in Discovery, `{+name}` in the raw method), and a version prefix.
The identifier is one string with one spelling, it carries no verb, and it is
what every generated SDK is built from -- so it maps to the SDK method the
provider will actually call. Measured on `cloudkms` v1: all 87 Discovery method
ids derive their `google-api-go-client` symbol by the rule in `sdk_symbol`.

`METHOD /path` stays supported. An NCP whose spec defines no identifiers has
nothing else to cite, and rejecting it would be a rule about publishing habits
rather than about coverage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

#: OpenAPI's operation keys. A path item holds these plus non-operation keys
#: such as `parameters`, which is why membership is tested rather than assumed.
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

_TEMPLATE = re.compile(r"\{[+]?[A-Za-z0-9_.]+=([^}]*)\}")
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def normalize_path(path: str) -> str:
    """Reduce a path template to a comparable shape.

    The same operation is spelled three ways across Google's own surfaces --
    `/v1/{name=projects/*/locations/*}`, `/v1/projects/{projectsId}/locations/{locationsId}`,
    and `/v1/{+name}`. Comparing them literally reports differences that are
    notation, not substance, so every placeholder collapses to `*` first.
    """
    expanded = _TEMPLATE.sub(r"\1", path)
    starred = _PLACEHOLDER.sub("*", expanded)
    return re.sub(r"/+", "/", "/" + starred.lstrip("/"))


@dataclass(frozen=True)
class Operation:
    """One callable operation, named the way its own specification names it."""

    operation_id: str
    http_method: str
    path: str
    #: The body this operation takes, by bare schema name, or "". Two operations
    #: that take the same body under different parents are the same subject at
    #: two scopes; two that merely end in the same word are not.
    request_schema: str = ""
    #: What the document says the operation does. A condition or a limit, such
    #: as "a stopped instance", is stated only here. It does not take part in
    #: equality: one operation is one operation, whatever its prose.
    description: str = field(default="", compare=False)
    #: Each field of the body this operation takes, with its description.
    request_fields: dict[str, str] = field(default_factory=dict, compare=False)
    #: Each parameter it takes in the query, with its description. A call can
    #: take its value there and send no body, as Compute's
    #: `setDeletionProtection` does, and a description there can limit the
    #: call, as an `updateMask` that may name only a few fields does.
    query_parameters: dict[str, str] = field(default_factory=dict, compare=False)
    #: The fields of the object this operation returns.
    response_fields: frozenset[str] = field(default=frozenset(), compare=False)
    #: The request's and the response's schemas as the document writes them,
    #: by reference or in place. A field's own schema is read from them: an
    #: update call whose body is the field's schema, such as a metadata setter
    #: whose body is the object's metadata, carries that field.
    request_body: Any = field(default=None, compare=False, repr=False)
    response_body: Any = field(default=None, compare=False, repr=False)
    #: The API whose document defines the operation. Two APIs can name their
    #: objects by the same path: Filestore and Parallelstore both use
    #: `v1/projects/*/locations/*/instances/*`.
    api: str = field(default="", compare=False)
    #: Each named schema of the document that defines the operation. A `$ref`
    #: in its bodies names one of them: a schema name is unique only inside
    #: one document, and two APIs can each define an `Instance`.
    schemas: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    #: Whether the document sets `deprecated: true` on the operation.
    deprecated: bool = field(default=False, compare=False)

    @property
    def normalized_path(self) -> str:
        return normalize_path(self.path)


@dataclass(frozen=True)
class Spec:
    """A specification reduced to what the gates ask of it."""

    kind: str
    operations: dict[str, Operation]
    methods_by_path: dict[str, frozenset[str]]
    schemas: frozenset[str]
    #: Operations with no identifier, which a plan cites as `METHOD /path`.
    unnamed: tuple[Operation, ...] = ()
    #: The keys of each schema's `properties`, which `schema:Name.field` cites.
    schema_fields: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def has_operation_ids(self) -> bool:
        return bool(self.operations)

    def every_operation(self) -> tuple[Operation, ...]:
        return (*self.operations.values(), *self.unnamed)

    def resolve_path(self, http_method: str, path: str) -> frozenset[str] | None:
        """Methods available at `path`, or None when the path is absent."""
        return self.methods_by_path.get(normalize_path(path))


def sdk_symbol(operation_id: str) -> str | None:
    """The generated SDK symbol for a dotted operation id, or None.

    Google's generated clients name a service type for the resource path and a
    method for the final segment, both PascalCase:

        cloudkms.projects.locations.keyRings.cryptoKeys.cryptoKeyVersions.asymmetricSign
        -> ProjectsLocationsKeyRingsCryptoKeysCryptoKeyVersionsService.AsymmetricSign

    Verified against `google-api-go-client` v0.297.0: 87 of 87 `cloudkms` v1 ids
    resolve to a real symbol by this rule.

    Returns None for a flat identifier, which is what OpenAPI's `operationId`
    usually is. There the identifier is typically the SDK method name already,
    but the surrounding client type is a generator's choice this cannot know,
    and a fabricated symbol is worse than none.
    """
    segments = operation_id.split(".")
    if len(segments) < 3:
        return None
    resource, method = segments[1:-1], segments[-1]
    if not resource:
        return None
    service = "".join(part[:1].upper() + part[1:] for part in resource)
    return f"{service}Service.{method[:1].upper() + method[1:]}"


def _properties(schema: Any, schemas: Any) -> dict[str, Any]:
    """The properties of a body, named by `$ref` or written in place."""
    if not isinstance(schema, dict):
        return {}
    name = str(schema.get("$ref") or "").rsplit("/", 1)[-1]
    body = (schemas or {}).get(name) if name else schema
    properties = body.get("properties") if isinstance(body, dict) else None
    return properties if isinstance(properties, dict) else {}


def _returned_fields(properties: dict[str, Any]) -> frozenset[str]:
    """The fields of a returned object. A response that holds one object field,
    such as `data`, wraps the object in it, so its fields are the object's too."""
    names = frozenset(str(name) for name in properties)
    if len(properties) == 1:
        [envelope] = properties.values()
        inner = envelope.get("properties") if isinstance(envelope, dict) else None
        if isinstance(inner, dict):
            names |= frozenset(str(name) for name in inner)
    return names


#: How many `$ref` and array steps a field's schema may take before its
#: fields: a list of references is two. A schema that names itself stops here.
_REFERENCE_HOPS = 4


def _properties_of(schemas: dict[str, Any], node: Any) -> dict[str, Any] | None:
    """The fields of the schema `node`, through each `$ref` into `schemas` and
    each array's items, or None for a value with no fields of its own."""
    for _ in range(_REFERENCE_HOPS):
        if not isinstance(node, dict):
            return None
        if node.get("$ref"):
            node = schemas.get(str(node["$ref"]).rsplit("/", 1)[-1])
        elif node.get("type") == "array":
            node = node.get("items")
        else:
            break
    properties = node.get("properties") if isinstance(node, dict) else None
    return properties if isinstance(properties, dict) else None


def nested_fields(schemas: dict[str, Any], node: Any, path: list[str]) -> frozenset[str]:
    """The fields of the schema at `path` inside `node`, a body of an
    operation whose document's `schemas` its references name. Empty when the
    path ends at a value with no fields of its own, such as a string."""
    for name in path:
        node = (_properties_of(schemas, node) or {}).get(name)
    return frozenset(str(key) for key in _properties_of(schemas, node) or {})


def nested_property(schemas: dict[str, Any], node: Any, path: list[str]) -> dict[str, Any] | None:
    """The field at `path` inside `node` as the document writes it, or None
    when the schema has no such field."""
    field: Any = None
    for name in path:
        field = (_properties_of(schemas, node) or {}).get(name)
        if not isinstance(field, dict):
            return None
        node = field
    return field if isinstance(field, dict) else None


def nested_description(schemas: dict[str, Any], node: Any, path: list[str]) -> str | None:
    """The description of the field at `path` inside `node`, or None when the
    schema has no such field. A field named by reference keeps its own
    description beside the `$ref`."""
    field = nested_property(schemas, node, path)
    return None if field is None else str(field.get("description") or "")


def returned_properties(operation: Operation) -> dict[str, dict[str, Any]]:
    """Each field of the object that `operation` returns, as its document
    writes it. A response that holds one object field, such as `data`, wraps
    the object."""
    properties = _properties_of(operation.schemas, operation.response_body) or {}
    if len(properties) == 1:
        properties = _properties_of(operation.schemas, next(iter(properties.values()))) or properties
    return {str(name): body for name, body in properties.items() if isinstance(body, dict)}


def returned_descriptions(operation: Operation) -> dict[str, str]:
    """Each field of the object that `operation` returns, with its description
    and what each of its values means, as Discovery's `enumDescriptions` say."""
    return {name: value_text(body) for name, body in returned_properties(operation).items()}


def value_text(body: dict[str, Any]) -> str:
    """A field's description, then what each of its values means."""
    meanings = [
        f"{value}: {text}" for value, text in zip(body.get("enum") or [], body.get("enumDescriptions") or []) if text
    ]
    return " ".join(text for text in [str(body.get("description") or ""), *meanings] if text)


def _in_query(parameters: Any, document: dict[str, Any]) -> dict[str, str]:
    """Each query parameter with its description, as Discovery (`location`) or
    OpenAPI (`in`, possibly behind a `$ref`) declares them."""
    if isinstance(parameters, dict):
        return {
            str(name): str(body.get("description") or "")
            for name, body in parameters.items()
            if isinstance(body, dict) and body.get("location") == "query"
        }
    found: dict[str, str] = {}
    shared = (document.get("components") or {}).get("parameters") or {}
    for body in parameters if isinstance(parameters, list) else []:
        if isinstance(body, dict) and body.get("$ref"):
            body = shared.get(str(body["$ref"]).rsplit("/", 1)[-1])
        if isinstance(body, dict) and body.get("in") == "query" and body.get("name"):
            found[str(body["name"])] = str(body.get("description") or "")
    return found


def _described(properties: dict[str, Any]) -> dict[str, str]:
    return {
        str(name): str(body.get("description") or "") if isinstance(body, dict) else ""
        for name, body in properties.items()
    }


def _walk_discovery(node: dict[str, Any], base: str, into: dict[str, Operation], schemas: Any) -> None:
    for method in (node.get("methods") or {}).values():
        # `flatPath` spells every parameter out; `path` may collapse a whole
        # resource name into one `{+name}` and cannot be compared positionally.
        path = method.get("flatPath") or method.get("path") or ""
        identifier = method.get("id")
        if not identifier:
            continue
        into[identifier] = Operation(
            operation_id=identifier,
            http_method=str(method.get("httpMethod", "")).upper(),
            # A Discovery `basePath` can end in a slash (`/compute/v1/`) or be
            # empty while the method path holds the version (`v1/projects`).
            path=f"{base.rstrip('/')}/{path.lstrip('/')}",
            request_schema=str((method.get("request") or {}).get("$ref") or ""),
            description=str(method.get("description") or ""),
            request_fields=_described(_properties(method.get("request"), schemas)),
            response_fields=_returned_fields(_properties(method.get("response"), schemas)),
            query_parameters=_in_query(method.get("parameters"), {}),
            request_body=method.get("request"),
            response_body=method.get("response"),
            schemas=schemas if isinstance(schemas, dict) else {},
            deprecated=method.get("deprecated") is True,
        )
    for child in (node.get("resources") or {}).values():
        _walk_discovery(child, base, into, schemas)


def _schema_fields(schemas: Any) -> dict[str, frozenset[str]]:
    """The keys of each schema's own `properties`. A field reached only through
    `allOf` or a `$ref` does not count."""
    if not isinstance(schemas, dict):
        return {}
    return {
        name: frozenset(body.get("properties") or {})
        for name, body in schemas.items()
        if isinstance(body, dict) and isinstance(body.get("properties") or {}, dict)
    }


def _from_discovery(document: dict[str, Any]) -> Spec:
    operations: dict[str, Operation] = {}
    _walk_discovery(document, document.get("basePath") or "", operations, document.get("schemas"))
    operations = {key: replace(operation, api=str(document.get("name") or "")) for key, operation in operations.items()}
    return Spec(
        kind="discovery",
        operations=operations,
        methods_by_path=_index_paths(operations.values()),
        schemas=frozenset(document.get("schemas") or {}),
        schema_fields=_schema_fields(document.get("schemas")),
    )


def _returned(body: dict[str, Any]) -> Any:
    """The schema of the first success response that has one."""
    # A YAML document may load `200` as a number beside `default`, so sort on text.
    for status, response in sorted((body.get("responses") or {}).items(), key=lambda item: str(item[0])):
        if str(status).startswith("2") and isinstance(response, dict):
            for media in (response.get("content") or {}).values():
                if isinstance(media, dict) and media.get("schema"):
                    return media["schema"]
    return None


def _from_openapi(document: dict[str, Any]) -> Spec:
    operations: dict[str, Operation] = {}
    collected: list[Operation] = []
    schemas = (document.get("components") or {}).get("schemas") or {}
    for path, item in (document.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for verb, body in item.items():
            if verb.lower() not in HTTP_METHODS:
                continue
            identifier = (body or {}).get("operationId") if isinstance(body, dict) else None
            declared = (
                next(
                    (
                        (media or {}).get("schema") or {}
                        for media in (((body or {}).get("requestBody") or {}).get("content") or {}).values()
                        if isinstance(media, dict)
                    ),
                    {},
                )
                if isinstance(body, dict)
                else {}
            )
            operation = Operation(
                operation_id=identifier or "",
                http_method=verb.upper(),
                path=path,
                request_schema=str(declared.get("$ref") or "").rsplit("/", 1)[-1],
                description=str((body or {}).get("description") or (body or {}).get("summary") or "")
                if isinstance(body, dict)
                else "",
                request_fields=_described(_properties(declared, schemas)),
                query_parameters=_in_query([*(item.get("parameters") or []), *(body.get("parameters") or [])], document)
                if isinstance(body, dict)
                else {},
                response_fields=_returned_fields(_properties(_returned(body), schemas))
                if isinstance(body, dict)
                else frozenset(),
                request_body=declared or None,
                response_body=_returned(body) if isinstance(body, dict) else None,
                api=str((document.get("info") or {}).get("title") or ""),
                schemas=schemas,
                deprecated=isinstance(body, dict) and body.get("deprecated") is True,
            )
            collected.append(operation)
            if identifier:
                operations[identifier] = operation
    return Spec(
        kind="openapi",
        operations=operations,
        methods_by_path=_index_paths(collected),
        schemas=frozenset(schemas),
        unnamed=tuple(operation for operation in collected if not operation.operation_id),
        schema_fields=_schema_fields(schemas),
    )


def _index_paths(operations: Any) -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for operation in operations:
        index.setdefault(operation.normalized_path, set()).add(operation.http_method)
    return {path: frozenset(methods) for path, methods in index.items()}


def from_document(document: Any, source: str = "specification") -> Spec:
    """Reduce a loaded specification to a `Spec`, whichever form it is in."""
    if not isinstance(document, dict):
        raise ValueError(f"{source} is not a mapping")
    if document.get("kind") == "discovery#restDescription" or "resources" in document:
        return _from_discovery(document)
    if "paths" in document:
        _require_openapi_3(document, source)
        return _from_openapi(document)
    raise ValueError(f"{source} is neither OpenAPI (no 'paths') nor a Discovery document (no 'resources')")


def _require_openapi_3(document: dict[str, Any], source: str) -> None:
    """Refuse a document with `paths` that does not declare OpenAPI 3.x.

    This reader indexes `components.schemas` and `requestBody` only. A Swagger
    2.0 document keeps them in `definitions` and in body parameters, so its
    schemas and request bodies read as absent. The provider step refuses the
    same documents.
    """
    version = str(document.get("openapi") or "")
    if version.startswith("3."):
        return
    swagger = document.get("swagger")
    if swagger:
        found = f"(swagger: {swagger}). A Swagger {swagger} document predates OpenAPI 3; supply its OpenAPI 3 form"
    elif version:
        found = f"(openapi: {version})"
    else:
        found = "(no 'openapi' version). It has 'paths' and declares no 'openapi' version"
    raise ValueError(f"{source} is not an OpenAPI 3.x document {found}")


def load(path: Path) -> Spec:
    text = path.read_text(encoding="utf-8")
    document = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    return from_document(document, str(path))


def load_many(paths: list[Path]) -> Spec:
    """Load several specifications and merge them into one. See `merge`."""
    return merge([load(path) for path in paths])


def merge(specs: list[Spec]) -> Spec:
    """Merge several specifications into one.

    An NCP is not always one API. GCP spreads the four module layers across
    Compute, GKE, IAM, KMS and more, each with its own document, and a coverage
    analysis spanning them needs every one in scope at once.

    A repeated identifier is refused rather than merged. Discovery ids are
    unique within one document but **not** across versions of one API:
    `cloudresourcemanager` v1 and v3 both define
    `cloudresourcemanager.projects.create` at different paths. Merged silently,
    whichever loaded last won, so a v3 reference read as absent while a v1
    reference read as verified -- one evidence string meaning two different
    operations depending on the order of `--spec`. Naming the conflict makes
    the operator choose the version they actually analysed.
    """
    if not specs:
        raise ValueError("no specification given")
    operations: dict[str, Operation] = {}
    for spec in specs:
        for identifier, operation in spec.operations.items():
            existing = operations.get(identifier)
            if existing is not None and existing != operation:
                raise ValueError(
                    f"'{identifier}' is defined twice, as "
                    f"{existing.http_method} {existing.path} and "
                    f"{operation.http_method} {operation.path}; pass one "
                    f"version of each API, not several"
                )
            operations[identifier] = operation
    merged_paths: dict[str, set[str]] = {}
    fields: dict[str, set[str]] = {}
    for spec in specs:
        for path, methods in spec.methods_by_path.items():
            merged_paths.setdefault(path, set()).update(methods)
        for name, names in spec.schema_fields.items():
            fields.setdefault(name, set()).update(names)
    return Spec(
        kind="+".join(sorted({spec.kind for spec in specs})),
        operations=operations,
        methods_by_path={p: frozenset(m) for p, m in merged_paths.items()},
        schemas=frozenset().union(*(spec.schemas for spec in specs)),
        unnamed=tuple(operation for spec in specs for operation in spec.unnamed),
        schema_fields={name: frozenset(names) for name, names in fields.items()},
    )
