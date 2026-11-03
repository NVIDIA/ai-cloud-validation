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

"""The operation an evidence reference names, and what that operation does.

`op:<identifier>` and `METHOD /path` can name one operation. A gate that
compares the strings reads one operation in two spellings as two. A gate that
reads only the segments of a dotted identifier sees nothing in a document whose
identifiers are flat, or that has none. So a gate resolves each reference to
the specification's operation first, and decides on the operation.
"""

from __future__ import annotations

from pathlib import Path

from api_spec import Operation, Spec, normalize_path

OPERATION_PREFIX = "op:"
SCHEMA_PREFIX = "schema:"
EXTERNAL_PREFIX = "ext:"

#: Final identifier segments that create an object. OpenAPI `operationId`s tend
#: to say `create`, Google Discovery says `insert`. Narrow on purpose: a dotted
#: identifier names its verb, and `compute.networks.addPeering` and
#: `setIamPolicy` are POSTs that create no object.
CREATING_VERBS = frozenset({"create", "insert"})

#: What `normalize_path` writes for each parameter segment.
PARAMETER = "*"


def resolve(reference: str, spec: Spec) -> Operation | None:
    """The operation a reference names, in either form, or None.

    A `METHOD /path` that several supplied documents share names no one
    operation. Google APIs repeat paths such as
    `/v1/projects/{p}/locations/{l}/operations`, and the first match would make
    the answer depend on the order of `--spec`.
    """
    if reference.startswith(OPERATION_PREFIX):
        return spec.operations.get(reference[len(OPERATION_PREFIX) :])
    matches = path_matches(reference, spec)
    return next(iter(matches)) if len(matches) == 1 else None


def path_matches(reference: str, spec: Spec) -> frozenset[Operation]:
    """Every operation a `METHOD /path` reference matches, across all documents."""
    verb, _, path = reference.partition(" ")
    if not path:
        return frozenset()
    wanted = normalize_path(path)
    return frozenset(
        operation
        for operation in spec.every_operation()
        if operation.http_method == verb.upper() and operation.normalized_path == wanted
    )


def resolves_into(reference: str, document: Spec) -> bool:
    """Whether a reference names something that one document defines.

    An `op:` or `METHOD /path` reference names the document that defines the
    operation. A `schema:` reference names each document that defines the
    schema.
    """
    if reference.startswith(OPERATION_PREFIX):
        return reference[len(OPERATION_PREFIX) :] in document.operations
    if reference.startswith(SCHEMA_PREFIX):
        return reference[len(SCHEMA_PREFIX) :].split(".", 1)[0] in document.schemas
    verb, _, path = reference.partition(" ")
    available = document.resolve_path(verb, path) if path else None
    return bool(available) and verb.upper() in available


def uncited_documents(references: list[str], documents: dict[Path, Spec]) -> list[str]:
    """A failure for each supplied document that no reference resolves into.

    The operator fixes the spec directory. A document in it that no evidence
    names is one the analysis did not read, and its requirements read as
    absent although nobody looked. `ext:` names an endpoint outside the NCP's
    API, so it reads no document.
    """
    cited = [ref for ref in references if not ref.startswith(EXTERNAL_PREFIX)]
    return [
        f"{path.name}: no evidence reference resolves into this document. It was "
        f"supplied and never cited: read it and cite what it defines, or ask the "
        f"operator to take it out of the supplied set"
        for path, document in documents.items()
        if not any(resolves_into(ref, document) for ref in cited)
    ]


def identity(reference: str, spec: Spec) -> Operation | str:
    """What to compare a reference by: its operation, or its own string.

    A reference that names no operation keeps its string. The `evidence` and
    `module-plan` gates fail such a reference before a comparison runs.
    """
    return resolve(reference, spec) or reference


def cited(operation: Operation) -> str:
    """The reference an artifact uses for an operation."""
    if operation.operation_id:
        return OPERATION_PREFIX + operation.operation_id
    return f"{operation.http_method} {operation.path}"


def describe(operation: Operation) -> str:
    """An operation in both forms, so a reader finds it in either spelling."""
    reference = cited(operation)
    if operation.operation_id:
        return f"{reference} ({operation.http_method} {operation.path})"
    return reference


def is_collection(path: str, spec: Spec) -> bool:
    """A path that ends in a literal segment, with an item path one parameter below."""
    normalized = normalize_path(path)
    last = normalized.rsplit("/", 1)[-1]
    return bool(last) and last != PARAMETER and f"{normalized}/{PARAMETER}" in spec.methods_by_path


def creates(operation: Operation, spec: Spec) -> bool:
    """Whether an operation creates an object.

    A dotted identifier names its verb. Any other operation creates when it is
    a `POST` on a collection path. A `POST` on an item path is an update or an
    action, and a `POST` on a path with no item path below it, such as
    `/v1/instance-operations/launch`, is an action that only a reading of the
    document can call a creation.
    """
    if "." in operation.operation_id:
        return operation.operation_id.rsplit(".", 1)[-1].lower() in CREATING_VERBS
    return operation.http_method == "POST" and is_collection(operation.path, spec)


def enclosing_creations(operation: Operation, spec: Spec) -> list[Operation]:
    """The creations of the collections that hold the object `operation` creates.

    Nearest first. A level that the specification never creates, such as a
    service root or a `locations` segment, carries no obligation and is left
    out.
    """
    if "." in operation.operation_id:
        return _enclosing_by_identifier(operation.operation_id, spec)
    return _enclosing_by_path(operation.normalized_path, spec)


def _enclosing_by_identifier(identifier: str, spec: Spec) -> list[Operation]:
    """`a.b.c.create` names an object inside `a.b`, which is inside `a`."""
    segments = identifier.split(".")[:-1]
    found: list[Operation] = []
    for depth in range(len(segments) - 1, 0, -1):
        prefix = ".".join(segments[:depth])
        for verb in sorted(CREATING_VERBS):
            creation = spec.operations.get(f"{prefix}.{verb}")
            if creation is not None:
                found.append(creation)
                break
    return found


def _enclosing_by_path(path: str, spec: Spec) -> list[Operation]:
    """`P/{x}/C` names an object inside `P`, while `P` is a collection.

    The walk stops at the first level that is not a collection, so a version
    prefix such as `/v1` carries no obligation.
    """
    segments = path.strip("/").split("/")
    found: list[Operation] = []
    while len(segments) >= 3 and segments[-2] == PARAMETER:
        segments = segments[:-2]
        parent = "/" + "/".join(segments)
        if not is_collection(parent, spec):
            break
        creation = resolve(f"POST {parent}", spec)
        if creation is not None:
            found.append(creation)
    return found


def nested_creations(operation: Operation, spec: Spec) -> list[Operation]:
    """The creations one collection level directly below the object that
    `operation` creates.

    A creation two levels down belongs to the object one level down, which
    owes it in turn when the plan creates that object.
    """
    if "." in operation.operation_id:
        return nested_by_identifier(operation.operation_id, spec)
    return nested_by_path(operation, spec)


def nested_by_identifier(identifier: str, spec: Spec) -> list[Operation]:
    """`a.b.create` makes an object that holds `a.b.<collection>`."""
    prefix = identifier.rsplit(".", 1)[0] + "."
    return sorted(
        (
            operation
            for key, operation in spec.operations.items()
            if key.startswith(prefix)
            and key.count(".") == identifier.count(".") + 1
            and key.rsplit(".", 1)[-1].lower() in CREATING_VERBS
        ),
        key=lambda operation: operation.operation_id,
    )


def nested_by_path(operation: Operation, spec: Spec) -> list[Operation]:
    """`POST P` makes `P/{x}`, which holds each collection `P/{x}/C`.

    `P/{x}/C` is a collection only when `P/{x}/C/{y}` is a path too, so an
    action such as `POST /instances/{id}/reboot` is not one.
    """
    if operation.http_method != "POST" or not is_collection(operation.path, spec):
        return []
    item = f"{operation.normalized_path}/{PARAMETER}/"
    return sorted(
        (
            other
            for other in spec.every_operation()
            if other.http_method == "POST"
            and other.normalized_path.startswith(item)
            and "/" not in other.normalized_path[len(item) :]
            and is_collection(other.path, spec)
        ),
        key=lambda other: other.normalized_path,
    )
