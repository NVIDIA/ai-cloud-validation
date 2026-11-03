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
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Instantiate the shipped test contracts against one NCP's module plan.

A case is instantiated, not written. The lifecycle, the verification rule and
the cleanup semantics come from the contract; the resource, the operations and
the assertions come from `module_plan`. What is left is what a script cannot
choose, and the contract names it: fixture values, an update that really
round-trips, and what counts as ready.

    instantiate  contract + module plan -> one case per subject
    check        a filled case -> every way it breaks its contract

`check` refuses a case whose judged fields are still empty. An unfilled case is
a template, and running it would report a pass for a test nobody finished.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _coverage_skill() -> Path:
    """The sibling skill that owns the requirement catalog and its reader.

    Reading it there rather than shipping a second copy is what keeps a case's
    filing and a coverage analysis agreeing about what a requirement is. The
    two skills install together, so the sibling is always beside this one.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        sibling = parent / "ncp-tf-coverage"
        if sibling.is_dir():
            return sibling
    raise SystemExit(
        f"case_builder: no ncp-tf-coverage skill beside {here.parents[1]}. "
        f"This reads the requirement catalog it ships; install both skills"
    )


sys.path.insert(0, str(_coverage_skill() / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import in_place  # noqa: E402

# The modules below write configuration paths by the provider's conversion of
# a document's field name, and read a server-assigned field by the provider's
# rule.
sys.path.append(str(in_place.provider_scripts()))

import attribute_names  # noqa: E402
import case_identity  # noqa: E402
import case_names  # noqa: E402
import case_refresh  # noqa: E402
import case_text  # noqa: E402
import dependency_forms  # noqa: E402
import dependency_graph  # noqa: E402
import dependency_names  # noqa: E402
import dependency_stubs  # noqa: E402
import destroy_read  # noqa: E402
import fixture_values  # noqa: E402
import go_alignment  # noqa: E402
import header_switch  # noqa: E402
import lifecycle_checks  # noqa: E402
import module_drift  # noqa: E402
import module_readme  # noqa: E402
import offline_values  # noqa: E402
import owed_report  # noqa: E402
import peer_case  # noqa: E402
import provider_readme  # noqa: E402
import provider_runner  # noqa: E402
import provider_settings  # noqa: E402
import read_first  # noqa: E402
import readiness_field  # noqa: E402
import recorded_specs  # noqa: E402
import refresh_run  # noqa: E402
import rendered_literals  # noqa: E402
import replaced_update  # noqa: E402
import replay_fields  # noqa: E402
import replay_render  # noqa: E402
import requirements_catalog  # noqa: E402
import resource_shape  # noqa: E402
import run_next_step  # noqa: E402
import runner_parts  # noqa: E402
import scope_flags  # noqa: E402
import selection_reason  # noqa: E402
import server_assigned  # noqa: E402
import suite_runner  # noqa: E402
import suite_values  # noqa: E402
import supplied_dependency  # noqa: E402
import update_fields  # noqa: E402
import workspace_versions  # noqa: E402
from case_names import KIT_PREFIX, NAME_MAX, RUN_ID, TOKEN_ROOM  # noqa: E402

#: The plan's group for the types that no layer describes. The provider and the
#: tests build them, and no module or blueprint holds them.
NO_MODULE = "other"

#: The catalog this kit ships, and the default for `--requirements`.
BUNDLED_REQUIREMENTS = _coverage_skill() / "references" / "requirements-for-ai-clouds-v2.4.md"

#: Where a refresh puts its backup: the system temporary directory, unless a
#: test names another.
BACKUP_ROOT: Path | None = None

REFERENCES = Path(__file__).resolve().parent.parent / "references"
CONTRACTS = REFERENCES / "contracts"
MODES = REFERENCES / "modes"

NAME_CHARSET = re.compile(r"^[a-z][a-z0-9-]*$")

LIFECYCLE_UPDATE_STEPS = ("apply_update", "plan_no_change")

#: A lifecycle step is named for what Terraform does; an operation is named for
#: what the API calls it. `destroy` is verified through the delete operation,
#: and without this the destroy check is silently dropped from every case.
VERIFICATION_OPERATION = {"create": "create", "update": "update", "destroy": "delete"}


class InputError(RuntimeError):
    """The input cannot produce a case; saying so beats emitting a broken one."""


@dataclass(frozen=True)
class Resource:
    """One resource from `module_plan`, with the layer that owns it."""

    logical_kind: str
    terraform_type: str
    layer: str
    operations: dict[str, str] = field(default_factory=dict)
    #: The req_ids the plan gives as the reason this resource exists, in the
    #: order the plan lists them. The first one files the case.
    requirements: tuple[str, ...] = ()
    #: For a dependency resource, the planned resources whose create needs it.
    dependency_of: tuple[str, ...] = ()
    #: The req_ids the case is filed under: its own, or for a dependency
    #: resource, those of the resource it serves.
    filed_under: tuple[str, ...] = ()
    #: Each update call's verb and the request fields that it changes.
    call_fields: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Each method that adds, changes or removes one entry inside the object,
    #: as the plan lists it. The provider sends it as it sends an update call.
    entry_methods: tuple[str, ...] = ()
    #: Each condition the plan records: its operation, kind, fields, handling
    #: and direction.
    conditions: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = ()

    @property
    def terraform_address(self) -> str:
        return f"{self.terraform_type}.test"

    @property
    def discriminator(self) -> str:
        return re.sub(r"[^a-z0-9-]+", "-", self.logical_kind.lower()).strip("-")


#: Where a document can put a request header, and which header results. A
#: replay matcher compares method, URL and body and never a header, so a header
#: the API demands is asserted here or nowhere.
#:
#: Declared auth is the usual source: neither specification language this reads
#: puts credentials in a parameter. A scheme type outside this table is reported
#: rather than assumed, and the agent derives it for review.
AUTHORIZATION = "Authorization"
SECURITY_HEADER = {
    "http": AUTHORIZATION,  # bearer, basic, anything else the scheme names
    "oauth2": AUTHORIZATION,
    "openIdConnect": AUTHORIZATION,
}


@dataclass(frozen=True)
class Header:
    """One header a request must carry, and what said so."""

    name: str
    source: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "required": self.required}


@dataclass(frozen=True)
class Operation:
    """A callable operation, reduced to what an offline case asserts."""

    ref: str
    http_method: str
    path: str
    required_parameters: tuple[str, ...] = ()
    #: Each parameter and the expression its value matches, where the document states one.
    parameter_patterns: tuple[tuple[str, str], ...] = ()
    request_schema: str = ""
    request_required_fields: tuple[str, ...] = ()
    #: Every top-level field of the body that a request may set: not the ones
    #: the server assigns.
    request_fields: tuple[str, ...] = ()
    #: The longest name the document says this body takes, or 0 when silent.
    name_limit: int = 0
    #: Where the operator's name goes in the body, as a dotted path.
    name_field: str = ""
    required_headers: tuple[Header, ...] = ()
    undetermined: tuple[str, ...] = ()


# --- reading the plan -------------------------------------------------------


#: What a document says about how long a name may be. Both languages write it in
#: prose rather than in a field, and consistently enough to read: `1-63
#: characters`, `6-30 characters long`. Without it one generic cap stands for
#: every API, and a name can exceed a limit that the document states plainly.
NAME_LIMIT = re.compile(r"(\d+)[-\u2013](\d+)\s+characters", re.IGNORECASE)

#: The fields a document uses for the name an operator supplies at create. Kept
#: narrow on purpose: `clusterId` and `id` name the parent of the thing being
#: created at least as often as they name the thing, and a name written into a
#: parent reference points the whole request somewhere else.
NAME_FIELDS = ("name", "accountid")


def settable_body_fields(schema: Any) -> tuple[str, ...]:
    """The top-level fields of a body that a request may set, other than an
    object: an object field is often the resource wrapped whole, and is not
    one field that a case changes."""
    properties = (schema or {}).get("properties") if isinstance(schema, dict) else None
    return tuple(
        sorted(
            name
            for name, described in (properties or {}).items()
            if not server_assigned.is_server_assigned(described) and not _is_object(described)
        )
    )


def _is_object(described: Any) -> bool:
    return isinstance(described, dict) and (bool(described.get("$ref")) or described.get("type") == "object")


def name_field(schema: dict[str, Any], method: str, resolve: Callable[[str], dict[str, Any]]) -> str:
    """Where the operator's name goes in this body, as a dotted path.

    Not always `name`, and `name` is not always it. Creating a service account
    takes the parent in a path parameter called `name`, and the account's own
    name in the body as `accountId`; writing the pattern into `name` set the
    parent and left the required field empty. Creating a cluster takes it one
    level down, inside the resource the request wraps.

    Empty when the document offers nowhere to put it. The renderer then writes
    no name rather than a wrong one, and the case says so.
    """
    properties = schema.get("properties") or {}
    for declared, described in properties.items():
        # A field the server assigns is not somewhere to put a name: the
        # provider declares it Computed, and Terraform refuses a configuration
        # that sets it.
        if str(declared).lower() == "name" and not server_assigned.is_server_assigned(described):
            return str(declared)
    demanded = required_body_fields(schema, method)
    for field_name in demanded:
        if str(field_name).lower() in NAME_FIELDS:
            return str(field_name)
    for field_name in demanded:
        reference = str((properties.get(field_name) or {}).get("$ref") or "")
        if not reference:
            continue
        inner = name_field(resolve(reference.rsplit("/", 1)[-1]), method, resolve)
        if inner:
            return f"{field_name}.{inner}"
    return ""


def stated_name_limit(schema: dict[str, Any]) -> int:
    """The longest name this body accepts, or 0 where the document is silent."""
    found = 0
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict) or str(name).lower() not in NAME_FIELDS:
            continue
        hit = NAME_LIMIT.search(str(prop.get("description") or ""))
        if hit:
            stated = int(hit.group(2))
            found = stated if not found else min(found, stated)
    return found


#: A third way a document demands a field, and the only one some APIs use: the
#: description opens with the word. It is a convention rather than a flag, but a
#: strict one -- the whole clause is `Required.` and it is the first thing said.
#: Without it the API that wraps its resource in a request body marks nothing at
#: all, and the wrapper reads as a resource that demands nothing.
REQUIRED_PROSE = re.compile(r"^\s*required\.", re.IGNORECASE)


def required_body_fields(schema: dict[str, Any], method: str = "") -> tuple[str, ...]:
    """The body fields this method demands, as the document marks them.

    Three forms, because no API uses all three and several use only one. A
    schema's own `required` list; a property annotated with the methods that
    demand it, since one schema serves create and update and they differ; and
    the word at the head of the description, which is all a protobuf-derived
    document ever says.
    """
    names = set(str(name) for name in schema.get("required") or ())
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        demanded = (prop.get("annotations") or {}).get("required") or ()
        if method and method in demanded:
            names.add(str(name))
        elif REQUIRED_PROSE.match(str(prop.get("description") or "")):
            names.add(str(name))
    return tuple(sorted(names))


def load_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InputError(f"{path}: {error}") from error
    if path.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def resources_of(
    coverage: dict[str, Any],
    *,
    layers: tuple[str, ...] = (),
    kinds: tuple[str, ...] = (),
) -> list[Resource]:
    """Every planned resource, narrowed by the scope the operator asked for.

    A flag takes a scope rather than switching something off, because what an
    operator may create depends on the permissions they hold at that moment,
    and that is usually partial rather than none.
    """
    plan = (coverage or {}).get("module_plan") or {}
    planned = plan.get("layers") or {}
    if not planned:
        raise InputError("coverage has no module_plan.layers; run the coverage step first")
    unknown_layers = sorted(set(layers) - set(planned))
    if unknown_layers:
        raise InputError(f"no such layer in module_plan: {', '.join(unknown_layers)}")

    # Resolved over the whole plan before narrowing: a dependency resource is
    # filed with the resource it serves, which may sit in a layer left out.
    filed = requirements_catalog.filed_requirements(
        [entry for body in planned.values() for entry in body.get("resources") or []]
    )
    found = [
        Resource(
            logical_kind=str(entry.get("logical_kind") or ""),
            terraform_type=str(entry.get("terraform_type") or ""),
            layer=name,
            operations=resource_shape.verbs(entry),
            requirements=tuple(
                str(binding.get("req_id")) for binding in entry.get("requirements") or [] if binding.get("req_id")
            ),
            dependency_of=tuple(str(item) for item in entry.get("dependency_of") or []),
            filed_under=tuple(filed.get(str(entry.get("terraform_type") or ""), ())),
            call_fields=tuple((verb, tuple(fields)) for verb, fields in resource_shape.call_fields(entry).items()),
            entry_methods=tuple(str(ref) for ref in entry.get("entry_methods") or []),
            conditions=tuple(
                (
                    str(c.get("operation") or ""),
                    str(c.get("kind") or ""),
                    tuple(str(f) for f in c.get("fields") or []),
                    str(c.get("handling") or ""),
                    str(c.get("direction") or ""),
                )
                for c in entry.get("conditions") or []
                if isinstance(c, dict)
            ),
        )
        for name, body in sorted(planned.items())
        if not layers or name in layers
        for entry in body.get("resources") or []
    ]
    if kinds:
        wanted = set(kinds)
        seen = {r.terraform_type for r in found} | {r.logical_kind for r in found}
        missing = sorted(wanted - seen)
        if missing:
            raise InputError(f"no such resource in module_plan: {', '.join(missing)}")
        found = [r for r in found if r.terraform_type in wanted or r.logical_kind in wanted]
    return found


def load_requirements(path: Path) -> dict[str, dict[str, Any]]:
    """The requirement catalog, in whichever form the operator supplied."""
    try:
        return requirements_catalog.load(path)
    except requirements_catalog.CatalogError as error:
        raise InputError(str(error)) from error


#: One spelling rule for a heading, shared with whatever else files by one.
slug = requirements_catalog.slug


def filing(resource: Resource, catalog: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    """The directories one resource's case is filed under, outermost first.

    Where a resource belongs is the requirement catalog's answer, not this
    script's: the plan says which requirements a resource serves, and the
    catalog says which headings those sit under.

    A resource can serve requirements under several headings -- one NCP's
    router serves three, each its own subsection -- and it is one case either
    way. The first req_id the plan lists decides. That is a filing tiebreak and
    changes nothing inside the case.

    A dependency resource is filed with the resource it serves, so the case that
    needs it and its own case sit side by side.
    """
    filed = resource.filed_under or resource.requirements
    if not filed:
        raise InputError(
            f"{resource.terraform_type}: the plan gives no requirement for it and "
            f"no resource it serves, so nothing says where it is filed. Run the "
            f"coverage check, which refuses a plan whose resources explain nothing"
        )
    req_id = filed[0]
    entry = catalog.get(req_id)
    if entry is None:
        raise InputError(
            f"{resource.terraform_type}: the plan cites {req_id}, which this "
            f"requirement catalog does not define. The plan and the catalog are "
            f"different revisions; run the coverage check, which names every one"
        )
    return tuple(slug(part) for part in (entry.get("section"), entry.get("subsection")) if part)


def narrowed_to_sections(
    resources: list[Resource],
    catalog: dict[str, dict[str, Any]],
    wanted: tuple[str, ...],
) -> list[Resource]:
    """Only the resources filed under a heading the operator named.

    The matching rule lives in the catalog module, because the provider narrows
    the same way over a different shape. Two copies of it are two answers to
    "did this run cover my section".
    """
    try:
        return requirements_catalog.narrowed_to_sections(
            resources, wanted, catalog, lambda r: r.filed_under or r.requirements
        )
    except requirements_catalog.CatalogError as error:
        raise InputError(f"no planned resource is filed under: {error}") from error


def buildable_layers(coverage: dict[str, Any]) -> list[str]:
    """The plan's groups that hold a type to build. `not_buildable` has nothing to apply."""
    planned = ((coverage or {}).get("module_plan") or {}).get("layers") or {}
    return sorted(
        name
        for name, body in planned.items()
        if str(body.get("verdict") or "") != "not_buildable" and (body.get("resources") or [])
    )


def module_layers(coverage: dict[str, Any]) -> list[str]:
    """The layers a module exists for: `other` holds types that no module holds."""
    return [name for name in buildable_layers(coverage) if name != NO_MODULE]


# --- reading the specification ----------------------------------------------


def security_headers(document: dict[str, Any], operation: dict[str, Any]) -> tuple[tuple[Header, ...], tuple[str, ...]]:
    """The headers the document's own auth declaration puts on this request.

    An operation may narrow the document's security, so its own list wins where
    it has one. Returns what was derived and what could not be.
    """
    if document.get("auth", {}).get("oauth2"):
        return (Header(AUTHORIZATION, "auth.oauth2"),), ()

    schemes = (document.get("components") or {}).get("securitySchemes") or {}
    declared = operation.get("security")
    if declared is None:
        declared = document.get("security") or []
    found: list[Header] = []
    undetermined: list[str] = []
    for entry in declared:
        for name in entry or {}:
            scheme = schemes.get(name) or {}
            kind = str(scheme.get("type") or "")
            if kind == "apiKey":
                if scheme.get("in") == "header":
                    found.append(Header(str(scheme.get("name") or ""), f"security:{name}"))
                continue
            if kind in SECURITY_HEADER:
                found.append(Header(SECURITY_HEADER[kind], f"security:{name}"))
                continue
            undetermined.append(
                f"security scheme {name!r} is {kind or 'untyped'}, which this "
                "reader does not map to a header; derive it and have it reviewed"
            )
    # Several schemes are alternatives, not a set to send together, and they
    # commonly land on the same header. Merge them and keep every source, so a
    # reviewer sees which alternatives the document offered.
    merged: dict[str, list[str]] = {}
    for header in found:
        if header.name:
            merged.setdefault(header.name, []).append(header.source)
    return (
        tuple(Header(name, "|".join(sources)) for name, sources in merged.items()),
        tuple(undetermined),
    )


def path_key(method: str, path: str) -> str:
    """The `METHOD /path` form, spelled one way.

    A path template has several legal spellings and the leading slash is the
    common one, so both the index and the lookup pass through here.
    """
    return f"{method.upper()} /{path.lstrip('/')}"


def resolve_operation(reference: str, operations: dict[str, Operation]) -> Operation | None:
    """Find an operation by whichever reference form the plan used.

    A plan names an operation by identifier where the specification defines one
    and by method and path where it does not. Indexing only the identifier makes
    the whole tier unbuildable for a specification of the second kind.
    """
    if reference.startswith("ext:"):
        raise InputError(
            f"{reference} names an endpoint outside the NCP's own API. A generated "
            "resource cannot call one, so it cannot be a resource's operation."
        )
    if reference in operations:
        return operations[reference]
    verb, _, path = reference.partition(" ")
    return operations.get(path_key(verb, path)) if path else None


def index_operations(documents: list[Any]) -> dict[str, Operation]:
    """Every operation the supplied documents define, under every reference form.

    Reduced on purpose. An offline case asserts the request a provider issues,
    so it needs the method, the path and what the document demands — not the
    expanded attribute tree.
    """
    found: dict[str, Operation] = {}
    for document in documents:
        if not isinstance(document, dict):
            continue
        if document.get("resources") and document.get("name"):
            found |= _discovery_operations(document)
        elif document.get("paths"):
            found |= _openapi_operations(document)
        else:
            raise InputError(
                "a supplied document is neither OpenAPI (a `paths` map) nor Discovery (a `name` and a `resources` map)"
            )
    return found | {
        path_key(operation.http_method, operation.path): operation
        for operation in found.values()
        if operation.http_method and operation.path
    }


def _discovery_operations(document: dict[str, Any]) -> dict[str, Operation]:
    api = str(document.get("name") or "")
    # A method path is relative to the service path, and GCP Compute's is not
    # empty (`compute/v1/`). Without it the request check expects a URL no
    # provider sends, and fails every create of that API.
    service_path = str(document.get("servicePath") or "")
    found: dict[str, Operation] = {}

    def walk(resources: Any, path: str) -> None:
        for name, body in (resources or {}).items():
            here = f"{path}.{name}" if path else name
            for method_name, meta in (body.get("methods") or {}).items():
                ref = f"op:{api}.{here}.{method_name}"
                schema = str((meta.get("request") or {}).get("$ref") or "")
                defined = document.get("schemas", {}).get(schema) or {}
                headers, undetermined = security_headers(document, meta)
                found[ref] = Operation(
                    ref=ref,
                    http_method=str(meta.get("httpMethod") or ""),
                    path=service_path + str(meta.get("flatPath") or meta.get("path") or ""),
                    required_parameters=tuple(
                        sorted(
                            name
                            for name, spec in (meta.get("parameters") or {}).items()
                            if isinstance(spec, dict) and spec.get("required")
                        )
                    ),
                    parameter_patterns=tuple(
                        sorted(
                            (name, str(spec["pattern"]))
                            for name, spec in (meta.get("parameters") or {}).items()
                            if isinstance(spec, dict) and spec.get("pattern")
                        )
                    ),
                    request_schema=schema,
                    request_required_fields=required_body_fields(defined, str(meta.get("id") or "")),
                    request_fields=settable_body_fields(defined),
                    name_limit=stated_name_limit(defined),
                    name_field=name_field(
                        defined,
                        str(meta.get("id") or ""),
                        lambda ref: document.get("schemas", {}).get(ref) or {},
                    ),
                    required_headers=headers,
                    undetermined=undetermined,
                )
            walk(body.get("resources"), here)

    walk(document.get("resources"), "")
    return found


def _openapi_operations(document: dict[str, Any]) -> dict[str, Operation]:
    found: dict[str, Operation] = {}
    for path, item in (document.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for verb, meta in item.items():
            if verb.lower() not in {"get", "put", "post", "delete", "patch"}:
                continue
            identifier = str(meta.get("operationId") or f"{verb}{path}")
            ref = f"op:{identifier}"
            body = (meta.get("requestBody") or {}).get("content") or {}
            declared = next(
                (
                    (media or {}).get("schema") or {}
                    for media in body.values()
                    if isinstance(media, dict) and (media or {}).get("schema")
                ),
                {},
            )
            schema = str(declared.get("$ref") or "")
            # A body is as often written into the operation as referenced from
            # it, and a reader that follows only references asserts nothing
            # about the body for a document of the first kind. An inline body is
            # named after its operation, so every case names one schema either way.
            if schema:
                name = schema.rsplit("/", 1)[-1]
                defined = ((document.get("components") or {}).get("schemas") or {}).get(name) or {}
            else:
                name = f"{identifier}Request" if declared else ""
                defined = declared
            declared_parameters = [
                parameter
                for parameter in list(meta.get("parameters") or []) + list(item.get("parameters") or [])
                if isinstance(parameter, dict)
            ]
            headers, undetermined = security_headers(document, meta)
            headers += tuple(
                Header(str(parameter.get("name")), "parameter", required=bool(parameter.get("required")))
                for parameter in declared_parameters
                if parameter.get("in") == "header"
            )
            found[ref] = Operation(
                ref=ref,
                http_method=verb.upper(),
                path=str(path),
                required_parameters=tuple(
                    sorted(
                        str(parameter.get("name"))
                        for parameter in declared_parameters
                        if parameter.get("required") and parameter.get("in") != "header"
                    )
                ),
                request_schema=name,
                request_required_fields=required_body_fields(defined),
                request_fields=settable_body_fields(defined),
                name_limit=stated_name_limit(defined),
                name_field=name_field(
                    defined,
                    "",
                    lambda ref: ((document.get("components") or {}).get("schemas") or {}).get(ref) or {},
                ),
                required_headers=headers,
                undetermined=undetermined,
            )
    return found


# --- naming -----------------------------------------------------------------


def name_pattern(discriminator: str, limit: int = 0) -> tuple[str, str]:
    """The name every resource this run creates carries, and why it is short.

    The id is substituted by the runner, so the pattern has to leave room
    for one. A name that overflows is truncated by the API, and a truncated
    name is one the sweeper cannot match back to its run.

    The subject's kind is in the name for a reader's benefit and nothing reads
    it back, so where the API's stated limit will not take it the kind is
    dropped rather than the run refused. What must survive is the prefix and the
    id: the sweeper finds a leaked resource by those two and by nothing else.
    """
    if not NAME_CHARSET.match(discriminator):
        raise InputError(f"discriminator {discriminator!r} is not a lowercase name starting with a letter")
    cap = min(limit, NAME_MAX) if limit else NAME_MAX
    pattern = f"{KIT_PREFIX}-{RUN_ID}-{discriminator}"
    if len(pattern) - len(RUN_ID) + TOKEN_ROOM <= cap:
        return pattern, ""
    bare = f"{KIT_PREFIX}-{RUN_ID}"
    if len(bare) - len(RUN_ID) + TOKEN_ROOM > cap:
        raise InputError(
            f"{bare} reaches {len(bare) - len(RUN_ID) + TOKEN_ROOM} characters with a "
            f"id and this API allows {cap}; the run id cannot be carried in a name "
            f"here, and without it a leaked resource cannot be found"
        )
    return bare, (
        f"{discriminator} left out of the name: with an id it reaches "
        f"{len(pattern) - len(RUN_ID) + TOKEN_ROOM} characters and this API allows {cap}"
    )


def labels_for(resource: Resource) -> dict[str, str]:
    """The same parts, put where a truncated name cannot reach.

    A label survives what a name cannot, so the sweeper reads these first and
    falls back to the name only where the API carries no labels.
    """
    return {
        f"{KIT_PREFIX}-run": RUN_ID,
        f"{KIT_PREFIX}-kind": resource.discriminator,
        f"{KIT_PREFIX}-layer": resource.layer,
    }


# --- instantiation ----------------------------------------------------------


def _shipped(directory: Path, name: str, kind: str) -> dict[str, Any]:
    path = directory / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in directory.glob("*.yaml")))
        raise InputError(f"no {kind} {name!r}; this skill ships: {known}")
    return load_document(path)


# An operator asks for a tier; the flag takes the contract that tier's tests
# assert. Two of the three are different words, so a tier reaching this flag is
# a translation nobody made rather than a typo, and the message has to say so.
TIER_CONTRACT = {"provider": "resource", "blueprint": "composition"}


def load_contract(name: str) -> dict[str, Any]:
    """What is under test: one resource, one module, or the layers together."""
    if name in TIER_CONTRACT:
        raise InputError(
            f"{name!r} is a tier, not a contract; the {name} tier's tests are --contract {TIER_CONTRACT[name]}"
        )
    return _shipped(CONTRACTS, name, "contract")


def load_mode(name: str) -> dict[str, Any]:
    """What the requests reach: nothing, a recording, or the NCP.

    Orthogonal to the contract. The same subject is driven three ways, and the
    mode decides how far the lifecycle gets and what counts as evidence.
    """
    return _shipped(MODES, name, "mode")


def _judged_fields(contract: dict[str, Any]) -> list[str]:
    return [str(entry["field"]) for entry in contract.get("judged") or []]


def specification_notes(operation: Operation | None) -> dict[str, Any]:
    """Where to read the body, and nothing that stands in for reading it.

    A copied part of a body reads as the whole body, so the case names the
    schema to read. The enumerated fields are only some of the fields that a
    create body defines, and nothing says which. A demanded body can reduce to
    a single `name`, which the renderer drops: the run id supplies it, so a
    reviewer would fill a value that nothing reads. A schema's name is neither
    partial nor wrong.
    """
    if operation is None:
        return {}
    notes: dict[str, Any] = {}
    if operation.request_schema:
        notes["schema"] = operation.request_schema
    return notes


def review_notes(
    contract: dict[str, Any],
    judged: list[str],
    derived: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """What each owed decision is, and what getting it wrong costs.

    The contract already states the question behind every judged field and why a
    plausible wrong answer is worse than an empty one. Emitting only the field
    names threw that away, and left a reviewer 67 paths with nothing to say what
    any of them meant. It travels with the case because that is what a reviewer
    opens.

    `derived` adds what the specification answers for that field, so the two
    halves of one decision sit together rather than one file apart. It is
    applied last: where the document can build the example, the contract's
    illustration of one is not what the reviewer should copy.

    An `advisory` entry reaches the reviewer the same way and is not owed: it
    already carries a generated value, and the question is whether that value
    holds here. A field right almost everywhere and wrong somewhere is worth a
    look and is not worth blocking every case on.
    """
    notes = []
    advisory = [str(e["field"]) for e in contract.get("advisory") or []]
    for entry in [*(contract.get("judged") or []), *(contract.get("advisory") or [])]:
        field = str(entry["field"])
        if field not in judged and field not in advisory:
            continue
        note = {
            "field": field,
            **{key: entry[key] for key in ("question", "constrained_by", "why", "shape", "example") if key in entry},
        }
        note.update((derived or {}).get(field) or {})
        notes.append(note)
    return notes


def _blank(path: str) -> Any:
    """What an unfilled judged field looks like. Empty, and present.

    Present because a reviewer has to see that a decision is owed. Empty
    because a placeholder value is a decision nobody made, and it would run.
    """
    return (
        {}
        if path.startswith("fixture.") or path in {"variables", "outputs", "wiring", case_identity.PROVED_BY}
        else None
    )


def _blank_judged(case: dict[str, Any], paths: list[str]) -> None:
    """Empty every judged field, wherever in the case it sits.

    The generated value for one of these is a plausible default, and a default
    a reviewer never sees is the same as a guess. A default of
    `absent` in `verification.destroy.expect` is wrong for a resource that
    soft-deletes, such as four GCP IAM types.
    """
    for path in paths:
        *parents, leaf = path.split(".")
        here: Any = case
        for segment in parents:
            here = here.get(segment) if isinstance(here, dict) else None
            if here is None:
                break
        if isinstance(here, dict):
            here[leaf] = _blank(path)


#: Which lifecycle steps a verb carries. A step whose verb the API does not
#: offer cannot run, and a case that keeps it fails for a reason a reader will
#: misread.
VERB_STEPS = {
    "update": list(LIFECYCLE_UPDATE_STEPS),
    "delete": ["destroy"],
}


def _update_calls(resource: Resource) -> list[str]:
    """The verbs of the resource's update calls: each one changes the object,
    so a resource with one has an update to test."""
    return [verb for verb in resource.operations if verb not in resource_shape.VERBS]


def narrow_lifecycle(contract: dict[str, Any], resource: Resource) -> tuple[list[str], list[dict[str, str]]]:
    """The lifecycle this subject can run, and every step it dropped.

    One thing narrows it: an operation the API does not offer. How far a run
    goes is not one of them. That is chosen when the test runs, so the case
    states the whole lifecycle and each mode reads the part it reaches.
    """
    lifecycle = list(contract.get("lifecycle") or [])
    dropped: list[dict[str, str]] = []

    for verb, steps in VERB_STEPS.items():
        if verb in resource.operations or (verb == "update" and _update_calls(resource)):
            continue
        for step in steps:
            if step in lifecycle:
                lifecycle.remove(step)
                dropped.append({"dropped": step, "reason": "no_operation"})
    return lifecycle, dropped


def instantiate_resource(
    contract: dict[str, Any],
    resource: Resource,
    *,
    operations: dict[str, Operation] | None = None,
    updated_in_place: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """One case for one resource, stating its whole lifecycle.

    The same document whichever mode runs it. It carries every step the API can
    support, the check that proves each, the cleanup they oblige, and the
    request each operation must issue. A run reads the part it reaches.

    A narrower case per mode would freeze a transient fact -- how far somebody
    means to go that day, and what happens to be recorded -- into a durable
    file, so widening the run later would mean regenerating rather than
    rerunning.
    """
    expected = expected_requests(resource, operations) if operations else []
    lifecycle, dropped = narrow_lifecycle(contract, resource)
    requirements = list(resource.requirements or resource.filed_under)
    judged = [field for field in _judged_fields(contract) if _applies(field, lifecycle, requirements)]
    case: dict[str, Any] = {
        "api_version": contract["api_version"],
        "kind": "case",
        "contract": contract["contract"],
        "resource": {
            "logical_kind": resource.logical_kind,
            "module": resource.layer,
            "terraform_address": resource.terraform_address,
            # Why this case exists, copied from the plan that already said so.
            # It is also what lets a progress scan read this directory alone
            # and still know which requirements have nothing covering them.
            # A dependency resource exists for the requirements of the resource
            # it serves, and `dependency_of` says so.
            "requirements": requirements,
            **(
                {"dependency_of": list(resource.dependency_of)}
                if resource.dependency_of and not resource.requirements
                else {}
            ),
            "operations": dict(sorted(resource.operations.items())),
        },
        "naming": _naming(resource, operations),
        "fixture": {key: {} for key in ("create", "update") if f"fixture.{key}" in judged},
        **({case_identity.PROVED_BY: {}} if case_identity.PROVED_BY in judged else {}),
        "lifecycle": lifecycle,
        "assertions": dict(contract.get("assertions") or {}),
        "judged": judged,
        "review": review_notes(contract, judged, _document_notes(resource, operations)),
    }
    if "readiness" in judged:
        case["readiness"] = None
    case["verification"], unverifiable = verification_for(contract, resource, lifecycle)
    dropped += unverifiable
    _blank_judged(case, judged)
    case["cleanup"] = dict(contract.get("cleanup") or {})
    if "delete" not in resource.operations:
        case["cleanup"] = {**case["cleanup"], "required": True, "manual": True}

    if expected:
        case["expected_requests"] = expected
    if dropped:
        case["narrowed"] = dropped
    owed_update = (
        update_fields.derive(resource, operations, updated_in_place)
        if operations and "apply_update" in lifecycle
        else {}
    )
    if owed_update.get("change") or owed_update.get("left_out"):
        case["update_fields"] = owed_update
    return case


def _naming(resource: Resource, operations: dict[str, Operation] | None) -> dict[str, Any]:
    """What this run's resources are called, and what the name had to give up."""
    create = (operations or {}).get(resource.operations.get("create", ""))
    pattern, shortened = name_pattern(resource.discriminator, create.name_limit if create else 0)
    naming: dict[str, Any] = {"name_pattern": pattern, "labels": labels_for(resource)}
    # Always present once a document has been read, empty included: absent means
    # nobody looked, and an empty string means the document was read and offers
    # nowhere to put a name. The renderer treats the two differently.
    if create is not None:
        naming["name_field"] = create.name_field
    if create is not None and create.name_limit:
        naming["name_limit"] = create.name_limit
    if shortened:
        naming["shortened"] = shortened
    return naming


def _document_notes(resource: Resource, operations: dict[str, Operation] | None) -> dict[str, dict[str, Any]]:
    """The document's own answers, per fixture, keyed by the field that owes one.

    Update is carried only where it is a different body from create. One schema
    usually serves both, and a second identical copy in the same file reads as a
    second decision rather than the same one.
    """
    if not operations:
        return {}
    found: dict[str, dict[str, Any]] = {}
    create = operations.get(resource.operations.get("create", ""))
    notes = _entry(create)
    if notes:
        found["fixture.create"] = notes
    update = operations.get(resource.operations.get("update", ""))
    if update is not None and (create is None or update.request_schema != create.request_schema):
        later = _entry(update)
        if later:
            found["fixture.update"] = later
    return found


def _entry(operation: Operation | None) -> dict[str, Any]:
    """What the document settles, and the body to copy where it settles enough.

    A document that demands nothing produces no example, and the contract's
    illustration stands. Replacing it with an empty map would offer a reviewer
    an answer that is already complete.
    """
    notes = specification_notes(operation)
    if not notes:
        return {}
    return dict(notes)


def verification_for(
    contract: dict[str, Any], resource: Resource, lifecycle: list[str]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """How each step is proved, by a call to the NCP rather than to Terraform.

    Every check reads: creation is proved by the resource being there, an update
    by the read coming back changed, and a destroy by it being gone. So a
    resource the API offers no read for cannot be verified at all, and the case
    says so rather than carrying a check nothing can run.
    """
    read = resource.operations.get("read")
    declared = contract.get("verification") or {}
    if not read:
        return {}, [{"dropped": f"verification.{step}", "reason": "no_read_operation"} for step in declared]

    verification: dict[str, Any] = {}
    dropped: list[dict[str, str]] = []
    for step, rule in declared.items():
        verb = VERIFICATION_OPERATION.get(step, step)
        if verb not in resource.operations or LIFECYCLE_STEP.get(step, step) not in lifecycle:
            continue
        verification[step] = {**rule, "operation": read} if isinstance(rule, dict) else rule
    return verification, dropped


#: Which lifecycle step each verification follows. A check for a step the case
#: does not run would be reported as a skipped check rather than an absent one.
LIFECYCLE_STEP = {"create": "apply_create", "update": "apply_update", "destroy": "destroy"}


def _applies(judged_field: str, lifecycle: list[str], requirements: list[str]) -> bool:
    """Whether a decision is still owed, given what the case will actually run
    and the requirements it cites.

    Read from the lifecycle the case runs, so that `instantiate` and `check`
    derive the same owed decisions from the case and its contract. Asking for a
    value no step will use wastes the one review that matters.
    """
    if judged_field == "fixture.update":
        return "apply_update" in lifecycle
    if judged_field == "readiness":
        return any(step.startswith("apply") for step in lifecycle)
    if judged_field == "verification.destroy.expect":
        return "destroy" in lifecycle
    if judged_field == case_identity.PROVED_BY:
        return bool(requirements)
    return True


#: What a recording of one operation is called. The recording step writes this
#: name and a replay run reads it; neither invents one. It is stated here rather
#: than in a mode document because every case carries the name, whether or not
#: the recording exists yet.
CASSETTE_NAME = "{terraform_type}-{verb}"


def cassette_name(resource: Resource, verb: str) -> str:
    """What a recording of this operation is called, once one is made."""
    return CASSETTE_NAME.format(terraform_type=resource.terraform_type, verb=verb)


def expected_requests(resource: Resource, operations: dict[str, Operation]) -> list[dict[str, Any]]:
    """What each of the resource's operations and entry methods must look
    like on the wire.

    Derived from the specification, so it holds whether or not anything has been
    recorded. A replay matcher compares the method, the URL and the body; it
    never compares a header, and a request with no body is matched on method and
    URL alone — so the header list here is the only place a header the document
    demands is checked.

    Each entry names the recording that would replay it. The name is stated
    before that recording exists, which is what lets the cases be written first
    and recorded afterwards.
    """
    expected: list[dict[str, Any]] = []
    entries = {resource_shape.call_verb(ref): ref for ref in resource.entry_methods}
    for verb, ref in sorted({**entries, **resource.operations}.items()):
        operation = resolve_operation(ref, operations)
        if operation is None:
            raise InputError(
                f"{resource.terraform_type}: {ref} is not defined in the supplied "
                "specifications; the plan and the documents disagree"
            )
        recorded = cassette_name(resource, verb)
        entry: dict[str, Any] = {
            "verb": verb,
            "ref": operation.ref,
            "cassette": recorded,
            "method": operation.http_method,
            "path": operation.path,
            "required_parameters": list(operation.required_parameters),
            "schema": operation.request_schema,
            "required_fields": list(operation.request_required_fields),
            "headers": [h.as_dict() for h in operation.required_headers],
        }
        if operation.undetermined:
            entry["undetermined"] = list(operation.undetermined)
        expected.append(entry)
    return expected


def _cassette_exists(directory: Path, name: str) -> bool:
    """A recording step chooses the extension; this tier chooses the name."""
    return bool(list(directory.glob(f"{name}.*")) or (directory / name).is_file())


def instantiate_module(contract: dict[str, Any], layer: str, resources: list[Resource]) -> dict[str, Any]:
    return {
        "api_version": contract["api_version"],
        "kind": "case",
        "contract": contract["contract"],
        "module": layer,
        "resources": [r.terraform_address for r in resources],
        "naming": {"name_pattern": name_pattern(layer)[0], "labels": {f"{KIT_PREFIX}-run": RUN_ID}},
        "variables": {},
        "outputs": {},
        "lifecycle": list(contract.get("lifecycle") or []),
        "assertions": dict(contract.get("assertions") or {}),
        "judged": _judged_fields(contract),
        "review": review_notes(contract, _judged_fields(contract)),
        "verification": dict(contract.get("verification") or {}),
        "cleanup": dict(contract.get("cleanup") or {}),
    }


def instantiate_composition(contract: dict[str, Any], layers: list[str]) -> dict[str, Any]:
    if len(layers) < 2:
        raise InputError(f"composition needs two buildable layers to have a seam; module_plan has {len(layers)}")
    return {
        "api_version": contract["api_version"],
        "kind": "case",
        "contract": contract["contract"],
        "layers": layers,
        "layer_order": [],
        "wiring": [],
        "variables": {},
        "naming": {"name_pattern": name_pattern("blueprint")[0], "labels": {f"{KIT_PREFIX}-run": RUN_ID}},
        "lifecycle": list(contract.get("lifecycle") or []),
        "assertions": dict(contract.get("assertions") or {}),
        "judged": _judged_fields(contract),
        "review": review_notes(contract, _judged_fields(contract)),
        "verification": dict(contract.get("verification") or {}),
        "cleanup": dict(contract.get("cleanup") or {}),
    }


# --- rendering --------------------------------------------------------------


@dataclass(frozen=True)
class HCLExpression:
    text: str


def resolved_dependencies(case: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Dependency cases in creation order, with each optional value named once."""
    ordered: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()
    owner = str((case.get("resource") or {}).get("logical_kind") or "").replace("_", "-")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", owner):
        raise InputError(f"invalid dependency owner {owner!r}")

    def visit(entry: dict[str, Any]) -> None:
        variable = str(entry.get("variable") or "")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", variable):
            raise InputError(f"invalid dependency variable {variable!r}")
        if variable in visiting:
            raise InputError(f"dependency cycle at {variable}")
        if variable in seen:
            if seen[variable]["_declared"] != entry:
                _another_form(case, seen[variable], entry, ordered)
            return
        resource = str(entry.get("resource") or "")
        if "values" in entry and not (resource and isinstance(entry["values"], dict) and entry["values"]):
            raise InputError(f"dependency {variable} values must be a non-empty mapping over a resource's case")
        if resource:
            parent = catalog.get(resource)
            if parent is None:
                raise InputError(f"dependency {variable} names missing case {resource}")
            if not entry.get("reference") and not entry.get("attribute"):
                raise InputError(f"dependency {variable} has no reference or attribute")
            if entry.get("reference") and entry.get("attribute"):
                raise InputError(
                    f"dependency {variable} has a reference and an attribute; the "
                    "attribute is what the created resource gives, so the reference is unused"
                )
            if entry.get("attribute") and not re.fullmatch(
                r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*", str(entry["attribute"])
            ):
                raise InputError(f"dependency {variable} has an invalid attribute")
            visiting.add(variable)
            for child in parent.get("dependencies") or []:
                visit(child)
            visiting.remove(variable)
            resolved = {**entry, "case": parent}
            # `{case}` stands for what the created name carries for this case.
            if entry.get("reference"):
                resolved["reference"] = str(entry["reference"]).replace("{case}", _created(resolved, case).tag)
        elif entry.get("required") is True:
            resolved = {
                **entry,
                **({"reference": str(entry["reference"]).replace("{case}", owner)} if entry.get("reference") else {}),
            }
        else:
            raise InputError(f"dependency {variable} needs a resource or required: true")
        seen[variable] = {**resolved, "_declared": entry}
        ordered.append(resolved)

    for entry in case.get("dependencies") or []:
        visit(entry)
    ordered = _drop_replaced(case, ordered)
    _refuse_unused(case, ordered)
    _refuse_unnamed_or_shared(case, ordered)
    _refuse_embedded(case, ordered)
    return ordered


def _another_form(
    case: dict[str, Any], first: dict[str, Any], entry: dict[str, Any], ordered: list[dict[str, Any]]
) -> None:
    """Accept a second declaration of one dependency that differs only in the
    form of its `reference`, when that form names the same created resource.

    The case's own declaration gives the form of the run value, so it replaces
    a form that another dependency declared first.
    """
    variable = str(entry["variable"])
    if not dependency_forms.another_form(first["_declared"], entry):
        raise InputError(f"conflicting definitions for dependency {variable}")
    made = _created(first, case)
    reference = str(entry["reference"]).replace("{case}", made.tag)
    if made.name not in reference:
        raise InputError(f"dependency {variable} creates {made.name}, which its reference {reference} does not name")
    if any(entry is own for own in case.get("dependencies") or []):
        for resolved in [first, *[e for e in ordered if e["variable"] == variable]]:
            resolved["reference"] = reference
        first["_declared"] = entry


def _other_form(entry: dict[str, Any], consumer: dict[str, Any], case: dict[str, Any]) -> str:
    """The form `consumer` takes `entry`'s dependency in, where it is not the run value's form."""
    if not entry.get("reference"):
        return ""
    own = dependency_forms.own_reference(consumer, str(entry["variable"]))
    reference = own.replace("{case}", _created(entry, case).tag)
    return "" if reference in ("", entry["reference"]) else reference


def _refuse_embedded(case: dict[str, Any], ordered: list[dict[str, Any]]) -> None:
    """A placeholder for a dependency the test may create is a whole value.

    Created, it becomes an expression -- the reference, or the created
    resource's attribute -- and only a whole value is replaced by one. Inside a
    longer string it would be filled from the variable, which is empty when the
    test creates the dependency.
    """
    fixtures = [named_fixture(case), *_peer_fixtures(case)] + [setup_fixture(e) for e in ordered if e.get("case")]
    for entry in ordered:
        if not entry.get("case"):
            continue
        token = "{" + entry["variable"] + "}"
        for fixture in fixtures:
            where = _embedded(fixture, token)
            if where:
                raise InputError(
                    f"dependency {entry['variable']} is part of {where}; a "
                    "dependency the test may create has to be a field's whole value"
                )


def _embedded(node: Any, token: str) -> str | None:
    """Where `token` is part of something larger than a whole value, or None.

    A map key is never a whole value: the created path writes an expression
    where a value is, and a key would be filled from the empty variable.
    """
    if isinstance(node, str):
        return f"the value {node!r}" if token in node and node != token else None
    keyed = [key for key in node if token in str(key)] if isinstance(node, dict) else []
    if keyed:
        return f"the map key {str(keyed[0])!r}"
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    for child in children:
        found = _embedded(child, token)
        if found:
            return found
    return None


def still_needs(entry: dict[str, Any], variable: str) -> bool:
    """Whether a created dependency still needs one its own case declares.

    `values` can set the field its case fills from that dependency -- an
    address made external needs no subnetwork -- and then it does not.
    """
    if not entry.get("case") or variable not in {
        str(child.get("variable") or "") for child in entry["case"].get("dependencies") or []
    }:
        return False
    return not (
        placeholder_context(named_fixture(entry["case"]), variable)
        and not placeholder_context(setup_fixture(entry), variable)
    )


def _drop_replaced(case: dict[str, Any], ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leave out a dependency's own dependency that nothing still needs.

    What the case itself declares is kept, so an unused one of those is still
    refused as a probable misspelling.
    """
    direct = {entry.get("variable") for entry in case.get("dependencies") or []}
    kept = list(ordered)
    while True:
        dropped = [
            entry
            for entry in kept
            if entry["variable"] not in direct
            and not any(still_needs(other, entry["variable"]) for other in kept if other is not entry)
        ]
        if not dropped:
            return kept
        kept = [entry for entry in kept if entry not in dropped]


def _refuse_unused(case: dict[str, Any], ordered: list[dict[str, Any]]) -> None:
    users = [named_fixture(case), *_peer_fixtures(case)]
    for entry in ordered:
        variable = entry["variable"]
        if any(placeholder_context(user, variable) for user in users) or any(
            placeholder_context(setup_fixture(parent), variable) for parent in ordered if parent.get("case")
        ):
            continue
        # Nothing names it, yet the API refuses the create until it exists.
        # The reason is what tells that apart from a misspelt placeholder.
        if entry.get("case") and str(entry.get("reason") or "").strip():
            continue
        raise InputError(
            f"dependency {variable} is unused by its case; one that must exist although nothing names it needs a reason"
        )


def _refuse_unnamed_or_shared(case: dict[str, Any], ordered: list[dict[str, Any]]) -> None:
    """Each created dependency resource has a name of its own, and the
    reference the case uses in its place names it.

    A reference that names something else sends the resource under test to a
    resource nobody created, and two entries with one name ask the API for the
    same resource twice.
    """
    created: dict[tuple[str, str], str] = {}
    for entry in ordered:
        if not entry.get("case"):
            continue
        name = created_name(entry, case)
        key = (entry["resource"], name)
        if key in created:
            raise InputError(
                f"dependencies {created[key]} and {entry['variable']} both create "
                f"{entry['resource']} {name}; give each its own values"
            )
        created[key] = entry["variable"]
        if entry.get("reference") and name not in str(entry["reference"]):
            raise InputError(
                f"dependency {entry['variable']} creates {name}, which its reference {entry['reference']} does not name"
            )


def setup_fixture(entry: dict[str, Any]) -> dict[str, Any]:
    """What a dependency entry creates: its case's create fixture, with the
    entry's `values` laid over it.

    A resource can need several of one type that differ in a field or two --
    one forwarding rule per protocol -- and each is an entry naming one case.
    """
    return fixture_values.merged_fixture(named_fixture(entry["case"]), dict(entry.get("values") or {}))


def dependency_entries(case: dict[str, Any]) -> list[dict[str, Any]]:
    return list(case.get("_resolved_dependencies") or [])


def optional_dependencies(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in dependency_entries(case) if entry.get("resource")]


def case_values(case: dict[str, Any]) -> list[str]:
    """All placeholders used by the subject's create and update, its peer or its
    dependency fixtures: the update step applies a value the create may not hold."""
    names = set(supplied_values([named_fixture(case), fixture_values.update_fixture(case), *_peer_fixtures(case)]))
    for entry in dependency_entries(case):
        names.update(supplied_values(entry.get("reference") or ""))
        if entry.get("case"):
            # Declared even when nothing names it: its setup count reads it.
            names.add(entry["variable"])
            names.update(supplied_values(setup_fixture(entry)))
            for used in optional_dependencies(case):
                names.update(supplied_values(_other_form(used, entry["case"], case)))
    return sorted(names)


def required_case_values(case: dict[str, Any]) -> list[str]:
    optional = {entry["variable"] for entry in optional_dependencies(case)}
    return [name for name in case_values(case) if name not in optional]


def hcl(value: Any, indent: int = 0) -> str:
    """A Python value as the HCL literal Terraform reads.

    Written here rather than taken from a library because the values come from a
    reviewed fixture and are plain scalars, lists and maps. A template engine
    would be a second thing to learn for a job this small.
    """
    pad = "  " * indent
    if isinstance(value, HCLExpression):
        return value.text
    if isinstance(value, bool):  # before int: a bool is an int in Python
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = ",\n".join(f"{pad}  {hcl(item, indent + 1)}" for item in value)
        return f"[\n{items},\n{pad}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = align({hcl_key(k): hcl(v, indent + 1) for k, v in value.items()}, indent + 1)
        return f"{{\n{items}\n{pad}}}"
    return rendered_literals.hcl_value(str(value))


def hcl_string(value: str) -> str:
    """A string as an HCL literal that Terraform will not interpolate.

    `${` and `%{` open a template in an HCL string. A fixture is written by an
    operator against their own API, so a literal holding either is theirs to
    write and not ours to reject -- doubling the brace is how HCL spells the
    brace itself.
    """
    return json.dumps(rendered_literals.template_escaped(value))


#: A map key Terraform reads as a name rather than as an expression.
HCL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def hcl_key(key: Any) -> str:
    """A map key, quoted when it is not an identifier.

    A GCP label key is a URL -- `cloudidentity.googleapis.com/groups.discussion_forum`
    -- and unquoted, Terraform reads the slash as division and reformats the line
    into an expression. The generated file then fails `terraform fmt -check` the
    first time anyone runs it, which is the check that exists to catch exactly
    this.
    """
    text = str(key)
    return text if HCL_IDENTIFIER.match(text) else rendered_literals.hcl_value(text)


def align(body: dict[str, str], indent: int = 1, *, style: str = "hcl") -> str:
    """Field lines, aligned the way the language's own formatter aligns them.

    Both formatters follow one rule: a run of single-line fields is aligned
    together, and a multi-line value ends the run. gofmt also ends the run
    before a key whose size is far from the sizes of the keys before it.
    Emitting it canonically here means generated files pass `terraform fmt
    -check` and `gofmt -l` without the kit shelling out to binaries it cannot
    assume are installed.
    """
    pad = ("\t" if style == "go" else "  ") * indent
    lines: list[str] = []
    group: list[tuple[str, str]] = []

    def render(label: str, value: str, width: int = 0) -> str:
        if style == "go":
            return f"{pad}{label.ljust(width)} {value}"
        return f"{pad}{label.ljust(width)} = {value}"

    def flush() -> None:
        if not group:
            return
        width = max(len(label) for label, _ in group)
        lines.extend(render(label, value, width) for label, value in group)
        group.clear()

    for key, value in body.items():
        label = f"{key}:" if style == "go" else key
        if "\n" in value:
            flush()
            lines.append(render(label, value))
            continue
        if style == "go" and go_alignment.starts_section([len(label) - 1 for label, _ in group], len(key)):
            flush()
        group.append((label, value))
    flush()
    return "\n".join(lines)


def go_struct(kind: str, body: dict[str, str], indent: int) -> str:
    """A Go composite literal, its fields aligned as gofmt aligns them."""
    pad = "\t" * indent
    return f"{kind}{{\n{align(body, indent + 1, style='go')}\n{pad}}}"


def provider_local_name(terraform_address: str) -> str:
    """The provider a resource type belongs to, by Terraform's own convention.

    A resource type is `<provider>_<kind>`, so the prefix names the provider and
    nothing has to be configured to know it.
    """
    return terraform_address.split("_", 1)[0]


def substituted(value: Any, run_id: str) -> Any:
    """A fixture value with the run id written in, at any depth.

    A reviewer puts the run id where the API actually takes the name --
    `role_id`, `workload_identity_pool_id`, `account_id` -- because that is the
    field the document says names the resource. Substituting only in
    `naming.name_pattern` left `{run_id}` in the rendered configuration as
    four literal characters: the API refuses the name, and the sweeper that
    matches on the id finds nothing.
    """
    if isinstance(value, str):
        return value.replace(RUN_ID, run_id)
    if isinstance(value, dict):
        return {_filled_key(k, RUN_ID, run_id): substituted(v, run_id) for k, v in value.items()}
    if isinstance(value, list):
        return [substituted(v, run_id) for v in value]
    return value


def _filled_key(key: Any, token: str, replacement: str) -> Any:
    """A map key with `token` filled, as a value is filled. An API can key a
    map by an account value: a reservation keys its shares by project id."""
    return key.replace(token, replacement) if isinstance(key, str) else key


#: A `{placeholder}` in a fixture. `run_id` is the kit's own and is supplied
#: by every runner; anything else names something only the account knows.
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def supplied_values(value: Any) -> list[str]:
    """The placeholders a fixture holds that the run has to fill, sorted.

    A fixture may not carry account data -- a project, an organisation, a
    verified domain -- and it may not carry the identifier of a resource that
    has to exist first. Both are written as a placeholder and supplied when the
    test runs, which is what `{run_id}` already does for the run's own name.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.update(_PLACEHOLDER.findall(node))
        elif isinstance(node, dict):
            for key, item in node.items():
                walk(str(key))
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return sorted(found - {"run_id"})


def carries_run_id(value: Any) -> bool:
    """Whether the fixture already names the run somewhere inside it."""
    if isinstance(value, str):
        return RUN_ID in value
    if isinstance(value, dict):
        return any(carries_run_id(v) for v in value.values())
    if isinstance(value, list):
        return any(carries_run_id(v) for v in value)
    return False


def named_fixture(case: dict[str, Any]) -> dict[str, Any]:
    """The create fixture with the run's name written where the API takes it.

    Which is not always an argument called `name`. Creating a service account
    takes the parent in a path parameter of that name and the account's own name
    in the body as `accountId`, so writing it into `name` set the parent and left
    the required field empty. Creating a cluster takes it one level down, inside
    the resource the request wraps.

    A document that offers nowhere to put it gets no name written. The run id
    then reaches the resource through its labels alone, and the case says so.
    """
    values = dict((case.get("fixture") or {}).get("create") or {})
    naming = case.get("naming") or {}
    # The case records the document's field; the fixture holds the provider's
    # attribute names, and Terraform refuses any other argument.
    field = attribute_names.configuration_path(str(naming.get("name_field", "name")))
    # A fixture that already carries the id has named the resource, in the
    # field the document says names it. Writing a second name beside it makes
    # one fact settable in two places, and two names for one fact can disagree.
    if not field or carries_run_id(values):
        return values
    # The pattern keeps its `{run_id}` here. Every renderer substitutes it
    # for what that run calls its id, and substituting it in two places lets the
    # offline and live renders disagree.
    literal = str(naming.get("name_pattern") or "")
    head, _, tail = field.partition(".")
    if tail:
        nested = dict(values.get(head) or {})
        values[head] = {tail: literal, **{k: v for k, v in nested.items() if k != tail}}
    else:
        values[head] = literal
    return {head: values.pop(head), **values}


def placeholder_context(value: Any, name: str) -> tuple[str, str] | None:
    """The field a supplied placeholder sits in, and the literal around it.

    A name on its own does not say what a value is, and an operator who has to
    find that out by reading the Go test has not been told. The field and the
    literal say it -- `parent`, holding `customers/{customer_id}` -- in the
    API's own wording rather than in a reading of it, so this stays true for an
    NCP nobody here has seen.
    """
    token = "{" + name + "}"
    found: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            if token in node:
                found.append((path, node))
        elif isinstance(node, dict):
            for key, child in node.items():
                if token in str(key):
                    found.append((path, str(key)))
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(value, "")
    return sorted(found)[0] if found else None


def placeholder_where(case: dict[str, Any], name: str) -> tuple[str, str] | None:
    """The field a run value fills: in the create, else in the update."""
    return placeholder_context(named_fixture(case), name) or placeholder_context(
        fixture_values.update_fixture(case), name
    )


def supplied_variable(name: str, where: tuple[str, str] | None) -> str:
    """One run-supplied variable, declared and described.

    The environment variable's name is in the description because that is what
    the operator has to set, and deriving `TFKIT_VAR_` from the Terraform name
    is a rule only this kit knows.
    """
    described = (
        f"Set TFKIT_VAR_{name.upper()}. Goes into `{where[0]}`, as {where[1]}."
        if where
        else f"Set TFKIT_VAR_{name.upper()}."
    )
    return (
        "\n# Supplied by the run. The case may not carry this: it names\n"
        "# something only the account knows, or something that has to\n"
        "# exist before this resource can.\n"
        f'variable "{name}" {{\n'
        "  type        = string\n"
        f"  description = {hcl_string(described)}\n"
        "}\n"
    )


def dependency_variable(entry: dict[str, Any]) -> str:
    name = entry["variable"]
    resource = entry["resource"]
    return (
        f'\nvariable "{name}" {{\n'
        + align(
            {
                "type": "string",
                "default": '""',
                "description": hcl_string(
                    f"TFKIT_VAR_{name.upper()} optionally names an existing {resource}; "
                    "empty creates one from its reviewed case."
                    if not entry.get("attribute")
                    else f"TFKIT_VAR_{name.upper()} optionally gives the {entry['attribute']} of an "
                    f"existing {resource}; empty creates one from its reviewed case and uses its "
                    f"{entry['attribute']}."
                ),
            }
        )
        + "\n}\n"
    )


def dependency_reference(entry: dict[str, Any], names: list[str]) -> str:
    """What the resource receives when the test creates its dependency."""
    if entry.get("attribute"):
        return created_attribute(entry)
    value = substituted(str(entry["reference"]), "${var.run_id}")
    for name in names:
        value = _as_variable(value, name)
    return hcl(value)


def created_attribute(entry: dict[str, Any]) -> str:
    """A value the API assigns to the created dependency -- an address's IP, a
    server-generated id -- which no reference written beforehand can state."""
    return f"{entry['resource']}.setup_{entry['variable']}[0].{entry['attribute']}"


def dependency_fixture(
    fixture: dict[str, Any],
    case: dict[str, Any],
    consumer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn exact dependency placeholders into supplied-or-created HCL values.

    `consumer` is the case whose declarations give the forms in `fixture`: the
    case of a created dependency resource, or by default the case itself.
    """
    names = case_values(case)
    rendered = substituted(fixture, "${var.run_id}")
    for name in names:
        rendered = _as_variable(rendered, name)
    optional = {entry["variable"]: entry for entry in optional_dependencies(case)}

    def text(part: str) -> str:
        return dependency_reference({"reference": part}, names)

    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            for name, entry in optional.items():
                if value == "${var." + name + "}":
                    other = _other_form(entry, consumer or case, case)
                    supplied = (
                        dependency_forms.supplied_in(
                            name,
                            entry["reference"],
                            other,
                            created_name(entry, case),
                            text,
                        )
                        if other
                        else f"var.{name}"
                    )
                    return HCLExpression(
                        f'var.{name} != "" ? {supplied} : '
                        + dependency_reference({**entry, "reference": other or entry.get("reference")}, names)
                    )
            return value
        if isinstance(value, dict):
            return {key: resolve(child) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child) for child in value]
        return value

    return suite_values.bare_references(resolve(rendered), HCLExpression)


def dependency_named_fixture(entry: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Give a setup resource a name owned by the case that creates it."""
    return _named_setup(entry, case)[0]


def created_name(entry: dict[str, Any], case: dict[str, Any]) -> str:
    """The name a dependency entry's setup resource is created with."""
    return _named_setup(entry, case)[1]


def _peer_fixtures(case: dict[str, Any]) -> list[dict[str, Any]]:
    """The peer's create fixture, named as a created resource is, or nothing."""
    if not peer_case.declared(case):
        return []
    try:
        return [_named_setup(peer_case.entry(case), case)[0]]
    except InputError as error:
        raise InputError(f"peer: {error}") from None


def _named_setup(entry: dict[str, Any], case: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """An entry with `values` may be one of several built from one case, so its
    variable, not its case's name, tells its name apart from the others'.
    """
    made = _created(entry, case)
    return made.values, made.name


def _created(entry: dict[str, Any], case: dict[str, Any]) -> dependency_names.Created:
    owner = str(case["resource"]["logical_kind"]).replace("_", "-")
    own = entry["variable"].replace("_", "-") if entry.get("values") else None
    try:
        return dependency_names.created(
            setup_fixture(entry), entry["case"], owner, f"{case_identity.case_id(case)}/{entry['variable']}", own
        )
    except dependency_names.NamingError as error:
        raise InputError(
            f"{case_identity.case_id(case)}: dependency {entry['case']['resource']['logical_kind']} {error}"
        ) from None


def dependency_main(
    case: dict[str, Any],
    fixture: dict[str, Any],
    *,
    include_target: bool,
    peer: dict[str, Any] | None = None,
) -> str:
    direct = {entry["variable"] for entry in case.get("dependencies") or [] if entry.get("resource")}

    def needed(variable: str) -> str:
        if variable in direct:
            return "true"
        consumers = [entry["variable"] for entry in optional_dependencies(case) if still_needs(entry, variable)]
        return " || ".join(f'(var.{consumer} == "" && ({needed(consumer)}))' for consumer in consumers)

    blocks: list[str] = []
    created = {entry["variable"] for entry in optional_dependencies(case)}
    for entry in optional_dependencies(case):
        parent = entry["case"]
        kind = entry["resource"]
        variable = entry["variable"]
        values = dependency_fixture(dependency_named_fixture(entry, case), case, parent)
        use = needed(variable)
        condition = f'var.{variable} == ""'
        if use != "true":
            condition += f" && ({use})"
        fields = {"count": HCLExpression(f"{condition} ? 1 : 0"), **values}
        parents = [
            child
            for child in parent.get("dependencies") or []
            if child.get("resource") and child.get("variable") in created and still_needs(entry, str(child["variable"]))
        ]
        # A value the entry lays over its case can name another created
        # resource, such as the network of a second gateway. A reference is a
        # string to Terraform, so only `depends_on` orders the two.
        parents += [
            other
            for other in optional_dependencies(case)
            if other["variable"] in supplied_values(entry.get("values") or {})
            and other["variable"] not in {child["variable"] for child in parents}
        ]
        if parents:
            refs = ", ".join(f"{child['resource']}.setup_{child['variable']}" for child in parents)
            fields["depends_on"] = HCLExpression(f"[{refs}]")
        blocks.append(resource_hcl(kind, f"setup_{variable}", fields))
    address = str((case.get("resource") or {}).get("terraform_address") or "")
    kind, _, label = address.partition(".")
    waits_for = [entry for entry in case.get("dependencies") or [] if entry.get("resource")]
    # The peer waits for the dependency resources, as the resource under test
    # does, and for nothing else: each is ready only when the other exists.
    targets = [(label, fixture)] + ([(peer_case.FIELD, peer)] if peer else [])
    for name, values in targets if include_target else []:
        fields = dependency_fixture(values, case)
        if waits_for:
            refs = ", ".join(f"{entry['resource']}.setup_{entry['variable']}" for entry in waits_for)
            fields["depends_on"] = HCLExpression(f"[{refs}]")
        blocks.append(resource_hcl(kind, name, fields))
    return "\n".join(blocks)


def resource_hcl(kind: str, name: str, fields: dict[str, Any]) -> str:
    """A resource block. The provider declares `timeouts` as a nested block,
    and Terraform refuses a block written as an argument."""
    timeouts = fields.get(fixture_values.TIMEOUTS)
    nested = isinstance(timeouts, dict)
    body = align(
        {key: hcl(value, 1) for key, value in fields.items() if not (nested and key == fixture_values.TIMEOUTS)}
    )
    if nested:
        body += "\n  timeouts {\n" + align({k: hcl(v, 2) for k, v in timeouts.items()}, 2) + "\n  }"
    return f'resource "{kind}" "{name}" {{\n{body}\n}}\n'


def render_workspace(
    case: dict[str, Any],
    *,
    source: str,
    version: str,
    include_target: bool = True,
    peer_of: dict[str, Any] | None = None,
    quoted_references: bool = False,
) -> dict[str, str]:
    """The root module a case applies.

    The run id is a variable rather than a baked-in string, because the same
    workspace is applied by different runs and each has to be findable
    separately. The name is built from it exactly as the case's pattern says.

    `peer_of` is the case whose create fixture the peer is made from: the
    case itself unless the configuration lays an update over its fixture.
    `quoted_references` keeps a value that is only a reference in its string,
    where a Go configuration with no variables fills it with `Sprintf`.
    """
    resource = case.get("resource") or {}
    address = str(resource.get("terraform_address") or "")
    if not address:
        raise InputError(f"{case.get('document', 'case')}: no terraform_address to render")
    kind, _, name = address.partition(".")
    local = provider_local_name(kind)
    fixture = named_fixture(case)
    if case.get("dependencies"):
        supplied = case_values(case)
        optional = {entry["variable"]: entry for entry in optional_dependencies(case)}
        variables = "".join(
            dependency_variable(optional[name])
            if name in optional
            else supplied_variable(name, placeholder_where(case, name))
            for name in supplied
        )
        main = dependency_main(
            case, fixture, include_target=include_target, peer=next(iter(_peer_fixtures(peer_of or case)), None)
        )
    else:
        variables = ""
        main = ""
    supplied = case_values(case)
    rendered = substituted(fixture, "${var.run_id}")
    # Not `name`: that is the resource's label, three lines up, and shadowing it
    # renamed every resource after the last value the run supplies.
    for supplied_name in supplied:
        rendered = _as_variable(rendered, supplied_name)
    if not quoted_references:
        rendered = suite_values.bare_references(rendered, HCLExpression)
    return {
        "versions.tf": workspace_versions.render(local, source, version),
        "variables.tf": (
            "# Substituted per run, so one run's resources can be found and swept\n"
            "# without touching another's.\n"
            'variable "run_id" {\n'
            "  type        = string\n"
            '  description = "Unique id for this run."\n'
            "}\n"
            + (
                variables
                if case.get("dependencies")
                else "".join(supplied_variable(name, placeholder_where(case, name)) for name in supplied)
            )
        ),
        "main.tf": main if case.get("dependencies") else resource_hcl(kind, name, rendered),
    }


#: What a supplied value is worth when nothing is created. It only has to be a
#: string the schema accepts, and naming it after itself keeps a plan readable.
OFFLINE_VALUE = "offline-{name}"


def substituted_value(value: Any, name: str, replacement: str) -> Any:
    """Replace one named placeholder with a literal, at any depth."""
    if isinstance(value, str):
        return value.replace("{" + name + "}", replacement)
    if isinstance(value, dict):
        return {
            _filled_key(k, "{" + name + "}", replacement): substituted_value(v, name, replacement)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [substituted_value(v, name, replacement) for v in value]
    return value


def _as_variable(value: Any, name: str) -> Any:
    """Replace one placeholder with the Terraform variable that supplies it."""
    if isinstance(value, str):
        return value.replace("{" + name + "}", "${var." + name + "}")
    if isinstance(value, dict):
        return {
            _filled_key(k, "{" + name + "}", "${var." + name + "}"): _as_variable(v, name) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_as_variable(v, name) for v in value]
    return value


def plan_assertions(address: str, configured: dict[str, Any]) -> list[tuple[Any, str, str, str]]:
    """Each value a plan must hold, at every depth: its path, both sides, and
    the message."""
    return [
        (path, left, hcl(value, 2), f"{'.'.join(path)} did not reach the resource as the fixture configured it")
        for path, left, value in offline_values.plan_checks(address, configured, state_string)
    ]


def render_offline_test(case: dict[str, Any]) -> str:
    """A Terraform test that plans and never applies.

    Every assertion here is a fact the fixture already states, checked against
    what the provider's schema actually accepted. A provider missing an
    attribute fails `validate`; one that renames or drops a value fails these.
    """
    if case.get("dependencies"):
        return render_dependency_offline_test(case)
    resource = case.get("resource") or {}
    address = str(resource.get("terraform_address") or "")
    run_id = "offline"
    # Assert what the workspace actually writes, which is the fixture with the
    # run id substituted and the name wherever this API takes it. Asserting a
    # field called `name` regardless checked a field half these resources do not
    # have, and missed the one that carries the run id.
    fixture = named_fixture(case)
    supplied = case_values(case)
    configured = substituted(fixture, run_id)
    # Nothing is created, so a supplied value only has to be a string the schema
    # accepts. Naming it after itself keeps the rendered plan readable.
    for name in supplied:
        configured = substituted_value(configured, name, OFFLINE_VALUE.format(name=name))
    asserts = "\n\n".join(
        f"  assert {{\n    condition     = {left} == {right}\n    error_message = {json.dumps(message)}\n  }}"
        for _, left, right, message in plan_assertions(address, configured)
    )
    return (
        "# Offline: this plans and stops. It reaches no API and needs no\n"
        "# credentials, so it proves the configuration and the schema agree and\n"
        "# nothing about whether the NCP would accept the request.\n"
        "variables {\n"
        + align(
            {
                "run_id": json.dumps(run_id),
                **{name: json.dumps(OFFLINE_VALUE.format(name=name)) for name in supplied},
            }
        )
        + "\n}\n\n"
        'run "plan_create" {\n'
        "  command = plan\n\n"
        f"{asserts}\n"
        "}\n"
    )


def render_dependency_offline_test(case: dict[str, Any]) -> str:
    address = str((case.get("resource") or {}).get("terraform_address") or "")
    fixture = named_fixture(case)
    optional = optional_dependencies(case)
    required = required_case_values(case)

    def offline_reference(entry: dict[str, Any]) -> str:
        if entry.get("attribute"):
            return OFFLINE_VALUE.format(name=entry["variable"])
        value = substituted(str(entry["reference"]), "offline")
        for name in required:
            value = substituted_value(value, name, OFFLINE_VALUE.format(name=name))
        return value

    def planned(subject: str, values: dict[str, Any]) -> list[tuple[bool, Any]]:
        """Each check of one resource, and whether the created path knows its value."""
        configured = substituted(values, "offline")
        for name in required:
            configured = substituted_value(configured, name, OFFLINE_VALUE.format(name=name))
        for entry in optional:
            configured = substituted_value(configured, entry["variable"], offline_reference(entry))
        # A value the API assigns to a dependency the test creates is unknown
        # until apply, so a plan cannot assert it.
        assigned = {
            path
            for path, value in fixture_values.leaves(values)
            if any(value == "{" + entry["variable"] + "}" for entry in optional if entry.get("attribute"))
        }
        who = "the resource" if subject == address else "the peer"
        return [
            (path not in assigned, (path, left, right, message.replace("the resource", who)))
            for path, left, right, message in plan_assertions(subject, configured)
        ]

    checks = planned(address, fixture) + [
        known for peer in _peer_fixtures(case) for known in planned(peer_case.address(case), peer)
    ]
    common = (
        "variables {\n"
        + align(
            {
                "run_id": json.dumps("offline"),
                **{name: json.dumps(OFFLINE_VALUE.format(name=name)) for name in required},
            }
        )
        + "\n}\n\n"
    )

    def plan(name: str, supplied: bool) -> str:
        values = (
            "  variables {\n"
            + align({entry["variable"]: json.dumps(offline_reference(entry)) for entry in optional}, 2)
            + "\n  }\n\n"
            if supplied and optional
            else ""
        )
        assertions = [
            (
                f"length({entry['resource']}.setup_{entry['variable']})",
                "0" if supplied else "1",
                f"{entry['variable']} setup count differs from the selected path",
            )
            for entry in optional
        ] + [check[1:] for known, check in checks if supplied or known]
        body = "\n\n".join(
            f"  assert {{\n    condition     = {left} == {right}\n    error_message = {json.dumps(message)}\n  }}"
            for left, right, message in assertions
        )
        return f'run "{name}" {{\n  command = plan\n\n' + values + body + "\n}\n"

    if not optional:
        return (
            "# Offline: the external dependency is supplied; this plans without\n"
            "# an API call or credentials.\n" + common + plan("plan_create", True)
        )
    return (
        "# Offline: both a supplied dependency and an in-test dependency plan\n"
        "# without an API call or credentials.\n"
        + common
        + plan("plan_supplied", True)
        + "\n"
        + plan("plan_in_test", False)
    )


def _formatted(text: str, pattern: str) -> tuple[str, list[str]]:
    """The text as a Go format string, and the arguments that fill it, in order.

    `fmt.Sprintf` fills its verbs left to right by where they sit in the string.
    Substituting each name in turn and appending its argument built the list in
    the order the names were *processed* instead, so a configuration naming two
    of them filled them the wrong way round -- and one naming none was still
    handed the run id, which does not compile.
    """
    arguments: list[str] = []

    def take(match: re.Match[str]) -> str:
        name = match.group(1)
        arguments.append("id" if name == "run_id" else f"supplied[{json.dumps(name)}]")
        return "%s"

    return re.sub(pattern, take, text), arguments


def go_text(text: str) -> str:
    """`text` as a Go string expression, with each placeholder filled by the run.

    A value carrying the run id is not known until the run has one, so it is
    built rather than quoted, and so is a path through a map keyed by a
    supplied value.
    """
    formatted, arguments = _formatted(text.replace("%", "%%"), r"\{([a-z_][a-z0-9_]*)\}")
    if not arguments:
        return json.dumps(text)
    # staticcheck S1025 refuses `fmt.Sprintf("%s", x)` for a string x.
    if formatted == "%s" and len(arguments) == 1:
        return arguments[0]
    return f"fmt.Sprintf({json.dumps(formatted)}, {', '.join(arguments)})"


def state_string(value: Any) -> str:
    """A fixture value as Terraform's state holds it.

    `resource.TestCheckResourceAttr` compares strings against state, and state
    spells every value its own way. Python spells a bool `True` and Terraform
    spells it `true`, so a rendered check on a boolean asserted `"True"` and
    could never pass -- against any provider, for any NCP.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _config_function(case: dict[str, Any], which: str, *, source: str, version: str) -> str:
    """The configuration one step applies, as a Go function returning HCL.

    The run id arrives as a format argument rather than a Terraform variable,
    because that is the idiom both mature reference providers use and it keeps
    the test readable as one file.
    """
    if case.get("dependencies"):
        return dependency_config_function(case, which, source=source, version=version)
    fixture = dict((case.get("fixture") or {}).get("create") or {})
    if which == "update":
        fixture = fixture_values.merged_fixture(fixture, (case.get("fixture") or {}).get("update") or {})
    body = render_workspace(
        {**case, "fixture": {"create": fixture}},
        source=source,
        version=version,
        quoted_references=True,
    )["main.tf"]
    # Sprintf reads `%` as a verb, and HCL writes `%{...}` for its own directives.
    body, arguments = _formatted(body.replace("%", "%%"), r"\$\{var\.([a-z_][a-z0-9_]*)\}")
    # A configuration that names the run has a verb for its id. One that does
    # not would hand `Sprintf` an argument nothing consumes -- `go vet` reports
    # it, and a reader takes it for a value the template dropped.
    signature = f"func {_config_name(case, which)}(id string, supplied map[string]string) string {{\n"
    if "%s" in body:
        joined = ", ".join(arguments)
        template = rendered_literals.go_raw("\n" + body, argument=True)
        return f"{signature}\treturn fmt.Sprintf({template}, {joined})\n}}\n"
    return (
        f"{signature}"
        f"\t_, _ = id, supplied // this configuration names nothing per run\n"
        f"\treturn {rendered_literals.go_raw(chr(10) + body.replace('%%', '%'))}\n"
        "}\n"
    )


def dependency_config_function(case: dict[str, Any], which: str, *, source: str, version: str) -> str:
    fixture = dict((case.get("fixture") or {}).get("create") or {})
    if which == "update":
        fixture = fixture_values.merged_fixture(fixture, (case.get("fixture") or {}).get("update") or {})
    rendered_case = {**case, "fixture": {**(case.get("fixture") or {}), "create": fixture}}
    if which == peer_case.REQUEST:
        rendered_case.pop(peer_case.FIELD, None)
    body = render_workspace(
        rendered_case,
        source=source,
        version=version,
        include_target=which not in ("setup", "teardown"),
        peer_of=case if which != peer_case.REQUEST else None,
    )["main.tf"]
    names = case_values(case)
    declarations = 'variable "run_id" { default = %q }\n'
    declarations += "".join(f'variable "{name}" {{ default = %q }}\n' for name in names)
    arguments = ", ".join(["id"] + [f"supplied[{json.dumps(name)}]" for name in names])
    template = rendered_literals.go_raw(f"\n{declarations}\n{body.replace('%', '%%')}", argument=True)
    return (
        f"func {_config_name(case, which)}"
        "(id string, supplied map[string]string) string {\n"
        f"\treturn fmt.Sprintf({template}, {arguments})\n"
        "}\n"
    )


def _config_name(case: dict[str, Any], which: str) -> str:
    return f"testAcc{case_identity.go_case_name(case)}Config_{which}"


def create_expectation(case: dict[str, Any]) -> dict[str, Any] | None:
    """What the case says this resource's create request must be.

    The entry is already in the case, written from the specification when the
    case was instantiated. Nothing new is asked of a reviewer to get this check.
    """
    for entry in case.get("expected_requests") or []:
        if isinstance(entry, dict) and str(entry.get("verb")) == "create":
            return entry
    return None


#: What the request check stands in for, and what the rendered expectation has
#: to agree with. Both are constants so that the expected value can be settled
#: here rather than built again in Go.
STUB_RUN_ID = "tfkitcheck"
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def stub_supplied(name: str) -> str:
    """The stand-in `stubValues` gives one supplied value."""
    return "tfkit-stub-" + name.replace("_", "-")


def stubbed_scalar(
    value: Any,
    dependency_stubs: dict[str, str] | None = None,
    *,
    run_id: str = STUB_RUN_ID,
) -> str | None:
    """A fixture scalar as it should arrive in the request, or None for a map
    or a list, whose values the request check compares one at a time."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return None
    return PLACEHOLDER.sub(
        lambda found: (
            run_id
            if found.group(1) == "run_id"
            else (dependency_stubs or {}).get(found.group(1), stub_supplied(found.group(1)))
        ),
        value,
    )


def request_dependency_stubs(case: dict[str, Any]) -> dict[str, str]:
    """Use each dependency's reference shape in a request-only configuration, and
    the form that the documents state for the field of an attribute dependency."""
    return {
        **dependency_stubs.stand_ins(case, stub_supplied),
        **{
            entry["variable"]: stubbed_scalar(entry["reference"], run_id="stub")
            for entry in dependency_entries(case)
            if entry.get("reference")
        },
    }


def compared_values(case: dict[str, Any], dependency_stubs: dict[str, str] | None = None) -> dict[str, str]:
    """Every fixture value this check can hold the request to."""
    out: dict[str, str] = {}
    for name, value in (named_fixture(case) or {}).items():
        arrived = stubbed_scalar(value, dependency_stubs)
        if arrived is not None:
            out[str(name)] = arrived
    return out


def render_request_test(case: dict[str, Any], *, source: str, version: str) -> str:
    """The test that asserts what a create would send, and stops there.

    It needs no account and no recording, because it answers every request with
    a refusal. Supplied values are placeholders for the same reason: nothing is
    created, so nothing has to exist, and requiring the live variables here would
    make an offline check need a live run's setup.
    """
    named = case_identity.go_case_name(case)
    want = create_expectation(case) or {}
    wanted = case_values(case)
    applied = peer_case.REQUEST if peer_case.declared(case) else "create"
    dependency_stubs = request_dependency_stubs(case)

    compared = compared_values(case, dependency_stubs)
    values = ""
    if compared:
        rows = align(
            {json.dumps(name): f"{json.dumps(value)}," for name, value in sorted(compared.items())}, 2, style="go"
        )
        values = (
            "\t// Every value the configuration sets, in the field it reached.\n"
            "\tcheckValues(t, observed, map[string]string{\n" + rows + "\n\t})\n\n"
        )
    values += offline_values.nested_request_check(
        [
            (
                tuple(str(stubbed_scalar(segment, dependency_stubs)) for segment in path),
                stubbed_scalar(value, dependency_stubs),
            )
            for path, value in fixture_values.leaves(fixture_values.api_values(named_fixture(case)))
            if len(path) > 1
        ]
    )

    # A header the credential layer adds is not assertable here: handing the
    # provider a transport bypasses authentication, so the case's `source` is
    # what decides which of its headers this tier can see.
    sent = [
        str(h.get("name"))
        for h in (want.get("headers") or [])
        if isinstance(h, dict)
        and str(h.get("name"))
        and h.get("required")
        and not str(h.get("source") or "").startswith("auth")
    ]
    headers = "[]string{" + ", ".join(json.dumps(h) for h in sent) + "}" if sent else "nil"

    fields = [str(f) for f in (want.get("required_fields") or []) if str(f)]
    listed = ", ".join(json.dumps(f) for f in fields)
    body = f"[]string{{{listed}}}" if fields else "nil"

    return (
        "// Generated from the case. Edit the case, not this file.\n\n"
        "package provider_test\n\n"
        "import (\n"
        '\t"testing"\n\n'
        '\t"github.com/hashicorp/terraform-plugin-testing/helper/resource"\n'
        ")\n\n"
        f"// TestRequest{named}_create asserts the request this resource's\n"
        f"// create would send: {want.get('method', '')} {want.get('path', '')}.\n"
        "//\n"
        "// A plan proves the configuration and the schema agree. It cannot show a\n"
        "// value that reached the wrong field, because nothing is sent.\n"
        f"func TestRequest{named}_create(t *testing.T) {{\n"
        "\trequestEnvironment(t)\n" + header_switch.turned_on(case) + "\tobserved := &requestObserver{}\n"
        f"\tsupplied := stubValues({', '.join(json.dumps(n) for n in wanted)})\n"
        + "".join(
            f"\tsupplied[{json.dumps(name)}] = {json.dumps(value)}\n"
            for name, value in sorted(dependency_stubs.items())
        )
        + "\n"
        + "\tresource.UnitTest(t, resource.TestCase{\n"
        "\t\tProtoV6ProviderFactories: requestProviderFactories(observed.answer),\n"
        "\t\tSteps: []resource.TestStep{{\n"
        f"\t\t\tConfig:      {_config_name(case, applied)}(stubRunID, supplied),\n"
        "\t\t\tExpectError: anyError,\n"
        "\t\t}},\n"
        "\t})\n\n" + read_first.skip_call(case) + values + "\tcheckRequest(t, observed, expectation{\n"
        f"\t\tmethod:         {json.dumps(str(want.get('method') or ''))},\n"
        f"\t\tpath:           {json.dumps(str(want.get('path') or ''))},\n"
        f"\t\trequiredFields: {body},\n"
        f"\t\theaders:        {headers},\n"
        "\t})\n"
        "}\n"
    )


def render_live_test(
    case: dict[str, Any],
    *,
    source: str,
    version: str,
    schema: dict[str, Any] | None = None,
) -> str:
    """The acceptance test a live case runs, for the framework to orchestrate.

    Terraform's own view is only half of it. Every step that changes something
    is followed by a call to the NCP, because a provider can report a success
    the NCP never performed.
    """
    if optional_dependencies(case):
        return render_dependency_live_test(case, schema or {})
    resource = case.get("resource") or {}
    address = str(resource.get("terraform_address") or "")
    kind = case_identity.case_id(case)
    lifecycle = list(case.get("lifecycle") or [])
    verification = case.get("verification") or {}
    quoted = json.dumps(address)

    def expected(value: Any) -> str:
        """The value this step must find, as Go source.

        A value carrying the run id is not known until the run has one, so it
        is built rather than quoted. Emitting the id's placeholder as a string
        literal asserted that the resource was called `{run_id}`, which no API
        would have accepted in the first place.
        """
        return go_text(state_string(value))

    def checks(values: dict[str, Any], step: str) -> str:
        lines = [
            f"\t\t\t\t\tresource.TestCheckResourceAttr({quoted}, {go_text('.'.join(path))}, {expected(value)}),"
            for path, value in fixture_values.leaves(values)
        ]
        if rule := verification.get(step) or {}:
            operation = str(rule.get("operation", ""))
            lines.append(fixture_values.read_check(address, operation, values, schema or {}, case, segment=go_text))
            lines += lifecycle_checks.ready_check(address, operation, case)
        return "resource.ComposeAggregateTestCheckFunc(\n" + "\n".join(lines) + "\n\t\t\t\t),"

    values = fixture_values.step_values(case, (case.get("fixture") or {}).get("create") or {})
    steps = [
        go_struct(
            "",
            {
                "Config": f"{_config_name(case, which)}(id, supplied),",
                **({"PlanOnly": "true,"} if kind == "plan" else {"Check": checks(values[which], which)}),
            },
            3,
        )
        + ","
        for kind, which in lifecycle_checks.steps(lifecycle)
    ]

    body = {"ProtoV6ProviderFactories": "testAccProtoV6ProviderFactories,"}
    if "destroy" in verification:
        operation, identifier = destroy_read.destroy_read(case)
        expect = str((verification["destroy"] or {}).get("expect", ""))
        body["CheckDestroy"] = (
            f"checkGone({quoted}, {json.dumps(operation)}, {json.dumps(identifier)}, {json.dumps(expect)}),"
        )
    body["Steps"] = "[]resource.TestStep{\n" + "\n".join(f"\t\t\t{s}" for s in steps) + "\n\t\t},"

    wanted = case_values(case)
    test = (
        f"func TestAcc{case_identity.go_case_name(case)}_lifecycle(t *testing.T) {{\n"
        + header_switch.calls(case)
        + "\tid := runID(t)\n"
        f"\tsupplied := runValues(t{''.join(', ' + json.dumps(n) for n in wanted)})\n\n"
        f"\tresource.Test(t, {go_struct('resource.TestCase', body, 1)})\n}}\n\n"
    )
    # Go refuses to compile a file that imports what it does not use, so the
    # import follows the content rather than the other way round. A resource
    # whose configuration and checks both name nothing after the run needs no
    # formatting at all.
    imports = ['\t"testing"']
    if "fmt." in test:
        imports.insert(0, '\t"fmt"')
    return (
        f"// Generated from the case for {kind}. Edit the case, not this file.\n"
        f"//\n// Ready means: {case.get('readiness') or 'unstated'}\n"
        "\npackage provider_test\n\n"
        "import (\n" + "\n".join(imports) + "\n\n"
        '\t"github.com/hashicorp/terraform-plugin-testing/helper/resource"\n'
        ")\n\n" + test.rstrip("\n") + "\n"
    )


def render_dependency_live_test(case: dict[str, Any], schema: dict[str, Any]) -> str:
    address = str((case.get("resource") or {}).get("terraform_address") or "")
    kind = case_identity.case_id(case)
    lifecycle = list(case.get("lifecycle") or [])
    verification = case.get("verification") or {}
    optional = {entry["variable"]: entry for entry in optional_dependencies(case)}
    quoted = json.dumps(address)

    def expected(value: Any) -> str:
        text = state_string(value)
        variable = text.removeprefix("{").removesuffix("}")
        if text == "{" + variable + "}" and variable in optional:
            fallback = str(optional[variable]["reference"])
            formatted, arguments = _formatted(fallback.replace("%", "%%"), r"\{([a-z_][a-z0-9_]*)\}")
            expression = (
                f"fmt.Sprintf({json.dumps(formatted)}, {', '.join(arguments)})" if arguments else json.dumps(fallback)
            )
            return f'chooseReference(supplied["{variable}"], {expression})'
        return go_text(text)

    def check(key: str, value: Any) -> str:
        entry = optional.get(str(value).removeprefix("{").removesuffix("}"))
        if entry and entry.get("attribute") and value == "{" + entry["variable"] + "}":
            created = f"{entry['resource']}.setup_{entry['variable']}.0"
            return (
                f"\t\t\t\t\tcheckSuppliedOrCreated({quoted}, {go_text(key)}, "
                f'supplied["{entry["variable"]}"], {json.dumps(created)}, '
                f"{json.dumps(entry['attribute'])}),"
            )
        return f"\t\t\t\t\tresource.TestCheckResourceAttr({quoted}, {go_text(key)}, {expected(value)}),"

    def checks(values: dict[str, Any], step: str) -> str:
        lines = [check(".".join(path), value) for path, value in fixture_values.leaves(values)]
        if rule := verification.get(step) or {}:
            operation = str(rule.get("operation", ""))
            lines.append(fixture_values.read_check(address, operation, values, schema, case, segment=go_text))
            lines += lifecycle_checks.ready_check(address, operation, case)
        if step == "create":
            lines.append(f"\t\t\t\t\trememberID({quoted}, &createdID),")
            lines += peer_case.create_checks(case) if peer_case.declared(case) else []
        return "resource.ComposeAggregateTestCheckFunc(\n" + "\n".join(lines) + "\n\t\t\t\t),"

    def step(label: str, body: dict[str, str]) -> str:
        return f"\t\t\t// {label}\n\t\t\t" + go_struct("", body, 3) + ","

    values = fixture_values.step_values(case, named_fixture(case))
    labels = {
        ("apply", "create"): "create subject with dependencies",
        ("apply", "update"): "update subject",
        ("plan", "create"): "plan no change",
        ("plan", "update"): "plan no change after update",
    }
    steps = [
        step(
            "setup dependencies",
            {
                "Config": f"{_config_name(case, 'setup')}(id, supplied),",
            },
        )
    ] + [
        step(
            labels[kind, which],
            {
                "Config": f"{_config_name(case, which)}(id, supplied),",
                **({"PlanOnly": "true,"} if kind == "plan" else {"Check": checks(values[which], which)}),
            },
        )
        for kind, which in lifecycle_checks.steps(lifecycle)
    ]
    destroy = verification.get("destroy") or {}
    operation, identifier = destroy_read.destroy_read(case)
    gone = (
        f"checkGoneByID({quoted}, &createdID, {json.dumps(operation)}, "
        f"{json.dumps(identifier)}, {json.dumps(str(destroy.get('expect', '')))}),"
    )
    if peer_case.declared(case):
        gone = peer_case.teardown_check(case, gone, operation, identifier, str(destroy.get("expect", "")))
    steps.append(
        step(
            "teardown subject while dependencies remain",
            {
                "Config": f"{_config_name(case, 'teardown')}(id, supplied),",
                "Check": gone,
            },
        )
    )
    body = {"ProtoV6ProviderFactories": "testAccProtoV6ProviderFactories,"}
    if created := lifecycle_checks.dependency_destroys(optional_dependencies(case)):
        body["CheckDestroy"] = created
    body["Steps"] = "[]resource.TestStep{\n" + "\n".join(steps) + "\n\t\t},"
    required = required_case_values(case)
    forms = dependency_stubs.supplied_forms(case)
    optional_lines = "".join(
        f'\tif value := os.Getenv("TFKIT_VAR_{name.upper()}"); value != "" {{\n'
        f'\t\tsupplied["{name}"] = '
        + supplied_dependency.go_value(
            optional[name], created_name(optional[name], case), _formatted, form=forms.get(name, "")
        )
        + "\n\t}\n"
        for name in sorted(optional)
    )
    test = (
        f"func TestAcc{case_identity.go_case_name(case)}_lifecycle(t *testing.T) {{\n"
        + header_switch.calls(case)
        + "\tid := runID(t)\n"
        f"\tsupplied := runValues(t{''.join(', ' + json.dumps(name) for name in required)})\n"
        + optional_lines
        + "\tvar createdID string\n"
        + (f"\tvar {peer_case.ID_VARIABLE} string\n" if peer_case.declared(case) else "")
        + "\n"
        + f"\tresource.Test(t, {go_struct('resource.TestCase', body, 1)})\n"
        + "}\n"
    )
    imports = ['\t"os"', '\t"testing"']
    if "fmt." in test:
        imports.insert(0, '\t"fmt"')
    return (
        f"// Generated from the case for {kind}. Edit the case, not this file.\n"
        f"//\n// Ready means: {case.get('readiness') or 'unstated'}\n"
        "\npackage provider_test\n\n"
        "import (\n" + "\n".join(imports) + "\n\n"
        '\t"github.com/hashicorp/terraform-plugin-testing/helper/resource"\n'
        ")\n\n" + test
    )


def render_config_functions(case: dict[str, Any], *, source: str, version: str) -> str:
    """The configuration each step applies, as Go functions returning HCL.

    Its own file because two tests apply it: the live lifecycle, and the request
    check that asserts what a create would send. Rendering it into both would
    define the same function twice in one package, and rendering it into one
    would make the other depend on a mode it was not run in.
    """
    # Which configurations exist is the case's answer; which ones a test applies
    # is the mode's. Keying this on the lifecycle would leave the update
    # configuration out of an offline render, and the live test that applies it
    # is in the same package.
    functions = _config_function(case, "create", source=source, version=version)
    if optional_dependencies(case):
        functions = _config_function(case, "setup", source=source, version=version) + "\n" + functions
        functions += "\n" + _config_function(case, "teardown", source=source, version=version)
        functions += f"\nvar _ = {_config_name(case, 'setup')}\nvar _ = {_config_name(case, 'teardown')}\n"
    if peer_case.declared(case):
        # Only the request check calls it, and a live render writes no request check.
        functions += "\n" + _config_function(case, peer_case.REQUEST, source=source, version=version)
        functions += f"\nvar _ = {_config_name(case, peer_case.REQUEST)}\n"
    if (case.get("fixture") or {}).get("update"):
        functions += "\n" + _config_function(case, "update", source=source, version=version)
        # Only the live lifecycle test calls the update configuration, so an
        # offline-only render leaves it without a caller, and the `unused`
        # linter refuses the package.
        functions += f"\nvar _ = {_config_name(case, 'update')}\n"
    return (
        "// Generated from the case. Edit the case, not this file.\n\n"
        "package provider_test\n\n"
        'import "fmt"\n\n' + functions
    )


def _run_block(name: str, command: str, asserts: list[tuple[str, str]], *, refresh_only: bool = False) -> str:
    """One `run` block: what it does, and what must hold after it."""
    options = "\n  plan_options {\n    mode = refresh-only\n  }\n" if refresh_only else ""
    body = "\n\n".join(
        f"  assert {{\n    condition     = {condition}\n    error_message = {json.dumps(message)}\n  }}"
        for condition, message in asserts
    )
    return f'run "{name}" {{\n  command = {command}\n' + options + (f"\n{body}\n" if body else "") + "}\n"


#: The variables a test file reads its run values from. A `.tftest.hcl` file
#: declares no variables, so the runner passes each as `TF_VAR_<name>`, the
#: run id as `tfkit_run_id` and each `TFKIT_VAR_<NAME>` as `tfkit_var_<name>`.
RUN_ID_VARIABLE = "tfkit_run_id"
RUN_VALUE_VARIABLE = "tfkit_var_{name}"


def _variables_block(values: dict[str, Any], *, applies: bool) -> str:
    # A module's inputs carry the run id the same way a resource's fixture
    # does -- `name_prefix` is the usual one -- and a variables block is where
    # the run supplies it. Left unsubstituted, the module names everything it
    # creates `{run_id}`.
    supplied = supplied_values(values)
    if applies:
        # What is created is named for this run, so a sweep finds it, and an
        # account value comes from the run rather than from the case.
        run_id = "${var." + RUN_ID_VARIABLE + "}"
        values = substituted(values, run_id)
        for name in supplied:
            reference = "${var." + RUN_VALUE_VARIABLE.format(name=name) + "}"
            values = substituted_value(values, name, reference)
        values = suite_values.bare_references(values, HCLExpression)
        shown_run_id = "var." + RUN_ID_VARIABLE
    else:
        # Offline nothing is created, so a stand-in only has to be a string the
        # schema accepts -- the same stand-in a resource's offline test uses.
        values = substituted(values, "offline")
        for name in supplied:
            values = substituted_value(values, name, OFFLINE_VALUE.format(name=name))
        shown_run_id = json.dumps("offline")
    body = {
        "run_id": shown_run_id,
        **{k: hcl(v, 1) for k, v in values.items()},
    }
    return "variables {\n" + align(body) + "\n}\n"


#: What a test that applies says about itself, above its blocks.
LIVE_HEADER = (
    "#\n# This creates real resources in the account, and `terraform test` destroys\n"
    "# them when the file ends; a destroy that fails fails the test. Run it with\n"
    "# `tools/run-tests.sh live` in the directory under test, which supplies the\n"
    "# run values. It cannot show that a second plan is empty: a test file has\n"
    "# no assertion over a plan's changes, and the resource acceptance tests\n"
    "# carry that check.\n"
)


def render_module_test(case: dict[str, Any]) -> str:
    """A Terraform test for one layer module, run from the module itself.

    `terraform test` runs a module's own `tests/` against that module as the
    root, so there is no workspace to write: the variables block supplies the
    inputs and the run blocks assert the outputs.
    """
    lifecycle = list(case.get("lifecycle") or [])
    applies = "apply_create" in lifecycle
    outputs = case.get("outputs") or {}
    asserts = [
        (
            resolved(f"output.{name}", expected),
            f"the {name} output did not resolve, so a consumer of this layer has nothing to read",
        )
        for name, expected in outputs.items()
    ]

    blocks = [_variables_block(case.get("variables") or {}, applies=applies)]
    blocks.append(_run_block("plan", "plan", []))
    if applies:
        blocks.append(_run_block("apply", "apply", asserts))
    if applies and "refresh" in lifecycle:
        # Every output read back from the API, not from what apply returned.
        blocks.append(_run_block("refresh", "plan", asserts, refresh_only=True))
    # Nothing is asserted about an output offline: its value is unknown until
    # apply, and `plan` already fails when an output references what is not
    # there. A second run block that asserts nothing would read as coverage.
    return (
        f"# Layer module: {case.get('module')}\n"
        "# Generated from the case beside this file. Edit the case, not this test.\n"
        + (
            "#\n# This applies nothing. It proves the module's variables are accepted\n"
            "# and its configuration is valid, and nothing about the NCP.\n"
            if not applies
            else LIVE_HEADER
        )
        + "\n"
        + "\n".join(blocks)
    )


def resolved(reference: str, expected: Any) -> str:
    """The condition that says a value is really there.

    What counts as empty depends on the type the reviewer gave the output.
    Comparing a list to `[]` is not it: Terraform compares a list against an
    empty tuple there, which is a type mismatch rather than a length check.
    """
    if isinstance(expected, (list, dict)):
        return f"length({reference}) > 0"
    if isinstance(expected, bool) or isinstance(expected, (int, float)):
        return f"{reference} != null"
    return f'{reference} != ""'


def render_composition_test(case: dict[str, Any]) -> str:
    """A Terraform test for the blueprint, asserting the seams between layers.

    Each assertion is one wiring pair: the output one layer publishes must be
    set, because the next layer's input reads it. A pair wired through a literal
    proves nothing about the seam, which is why the pairs are reviewed.
    """
    lifecycle = list(case.get("lifecycle") or [])
    applies = "apply_create" in lifecycle
    asserts = []
    for pair in case.get("wiring") or []:
        source = str((pair or {}).get("from") or "")
        target = str((pair or {}).get("to") or "")
        if not source:
            continue
        asserts.append(
            (
                resolved(f"module.{source}", (pair or {}).get("type", "")),
                f"{source} is empty, so {target or 'the next layer'} has nothing to consume",
            )
        )
    order = " then ".join(str(layer) for layer in case.get("layer_order") or [])
    blocks = [
        _variables_block(case.get("variables") or {}, applies=applies),
        _run_block("plan", "plan", []),
    ]
    if applies:
        blocks.append(_run_block("apply", "apply", asserts))
    if applies and "refresh" in lifecycle:
        blocks.append(_run_block("refresh", "plan", asserts, refresh_only=True))
    return (
        "# Blueprint: every buildable layer, composed.\n"
        "# Generated from the case beside this file. Edit the case, not this test.\n"
        + (f"#\n# Layer order: {order}\n" if order else "")
        + (
            "#\n# This applies nothing. It proves the layers compose into a valid\n"
            "# configuration, and nothing about whether the NCP accepts it.\n"
            if not applies
            else LIVE_HEADER
        )
        + "\n"
        + "\n".join(blocks)
    )


# --- checking ---------------------------------------------------------------


#: One reader of a judged path, shared with the refresh that writes one.
_reach = case_refresh.reach


def check_case(case: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    """Every way the case breaks its contract, empty when it holds."""
    broken: list[str] = []
    if case.get("contract") != contract.get("contract"):
        broken.append(f"case declares contract {case.get('contract')!r}, checked against {contract.get('contract')!r}")
        return broken
    if case.get("api_version") != contract.get("api_version"):
        broken.append(f"api_version is {case.get('api_version')!r}")

    lifecycle = list(case.get("lifecycle") or [])
    # The contract states what a case owes; the case's own list can only add to it.
    requirements = list((case.get("resource") or {}).get("requirements") or [])
    owed = [field for field in _judged_fields(contract) if _applies(field, lifecycle, requirements)]
    for path in dict.fromkeys([*owed, *(case.get("judged") or [])]):
        if _reach(case, path) in (None, {}, [], ""):
            broken.append(f"{path} is unfilled; a case nobody finished would run and report a pass")

    contract_lifecycle = list(contract.get("lifecycle") or [])
    if contract_lifecycle:
        # One entry drops one step. `plan_no_change` appears twice, and treating
        # the dropped names as a set would expect both to be gone.
        expected = list(contract_lifecycle)
        for entry in case.get("narrowed") or []:
            step = entry.get("dropped")
            if step in expected:
                expected.remove(step)
        if lifecycle != expected:
            broken.append(f"lifecycle is {lifecycle}, expected {expected}")

    if "mode" in case:
        broken.append(
            "the case names a mode; how far a run goes is decided when it runs, "
            "and a case that fixes it cannot be rerun any other way"
        )
    broken += update_fields.refusals(case)
    broken += destroy_read.refusals(case) + case_identity.refusals(case) + peer_case.refusals(case)
    broken += (
        case_text.refusals(case)
        + case_names.refusals(case)
        + dependency_graph.refusals(case)
        + dependency_stubs.refusals(case)
    )

    pattern = _reach(case, "naming.name_pattern")
    if pattern is not None:
        # The prefix is fixed; the separator after it is not. A GCP custom role
        # id forbids the hyphen, so a case that obeys the API has to spell the
        # same three parts another way, and the check that required the hyphen
        # made the convention unreachable rather than tighter.
        if not str(pattern).startswith(KIT_PREFIX):
            broken.append(f"name_pattern {pattern!r} does not open with the kit's prefix")
        if RUN_ID not in str(pattern):
            broken.append(f"name_pattern {pattern!r} carries no run id; the sweeper needs one")
    return broken


# --- commands ---------------------------------------------------------------


SELECTION_FILE = "selection.yaml"

#: The case states the question and the cost. It cannot state where the answer
#: goes, because `review` is a list beside the fields rather than above them,
#: and a reviewer who writes the answer into `review` changes nothing and is
#: told nothing.
CASE_HEADER = """\
# The decisions this case owes, and where each answer goes.
#
# Every entry under `review` names a field. The answer goes at that path in this
# file: `fixture.create` is the `create:` key under `fixture:`, `readiness` is
# the key of that name. Write it in the shape the entry gives, and follow the
# example beside it.
#
# `review` is generated from the contract. An answer written there is read by
# nothing. When a fixture value names another resource, declare it under
# `dependencies` (the skill says how). Every other field comes from the API
# documents, and rewriting one here makes this case disagree with the API
# rather than with the provider.
"""

#: `yaml.safe_dump` writes no comments, so this is prepended. It travels with
#: the record because a reader who opens the file is the one who needs it.
SELECTION_HEADER = """\
# What the cases in this directory cover, and what they do not.
#
# A run over part of the plan and a run over all of it leave directories that
# look alike, so this is the only thing that tells them apart. Every skip states
# its reason, and a skip is never a pass.
"""


def cases_in(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every case document under a directory, with where it sits."""
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.rglob("*.yaml")):
        try:
            document = load_document(path)
        except InputError:
            continue  # not ours to read, and not ours to destroy either
        if isinstance(document, dict) and document.get("kind") == "case":
            found.append((path, document))
    return found


def covered_by(cases: list[dict[str, Any]], coverage: dict[str, Any]) -> set[str]:
    """The planned resources these cases test, read from the cases themselves.

    A module case lists the resources it was written for, and a layer can gain
    one after it was; the file name says only which layer.
    """
    found: set[str] = set()
    for case in cases:
        contract = case.get("contract")
        if contract == "resource":
            address = str((case.get("resource") or {}).get("terraform_address") or "")
            found.add(address.partition(".")[0])
        elif contract == "module":
            found |= {str(item).partition(".")[0] for item in case.get("resources") or []}
        elif contract == "composition":
            layers = set(case.get("layers") or ())
            found |= {r.terraform_type for r in resources_of(coverage) if r.layer in layers}
    return found - {""}


def derive_subject(
    contract: dict[str, Any],
    coverage: dict[str, Any],
    operations: dict[str, Operation],
    subject: str,
    case_name: str = "",
    graph: dict[str, dependency_graph.Derived] | None = None,
    updated_in_place: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> dict[str, Any]:
    """The case the current kit derives for a resource type, a layer or the blueprint.

    `instantiate`, `check` and `render` all call this, so they cannot disagree
    about what the current kit derives. `graph` holds each type's derived dependencies,
    and `updated_in_place` each type's fields that the provider updates in place.
    """
    kind = contract["contract"]
    if kind == "composition":
        return instantiate_composition(contract, module_layers(coverage))
    if kind == "module":
        if subject == NO_MODULE:
            raise InputError(f"{NO_MODULE!r} has no module: its types get resource cases only")
        if subject not in module_layers(coverage):
            raise InputError(f"the module plan has no buildable layer {subject!r}")
        return instantiate_module(contract, subject, resources_of(coverage, layers=(subject,)))
    found = [r for r in resources_of(coverage) if r.terraform_type == subject]
    if not found:
        raise InputError(f"the module plan has no resource {subject!r}")
    case = dependency_graph.placed(
        instantiate_resource(
            contract, found[0], operations=operations, updated_in_place=(updated_in_place or {}).get(subject, ())
        ),
        (graph or {}).get(subject),
    )
    return case_identity.further_case(case, case_name) if case_name else case


def derive_again(path: Path, case: dict[str, Any], read: dict[Any, Any]) -> dict[str, Any]:
    """What `instantiate` writes for this case now, from the documents it records.

    `read` keeps each coverage and its documents for the run: the cases of one
    directory record the same ones, and a document is read once.
    """
    try:
        inputs = case_refresh.recorded_inputs(path, case, Path.cwd())
        if inputs not in read:
            coverage = load_document(inputs[0])
            paths = list(inputs[1]) or spec_documents(coverage, inputs[0])
            documents = [load_document(p) for p in paths]
            read[inputs] = (
                coverage,
                index_operations(documents),
                dependency_graph.derived(documents, coverage, inputs[2]),
                in_place.derive(coverage, tuple(Path(p) for p in paths), *inputs[3:]),
            )
        coverage, operations, graph, updated = read[inputs]
        fresh = derive_subject(
            load_contract(str(case.get("contract"))),
            coverage,
            operations,
            *case_refresh.subject_of(case),
            graph=graph,
            updated_in_place=updated,
        )
    except (InputError, in_place.ReviewError) as error:
        raise case_refresh.RefreshError(f"the current kit cannot derive it again: {error}") from error
    return case_refresh.with_origin(fresh, case["derived_from"])


def stale(path: Path, case: dict[str, Any], read: dict[Any, Any]) -> list[str]:
    """Why the current kit would write this case differently, empty when it would not."""
    try:
        fields = load_contract(str(case.get("contract"))).get("case_fields") or ()
    except InputError:
        return []  # the contract check names a contract that does not exist
    return case_refresh.staleness(path, case, lambda p, c: derive_again(p, c, read), fields)


def selection_record(
    contract: str,
    coverage: dict[str, Any],
    selected: list[Resource],
) -> dict[str, Any]:
    """What this run covers, and what it does not.

    Without this a narrowed run and a full run produce the same directory of
    green cases, and a reader cannot tell one from the other. A skip is never a
    pass, and it has to say which kind of skip it was.

    Which kind is a property of the resource, not of the command line. A run
    that is narrowed to one requirement section must not report every resource
    it left behind as `not_buildable`. That reads as "the API cannot do this"
    when the truth is "this run did not ask for it".
    """
    everything = resources_of(coverage)
    buildable = set(buildable_layers(coverage))
    chosen = {r.terraform_type for r in selected}
    skipped = []
    for resource in everything:
        if resource.terraform_type in chosen:
            continue
        reason = "not_selected" if resource.layer in buildable else "not_buildable"
        skipped.append({"resource": resource.terraform_type, "layer": resource.layer, "reason": reason})
    return {
        "api_version": "terraform.ncp/v1",
        "kind": "selection",
        "contract": contract,
        "layers_selected": sorted({r.layer for r in selected}),
        "layers_skipped": sorted({r.layer for r in everything} - {r.layer for r in selected}),
        "layers_not_buildable": sorted({r.layer for r in everything} - buildable),
        "resources_selected": sorted(chosen),
        "resources_skipped": skipped,
    }


def spec_documents(coverage: dict[str, Any], coverage_path: Path) -> list[Path]:
    """The specifications this coverage was derived from, each the one it read."""
    try:
        return recorded_specs.documents(coverage, coverage_path)
    except recorded_specs.SpecError as error:
        raise InputError(str(error)) from error


def _refuse_an_unstated_scope(args: argparse.Namespace) -> None:
    """Whole-plan runs are chosen, not fallen into.

    A default scope would mean every resource in the plan, and nothing in
    the run would have to ask. The size of what a run instantiates is the
    operator's decision -- every case carries judged fields a person then
    fills by hand -- and a decision nobody is asked to make is a decision the
    tool made.

    `--all` is still one word. It records that the whole plan was meant.
    """
    if refusal := scope_flags.unread(args) or case_identity.scope_refusal(args.contract, args.resource, args.case_name):
        raise InputError(refusal)
    if args.all or args.layer or args.resource or args.section:
        return
    raise InputError(
        "no scope was given, so this run would instantiate the whole plan. Run "
        "`progress` first and show the operator what each section already has, "
        "then narrow with --section, --layer or --resource. Pass --all when the "
        "whole plan is what they chose."
    )


def _subjects(
    args: argparse.Namespace,
    coverage: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    found: list[tuple[Path, dict[str, Any]]],
) -> tuple[list[refresh_run.Subject], list[Resource]]:
    """What this run derives, and the planned resources it covers.

    A run over a type reaches each existing case of it, the named ones too:
    `check` refuses a stale named case, and no other run would refresh it.
    Only a resource has a requirement of its own to be filed under; a module
    case covers a whole layer and a composition every layer.
    """
    if args.contract == "composition":
        buildable = module_layers(coverage)
        return [("", "", "composition", ())], [r for r in resources_of(coverage) if r.layer in buildable]
    if args.contract == "module":
        chosen = resources_of(coverage, layers=tuple(args.layer or ()))
        layers = sorted({r.layer for r in chosen} & set(module_layers(coverage)))
        return [(layer, "", f"module-{layer}", ()) for layer in layers], [r for r in chosen if r.layer in layers]
    chosen = narrowed_to_sections(
        resources_of(coverage, layers=tuple(args.layer or ()), kinds=tuple(args.resource or ())),
        catalog,
        tuple(args.section or ()),
    )
    named = {case_refresh.subject_of(case) for _, case in found if case.get("contract") == "resource"}
    subjects: list[refresh_run.Subject] = []
    for resource in chosen:
        kind = resource.terraform_type
        names = [args.case_name] if args.case_name else ["", *sorted(name for of, name in named if of == kind and name)]
        subjects += [(kind, name, "_".join(filter(None, (kind, name))), filing(resource, catalog)) for name in names]
    return subjects, chosen


def _write_case(path: Path, case: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CASE_HEADER + yaml.safe_dump(case, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"{path}", file=sys.stderr)


def command_instantiate(args: argparse.Namespace) -> int:
    _refuse_an_unstated_scope(args)
    contract = load_contract(args.contract)
    coverage_path = Path(args.coverage)
    coverage = load_document(coverage_path)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    found = cases_in(out)
    present = covered_by([case for _, case in found], coverage)
    catalog = load_requirements(Path(args.requirements) if args.requirements else BUNDLED_REQUIREMENTS)
    named = [Path(s) for s in args.spec or ()]
    paths = named or spec_documents(coverage, coverage_path)
    documents = [load_document(path) for path in paths]
    operations = index_operations(documents)
    graph = dependency_graph.derived(documents, coverage, args.account_scope or "")
    updated = in_place.derive(coverage, tuple(Path(path) for path in paths), args.provider_schema, args.provider_data)
    record = case_refresh.origin(
        coverage_path, named, Path.cwd(), args.account_scope or "", args.provider_schema, args.provider_data
    )

    subjects, covered = _subjects(args, coverage, catalog, found)
    if not subjects:
        raise InputError("the selection matched no subject; nothing to instantiate")
    what = "module" if args.contract == "module" else "Terraform type"
    if unnamed := [why for subject, *_ in subjects if subject and (why := case_identity.name_refusal(what, subject))]:
        raise InputError("; ".join(unnamed))
    existing = {case_refresh.subject_of(case): path for path, case in found if case.get("contract") == args.contract}
    report = case_refresh.Report()
    built = refresh_run.derive_all(
        subjects,
        lambda subject, name: case_refresh.with_origin(
            derive_subject(contract, coverage, operations, subject, name, graph, updated), record
        ),
        existing,
        out,
        report,
        InputError,
    )
    fields = contract.get("case_fields") or ()
    if not args.overwrite and (written := refresh_run.unjudged(built, out, fields, load_document)):
        raise InputError(
            "the kit derives none of these fields and `judged` does not name them, so a refresh "
            "would drop the answers in them:\n  " + "\n  ".join(written) + "\nAdd each one to "
            "its case's `judged` list to keep it, or remove it to drop it. No case was written."
        )
    added = refresh_run.write_all(
        built,
        out,
        report,
        case_fields=fields,
        read=load_document,
        write=_write_case,
        check=lambda case: check_case(case, contract),
        overwrite=args.overwrite,
        backup_root=BACKUP_ROOT,
    )
    owed_report.report(
        added,
        out,
        left_out=_record_scope(args, coverage, covered, present, out),
        selection_file=SELECTION_FILE if args.contract == "resource" else "",
    )
    return 0


def _record_scope(
    args: argparse.Namespace,
    coverage: dict[str, Any],
    covered: list[Resource],
    present: set[str],
    out: Path,
) -> int:
    """Write what the directory leaves out, and return how many resources that is.

    Only a resource run can cover part of a plan. A module case lists the whole
    layer it covers and a composition case every buildable layer, so a record
    beside them would only repeat the case and list every other layer.
    """
    left_out = 0
    if args.contract == "resource":
        # The record describes the directory, not this run. A run that adds one
        # resource to a directory holding the rest would otherwise report the
        # rest as not selected.
        chosen_types = {r.terraform_type for r in covered}
        covered += [
            r for r in resources_of(coverage) if r.terraform_type not in chosen_types and r.terraform_type in present
        ]
        record = selection_record(args.contract, coverage, covered)
        selection_reason.with_answers(record["resources_skipped"], out / SELECTION_FILE)
        (out / SELECTION_FILE).write_text(
            SELECTION_HEADER + yaml.safe_dump(record, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        left_out = len(record["resources_skipped"])
    elif args.contract == "composition":
        planned = (coverage.get("module_plan") or {}).get("layers") or {}
        buildable = set(module_layers(coverage))
        unbuilt = [
            f"{name} ({'no module' if name == NO_MODULE else (layer or {}).get('verdict')})"
            for name, layer in sorted(planned.items())
            if name not in buildable
        ]
        if unbuilt:
            print(f"Not composed, with the verdict the coverage gives each: {', '.join(unbuilt)}.", file=sys.stderr)
    return left_out


#: What a requirement has behind it. Ordered worst first, so a report reads as
#: a queue rather than as a table somebody has to sort.
MISSING, TEST_ONLY, DONE = "missing", "test-only", "done"


def implemented(logical_kind: str, provider_files: tuple[str, ...]) -> bool:
    """Whether a provider directory holds a file that looks like this resource.

    Matched on letters and digits alone, in both names, because no generated
    provider exists yet to fix a filename convention and this must not invent
    one: `service_account_resource.go`, `resource_service_account.go` and
    `serviceaccount.go` all answer the same question.

    It is a reading of a filename, not a fact about the code. The report says
    so, and nothing decides anything on it.
    """
    wanted = re.sub(r"[^a-z0-9]+", "", logical_kind.lower())
    return bool(wanted) and any(wanted in name for name in provider_files)


def provider_file_names(directory: Path | None) -> tuple[str, ...]:
    if directory is None:
        return ()
    if not directory.is_dir():
        raise InputError(f"no such provider directory: {directory}")
    # The cases, the workspaces and the recordings live under the provider's
    # `tests/`, and a Go test sits beside its code. Each is named after the
    # resource it tests, so counting it would read a scaffolded resource as
    # built.
    tests = directory / "tests"
    return tuple(
        re.sub(r"[^a-z0-9]+", "", path.stem.lower())
        for path in directory.rglob("*")
        if path.is_file() and not path.is_relative_to(tests) and not path.name.endswith("_test.go")
    )


def scan_progress(
    cases: Path,
    catalog: dict[str, dict[str, Any]],
    *,
    provider: Path | None = None,
    unattributed: list[str] | None = None,
) -> list[dict[str, Any]]:
    """What each requirement has behind it, computed now and stored nowhere.

    Nothing is written, and nothing is remembered between runs. A status file
    is a second thing to keep true, and the first time it disagrees with the
    directory it is the directory that is right. Reading the directory every
    time cannot go stale.

    A requirement with no case is reported too. A scan that listed only what
    exists answers "what have I built" and never "what is left", and the second
    question is the one an operator opens this for.
    """
    # A directory that does not exist yet is the answer "nothing is built",
    # which is the state this scan is most useful in and the one a first run is
    # always in. Refusing there would send an operator to the skill to find out
    # why the question cannot be asked before any work has started.
    provider_files = provider_file_names(provider)

    covered: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(cases.rglob("*.yaml")):
        case = load_document(path)
        if not isinstance(case, dict) or case.get("kind") != "case":
            continue
        subject = case.get("resource") or {}
        kind = str(subject.get("logical_kind") or "")
        entry = {
            "logical_kind": kind,
            "case": str(path.relative_to(cases)),
            "status": DONE if implemented(kind, provider_files) else TEST_ONLY,
        }
        if subject.get("dependency_of"):
            # A dependency case exists for another case's requirement. Counting
            # it there would report the requirement covered by a resource that
            # only something else needs.
            continue
        serves = subject.get("requirements") or []
        if not serves and unattributed is not None:
            # A case that names no requirement counts towards nothing, and a
            # report of all-missing over a directory full of work reads as a
            # scan that failed rather than as a case that is short a field.
            unattributed.append(entry["case"])
        for req_id in serves:
            covered.setdefault(str(req_id), []).append(entry)

    rows: list[dict[str, Any]] = []
    for req_id, requirement in requirements_catalog.served(catalog).items():
        behind = covered.get(req_id) or []
        rows.append(
            {
                "req_id": req_id,
                "section": requirement.get("section", ""),
                "subsection": requirement.get("subsection", ""),
                "status": (MISSING if not behind else DONE if all(e["status"] == DONE for e in behind) else TEST_ONLY),
                "resources": [entry["logical_kind"] for entry in behind],
            }
        )
    return rows


def command_progress(args: argparse.Namespace) -> int:
    catalog = load_requirements(Path(args.requirements) if args.requirements else BUNDLED_REQUIREMENTS)
    provider = Path(args.provider_dir) if args.provider_dir else None
    cases = Path(args.cases)
    unattributed: list[str] = []
    rows = scan_progress(cases, catalog, provider=provider, unattributed=unattributed)

    # An empty answer is a true one before any work has started, and it is
    # indistinguishable from a mistyped path. Say which this is.
    if not cases.is_dir():
        print(f"{cases} does not exist yet, so nothing is built there.", file=sys.stderr)

    heading = ""
    for row in rows:
        here = f"{row['section']} / {row['subsection']}".rstrip(" /")
        if here != heading:
            heading = here
            print(f"\n{heading}")
        served = ", ".join(row["resources"])
        print(f"  {row['status']:<10} {row['req_id']:<8} {served}")

    tally = {name: 0 for name in (DONE, TEST_ONLY, MISSING)}
    for row in rows:
        tally[row["status"]] += 1
    print(
        f"\n{len(rows)} requirement(s): {tally[DONE]} done, {tally[TEST_ONLY]} test-only, {tally[MISSING]} missing.",
        file=sys.stderr,
    )
    if provider is None:
        print(
            "No provider directory was given, so nothing can be more than "
            "test-only here. Pass --provider-dir to have the scan look for a "
            "generated resource beside each case.",
            file=sys.stderr,
        )
    else:
        print(
            "`done` means a file under the provider directory is named after "
            "the resource. That is a reading of a filename, not a check that "
            "the code is right.",
            file=sys.stderr,
        )
    if unattributed:
        shown = ", ".join(sorted(unattributed)[:3])
        more = f", and {len(unattributed) - 3} more" if len(unattributed) > 3 else ""
        print(
            f"{len(unattributed)} case(s) name no requirement, so nothing above "
            f"counts them: {shown}{more}. Each one's `resource.requirements` is "
            f"empty; the module plan says what belongs there.",
            file=sys.stderr,
        )
    print(
        "Nothing was written. This is computed from the directories every time "
        "it runs, so it cannot disagree with them.",
        file=sys.stderr,
    )
    return 0


def command_check(args: argparse.Namespace) -> int:
    cases = sorted(Path(args.cases).rglob("*.yaml"))
    if not cases:
        raise InputError(f"no cases at {args.cases}")
    if bool(args.resource_cases) != bool(args.references):
        raise InputError(
            "--resource-cases and --references go together: the comparison "
            "needs the reviewed resource cases and the module's references.yaml"
        )
    compare = None
    if args.resource_cases:
        reviewed = module_drift.resource_fixtures(Path(args.resource_cases), load_document)
        review = load_document(Path(args.references)) or {}
        pluralize = module_drift.module_skill_pluralize()

        def compare(case: dict[str, Any]) -> list[str]:
            return module_drift.differences(
                case, reviewed, review, pluralize, note=lambda line: print(f"NOTE  {line}", file=sys.stderr)
            )

    failed = 0
    found: list[tuple[Path, dict[str, Any]]] = []
    export = fixture_values.read_export(args.provider_export, None)
    read: dict[Any, Any] = {}
    for path in cases:
        case = load_document(path)
        if isinstance(case, dict) and case.get("kind") == "selection":
            failed += _report(path.relative_to(args.cases), selection_reason.refusals(case))
            continue
        if not isinstance(case, dict) or case.get("kind") != "case":
            raise InputError(f"{path}: not a case; it declares no kind")
        broken = check_case(case, load_contract(str(case["contract"]))) + readiness_field.refusals(case, export)
        broken += replaced_update.refusals(case, export)
        broken += dependency_names.attribute_refusals(case, export, fixture_values.resource_block)
        for note in dependency_names.unchecked_paths(case, export):
            print(f"NOTE  {path.relative_to(args.cases)}: {note}", file=sys.stderr)
        broken += stale(path, case, read)
        if case.get("contract") == "module":
            if compare:
                broken += compare(case)
            else:
                # A comparison that skips itself in silence reads as one that passed.
                print(
                    f"NOTE  {path.relative_to(args.cases)}: not compared with the reviewed "
                    "resource cases; pass --resource-cases and --references",
                    file=sys.stderr,
                )
        # Relative to the directory asked about, not the bare name: a case is
        # filed under the requirement headings it serves, and the name alone
        # no longer says which part of the plan it belongs to.
        shown = path.relative_to(args.cases)
        found.append((shown, case))
        failed += _report(shown, broken)
    readiness_field.note_unchecked(found, export)
    clashes = case_identity.collisions(found)
    for clash in clashes:
        print(f"FAIL  {clash}")
    return 1 if failed or clashes else 0


def _report(shown: Path, broken: list[str]) -> int:
    """Print one document's result; 1 when it failed."""
    print(f"{'FAIL' if broken else 'PASS'}  {shown}")
    for reason in broken:
        print(f"        {reason}")
    return 1 if broken else 0


#: What renders today, and what each shape renders to. A shape absent here is
#: reported rather than skipped, so a run that rendered nothing says so.
RENDERABLE = {
    ("resource", "offline"),
    ("resource", "live"),
    ("resource", "replay"),
    ("module", "offline"),
    ("module", "live"),
    ("composition", "offline"),
    ("composition", "live"),
}

#: The one thing a generated check cannot know: how this NCP is called. The
#: provider supplies it, and every check goes through it rather than through the
#: provider under test.
INDEPENDENT_CHECK = """// The NCP call every independent check makes. Terraform's own view of a resource
// is what these tests put under test, so it cannot also be the evidence: each
// check here asks the NCP directly, through the read operation the module plan
// names.
//
// The provider supplies the reader. Nothing in this file knows how this NCP
// authenticates, and nothing in it calls the provider.

package provider_test

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
	"github.com/hashicorp/terraform-plugin-testing/terraform"
)

// IndependentReader reads one resource straight from the NCP API.
type IndependentReader interface {
	// Read returns the resource's own object as the read operation returns
	// it, outside any envelope the API puts around it, so a check can compare
	// each configured value with the NCP's.
	Read(ctx context.Context, operation, id string) (resource map[string]any, found bool, err error)
	// Gone answers the question the case asked about this resource's destroy.
	// An API that soft-deletes still answers the read, so "the read found
	// nothing" is only one of the answers a destroyed resource gives, and which
	// one this resource gives is what `expect` carries.
	Gone(ctx context.Context, operation, id, expect string) (gone bool, err error)
}

// Assigned by the provider's own test setup.
var independentReader IndependentReader

// runValues supplies what a case may not carry: something only the account
// knows -- a project, an organisation, a verified domain -- or the identifier of
// a resource that has to exist before this one. Each comes from
// TFKIT_VAR_<NAME>, and a run missing one stops before it creates anything,
// for the same reason a run with no id does.
//
// It names every missing variable, not the first. Stopping at the first makes
// an operator find the set one failed run at a time, and each of those runs
// costs the time of everything the case does before it.
func runValues(t *testing.T, names ...string) map[string]string {
	t.Helper()
	supplied := map[string]string{}
	missing := []string{}
	for _, name := range names {
		variable := "TFKIT_VAR_" + strings.ToUpper(name)
		value := os.Getenv(variable)
		if value == "" {
			missing = append(missing, variable)
			continue
		}
		supplied[name] = value
	}
	if len(missing) > 0 {
		t.Fatalf("unset: %s. This case needs every one and cannot invent any; "+
			"RUN_VARIABLES.md says what each one names and where it goes",
			strings.Join(missing, ", "))
	}
	return supplied
}

// runID names everything one run creates, so a later sweep can find exactly
// this run's resources and nothing else.
func runID(t *testing.T) string {
	t.Helper()
	if id := os.Getenv("TFKIT_RUN_ID"); id != "" {
		return id
	}
	t.Fatal("TFKIT_RUN_ID is unset; a run whose resources cannot be found must not create any")
	return ""
}

func stateID(state *terraform.State, address string) (string, error) {
	rs, ok := state.RootModule().Resources[address]
	if !ok {
		return "", fmt.Errorf("%s is not in state", address)
	}
	if rs.Primary.ID == "" {
		return "", fmt.Errorf("%s has no id, so the NCP cannot be asked about it", address)
	}
	return rs.Primary.ID, nil
}

// rememberID keeps the subject's id across the teardown step, after Terraform
// has removed it from state but while its setup resources still exist.
func rememberID(address string, saved *string) resource.TestCheckFunc {
	return func(state *terraform.State) error {
		id, err := stateID(state, address)
		if err == nil {
			*saved = id
		}
		return err
	}
}

// suppliedName refuses a supplied dependency value that is not a name. The
// test puts it where its case's reference holds the created resource's name,
// and a path there builds a reference to nothing, which the API refuses with
// an error that names neither the variable nor the cause.
func suppliedName(t *testing.T, variable, value string) string {
	t.Helper()
	if strings.Contains(value, "/") {
		t.Fatalf("%s holds a path: give the name of the existing resource; each test builds the form its API takes", variable)
	}
	return value
}

// suppliedForm refuses a supplied attribute value in another form than its
// field states. The test sends the value as it is, and the API answers another
// form with an error that names neither the variable nor the form.
func suppliedForm(t *testing.T, variable, value, form string) string {
	t.Helper()
	if !regexp.MustCompile(form).MatchString(value) {
		t.Fatalf("%s does not take the form %s that its field states: give the existing resource in that form, or leave it unset so the test creates one", variable, form)
	}
	return value
}

func chooseReference(supplied, fallback string) string {
	if supplied != "" {
		return supplied
	}
	return fallback
}

// checkSuppliedOrCreated holds an attribute to the value the run supplied or,
// when the test created the dependency itself, to the created resource's own
// attribute: a value the API assigned, which nothing could state beforehand.
func checkSuppliedOrCreated(address, key, supplied, created, attribute string) resource.TestCheckFunc {
	if supplied != "" {
		return resource.TestCheckResourceAttr(address, key, supplied)
	}
	return resource.TestCheckResourceAttrPair(address, key, created, attribute)
}

var _ = checkSuppliedOrCreated

// destroyedRead is what the destroy check reads: the destroyed resource
// itself, or another resource whose identifier is built from its own. Some
// resources show a destroy only on another one, as a key shows it on its
// versions.
func destroyedRead(template, id string) string {
	return strings.ReplaceAll(template, "{id}", id)
}

func checkGoneByID(address string, saved *string, operation, template, expect string) resource.TestCheckFunc {
	return func(_ *terraform.State) error {
		if *saved == "" {
			return fmt.Errorf("%s has no recorded id to verify its destroy", address)
		}
		read := destroyedRead(template, *saved)
		gone, err := independentReader.Gone(context.Background(), operation, read, expect)
		if err != nil {
			return fmt.Errorf("reading %s (%s) from the NCP: %w", address, read, err)
		}
		if !gone {
			return fmt.Errorf("Terraform destroyed %s, and the NCP does not report %q", address, expect)
		}
		return nil
	}
}

// checkGone asks the NCP whether the resource Terraform reported destroying is
// really gone. A delete that returns success and removes nothing fails here.
//
// What "gone" means is the case's answer, not this file's: some APIs stop
// answering the read, and some keep answering it with a marker.
func checkGone(address, operation, template, expect string) func(*terraform.State) error {
	return func(state *terraform.State) error {
		id, err := stateID(state, address)
		if err != nil {
			return nil // never created, so nothing to have leaked
		}
		read := destroyedRead(template, id)
		gone, err := independentReader.Gone(context.Background(), operation, read, expect)
		if err != nil {
			return fmt.Errorf("reading %s (%s) from the NCP: %w", address, read, err)
		}
		if !gone {
			return fmt.Errorf(
				"Terraform destroyed %s, and the NCP does not report %q", address, expect)
		}
		return nil
	}
}
""" + (
    fixture_values.CHECK_READ
    + fixture_values.READ_WALKER
    + lifecycle_checks.CHECK_READY
    + lifecycle_checks.READY_WALKER
)


REQUEST_CHECK = (
    r"""// What the provider would send, asserted without an account.
//
// The offline workspaces assert the plan: that the configuration and the schema
// agree. They say nothing about the request, so a resource can be configured
// correctly and still put a value in the wrong field -- and that is only seen by
// a run with credentials, which is the most expensive place to learn it.
//
// This answers every request with a refusal and asserts what arrived. It needs
// no account, no recording and no plausible response, because the claim is about
// the request and stops there.
//
// Generated. Edit the cases, not this file.

package provider_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/hashicorp/terraform-plugin-go/tfprotov6"
)

// RoundTrip answers one request. The provider's transport is substitutable so
// that requests can be observed; this is the observer.
type RoundTrip func(*http.Request) (*http.Response, error)

// requestProviderFactories is assigned by the provider's own test setup, which
// is the only thing that knows how this provider is constructed. Nothing in
// this file does, and the transport it hands over never authenticates.
var requestProviderFactories func(RoundTrip) map[string]func() (tfprotov6.ProviderServer, error)

// requestEnvironment is assigned by the same setup: the provider's own settings,
// as stubs, for the duration of one test. A provider that cannot resolve its
// project or region refuses before it builds anything, and the check would then
// report that no request was sent -- which is true and says nothing. What those
// settings are called is the provider's to know and not this file's.
var requestEnvironment func(t *testing.T)

// anyError matches whatever the provider reports when a request is refused.
// Which refusal it reports is the provider's business; that it sent the request
// is this file's.
var anyError = regexp.MustCompile(`(?s).`)

type capturedRequest struct {
	method  string
	path    string
	query   url.Values
	headers http.Header
	body    map[string]any
}

type requestObserver struct {
	mu       sync.Mutex
	captured []capturedRequest
}

// answer records the request and refuses it. A refusal rather than a plausible
// response is deliberate: inventing one means deciding what the API returns,
// which is the thing no offline test is allowed to know.
func (o *requestObserver) answer(request *http.Request) (*http.Response, error) {
	captured := capturedRequest{
		method:  request.Method,
		path:    request.URL.Path,
		query:   request.URL.Query(),
		headers: request.Header.Clone(),
	}
	if request.Body != nil {
		raw, err := io.ReadAll(request.Body)
		if err == nil && len(raw) > 0 {
			_ = json.Unmarshal(raw, &captured.body)
		}
	}
	o.mu.Lock()
	o.captured = append(o.captured, captured)
	o.mu.Unlock()

	refusal := `{"error":{"code":501,"status":"NOT_IMPLEMENTED",` +
		`"message":"tfkit request check: this check answers no request"}}`
	return &http.Response{
		StatusCode: http.StatusNotImplemented,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(bytes.NewReader([]byte(refusal))),
		Request:    request,
	}, nil
}

func (o *requestObserver) first() (capturedRequest, bool) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if len(o.captured) == 0 {
		return capturedRequest{}, false
	}
	return o.captured[0], true
}

// pathMatcher turns an operation's path template into what a real path has to
// look like. Each parameter stands for one segment, whatever the run put there.
func pathMatcher(template string) *regexp.Regexp {
	quoted := regexp.QuoteMeta(strings.Trim(template, "/"))
	segment := regexp.MustCompile(`\\\{[^}]*\\\}`)
	return regexp.MustCompile("^" + segment.ReplaceAllString(quoted, `[^/]+`) + "$")
}

// expectation is what the case says this operation's request must be.
type expectation struct {
	method         string
	path           string
	requiredFields []string
	// headers the provider itself sets. A header the credential layer adds --
	// an Authorization -- is not here: handing over a transport bypasses
	// authentication, so this check would report every one of them missing.
	headers []string
}

func checkRequest(t *testing.T, observed *requestObserver, want expectation) {
	t.Helper()
	got, sent := observed.first()
	if !sent {
		t.Fatalf("the provider sent no request at all; the case expects %s %s",
			want.method, want.path)
	}
	if got.method != want.method {
		t.Errorf("method = %s, the case says %s", got.method, want.method)
	}
	if matcher := pathMatcher(want.path); !matcher.MatchString(strings.Trim(got.path, "/")) {
		t.Errorf("path = %q, which is not the case's %q", got.path, want.path)
	}
	for _, header := range want.headers {
		if got.headers.Get(header) == "" {
			t.Errorf("the request carries no %s, which this operation needs. "+
				"Sent: %v", header, headerNames(got.headers))
		}
	}
	for _, field := range want.requiredFields {
		if !present(got.body, field) {
			t.Errorf("the body carries no %q, which the operation requires. Body: %s",
				field, render(got.body))
		}
	}
}

// present looks up a field the document names, including a nested one written
// with dots. A required field absent from the body is the defect this file
// exists for: the configuration was right and the value went somewhere else.
func present(body map[string]any, field string) bool {
	current := any(body)
	for _, part := range strings.Split(field, ".") {
		mapping, ok := current.(map[string]any)
		if !ok {
			return false
		}
		current, ok = mapping[part]
		if !ok {
			return false
		}
	}
	switch value := current.(type) {
	case nil:
		return false
	case string:
		return value != ""
	}
	return true
}

// stubRunID names what this check creates, which is nothing. A request check
// applies no resource, so it needs no unique id and no sweep to find it later.
const stubRunID = "tfkitcheck"

// stubValues stands in for what a live run supplies. Nothing is created here,
// so nothing has to exist first -- and requiring the live variables would make
// an offline check need a live run's setup. Which field a value lands in is what
// this asserts; which value it is, is not.
func stubValues(names ...string) map[string]string {
	supplied := map[string]string{}
	for _, name := range names {
		supplied[name] = "tfkit-stub-" + strings.ReplaceAll(name, "_", "-")
	}
	return supplied
}

// checkValues asserts that every value the configuration set arrives in the
// request, in the field whose name corresponds to it.
//
// The correspondence is found among the keys that actually arrived, never
// generated from the configuration's spelling. Generating it would be the
// reversal the kit forbids -- `oidc` and `id` do not survive one -- and a test
// that reversed would assert its own guess rather than the document's answer.
func checkValues(t *testing.T, observed *requestObserver, want map[string]string) {
	t.Helper()
	got, sent := observed.first()
	if !sent {
		return // checkRequest has already said so
	}
	for _, name := range sorted(want) {
		// An argument reaches the request in one of three places, and which one
		// is the document's choice: a field in the body, a query parameter, or a
		// segment of the path. Looking in only one of them reports a value as
		// missing when it arrived somewhere else.
		key, ambiguous := corresponding(got.body, name)
		if ambiguous {
			t.Errorf("more than one field in the body corresponds to %q, so which one "+
				"carries it cannot be decided here. Body: %s", name, render(got.body))
			continue
		}
		if key != "" {
			if arrived := scalar(got.body[key]); arrived != want[name] {
				t.Errorf("the body's %q is %q; the configuration set %q",
					key, arrived, want[name])
			}
			continue
		}
		if parameter := correspondingQuery(got.query, name); parameter != "" {
			if arrived := got.query.Get(parameter); arrived != want[name] {
				t.Errorf("the query's %q is %q; the configuration set %q",
					parameter, arrived, want[name])
			}
			continue
		}
		// The path names its segments the document's way, not the
		// configuration's, so there is no name to correspond -- only the value.
		if strings.Contains(got.path, want[name]) {
			continue
		}
		t.Errorf("the configuration sets %q to %q, and it is in no field, no query "+
			"parameter and not in the path. Path: %s Body: %s",
			name, want[name], got.path, render(got.body))
	}
}

// corresponding finds the body field a configuration argument became. Terraform
// takes lowercase and underscores; a document writes what it likes. Comparing
// the two with both removed matches them without either one being derived from
// the other.
func corresponding(body map[string]any, name string) (key string, ambiguous bool) {
	wanted := flatten(name)
	for candidate := range body {
		if flatten(candidate) != wanted {
			continue
		}
		if key != "" {
			return "", true
		}
		key = candidate
	}
	return key, false
}

func headerNames(headers http.Header) []string {
	names := make([]string, 0, len(headers))
	for name := range headers {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func correspondingQuery(query url.Values, name string) string {
	wanted := flatten(name)
	for candidate := range query {
		if flatten(candidate) == wanted {
			return candidate
		}
	}
	return ""
}

func scalar(value any) string {
	if value == nil {
		return ""
	}
	return fmt.Sprintf("%v", value)
}

func sorted(values map[string]string) []string {
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func render(body map[string]any) string {
	if body == nil {
		return "(no JSON body)"
	}
	text, err := json.Marshal(body)
	if err != nil {
		return fmt.Sprintf("%v", body)
	}
	return string(text)
}
"""
    + offline_values.CHECK_NESTED
    + offline_values.REQUEST_WALKER
    + (read_first.SKIP_WHEN_THE_CREATE_READS_FIRST)
)


def reached(lifecycle: list[str], mode: dict[str, Any]) -> list[str]:
    """The part of a case's lifecycle a run in this mode reaches.

    Cutting here rather than when the case was written is the whole point: the
    same case runs offline today and live tomorrow, and nothing is regenerated
    to widen it.
    """
    stop = mode.get("stops_before")
    if not stop or stop not in lifecycle:
        return list(lifecycle)
    return lifecycle[: lifecycle.index(stop)]


def _render(
    case: dict[str, Any],
    shape: tuple[str, str],
    out: Path,
    args: argparse.Namespace,
    mode: dict[str, Any],
    export: dict[str, Any],
) -> tuple[Path, dict[str, str]]:
    """Where one case's rendered files go, and what they are.

    The resource contract needs a root module of its own, so it gets a directory.
    The other two are run from the thing they test -- `terraform test` treats a
    module's own directory as the root -- so they write one file where they are
    pointed.
    """
    contract, name = shape
    case = {**case, "lifecycle": reached(list(case.get("lifecycle") or []), mode)}
    if contract == "module":
        return out, {f"{case.get('module')}_{name}.tftest.hcl": render_module_test(case)}
    if contract == "composition":
        return out, {f"blueprint_{name}.tftest.hcl": render_composition_test(case)}

    # Every name comes from the case id, so two cases of one type share no file.
    kind = case_identity.case_id(case)
    if name == "replay":
        recorded = replay_render.cassettes(case, Path(args.cassettes))
        return out, {
            f"replay_{kind}_test.go": replay_render.render_test(
                case,
                recorded,
                go_name=case_identity.go_case_name(case),
                config_name=_config_name,
                values=case_values(case),
                steps=lifecycle_checks.steps(list(case.get("lifecycle") or [])),
                forms="".join(
                    supplied_dependency.replay_line(entry, created_name(entry, case), _formatted)
                    for entry in sorted(optional_dependencies(case), key=lambda e: e["variable"])
                ),
            ),
            f"config_{kind}_test.go": render_config_functions(
                case, source=args.provider_source, version=args.provider_version
            ),
            "request_check_test.go": REQUEST_CHECK,
            "replay_check_test.go": replay_render.REPLAYER,
        }
    if name == "live":
        return out, {
            f"resource_{kind}_test.go": render_live_test(
                case,
                source=args.provider_source,
                version=args.provider_version,
                schema=fixture_values.resource_block(export, case_identity.terraform_type(case)),
            ),
            f"config_{kind}_test.go": render_config_functions(
                case, source=args.provider_source, version=args.provider_version
            ),
            "independent_check_test.go": INDEPENDENT_CHECK,
        }
    workspace = render_workspace(case, source=args.provider_source, version=args.provider_version)
    # Named for the case, as a module test is named for its module. Every
    # workspace called this file the same thing, so a run over all of them
    # reported results that named no subject, and nothing could attribute one.
    workspace[f"{kind}_{name}.tftest.hcl"] = render_offline_test(case)

    # The workspace is a Terraform root module and gets a directory. The request
    # check is Go, so it belongs to the package the other tests are in -- a
    # `.go` file inside a workspace would be a package of its own, compiled by
    # nothing.
    files = {f"{kind}/{leaf}": content for leaf, content in workspace.items()}
    if create_expectation(case):
        files[f"request_{kind}_test.go"] = render_request_test(
            case, source=args.provider_source, version=args.provider_version
        )
        files[f"config_{kind}_test.go"] = render_config_functions(
            case, source=args.provider_source, version=args.provider_version
        )
        files["request_check_test.go"] = REQUEST_CHECK
    return out, files


def replay_unavailable(cases: list[dict[str, Any]], cassettes: Path | None) -> str | None:
    """Why replay cannot run yet, or nothing when it can.

    Replay needs recordings, and a recording is made by running live once
    against a working provider. Reporting that here keeps the absence where it
    belongs: the cases are written and correct, and only the transport is
    missing. Nothing about them has to change when the recordings arrive.
    """
    if cassettes is None:
        return "no --cassettes directory was given"
    if not cassettes.is_dir():
        return f"there is no directory at {cassettes}"
    wanted = {str(entry.get("cassette")) for case in cases for entry in case.get("expected_requests") or []}
    if not any(_cassette_exists(cassettes, name) for name in wanted):
        return f"{cassettes} holds no recording for any of the {len(wanted)} operations these cases assert"
    return None


def supplied_rows(
    cases: list[dict[str, Any]],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Every value a live run has to supply, and the cases that need none.

    One derivation, because the table an operator reads and the file an
    operator copies must not be able to disagree about the set.
    """
    rows: list[tuple[str, str, str]] = []
    none_needed: list[str] = []
    for case in cases:
        kind = case_identity.case_id(case)
        if not kind:
            continue
        supplied = required_case_values(case)
        if not supplied:
            none_needed.append(kind)
            continue
        for name in supplied:
            where = placeholder_where(case, name)
            goes = f"`{where[0]}`, as `{where[1]}`" if where else "the fixture"
            rows.append((kind, f"TFKIT_VAR_{name.upper()}", goes))
    return rows, none_needed


def optional_rows(cases: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for case in cases:
        kind = case_identity.case_id(case)
        for entry in optional_dependencies(case):
            rows.append(
                (
                    kind,
                    f"TFKIT_VAR_{entry['variable'].upper()}",
                    f"Set, the name of an existing `{entry['resource']}`; each test builds "
                    "the reference its own API takes. "
                    f"Unset creates `{entry['resource']}` from its reviewed case."
                    if supplied_dependency.builds_reference(entry, created_name(entry, case))
                    else f"Unset creates `{entry['resource']}` from its reviewed case."
                    if not entry.get("attribute")
                    else f"Set, the `{entry['attribute']}` of an existing `{entry['resource']}`. "
                    f"Unset creates one from its reviewed case and uses its `{entry['attribute']}`.",
                )
            )
    return rows


RUNNER_SUBDIR = runner_parts.RUNNER_SUBDIR
RUNNER_NAME = runner_parts.RUNNER_NAME


def _comment(text: str) -> list[str]:
    """Each line of the text with the comment mark, so that no line of case
    text is read as a command when the operator's runner sources `.env`."""
    return [f"# {line}" for line in text.splitlines() or [""]]


def render_env_example(cases: list[dict[str, Any]], provider_lines: list[str] | None = None, mirror: str = "") -> str:
    """The one file an operator copies before a run.

    `RUN_VARIABLES.md` already says what each value names; it is a document to
    read. This is the same set as a file to fill in, so that the reader types
    values instead of assembling exports. Copied unchanged it is a working
    offline run, because the offline tests read none of these values. The
    difference between the two modes is then what the reader typed, not which
    document the reader found.
    """
    provider_lines = provider_lines or []
    rows, none_needed = supplied_rows(cases)
    optional = optional_rows(cases)
    by_variable: dict[str, list[tuple[str, str]]] = {}
    for kind, variable, goes in sorted(rows):
        by_variable.setdefault(variable, []).append((kind, goes))

    lines = [
        "# What a run supplies, as a file to copy.",
        "#",
        "#   cp .env.example .env",
        f"#   ./{RUNNER_SUBDIR}/{RUNNER_NAME} offline",
        "#",
        "# An offline run reads no value in this file. It needs no account and no",
        "# credentials. Copy the file without a change and the offline run is green.",
        "#",
        "# A live run is this same file with real values in it. Each value names",
        "# something the cases cannot carry: something only the account knows, or a",
        "# resource that must exist before the case runs. A run without one stops",
        "# before it creates anything. That is deliberate. A guessed value builds the",
        "# resource against the wrong parent.",
        "#",
        "# Copy this file to .env, which git ignores. Keep real ids out of the",
        "# repository, and put no value in this file.",
        "#",
        "# Generated with the cases. An edit here changes nothing.",
        "",
        *runner_parts.RUN_ID_LINES,
    ]
    if mirror:
        lines += runner_parts.mirror_lines(
            mirror, f"`{RUNNER_SUBDIR}/{RUNNER_NAME} build` writes it, and the layer modules and the blueprint read it."
        )

    lines += provider_lines

    lines.append("# ---- what the cases need ----")
    lines.append("#")
    if by_variable:
        lines += [
            "# RUN_VARIABLES.md is the same set as a table, with the case each value",
            "# belongs to.",
            "",
        ]
        for variable, places in sorted(by_variable.items()):
            for kind, goes in places:
                lines += _comment(f"{kind}: goes into {goes.replace('`', '')}")
            lines.append(f"{variable}=")
            lines.append("")
    else:
        lines += ["# No case needs a supplied value.", ""]

    if optional:
        lines += ["# ---- optional pre-created dependencies ----", "#"]
        for variable in sorted({row[1] for row in optional}):
            for kind, named, goes in optional:
                if named == variable:
                    lines += _comment(f"{kind}: {goes}")
            lines += [f"{variable}=", ""]

    if none_needed:
        lines += [
            "# Needing none: " + ", ".join(sorted(set(none_needed))) + ".",
            "",
        ]
    return "\n".join(lines)


def render_run_variables(cases: list[dict[str, Any]]) -> str:
    """The whole set of values a live run has to supply, as one document.

    Each place that knows part of the set says only part of it: a variable
    declared with no description, a Go call naming two of them, and a README
    example carrying whichever the generating run used. Without a list, an
    operator finds the set by running the tests and reading what refuses. A
    list is cheap to generate and nobody has to fail to read it.
    """
    rows, none_needed = supplied_rows(cases)
    optional = optional_rows(cases)

    lines = [
        "# What a live run has to supply",
        "",
        "Required values name account settings or external resources the tests",
        "cannot create. Optional dependency values name pre-created resources,",
        "or give the value a row states; when omitted, the test creates them",
        "from their own reviewed cases.",
        "",
        "A run missing a required value stops before it creates anything.",
        "",
        "The offline tests need none of these. They are read by the live Go tests,",
        "which also need `TF_ACC=1` and `TFKIT_RUN_ID`.",
        "",
        "This file is generated with the cases. Editing it changes nothing.",
        "",
    ]
    if rows:
        lines += [
            "| case | variable | goes into |",
            "|---|---|---|",
            *(f"| `{kind}` | `{variable}` | {goes} |" for kind, variable, goes in sorted(rows)),
            "",
        ]
    if optional:
        lines += [
            "Optional pre-created dependencies:",
            "",
            "| case | variable | when unset |",
            "|---|---|---|",
            *(f"| `{kind}` | `{variable}` | {goes} |" for kind, variable, goes in sorted(optional)),
            "",
        ]
    external = [
        (case_identity.case_id(case), str(entry.get("reason") or ""))
        for case in cases
        for entry in case.get("dependencies") or []
        if entry.get("required") is True
    ]
    for kind, reason in external:
        lines += [f"`{kind}`: {reason}", ""]
    if none_needed:
        lines += [
            "Needing none: " + ", ".join(f"`{kind}`" for kind in sorted(set(none_needed))) + ".",
            "",
        ]
    return "\n".join(lines)


#: The first version a constraint names. A filesystem mirror holds an exact
#: version, and a constraint is what a configuration carries, so one is read out
#: of the other rather than asked for twice.
VERSION_IN = re.compile(r"\d+(?:\.\d+)*")


def build_version(args: argparse.Namespace) -> str:
    """Which version the entry point builds into its mirror."""
    if args.provider_build_version:
        return str(args.provider_build_version)
    found = VERSION_IN.search(str(args.provider_version or ""))
    if not found:
        raise InputError(
            f"--provider-version {args.provider_version!r} names no version, so the "
            "entry point cannot lay out a mirror. Give --provider-build-version."
        )
    return found.group(0)


def _write_runner(
    runner_out: Path, out: Path, args: argparse.Namespace, cases: list[dict[str, Any]]
) -> tuple[list[str], str]:
    """The provider's entry point, the file it reads, and the README section that runs it.

    The artifact directory is given rather than assumed. The layer modules and
    the blueprint each get their own, from `suite_runner`, and read the
    provider from the mirror this one's `build` writes.
    """
    mirror = runner_parts.relative_mirror(args.provider_mirror, runner_out)
    export = Path(args.provider_export) if args.provider_export else (runner_out / provider_settings.EXPORT_NAME)
    example = runner_out / ".env.example"
    if args.mode != "replay":  # its cases miss each one without recordings
        example.write_text(
            render_env_example(cases, provider_settings.section(export), mirror).rstrip("\n") + "\n",
            encoding="utf-8",
        )
    script = runner_out / RUNNER_SUBDIR / RUNNER_NAME
    script.parent.mkdir(parents=True, exist_ok=True)
    workspaces = os.path.relpath(out, runner_out)
    sweeper = runner_parts.relative_sweeper(args.sweeper, runner_out)
    script.write_text(
        provider_runner.render_runner(
            workspaces=workspaces,
            source=args.provider_source,
            version=build_version(args),
            mirror=mirror,
            sweeper=sweeper,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    readme = provider_readme.write(runner_out, workspaces=workspaces, mirror=mirror, sweeper=sweeper)
    step = run_next_step.text(
        runner_out, script, out / "RUN_VARIABLES.md", export, supplied_rows(cases)[0], optional_rows(cases)
    )
    written = [str(script), str(readme)]
    return ([str(example)] if args.mode != "replay" else []) + written, step


def _refuse_an_unrunnable_layout(args: argparse.Namespace, loaded: list[Any]) -> None:
    """Refuse, before anything is written, an entry point that could not run."""
    contracts = {case.get("contract") for case in loaded if isinstance(case, dict) and case.get("kind") == "case"}
    if not args.provider_mirror:
        raise InputError(
            "--runner-out needs --provider-mirror: the filesystem mirror the "
            "provider's `build` writes and each module's tests read. Ask the "
            "operator where it goes."
        )
    build_version(args)
    # The export the render will read: the one named, else the provider's own.
    export = (
        Path(args.provider_export) if args.provider_export else (Path(args.runner_out) / provider_settings.EXPORT_NAME)
    )
    if args.provider_export or export.exists():
        try:
            document = json.loads(export.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise InputError(f"{export} is not a readable JSON file: {error}") from None
        # The same reading the render does later, so a shape it cannot read is
        # refused here and not after the workspaces are written.
        try:
            provider_settings.settings(document)
        except (AttributeError, TypeError) as error:
            raise InputError(f"{export} is not a schema export: {error}") from None
    if "resource" in contracts and contracts - {"resource"}:
        raise InputError(
            "these cases hold the provider's and a module's. Each directory gets its "
            "own entry point, so render each subject's cases on their own."
        )
    if contracts - {"resource"}:
        runner_out, out = Path(args.runner_out), Path(args.out)
        if Path(os.path.relpath(out.absolute(), runner_out.absolute())).parts[:1] == ("..",):
            raise InputError(
                f"--out {out} is outside --runner-out {runner_out}. A module's tests "
                "run from the module, so its entry point goes in the module's own "
                "directory."
            )
        if not args.provider_export:
            raise InputError(
                "a module's .env configures the provider, so its example lists the "
                "provider's settings. Give --provider-export, the provider's "
                f"{provider_settings.EXPORT_NAME}."
            )


def _write_suite_runner(
    runner_out: Path, out: Path, args: argparse.Namespace, modules: list[tuple[Path, dict[str, Any]]]
) -> list[str]:
    """A layer module's or the blueprint's own entry point, in its own directory,
    and a module's README section."""
    written = suite_runner.write(
        runner_out,
        out,
        source=args.provider_source,
        version=build_version(args),
        mirror=args.provider_mirror,
        export=Path(args.provider_export),
        sweeper=args.sweeper,
    )
    if modules:  # the cases of one module directory test one layer
        path, case = modules[0]
        gap = module_readme.layer_gap(path, case, Path.cwd())
        written.append(str(module_readme.write(runner_out, out, Path(args.cases), gap)))
    return written


def _refuse_stale(cases: list[Path], loaded: list[Any]) -> None:
    """Refuse, before anything is written, a render from a case that an older kit derived.

    Stopped rather than skipped: a skipped case keeps the tests of its last
    render on disk, and the offline entry point runs them and passes.
    """
    read: dict[Any, Any] = {}
    if found := refresh_run.stale_cases(cases, loaded, lambda path, case: stale(path, case, read)):
        raise InputError(
            f"{len(found)} case(s) are not what the current kit derives, so no test "
            "is rendered:\n  " + "\n  ".join(found)
        )


def command_render(args: argparse.Namespace) -> int:
    cases = sorted(Path(args.cases).rglob("*.yaml"))
    if not cases:
        raise InputError(f"no cases at {args.cases}")
    out = Path(args.out)
    written: list[str] = []
    refused: list[str] = []
    unkept: list[str] = []
    supplying: list[dict[str, Any]] = []
    live_modules: list[dict[str, Any]] = []
    modules: list[tuple[Path, dict[str, Any]]] = []
    suite_written = False
    # Two cases rendering to one path would leave whichever ran last, silently.
    claimed: dict[str, str] = {}
    claimed_by: dict[str, str] = {}

    loaded = [load_document(path) for path in cases]
    _refuse_stale(cases, loaded)
    if args.runner_out:
        _refuse_an_unrunnable_layout(args, loaded)
    if clashes := case_identity.collisions(list(zip(cases, loaded))):
        raise InputError("; ".join(clashes))
    # A dependency on a type resolves to its unnamed case.
    catalog = {
        case_identity.terraform_type(case): case
        for case in loaded
        if isinstance(case, dict) and case.get("contract") == "resource" and not case_identity.case_name(case)
    }
    resolved = {
        str(path): resolved_dependencies(case, catalog)
        for path, case in zip(cases, loaded)
        if isinstance(case, dict) and case.get("kind") == "case" and case.get("dependencies")
    }
    if clashes := dependency_names.shared(
        [
            (
                case_identity.case_id(case),
                [(entry, created_name(entry, case)) for entry in resolved[str(path)] if entry.get("case")],
            )
            for path, case in zip(cases, loaded)
            if str(path) in resolved
        ]
    ):
        raise InputError(
            "two cases would create one dependency resource, so no test is rendered:\n  " + "\n  ".join(clashes)
        )
    mode = load_mode(args.mode)
    export = fixture_values.read_export(args.provider_export, args.runner_out)
    if args.mode == "replay":
        why = replay_unavailable(
            [c for c in loaded if isinstance(c, dict) and c.get("kind") == "case"],
            Path(args.cassettes) if args.cassettes else None,
        )
        if why:
            raise InputError(
                f"replay is not available yet: {why}. Record them with the "
                "ncp-tf-cassette skill: it sends each request of a case to the NCP's "
                "API directly, never through the provider -- one taken through the "
                "provider records the provider rather than the API. It needs an "
                "account once, and the provider's schema export, which marks each "
                "field that holds a secret. The cases need no change when the "
                "recordings arrive."
            )

    for path, case in zip(cases, loaded):
        if not isinstance(case, dict) or case.get("kind") != "case":
            continue
        if case.get("dependencies"):
            case = {**case, "_resolved_dependencies": resolved[str(path)]}
        shape = (str(case.get("contract")), args.mode)
        if shape not in RENDERABLE:
            refused.append(f"{path.name}: {shape[0]}/{shape[1]} has no renderer yet")
            continue
        if args.mode == "live" and (unguarded := header_switch.refusals(case, export)):
            # Skipped, the case would leave a live run that never sends the header.
            raise InputError(f"{path.name}: {unguarded[0]}")
        broken = check_case(case, load_contract(shape[0])) + readiness_field.refusals(case, export)
        broken += replaced_update.refusals(case, export)
        broken += dependency_names.attribute_refusals(case, export, fixture_values.resource_block)
        for note in dependency_names.unchecked_paths(case, export):
            print(f"NOTE  {path.name}: {note}", file=sys.stderr)
        if broken:
            refused.append(f"{path.name}: not ready to render -- {broken[0]}")
            continue
        if shape == ("resource", "replay"):
            recorded = replay_render.cassettes(case, Path(args.cassettes))
            absent = replay_render.missing(case, recorded)
            if needed := [verb for verb in replay_render.REQUIRED if verb in absent]:
                refused.append(f"{path.name}: no replay test -- no_cassette for {', '.join(needed)}")
                continue
            refused += [
                f"{path.name}: the {verb} step -- no_cassette" for verb in replay_render.dropped_steps(case, recorded)
            ]
            unkept += replay_fields.unkept(
                path.name, recorded, fixture_values.resource_block(export, case_identity.terraform_type(case))
            )

        destination, files = _render(case, shape, out, args, mode, export)
        if shape[0] == "resource":
            supplying.append(case)
        else:
            suite_written = True
        if shape[0] == "module":
            modules.append((path, case))
        if shape in (("module", "live"), ("composition", "live")):
            live_modules.append(case)
        destination.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if str(path) in claimed and content != claimed[str(path)]:
                raise InputError(
                    f"{path} would be written twice with different content, by "
                    f"{claimed_by[str(path)]} and {case.get('document', path.name)}. "
                    "Render each subject into the directory that holds it."
                )
            claimed[str(path)] = content
            claimed_by[str(path)] = str(case.get("document") or path.name)
            path.write_text(content, encoding="utf-8")
        written.append(str(destination))
    if args.mode == "live" and any(header_switch.calls(case) for case in supplying):
        # The provider's lint refuses a function that no test calls.
        (out / "independent_check_test.go").write_text(INDEPENDENT_CHECK + header_switch.GUARD, encoding="utf-8")

    for line in refused:
        print(f"skipped: {line}", file=sys.stderr)
    if args.mode == "replay" and not export:
        print("no provider schema export, so no recorded read was compared with the schema", file=sys.stderr)
    for line in unkept:
        print(f"not kept: {line}", file=sys.stderr)
    for line in written:
        print(line, file=sys.stderr)
    if not written:
        raise InputError("nothing was rendered; every case was skipped")
    # A replay render skips each case without recordings, so only an offline or
    # a live render holds every value that a live run supplies.
    if supplying and args.mode != "replay":
        listed = out / "RUN_VARIABLES.md"
        listed.write_text(render_run_variables(supplying), encoding="utf-8")
        print(f"{listed}", file=sys.stderr)
    next_step = ""
    if supplying and args.runner_out:
        runner_files, next_step = _write_runner(Path(args.runner_out), out, args, supplying)
        for line in runner_files:
            print(line, file=sys.stderr)
    module_rows = suite_values.module_rows(live_modules, supplied_values, placeholder_context)
    if module_rows:
        listed = out / "RUN_VARIABLES.md"
        listed.write_text(suite_values.render_module_run_variables(module_rows), encoding="utf-8")
        print(f"{listed}", file=sys.stderr)
    elif live_modules and not supplying:
        # A list left from an earlier render asks for a value no test reads now.
        (out / "RUN_VARIABLES.md").unlink(missing_ok=True)
    if suite_written and args.runner_out and args.mode in ("offline", "live"):
        for line in _write_suite_runner(Path(args.runner_out), out, args, modules):
            print(line, file=sys.stderr)
    print(
        f"\n{len(written)} workspaces written. Run them with `terraform test` in each.",
        file=sys.stderr,
    )
    if next_step:
        print(next_step, file=sys.stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="case_builder", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    child = subparsers.add_parser("instantiate", help="emit cases from a contract and a module plan")
    child.add_argument("--contract", required=True, help="what is under test: resource, module or composition")
    child.add_argument("--coverage", required=True, help="path to coverage.yaml")
    child.add_argument("--out", required=True, help="directory to write cases into")
    child.add_argument(
        "--spec",
        action="append",
        help="a pinned specification; read from the coverage when not given",
    )
    child.add_argument("--account-scope", help="the provider's, as 'projects/{project}'; no dependency")
    child.add_argument(
        "--all",
        action="store_true",
        help="instantiate the whole plan; required when no other scope is named",
    )
    child.add_argument("--layer", action="append", help="restrict to a layer; repeatable")
    child.add_argument("--resource", action="append", help="restrict to a resource; repeatable")
    child.add_argument(
        "--section",
        action="append",
        help=("restrict to a requirement heading, matched as a substring of the section or the subsection; repeatable"),
    )
    child.add_argument(
        "--requirements",
        help="requirement catalog (default: the copy the coverage skill ships)",
    )
    child.add_argument(
        "--overwrite",
        action="store_true",
        help="write each selected case again without carrying its judged answers; the backup and the report keep them",
    )
    child.add_argument(
        "--case-name",
        help="write a further, named case of the one --resource, as <type>_<name>.yaml",
    )
    child.add_argument(
        "--provider-schema",
        type=Path,
        help="the provider's reviewed provider-schema.json, whose review answers say which fields replace the object",
    )
    child.add_argument(
        "--provider-data",
        type=Path,
        help="the provider's per-resource data, "
        "whose `update_mappings` name the fields the update sends at another path",
    )

    child = subparsers.add_parser("check", help="check filled cases against their contracts")
    child.add_argument("--cases", required=True, help="directory holding the cases")
    child.add_argument(
        "--resource-cases",
        help="the provider's reviewed resource cases, to compare a module case's values with",
    )
    child.add_argument("--references", help="the module's references.yaml, with --resource-cases")
    child.add_argument("--provider-export", help="the provider's provider-schema-export.json")

    child = subparsers.add_parser("progress", help="what each requirement has behind it, scanned now")
    child.add_argument("--cases", required=True, help="directory holding the cases")
    child.add_argument(
        "--provider-dir",
        help="a generated provider's tree; without it nothing reads as done",
    )
    child.add_argument(
        "--requirements",
        help="requirement catalog (default: the copy the coverage skill ships)",
    )

    child = subparsers.add_parser("render", help="turn filled cases into runnable tests")
    child.add_argument("--cases", required=True, help="directory holding the filled cases")
    child.add_argument("--out", required=True, help="directory to write workspaces into")
    child.add_argument(
        "--mode",
        required=True,
        help="how far the rendered run goes: offline (plan and stop), replay (a recording), live (the NCP)",
    )
    child.add_argument(
        "--cassettes",
        help="directory of recorded exchanges; replay needs it, and refuses when it holds none",
    )
    child.add_argument(
        "--provider-source",
        required=True,
        help="the provider's source address, as the target repository states it",
    )
    child.add_argument("--provider-version", default=">= 0.1.0", help="its version constraint")
    child.add_argument(
        "--runner-out",
        help="the artifact's own directory, to carry the entry point and .env.example; omitted, neither is written",
    )
    child.add_argument(
        "--provider-mirror",
        help="the filesystem mirror the provider's build goes into; with --runner-out, "
        "written into the entry point relative to that directory",
    )
    child.add_argument(
        "--provider-export",
        help="the provider's provider-schema-export.json, for its settings; a provider "
        "render reads the one in --runner-out",
    )
    child.add_argument(
        "--sweeper",
        help="the directory of the rendered ncp-tf-sweeper; with --runner-out, each live "
        "run ends with its listing of what the run left",
    )
    child.add_argument(
        "--provider-build-version",
        help="the version the entry point builds into its mirror; taken from --provider-version when it names one",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "instantiate":
            return command_instantiate(args)
        if args.command == "render":
            return command_render(args)
        if args.command == "progress":
            return command_progress(args)
        return command_check(args)
    except (InputError, in_place.ReviewError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
