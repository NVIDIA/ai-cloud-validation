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
"""The sweep plan: how to list and delete each planned type, derived from the documents.

Everything here is read from what the operator already reviewed: the module
plan names each type and its create and delete operations, the specification
documents that the coverage records hold the list operation and its
parameters, the reviewed resource cases give each parameter its value, and the
account scope that the operator gave the provider fills a parameter that names
the account. No value is guessed. A parameter that nothing gives stops the
render, because a sweeper that lists in the wrong scope finds nothing and reads
as clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import attribute_names
import run_marker


class PlanError(ValueError):
    """The sweep plan cannot be derived as the documents stand."""


#: Why a type is left out of a listing or a delete. Closed, so a reader can act
#: on each one.
NO_LIST = "no_list_operation"
NO_DELETE = "no_delete_operation"
#: Why a type is not listed: nothing in its resources says which run made them.
NO_RUN_ID = "carries_no_run_id"
#: Why a type is not listed: several documents define its create, so nothing
#: says which API's list returns its resources.
CREATE_IN_SEVERAL = "create_in_several_documents"
#: Why a planned type is not swept at all: no run creates it.
NO_CASE = "no_reviewed_case"
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass
class Param:
    """One required parameter of a list operation, and where its value comes from."""

    name: str
    #: A value with `{name}` placeholders, each read from `TFKIT_VAR_<NAME>`.
    #: With `env`, it starts with the account scope: the variable fills the
    #: first placeholder.
    template: str = ""
    #: An environment variable that holds the whole value, or the account scope's.
    env: str = ""
    #: A planned type: the value is each resource of it that the sweep found.
    scope_type: str = ""
    #: The document pattern of the parameter, which a supplied value must match.
    pattern: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        found = {"name": self.name, "source": self.source}
        for key in ("template", "env", "scope_type", "pattern"):
            if getattr(self, key):
                found[key] = getattr(self, key)
        return found

    def value(self) -> tuple[str, str, str, str]:
        return self.name, self.template, self.env, self.scope_type


@dataclass
class Create:
    """One create that a run sends for a type: a reviewed case's, or a
    dependency's, whose `values` lie over the type's unnamed case."""

    whose: str
    values: dict[str, Any]
    dependencies: dict[str, dict[str, Any]]
    #: The fields that hold another resource's name in the form the documents
    #: state: the case's derived dependencies.
    derived: frozenset[str] = frozenset()


@dataclass
class SweepType:
    terraform_type: str
    list_method: str = ""
    list_path: str = ""
    list_ref: str = ""
    delete_method: str = ""
    delete_path: str = ""
    delete_ref: str = ""
    params: list[Param] = field(default_factory=list)
    separator: str = "-"
    #: The texts that the cases write before the kit prefix, as an API requires.
    name_prefixes: list[str] = field(default_factory=list)
    run_label: str = ""
    #: The fields other than the name that carry the run id, each with its source.
    run_fields: list[tuple[str, str]] = field(default_factory=list)
    #: The planned type whose resources carry the run id for this type's.
    by_parent: str = ""
    #: The field and the value that the NCP's read shows on a resource that it
    #: deleted and still lists, and their source.
    deleted_field: str = ""
    deleted_value: str = ""
    deleted_source: str = ""
    #: The planned types this one must be deleted after.
    after: list[str] = field(default_factory=list)
    cannot_list: str = ""
    cannot_delete: str = ""

    def as_dict(self) -> dict[str, Any]:
        found: dict[str, Any] = {"terraform_type": self.terraform_type}
        if self.list_ref:
            found["list"] = {
                "operation": self.list_ref,
                "method": self.list_method,
                "path": self.list_path,
                "parameters": [p.as_dict() for p in self.params],
            }
        if self.delete_ref:
            found["delete"] = {"operation": self.delete_ref, "method": self.delete_method, "path": self.delete_path}
        found["name_separator"] = self.separator
        if self.name_prefixes:
            found["name_prefixes"] = self.name_prefixes
        if self.run_label:
            found["run_label"] = self.run_label
        if self.run_fields:
            found["run_fields"] = [{"field": f, "source": s} for f, s in self.run_fields]
        if self.by_parent:
            found["by_parent"] = self.by_parent
        if self.deleted_field:
            found["deleted_when"] = {
                "field": self.deleted_field,
                "value": self.deleted_value,
                "source": self.deleted_source,
            }
        if self.after:
            found["delete_after"] = self.after
        return found


def planned(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    layers = ((coverage.get("module_plan") or {}).get("layers")) or {}
    return [
        entry
        for body in layers.values()
        for entry in (body or {}).get("resources") or []
        if entry.get("terraform_type")
    ]


def _naming(case: dict[str, Any]) -> tuple[str, list[str], str]:
    """The separator after the kit prefix, the texts before it, and the label
    key that holds the run id.

    The separator is read from the name pattern and from each create value
    that holds the run id: a case whose `name_field` is empty sends no name
    pattern, and the API makes the name from a value such as a role id. The
    core also tries `-`, so a separator other than `-` is the one recorded. An
    empty separator is an API that permits none. A text before the kit prefix
    in the last segment of such a value is one that the API requires, as GCP
    requires `custom.`; a text that holds a placeholder is no fixed text.
    """
    pattern = str((case.get("naming") or {}).get("name_pattern") or "")
    found = {matched.group(1) for matched in [re.match(r"tfkit([^a-z0-9{])", pattern)] if matched}
    prefixes: set[str] = set()
    values = (case.get("fixture") or {}).get("create") or {}
    for keys in run_marker.run_values(values):
        value: Any = values
        for key in keys:
            value = value[key]
        found.update(re.findall(r"tfkit([^a-z0-9{]?)\{run_id\}", str(value)))
        before = re.match(r"([^{}]+?)tfkit[^a-z0-9{]?\{run_id\}", str(value).rpartition("/")[2])
        prefixes.update([before.group(1)] if before else [])
    labels = (case.get("naming") or {}).get("labels") or {}
    label = next((str(k) for k, v in labels.items() if str(v) == "{run_id}"), "")
    return next(iter(sorted(found - {"-"})), "-"), sorted(prefixes), label


def _supplied(dependency: dict[str, Any]) -> str:
    """The form a supplied dependency takes in a scope.

    `TFKIT_VAR_<VARIABLE>` holds a name, and the case's reference says where
    the name goes: `projects/{project_id}/clusters/tfkit-...` takes it as its
    last segment.
    """
    variable = "{" + str(dependency["variable"]) + "}"
    reference = str(dependency.get("reference") or "")
    return reference.rpartition("/")[0] + "/" + variable if "/" in reference else variable


def _dependencies(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(d.get("variable")): d for d in case.get("dependencies") or [] if isinstance(d, dict) and d.get("resource")
    }


def _derived(case: dict[str, Any]) -> frozenset[str]:
    return frozenset(str(d.get("field")) for d in case.get("derived_dependencies") or [] if isinstance(d, dict))


def _creates(kind: str, cases: dict[str, list[tuple[str, dict[str, Any]]]], merge: Any) -> list[Create]:
    """Each create that a run sends for a type: each reviewed case of it, and
    each dependency of it that a case declares with `values`, laid over the
    type's unnamed case as the test's setup step lays them. A named case or a
    dependency can create the resource in another scope than the unnamed case."""
    own = cases.get(kind) or []
    found = [
        Create(
            "the case's" if not name else f"the {name} case's",
            (case.get("fixture") or {}).get("create") or {},
            _dependencies(case),
            _derived(case),
        )
        for name, case in own
    ]
    unnamed = next((case for name, case in own if not name), None)
    for other, entries in sorted(cases.items()):
        for name, case in entries:
            for entry in case.get("dependencies") or []:
                values = (entry or {}).get("values")
                if unnamed is None or entry.get("resource") != kind or not isinstance(values, dict):
                    continue
                whose = f"the {other}{' ' + name if name else ''} case's dependency {entry.get('variable')}, its"
                base = (unnamed.get("fixture") or {}).get("create") or {}
                found.append(Create(whose, merge(base, values), _dependencies(unnamed), _derived(unnamed)))
    return found


def _completed(value: str, pattern: str, scope: Any, settings: dict[str, str]) -> bool:
    """Whether a case value is relative to the account scope: it does not match
    the parameter's pattern, and the scope, `/` and the value do. The provider
    sends the value so completed."""
    if not pattern or scope is None or scope.setting not in settings:
        return False
    sample = PLACEHOLDER.sub("x", value)
    whole = PLACEHOLDER.sub("x", scope.template) + "/" + sample
    return not re.search(pattern, sample) and bool(re.search(pattern, whole))


def _param(
    name: str,
    create: Create,
    settings: dict[str, str],
    kind: str,
    pattern: str,
    scope: Any,
    nests: dict[str, list[str]],
) -> Param:
    key = attribute_names.root_name(name)
    created = create.values
    named = PLACEHOLDER.fullmatch(str(created.get(key, "")))
    variable = named.group(1) if named else key
    if variable in create.dependencies and (named or key not in created):
        dependency = create.dependencies[variable]
        param = Param(
            name,
            scope_type=str(dependency["resource"]),
            template=_supplied(dependency),
            pattern=pattern,
            source=f"{create.whose} dependency {variable}",
        )
        # A relative reference is completed with the account scope, as a
        # relative case value is, because the provider sends it so.
        if _completed(param.template, pattern, scope, settings):
            param.template, param.env = f"{scope.template}/{param.template}", settings[scope.setting]
            param.source += f", completed with the account scope {scope.template}"
        return param
    if key in created and isinstance(created[key], (str, int)):
        value, source = str(created[key]), f"{create.whose} fixture.create.{key}"
        param = Param(name, template=value, source=source)
        if _completed(value, pattern, scope, settings):
            param = Param(
                name,
                template=f"{scope.template}/{value}",
                env=settings[scope.setting],
                source=f"{source}, completed with the account scope {scope.template}",
            )
        # A value in the name pattern of another planned type is also each
        # resource of that type that the run left: a key of the run's account.
        others = [other for other in nests.get(pattern, []) if other != kind]
        param.scope_type = others[0] if len(others) == 1 else ""
        return param
    if key in settings:
        return Param(name, env=settings[key], source=f"the provider setting {key}")
    # The provider fills a parameter of this pattern from its account scope, so
    # the sweeper lists where the provider creates.
    if scope is not None and pattern == scope.pattern and scope.setting in settings:
        return Param(
            name,
            template=scope.template,
            env=settings[scope.setting],
            source=f"the account scope {scope.template}, filled by the provider setting {scope.setting}",
        )
    raise PlanError(
        f"{kind}: the list operation's parameter {name} has no value: the reviewed case "
        f"gives none, declares no dependency {key}, no provider setting is named {key}, "
        "and no account scope matches it"
    )


def _name_patterns(
    coverage: dict[str, Any], cases: dict[str, Any], operations: dict[str, Any], resolve: Any
) -> dict[str, list[str]]:
    """Each planned type that a case builds, by the pattern of the one name its
    delete takes."""
    found: dict[str, list[str]] = {}
    for entry in planned(coverage):
        kind, ops = str(entry["terraform_type"]), entry.get("operations") or {}
        delete = resolve(str(ops["delete"]), operations) if kind in cases and ops.get("delete") else None
        if delete is not None and len(delete.required_parameters) == 1:
            pattern = dict(delete.parameter_patterns).get(delete.required_parameters[0], "")
            if pattern:
                found.setdefault(pattern, []).append(kind)
    return found


def _selected_by(sweep: SweepType, creates: list[Create], create: Any, docs: Any) -> None:
    """How a resource of the type says that it is the run's.

    Its own name when the create takes one, a run field, or a label. A type
    with none of these is the run's through the planned type it is listed
    under, and with no such type nothing says which run made it. A create
    value that holds the run id is the type's own when the list returns it as
    a run field, or when the create takes it in a field or a parameter that the
    listed resource does not have, which the API makes the name. Any other
    such value says nothing about a listed resource.
    """
    own = bool(create.name_field)
    for made in creates:
        for keys in run_marker.run_values(made.values):
            run_field = docs.run_field(create.ref, sweep.list_ref, keys)
            own = own or bool(run_field) or docs.names_the_resource(create.ref, sweep.list_ref, keys)
            if run_field and run_field not in {f for f, _ in sweep.run_fields}:
                sweep.run_fields.append((run_field, f"{made.whose} fixture.create.{'.'.join(keys)}"))
    if own or (sweep.run_label and docs.has_labels(sweep.list_ref)):
        return
    _created_fields(sweep, creates, create, docs)
    parents = [p.scope_type for p in sweep.params if p.scope_type]
    if parents:
        sweep.by_parent = parents[0]
    elif not sweep.run_fields:
        sweep.cannot_list = NO_RUN_ID


def _created_fields(sweep: SweepType, creates: list[Create], create: Any, docs: Any) -> None:
    """The run fields of a type with no run id of its own: each field that the
    list returns and that a case fills with a declared dependency resource's
    whole placeholder. The test creates that resource with the run id in its
    name field, so a value such as its email can start with it, and the core
    selects by the field only when it does. A derived dependency's field holds
    the other resource's name in the form the documents state, such as
    `groups/<id>`, which never starts with the run id."""
    for made in creates:
        for keys, variable in run_marker.dependency_values(made.values, made.dependencies):
            run_field = "" if ".".join(keys) in made.derived else docs.run_field(create.ref, sweep.list_ref, keys)
            if run_field and run_field not in {f for f, _ in sweep.run_fields}:
                sweep.run_fields.append((run_field, f"{made.whose} dependency {variable}, which the test creates"))


def _deleted_when(sweep: SweepType, cases: list[tuple[str, dict[str, Any]]], kit: Any) -> None:
    """The field and the value that the reviewed case's destroy expectation
    names, in the `<field> == <value>` form of a readiness. Some NCPs keep a
    deleted resource readable and listable for a while, and refuse its delete.
    `absent` and prose name no field. An expectation read through another
    resource (`via`) names a field of that resource, not of a listed one."""
    for name, case in cases:
        destroy = (case.get("verification") or {}).get("destroy")
        if not isinstance(destroy, dict) or destroy.get("via"):
            continue
        found = kit.lifecycle_checks.READINESS.fullmatch(str(destroy.get("expect") or ""))
        if found:
            sweep.deleted_field, sweep.deleted_value = found.group(1), found.group(2)
            whose = "the case's" if not name else f"the {name} case's"
            sweep.deleted_source = f"{whose} verification.destroy.expect"
            return


def _prose_required(
    listing: Any,
    creates: list[Create],
    docs: Any,
    kit: Any,
    settings: dict[str, str],
    kind: str,
    scope: Any,
    nests: dict[str, list[str]],
) -> list[Param]:
    """Each parameter that the document requires only in its prose, with the
    value that a create gave it. Some lists refuse a request without their
    parent and say so only in the description. A list under the parent that
    the create used finds what the create made. An optional parameter that
    the prose does not require, such as a filter, is not passed: a create value
    of the same name, such as a sink's log filter, is not where it was made."""
    found = []
    patterns = dict(listing.parameter_patterns)
    for name, description in docs.optional_parameters(listing.ref).items():
        if not kit.REQUIRED_PROSE.match(description):
            continue
        for made in creates:
            if attribute_names.root_name(name) in made.values:
                param = _param(name, made, settings, kind, patterns.get(name, ""), scope, nests)
                param.source += ", which the create was made under"
                found.append(param)
    return found


def _listing(create: Any, by_document: list[dict[str, Any]], kit: Any) -> tuple[Any, str]:
    """The `GET` on the create's collection path in the document that defines
    the create, or why there is none. Two APIs can name their collections by
    the same path, and the list of the other API never returns the resource."""
    owners = [operations for operations in by_document if create is not None and create.ref in operations]
    if len(owners) > 1:
        return None, CREATE_IN_SEVERAL
    listing = owners[0].get(kit.path_key("GET", create.path)) if owners else None
    return listing, "" if listing is not None else NO_LIST


def _sweep_type(
    entry: dict[str, Any],
    cases: dict[str, Any],
    operations: dict[str, Any],
    kit: Any,
    docs: Any,
    settings: dict[str, str],
    scope: Any,
    nests: dict[str, list[str]],
    by_document: list[dict[str, Any]],
) -> SweepType:
    kind = str(entry["terraform_type"])
    separator, prefixes, label = _naming(cases[kind][0][1])
    sweep = SweepType(kind, separator=separator, name_prefixes=prefixes, run_label=label)
    ops = entry.get("operations") or {}
    create = kit.resolve_operation(str(ops["create"]), operations) if ops.get("create") else None
    listing, sweep.cannot_list = _listing(create, by_document, kit)
    if listing is not None:
        sweep.list_ref, sweep.list_method, sweep.list_path = (listing.ref, listing.http_method, listing.path)
        patterns = dict(listing.parameter_patterns)
        creates = _creates(kind, cases, kit.fixture_values.merged_fixture)
        for made in creates:
            for name in listing.required_parameters:
                param = _param(name, made, settings, kind, patterns.get(name, ""), scope, nests)
                if param.value() not in {p.value() for p in sweep.params}:
                    sweep.params.append(param)
        for param in _prose_required(listing, creates, docs, kit, settings, kind, scope, nests):
            if param.value() not in {p.value() for p in sweep.params}:
                sweep.params.append(param)
        _selected_by(sweep, creates, create, docs)
        _deleted_when(sweep, cases[kind], kit)
    delete = kit.resolve_operation(str(ops["delete"]), operations) if ops.get("delete") else None
    if delete is None:
        sweep.cannot_delete = NO_DELETE
    else:
        sweep.delete_ref, sweep.delete_method, sweep.delete_path = (delete.ref, delete.http_method, delete.path)
    return sweep


def derive(
    coverage: dict[str, Any],
    cases: dict[str, list[tuple[str, dict[str, Any]]]],
    documents: list[Any],
    kit: Any,
    settings: dict[str, str],
    scope: Any = None,
) -> tuple[list[SweepType], list[str]]:
    """The sweep plan, in deletion order: each type after those it must outlive.

    `cases` holds each type's reviewed cases by name, the unnamed one first.
    `kit` is the scaffold's reader, so the two skills read one document the
    same way. Also each planned type that no reviewed case builds. No run
    creates it, and its list can need values that only a case gives.
    """
    operations = kit.index_operations(documents)
    by_document = [kit.index_operations([document]) for document in documents]
    docs = run_marker.Documents(documents)
    not_built = [str(e["terraform_type"]) for e in planned(coverage) if str(e["terraform_type"]) not in cases]
    nests = _name_patterns(coverage, cases, operations, kit.resolve_operation)
    found = {
        str(entry["terraform_type"]): _sweep_type(
            entry, cases, operations, kit, docs, settings, scope, nests, by_document
        )
        for entry in planned(coverage)
        if str(entry["terraform_type"]) not in not_built
    }
    # A dependency resource is deleted after each resource that uses it, whether
    # the plan says so with `dependency_of` or a case declares it.
    for entry in planned(coverage):
        for served in entry.get("dependency_of") or []:
            if served in found and entry["terraform_type"] in found:
                found[str(entry["terraform_type"])].after.append(str(served))
    for kind, entries in cases.items():
        for _, case in entries:
            for dependency in case.get("dependencies") or []:
                used = str((dependency or {}).get("resource") or "")
                if used in found and kind in found and kind not in found[used].after:
                    found[used].after.append(kind)
    for sweep in found.values():
        for param in sweep.params:
            if param.scope_type in found and sweep.terraform_type not in found[param.scope_type].after:
                found[param.scope_type].after.append(sweep.terraform_type)
    return _ordered(found), not_built


def _ordered(found: dict[str, SweepType]) -> list[SweepType]:
    """Each type after every type it must outlive; a cycle is refused."""
    ordered: list[SweepType] = []
    placed: set[str] = set()
    visiting: set[str] = set()

    def place(kind: str) -> None:
        if kind in placed:
            return
        if kind in visiting:
            raise PlanError(f"{kind} must be deleted both before and after another type")
        visiting.add(kind)
        for first in sorted(found[kind].after):
            if first in found:
                place(first)
        visiting.discard(kind)
        placed.add(kind)
        ordered.append(found[kind])

    for kind in sorted(found):
        place(kind)
    return ordered


def document(plan: list[SweepType], not_built: list[str], sources: list[Path]) -> dict[str, Any]:
    """`sweep.yaml`: each fact the sweeper acts on, with where it came from."""
    return {
        "api_version": "terraform.ncp/v1",
        "kind": "sweep_plan",
        "read": [str(p) for p in sources],
        "order": [t.terraform_type for t in plan],
        "types": [t.as_dict() for t in plan if not t.cannot_list],
        "cannot_list": [{"terraform_type": t.terraform_type, "reason": t.cannot_list} for t in plan if t.cannot_list],
        "cannot_delete": [
            {"terraform_type": t.terraform_type, "reason": t.cannot_delete} for t in plan if t.cannot_delete
        ],
        "not_built": [{"terraform_type": kind, "reason": NO_CASE} for kind in not_built],
    }
