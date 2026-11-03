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
#
# The kit maintains this script. Every path is an argument: the script runs
# inside whatever repository the operator installed the skill in, and that
# repository's layout is theirs, not ours. The script also checks the module
# plan: the layer grouping key, the resource projection, and the gates over
# both.

"""Check an NCP's API coverage and module plan, and report on them.

Answers "does this NCP's public API expose a surface that could satisfy our
requirements, and what Terraform resources does that imply" without a live
account or a test run.

The curated judgement — does this endpoint really satisfy this requirement — is
made by a person reading the spec. This script does not attempt semantic
matching. What it checks mechanically is that the curation is internally
consistent and that nothing in it has gone stale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_spec
import assumed_fields
import catalog_layers
import deprecated_plan
import method_conditions
import nested_completeness
import open_fields
import operation_refs
import own_methods
import own_object
import plan_decisions
import plan_overrides
import planned_objects
import provenance
import replaced_types
import requirement_types
import requirements_catalog
import resource_shape
import silent_update
import unmentioned_creations
from api_spec import Spec, sdk_symbol

STATUSES = frozenset({"present", "partial", "absent", "not_applicable"})

#: Checked only to validate an evidence reference's form. Shared with the spec
#: reader so one definition governs both what is accepted and what is indexed.
HTTP_METHODS = api_spec.HTTP_METHODS

#: Evidence naming an operation by the identifier its own specification gives
#: it. Preferred over `METHOD /path`: one spelling, no HTTP verb to get wrong,
#: and it maps to the SDK method the provider will call. See `api_spec`.
OPERATION_PREFIX = operation_refs.OPERATION_PREFIX

#: Evidence naming an endpoint outside the NCP's own API. A GKE requirement may
#: genuinely be satisfied through the Kubernetes API, and a boot requirement
#: through the instance metadata server, but neither is in the NCP's
#: specification and no provider resource can be built from either. Marking
#: them keeps the claim while making it impossible to mistake for NCP surface.
EXTERNAL_PREFIX = operation_refs.EXTERNAL_PREFIX
VERDICTS = frozenset({"buildable", "partial", "not_buildable", "not_applicable"})

#: Statuses whose claim no evidence list can express, so prose is the only
#: carrier. `partial` asserts that something is missing, and what is missing is
#: by definition not among the references cited. `not_applicable` asserts the
#: requirement does not apply to this NCP at all, which is a judgement about
#: the requirement rather than about the API.
#:
#: `absent` is deliberately not here. A search that found nothing has nothing
#: to cite and nothing to explain, and demanding prose there would only invite
#: prose written to satisfy a gate. `present` is carried by its evidence.
NOTE_REQUIRED = frozenset({"partial", "not_applicable"})

#: Language naming something an API shape cannot prove: a quantity, or a named
#: audit regime. A requirement whose description matches is never `present`,
#: note or no note: this analysis reads an API shape and runs no account. It is
#: `partial` when its controls exist, and the note says what is unproven.
#:
#: Narrow on purpose. *Certificates* are a cryptographic object, not a
#: compliance certification, so the audit words match whole. `%` carries no
#: trailing word boundary because `%` is not a word character and "99.9% of the
#: time" would slip through one.
BEHAVIOURAL = re.compile(
    r"[<>]=?\s*\d|\b(?:within|less than|under|no more than|at least|up to)\s+\d"
    r"|\b\d+(?:\.\d+)?\s*%"
    r"|\b\d+(?:\.\d+)?\s*(?:percent|ms|sec|secs|seconds?|min|mins|minutes?|hours?|days?)\b"
    r"|\bSOC\s?2\b|\bISO\s?\d{4}\b|\bFedRAMP\b"
    r"|\bcertification\b|\bcertified\b",
    re.IGNORECASE,
)

#: The four standard module layers, fixed by the design document. `other`
#: holds what no layer's catalog headings describe. A type there is planned,
#: and the provider and the tests build it; no module is generated for it.
LAYERS = ("iam", "network", "cluster", "compute")
CLASSIFICATIONS = frozenset({*LAYERS, "other"})

#: The requirement catalog ships with the skill, pinned in ../SOURCE.yaml,
#: so the kit runs without a validation-repository checkout on disk. Override it
#: with --requirements to read a different revision.
BUNDLED_REQUIREMENTS = Path(__file__).resolve().parent.parent / "references" / "requirements-for-ai-clouds-v2.4.md"


class InputError(Exception):
    """An input is missing or unreadable. Distinct from a gate failure."""


@dataclass
class GateResult:
    gate: str
    failures: list[str] = field(default_factory=list)
    #: What the gate could not check, and why. A note never fails the gate, but
    #: it must reach the operator: a gate that silently checked less than its
    #: name implies is indistinguishable from one that found nothing wrong.
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InputError(f"no such file: {path}") from error
    except yaml.YAMLError as error:
        raise InputError(f"{path} is not valid YAML: {error}") from error


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InputError(f"no such file: {path}") from error
    except json.JSONDecodeError as error:
        raise InputError(f"{path} is not valid JSON: {error}") from error


def load_requirements(path: Path) -> dict[str, dict[str, Any]]:
    """Return the requirement catalog keyed by `req_id`.

    Reading it is `requirements_catalog`'s job, because the published guide and
    the YAML catalog state the same things in different files and
    nothing above this line should know which arrived.
    """
    try:
        catalog = requirements_catalog.load(path)
        placed = catalog_layers.layers(catalog)
    except (requirements_catalog.CatalogError, catalog_layers.UnknownHeading) as error:
        raise InputError(f"{path}: {error}") from error
    for req_id, (heading, layer) in placed.items():
        catalog[req_id] = {**catalog[req_id], "layer_heading": heading, "layer": layer}
    return catalog


#: Everything a Terraform identifier may not carry. An NCP's name is whatever
#: the operator calls it, and several carry a hyphen, so the name is converted
#: where an identifier is needed rather than constrained at the source.
NOT_TERRAFORM_IDENTIFIER = re.compile(r"[^a-z0-9_]+")


def terraform_identifier(name: str) -> str:
    """An NCP's name, spelled the way a Terraform identifier can carry it.

    A resource type prefix is the provider's local name. A hyphen there is legal
    HCL and unusable downstream: the test generator turns the same string into a
    Go identifier, and `my-isv_instance` renders `TestAccMy-IsvInstance`, which
    Go will not parse. One conversion, stated once, so a plan, a provider and a
    test all spell the NCP the same way.
    """
    return NOT_TERRAFORM_IDENTIFIER.sub("_", name.strip().lower()).strip("_")


def coverage_ncp(coverage: dict[str, Any]) -> str | None:
    """Return the NCP a coverage file describes.

    `provider` is the key the upstream tool writes. A coverage file that
    already holds it must stay readable, so both keys are read as-is. `ncp` is
    accepted too, because "provider" is badly overloaded here: a Terraform
    provider is a different thing from the NCP, and the two appear in the same
    sentence constantly.
    """
    value = coverage.get("ncp") or coverage.get("provider")
    return str(value) if value else None


def mapping_parts(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a requirement's parts, or an empty list when it has none.

    A requirement covering several concerns splits into parts, each owned by one
    layer. Most requirements need no split and carry a flat `layers` list.
    """
    parts = mapping.get("parts") or []
    return parts if isinstance(parts, list) else []


def mapping_layers(mapping: dict[str, Any]) -> list[str]:
    """Return the layers a requirement touches, however it expresses them.

    Parts are the single source of truth when present: a split requirement's
    layers are exactly its parts' layers, so the two cannot drift apart.
    """
    parts = mapping_parts(mapping)
    if parts:
        seen: list[str] = []
        for part in parts:
            layer = part.get("layer")
            if layer and layer not in seen:
                seen.append(layer)
        return seen
    return list(mapping.get("layers", []) or [])


def mapping_evidence(mapping: dict[str, Any]) -> list[tuple[str, str]]:
    """Return `(where, ref)` for every evidence reference on a requirement."""
    refs = [(mapping["req_id"], ref) for ref in mapping.get("evidence", []) or []]
    for part in mapping_parts(mapping):
        label = f"{mapping['req_id']}/{part.get('part_id', '?')}"
        refs.extend((label, ref) for ref in part.get("evidence", []) or [])
    return refs


def _form_of_path_reference(ref: str) -> str | None:
    """Whether `ref` is shaped like `METHOD /path`, ignoring any specification."""
    parts = ref.split(" ", 1)
    if len(parts) != 2:
        return f"'{ref}' is neither 'METHOD /path' nor 'schema:Name'"
    method, path = parts
    if not path.startswith("/"):
        return f"'{ref}' has no leading '/' on its path"
    if method.lower() not in HTTP_METHODS:
        return f"'{method}' is not an HTTP method"
    return None


def resolve_evidence(ref: str, spec: Spec | None) -> str | None:
    """Return why `ref` does not resolve against `spec`, or None when it does.

    Two forms, both from the original tool: `METHOD /path`, and `schema:Name`
    optionally suffixed with `.field`.

    `spec` is None when the operator supplied no specification. Not every NCP
    publishes one: GCP ships Discovery documents and a generated SDK, and no
    OpenAPI document that a `paths` lookup could read. Refusing to run there
    would make the gate a statement about Google's publishing choices rather
    than about the analysis.

    Without a spec the form is still checked and only existence is dropped. That
    keeps the cheap half of the gate -- a malformed reference, an SDK symbol, or
    a documentation URL pasted where a path belongs is still caught -- and the
    evidence gate records what it could not confirm.
    """
    if ref.startswith(EXTERNAL_PREFIX):
        # Deliberately never resolved: it is declared to be outside the spec.
        # The form is still checked, so a marker cannot launder a typo.
        remainder = ref[len(EXTERNAL_PREFIX) :]
        if not remainder:
            return f"'{ref}' names no endpoint"
        return _form_of_path_reference(remainder)

    if ref.startswith(OPERATION_PREFIX):
        identifier = ref[len(OPERATION_PREFIX) :]
        if not identifier:
            return f"'{ref}' names no operation"
        if spec is None:
            return None
        if not spec.has_operation_ids:
            return (
                f"the specification defines no operation identifiers, so "
                f"'{identifier}' cannot resolve; cite 'METHOD /path' instead"
            )
        if identifier not in spec.operations:
            return f"operation '{identifier}' not found in spec"
        return None

    if ref.startswith(operation_refs.SCHEMA_PREFIX):
        name, _, field_name = ref[len(operation_refs.SCHEMA_PREFIX) :].partition(".")
        if not name:
            return f"'{ref}' names no schema"
        if spec is None:
            return None
        if name not in spec.schemas:
            return f"schema '{name}' not found in spec"
        if field_name and field_name not in spec.schema_fields.get(name, ()):
            return f"schema '{name}' declares no field '{field_name}' in its properties"
        return None

    malformed = _form_of_path_reference(ref)
    if malformed:
        return malformed
    method, path = ref.split(" ", 1)
    if spec is None:
        return None
    available = spec.resolve_path(method, path)
    if available is None:
        return f"path '{path}' not found in spec"
    if method.upper() not in available:
        return f"method '{method}' not found on path '{path}'; it has {', '.join(sorted(available))}"
    matches = operation_refs.path_matches(ref, spec)
    if len(matches) > 1:
        # The completeness gates decide on one operation. A reference that
        # names several would pass them unread.
        return (
            f"it names {len(matches)} operations of the supplied documents: "
            f"{', '.join(sorted(operation_refs.cited(item) for item in matches))}; "
            f"cite one by identifier"
        )
    return None


# --------------------------------------------------------------------------
# Gates. Each takes the loaded inputs and returns a GateResult. They run in
# declared order and stop at the first failure, because a later gate reading
# an artifact an earlier gate rejected reports noise rather than a cause.
# --------------------------------------------------------------------------


def gate_coverage_schema(ctx: Context) -> GateResult:
    result = GateResult("coverage-schema")
    coverage = ctx.coverage
    # Reported, never failed. Re-checking an archived artifact against a newer
    # skill is a normal thing to do, and failing would make every backup
    # uncheckable.
    drift = provenance.mismatch(coverage)
    if drift:
        result.notes.append(drift)
    if not isinstance(coverage.get("mappings"), list):
        result.failures.append("coverage has no 'mappings' list")
        return result
    if not coverage_ncp(coverage):
        result.failures.append("coverage does not name the NCP it describes")

    seen: set[str] = set()
    for mapping in coverage["mappings"]:
        req_id = mapping.get("req_id")
        if not req_id:
            result.failures.append("a mapping entry has no req_id")
            continue
        if req_id in seen:
            result.failures.append(
                f"{req_id}: duplicate mapping entry — a requirement covering several "
                f"concerns splits into 'parts' within one entry, not two entries"
            )
        seen.add(req_id)
        status = mapping.get("status")
        if status not in STATUSES:
            result.failures.append(f"{req_id}: unknown status {status!r}, expected one of {sorted(STATUSES)}")
        # A claim of full coverage with nothing behind it. A `present` status
        # with no evidence is always a defect.
        if status == "present" and not (mapping.get("evidence") or []) and not (mapping.get("parts") or []):
            result.failures.append(f"{req_id}: status 'present' cites no evidence")
        has_note = bool(str(mapping.get("note") or "").strip())
        if status in NOTE_REQUIRED and not has_note:
            result.failures.append(f"{req_id}: status {status!r} must carry a note; the evidence cannot state it")
        # `present` over a quantity is a claim the API shape cannot settle. The
        # run that writes the artifact sees this gate, so the rubric lives here.
        if status == "present":
            described = str((ctx.requirements.get(req_id) or {}).get("description") or "")
            if BEHAVIOURAL.search(described):
                result.failures.append(
                    f"{req_id}: 'present' over a requirement naming a quantity or an "
                    f"audit regime, which an API shape cannot prove. Use 'partial', "
                    f"and say in the note what is unproven"
                )
        result.failures.extend(_part_failures(mapping))
    return result


def _part_failures(mapping: dict[str, Any]) -> list[str]:
    """Validate one requirement's parts. Empty list when it has none."""
    parts = mapping_parts(mapping)
    if not parts:
        return []

    req_id = mapping["req_id"]
    failures: list[str] = []

    # Two sources for one fact is how they drift apart. Parts own the layers,
    # and an empty list is a second statement that contradicts them.
    if "layers" in mapping:
        failures.append(
            f"{req_id}: has both 'parts' and 'layers'; the layers of a split "
            f"requirement are its parts' layers, so remove the 'layers' key"
        )

    seen: set[str] = set()
    for index, part in enumerate(parts):
        part_id = part.get("part_id")
        if not part_id:
            failures.append(f"{req_id}: part {index} has no part_id")
            continue
        if part_id in seen:
            failures.append(f"{req_id}/{part_id}: duplicate part_id")
        seen.add(part_id)

        layer = part.get("layer")
        if layer not in CLASSIFICATIONS:
            failures.append(f"{req_id}/{part_id}: unknown layer {layer!r}, expected one of {sorted(CLASSIFICATIONS)}")
        status = part.get("status")
        if status not in STATUSES:
            failures.append(f"{req_id}/{part_id}: unknown status {status!r}, expected one of {sorted(STATUSES)}")
    return failures


def gate_spec_documents(ctx: Context) -> GateResult:
    """The documents the analysis says it read are the documents it was given.

    `spec_documents` is how the next step finds the file behind each `op:`
    reference. The scaffold refuses to start without it, so this step must
    refuse first. Otherwise the artifact passes every gate here and stops the scaffold
    one session later.

    The control is the specifications themselves, which this run already holds.
    The check recomputes the digest from the file on disk. A replaced
    specification therefore fails, and an absent or unreachable file cannot pass
    silently. It needs no second file.
    """
    result = GateResult("spec-documents")
    supplied = list(ctx.spec_files)
    declared = ctx.coverage.get("spec_documents") or {}

    if not supplied:
        return result
    if not declared:
        result.failures.append(
            f"{len(supplied)} specification(s) were read and the analysis records "
            f"no spec_documents. The next step resolves every operation through "
            f"that block and cannot start without it"
        )
        return result

    by_name = {path.name: path for path in supplied}
    recorded = {_document_file(entry): name for name, entry in declared.items() if isinstance(entry, dict)}
    for name in sorted(set(by_name) - set(recorded)):
        result.failures.append(f"spec_documents records no entry for {name}, which this analysis read")
    for name, entry in sorted(declared.items()):
        if not isinstance(entry, dict):
            result.failures.append(f"spec_documents.{name} is not a mapping")
            continue
        path = by_name.get(_document_file(entry))
        if path is None:
            result.failures.append(
                f"spec_documents.{name}.file is {entry.get('file')!r}, which was "
                f"not among the specifications this analysis read"
            )
            continue
        claimed = str(entry.get("digest") or "")
        if not claimed:
            result.failures.append(
                f"spec_documents.{name} records no digest, so nothing pins which revision of {path.name} was read"
            )
            continue
        actual = _file_digest(path)
        if claimed != actual:
            result.failures.append(f"spec_documents.{name}.digest is {claimed!r}; {path.name} on disk is {actual!r}")
    return result


def _document_file(entry: dict[str, Any]) -> str:
    """The file name an entry records, however much path it carries."""
    return Path(str(entry.get("file") or "")).name


def _file_digest(path: Path) -> str:
    """A specification's digest, in the form an analysis records it."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def gate_evidence(ctx: Context) -> GateResult:
    result = GateResult("evidence")
    known = set(ctx.requirements)
    mapped: set[str] = set()

    for mapping in ctx.coverage["mappings"]:
        req_id = mapping["req_id"]
        mapped.add(req_id)
        if req_id not in known:
            result.failures.append(f"{req_id}: not a req_id this catalog defines")
        for label, ref in mapping_evidence(mapping):
            reason = resolve_evidence(ref, ctx.spec)
            if reason:
                result.failures.append(f"{label}: evidence {ref!r} does not resolve ({reason})")
    result.failures.extend(
        operation_refs.uncited_documents(
            [ref for mapping in ctx.coverage["mappings"] for _, ref in mapping_evidence(mapping)],
            ctx.documents,
        )
    )

    external = sorted(
        {
            f"{mapping['req_id']}: {ref}"
            for mapping in ctx.coverage["mappings"]
            for _, ref in mapping_evidence(mapping)
            if ref.startswith(EXTERNAL_PREFIX)
        }
    )
    if external:
        result.notes.append(
            f"{len(external)} reference(s) name an endpoint outside the NCP API and "
            f"were not resolved: " + "; ".join(external)
        )

    unserved = set(requirements_catalog.unserved(ctx.requirements))
    for req_id in sorted(known - mapped - unserved):
        result.failures.append(f"{req_id}: no coverage mapping entry (an analysis gap, not an API gap)")
    if unserved:
        result.notes.append(
            f"{len(unserved)} requirement(s) ask for a practice rather than for "
            f"anything an API creates, so no analysis is demanded of them: " + ", ".join(sorted(unserved))
        )
    return result


def gate_layers(ctx: Context) -> GateResult:
    """Only the four standard layers, plus `other`, may appear, and each
    requirement that the plan builds for includes its catalog heading's layer."""
    result = GateResult("layers")

    for mapping in ctx.coverage["mappings"]:
        for layer in mapping_layers(mapping):
            if layer not in CLASSIFICATIONS:
                result.failures.append(
                    f"{mapping['req_id']}: unknown layer {layer!r}, expected one of {sorted(CLASSIFICATIONS)}"
                )
        result.failures.extend(_heading_layer_failures(ctx, mapping))

    if ctx.module_plan is None:
        return result
    for name in ctx.module_plan.get("layers", {}):
        if name not in CLASSIFICATIONS:
            result.failures.append(
                f"module plan declares unknown layer {name!r}, expected one of {sorted(CLASSIFICATIONS)}"
            )
    return result


def _heading_layer_failures(ctx: Context, mapping: dict[str, Any]) -> list[str]:
    """A `present` or `partial` requirement's layers include the layer of the
    catalog heading it sits under. Only those decide which module holds a type,
    and the heading is a fact of the catalog where a reading is not. An analysis
    that tags no layer on a requirement makes no layer claim to check."""
    if mapping.get("status") not in requirement_types.SERVED or not mapping_layers(mapping):
        return []
    entry = ctx.requirements.get(str(mapping.get("req_id"))) or {}
    layer = entry.get("layer")
    if not layer or layer in mapping_layers(mapping):
        return []
    return [
        f"{mapping['req_id']}: sits under the catalog heading {entry['layer_heading']!r}, which the "
        f"{layer} layer covers, so its layers must include {layer!r}; it has {mapping_layers(mapping)}. "
        f"A part can add another layer"
    ]


def gate_module_plan(ctx: Context) -> GateResult:
    result = GateResult("module-plan")
    ncp = coverage_ncp(ctx.coverage)
    if ctx.module_plan is None:
        result.failures.append(
            "coverage has no 'module_plan' section, so nothing says which Terraform resources the analysis implies"
        )
        return result

    layers = ctx.module_plan.get("layers")
    if not isinstance(layers, dict) or not layers:
        result.failures.append("module plan has no 'layers' mapping")
        return result

    # An absent capability has to be visible. A plan that simply omits `iam`
    # reads as "not considered", which is the one thing a verdict must never
    # leave ambiguous.
    for name in LAYERS:
        if name not in layers:
            result.failures.append(
                f"{name}: no verdict; every layer needs an explicit one, including 'not_buildable' with a reason"
            )

    covered = _coverage_parts(ctx.coverage)
    owner: dict[str, str] = {}
    for name, layer in layers.items():
        verdict = layer.get("verdict")
        if name == "other":
            # No module is generated for `other`, so a verdict there would
            # promise one that never arrives.
            if verdict is not None:
                result.failures.append("other: takes no verdict, because no module is generated for it")
        elif verdict not in VERDICTS:
            result.failures.append(f"{name}: unknown verdict {verdict!r}, expected one of {sorted(VERDICTS)}")
        resources = layer.get("resources", []) or []

        # A verdict is a claim; the resource list is its evidence. They have to
        # agree, or the plan says one thing and generates another.
        if verdict in {"buildable", "partial"} and not resources:
            result.failures.append(f"{name}: verdict {verdict!r} but no resources listed")
        if verdict in {"not_buildable", "not_applicable"} and resources:
            result.failures.append(f"{name}: verdict {verdict!r} but {len(resources)} resource(s) listed")
        if verdict == "partial" and not layer.get("gap"):
            result.failures.append(f"{name}: verdict 'partial' must state the gap")
        # Every verdict costs a sentence. A verdict that costs nothing becomes
        # the default answer, and then it no longer reports what the
        # requirements show.
        if verdict in {"not_buildable", "buildable", "not_applicable"} and not layer.get("reason"):
            result.failures.append(f"{name}: verdict {verdict!r} must state the reason")

        for resource in resources:
            kind = resource.get("logical_kind")
            if not kind:
                result.failures.append(f"{name}: a resource has no logical_kind")
                continue
            if kind in owner:
                result.failures.append(f"{kind}: claimed by two layers, {owner[kind]} and {name}")
            else:
                owner[kind] = name
            terraform_type = resource.get("terraform_type")
            if not terraform_type:
                result.failures.append(f"{kind}: no terraform_type")
            elif ncp and not str(terraform_type).startswith(f"{terraform_identifier(ncp)}_"):
                # The type name declares whose provider this is. `<ncp>_instance`
                # is a plan to generate a provider; `google_compute_instance` is
                # a plan to configure one HashiCorp already ships. The provider
                # generator and the test generator both build from this string,
                # so the two produce entirely different downstream work -- and
                # for an NCP with no existing provider, the second names nothing.
                prefix = terraform_identifier(ncp)
                spelling = (
                    f" ({ncp!r} carries a character a Terraform identifier cannot, so the prefix is the converted name)"
                    if prefix != ncp
                    else ""
                )
                result.failures.append(
                    f"{kind}: terraform_type {terraform_type!r} does not start "
                    f"with '{prefix}_'{spelling}; this plan generates {ncp}'s "
                    f"provider rather than configuring another"
                )
            operations = resource.get("operations") or {}
            if not operations:
                result.failures.append(f"{kind}: no operations, so nothing proves it is buildable")
            for verb, ref in operations.items():
                if ref.startswith(EXTERNAL_PREFIX):
                    # A requirement may be satisfied outside the NCP's API; a
                    # resource may not be built there. Nothing in the provider
                    # can create a Kubernetes object from a `google_*` type, so
                    # a plan resting on one would be generated and then fail.
                    result.failures.append(
                        f"{kind}: {verb} operation {ref!r} is outside the NCP API, "
                        f"so no provider resource can implement it"
                    )
                    continue
                reason = resolve_evidence(ref, ctx.spec)
                if reason:
                    result.failures.append(f"{kind}: {verb} operation {ref!r} does not resolve ({reason})")
            result.failures.extend(resource_shape.failures(kind, resource, ctx.spec))
            result.failures.extend(plan_overrides.failures(kind, resource, ctx.spec))
            result.failures.extend(deprecated_plan.failures(kind, resource, ctx.spec))
            if not resource.get("requirements") and not resource.get("dependency_of"):
                result.failures.append(
                    f"{kind}: no requirements and no dependency_of, so nothing explains why it exists"
                )
            for binding in resource.get("requirements", []) or []:
                result.failures.extend(_binding_failures(ctx, covered, kind, binding))
    result.failures.extend(_dependency_failures(layers))
    return result


def _dependency_failures(layers: dict[str, Any]) -> list[str]:
    """A dependency resource serves a planned resource that a requirement explains.

    A dependency resource is planned because another resource's create needs
    it, and no requirement names it. So what explains it is the resource it
    serves. That resource must be in the plan, and following `dependency_of`
    from it must reach a resource a requirement names. Otherwise the plan can
    justify any resource by pointing it at another unjustified one.
    """
    planned = {
        str(resource["terraform_type"]): resource
        for layer in layers.values()
        for resource in (layer or {}).get("resources") or []
        if isinstance(resource, dict) and resource.get("terraform_type")
    }
    failures: list[str] = []
    for kind, resource in planned.items():
        served = resource.get("dependency_of")
        if served is None:
            continue
        if not isinstance(served, list) or not served or not all(isinstance(name, str) and name for name in served):
            failures.append(f"{kind}: dependency_of must list the terraform_type of each planned resource it serves")
            continue
        for name in served:
            if name == kind:
                failures.append(f"{kind}: dependency_of names itself")
            elif name not in planned:
                failures.append(f"{kind}: dependency_of names {name}, which the module plan does not plan")
        if not resource.get("requirements") and not _serves_a_requirement(kind, planned):
            failures.append(
                f"{kind}: no resource it serves is explained by a requirement, directly "
                f"or through another dependency resource, so nothing explains why it exists"
            )
    return failures


def _serves_a_requirement(kind: str, planned: dict[str, dict[str, Any]]) -> bool:
    """Whether following `dependency_of` from `kind` reaches a requirement resource."""
    seen = {kind}
    waiting = list(planned[kind].get("dependency_of") or [])
    while waiting:
        name = waiting.pop()
        if name in seen or name not in planned:
            continue
        seen.add(name)
        if planned[name].get("requirements"):
            return True
        served = planned[name].get("dependency_of")
        if isinstance(served, list):
            waiting.extend(str(item) for item in served)
    return False


def is_dependency_resource(resource: dict[str, Any]) -> bool:
    """Planned because another resource needs it, and named by no requirement."""
    return not resource.get("requirements") and bool(resource.get("dependency_of"))


def _coverage_parts(coverage: dict[str, Any]) -> dict[str, set[str]]:
    """Return each analysed `req_id` and the set of part_ids it declares."""
    return {
        mapping["req_id"]: {part["part_id"] for part in mapping_parts(mapping) if part.get("part_id")}
        for mapping in coverage["mappings"]
        if mapping.get("req_id")
    }


def _binding_failures(ctx: Context, covered: dict[str, set[str]], kind: str, binding: dict[str, Any]) -> list[str]:
    """Check one resource-to-requirement binding against the coverage analysis.

    The catalog says a req_id exists; only `coverage.yaml` says this NCP's API
    was found to support it. A resource justified by a requirement nobody
    analysed has no evidence behind it.
    """
    req_id = binding.get("req_id")
    if req_id not in ctx.requirements:
        return [f"{kind}: requirement {req_id!r} is not a known req_id"]
    if req_id not in covered:
        return [f"{kind}: requirement {req_id!r} has no entry in the coverage analysis"]

    part_id = binding.get("part_id")
    if part_id is None:
        return []
    if part_id not in covered[req_id]:
        known = sorted(covered[req_id])
        return [
            f"{kind}: {req_id} has no part {part_id!r} in the coverage analysis"
            + (f"; it declares {known}" if known else "; it declares no parts")
        ]
    return []


def gate_plan_completeness(ctx: Context) -> GateResult:
    """Every creation the analysis cites must be planned, or declared unplanned.

    `module-plan` checks that each planned resource is justified. It cannot see
    the opposite error: an analysis that proves the API creates an object, and a
    plan that then omits it. An analysis can cite `compute.reservations.insert`,
    `compute.images.insert` and `compute.disks.insert` as evidence and plan only
    `gcp_instance`. No other gate sees that, and a compute layer that can create
    one resource cannot build GPU compute.

    Not every creation is a Terraform resource of its own -- a key version
    belongs to its key -- so the escape hatch is required. It records the
    decision in the artifact rather than letting silence stand for it.
    """
    result = GateResult("plan-completeness")
    if ctx.module_plan is None or ctx.spec is None:
        return result

    spec = ctx.spec
    planned = {
        operation_refs.identity(ref, spec)
        for layer in ctx.module_plan.get("layers", {}).values()
        for resource in layer.get("resources", []) or []
        for ref in (resource.get("operations") or {}).values()
    }
    excused = {operation_refs.identity(ref, spec) for ref in _excused_operations(ctx.module_plan)}

    for entry in ctx.module_plan.get("unplanned_operations") or []:
        if isinstance(entry, dict) and entry.get("operation") and not entry.get("reason"):
            result.failures.append(
                f"unplanned_operations: {entry['operation']} has no reason. An "
                f"omission with no stated reason is indistinguishable from an oversight"
            )

    for where, ref, operation in _creating_evidence(ctx.coverage, spec):
        if operation in planned or operation in excused:
            continue
        result.failures.append(
            f"{where}: evidence {ref} creates an object that no planned resource "
            f"claims. Add the resource, or list the operation under "
            f"module_plan.unplanned_operations with a reason"
        )
    return result


def gate_requirement_types(ctx: Context) -> GateResult:
    """A requirement that names a created object has a planned type, or a reason."""
    result = GateResult("requirement-types")
    if ctx.module_plan is not None and ctx.spec is not None:
        result.failures.extend(requirement_types.failures(ctx.coverage, ctx.module_plan, ctx.spec))
    return result


def gate_parent_completeness(ctx: Context) -> GateResult:
    """A cited child obliges the analysis to name the collection holding it.

    `plan-completeness` reads the operations the analysis cites, so an operation
    it never cites is never asked about. Silence is therefore cheaper than an
    omission: excusing a creation costs a reason, naming nothing costs nothing.
    An analysis can cite
    `iam.locations.workforcePools.providers.scimTenants.create` and name
    neither `workforcePools.providers.create` nor `workforcePools.create`
    anywhere in the artifact. The plan then drops the whole workforce
    federation surface, and no objection is recorded against it.

    A child creation proves its parent collection exists. So where the
    specification also creates that parent, the analysis owes it a mention --
    cited, planned, or excused. It does not owe it a resource: a parent may be
    excused with a reason, like any other creation.
    """
    result = GateResult("parent-completeness")
    if ctx.module_plan is None or ctx.spec is None:
        return result

    spec = ctx.spec
    named = {operation_refs.identity(ref, spec) for ref in _named_operations(ctx.coverage, ctx.module_plan)}
    for where, ref, operation in _creating_evidence(ctx.coverage, spec):
        for parent in operation_refs.enclosing_creations(operation, spec):
            if parent in named:
                continue
            result.failures.append(
                f"{where}: evidence {ref} creates an object inside a collection "
                f"the specification creates too, with "
                f"{operation_refs.describe(parent)}, which this "
                f"analysis never names. Cite it, plan it, or list it under "
                f"module_plan.unplanned_operations with a reason"
            )
    return result


def gate_nested_completeness(ctx: Context) -> GateResult:
    """See `nested_completeness`: a planned resource names its direct nested creations."""
    result = GateResult("nested-completeness")
    if ctx.module_plan is None or ctx.spec is None:
        return result
    named = {operation_refs.identity(ref, ctx.spec) for ref in _named_operations(ctx.coverage, ctx.module_plan)}
    planned = [
        (
            f"module_plan.layers.{layer}.{name.get('terraform_type', '?')}",
            str((name.get("operations") or {}).get("create") or ""),
        )
        for layer, name in _planned_resources(ctx.module_plan)
    ]
    result.failures.extend(nested_completeness.failures(planned, named, ctx.documents))
    return result


def gate_scope_choice(ctx: Context) -> GateResult:
    """A creating operation offered at several parents is a decision, not a fact.

    Several APIs publish the same create under more than one parent: a policy at
    an organization, a folder or a project; a sink at a project, a folder, an
    organization or a billing account. The bodies are identical and the analysis
    picks one, so the artifact showed a choice without showing that a choice
    existed. A GCP plan can place org-policy at the organization node --
    inherited by every project underneath it -- while the project-scoped
    operation appears nowhere in `coverage.yaml` or `coverage.md`. The artifact
    then lists no alternative, so a reviewer has nothing to look at.

    `plan-completeness` cannot see this. It is defined over the operations the
    analysis cited, and the sibling was never cited. Which is right for the 582
    creating operations in those documents that are nobody's business, and wrong
    for the one that differs from a chosen operation only in its parent.

    Where a resource creates at a parent it does not own, a test run reaches
    what it did not create. That is the operator's call, and this gate is what
    puts it in front of them.
    """
    result = GateResult("scope-choice")
    if ctx.module_plan is None or ctx.spec is None:
        return result

    for layer, name in _planned_resources(ctx.module_plan):
        create = str((name.get("operations") or {}).get("create") or "")
        siblings = _scope_siblings(create, ctx.spec)
        if not siblings:
            continue
        declared = {str(item) for item in name.get("scope_alternatives") or []}
        missing = sorted(siblings - declared)
        where = f"module_plan.layers.{layer}.{name.get('terraform_type', '?')}"
        if missing:
            result.failures.append(
                f"{where}: {create} is one of several parents this API offers for "
                f"the same body. Record the rest under `scope_alternatives` and say "
                f"why this one, in `scope_note`: {', '.join(missing)}"
            )
        elif not str(name.get("scope_note") or "").strip():
            result.failures.append(
                f"{where}: scope_alternatives are recorded and scope_note is empty. "
                f"A choice with no stated reason is indistinguishable from an oversight"
            )
    return result


def gate_update_choice(ctx: Context) -> GateResult:
    """A whole-object PUT planned where the same path takes a PATCH of the same body.

    Terraform's update applies the change the plan shows. A PATCH sends that
    change; a PUT replaces the object, so a field the configuration leaves out
    is cleared and the provider has to send the whole current object to keep
    it. Where a document offers both on one path with one body, nothing in it
    says which to plan, and a plan that names the PUT without a reason names it
    by accident.
    """
    result = GateResult("update-choice")
    if ctx.module_plan is None or ctx.spec is None:
        return result

    for layer, name in _planned_resources(ctx.module_plan):
        update = str((name.get("operations") or {}).get("update") or "")
        patches = _patch_siblings(update, ctx.spec)
        if not patches or str(name.get("update_note") or "").strip():
            continue
        where = f"module_plan.layers.{layer}.{name.get('terraform_type', '?')}"
        result.failures.append(
            f"{where}: {update} replaces the whole object, and "
            f"{', '.join(patches)} changes only the fields it is sent, on the same "
            f"path with the same body. Plan the PATCH, or say in `update_note` why "
            f"the replacement is right"
        )
    return result


def gate_own_object(ctx: Context) -> GateResult:
    """See `own_object`: a planned resource creates the object that it owns."""
    result = GateResult("own-object")
    if ctx.module_plan is None or ctx.spec is None:
        return result
    planned = [
        (
            f"module_plan.layers.{layer}.{name.get('terraform_type', '?')}",
            {verb: str(ref) for verb, ref in (name.get("operations") or {}).items()},
        )
        for layer, name in _planned_resources(ctx.module_plan)
    ]
    result.failures.extend(own_object.failures(planned, ctx.spec))
    return result


def gate_own_methods(ctx: Context) -> GateResult:
    """See `own_methods`: a planned object accounts for every method on its own path."""
    result = GateResult("own-methods")
    if ctx.module_plan is None or ctx.spec is None:
        return result
    planned = [
        (f"module_plan.layers.{layer}.{name.get('terraform_type', '?')}", name)
        for layer, name in _planned_resources(ctx.module_plan)
    ]
    result.failures.extend(own_methods.failures(planned, ctx.module_plan, ctx.spec))
    return result


def gate_method_conditions(ctx: Context) -> GateResult:
    """See `method_conditions`: each recorded condition quotes its method's description."""
    result = GateResult("method-conditions")
    if ctx.module_plan is None or ctx.spec is None:
        return result
    planned = [
        (f"module_plan.layers.{layer}.{name.get('terraform_type', '?')}", name)
        for layer, name in _planned_resources(ctx.module_plan)
    ]
    result.failures.extend(method_conditions.failures(planned, ctx.spec))
    return result


def gate_open_fields(ctx: Context) -> GateResult:
    """See `open_fields`: each update states its limit, and each field that
    the plan leaves open is decided or listed as silent."""
    result = GateResult("open-fields")
    if ctx.module_plan is None or ctx.spec is None:
        return result
    result.failures.extend(open_fields.failures(ctx.coverage, ctx.spec, ctx.spec_files))
    return result


def gate_shared_create(ctx: Context) -> GateResult:
    """See `planned_objects`: two planned types never share one create."""
    result = GateResult("shared-create")
    if ctx.module_plan is None or ctx.spec is None:
        return result
    planned = [
        (f"module_plan.layers.{layer}.{name.get('terraform_type', '?')}", name)
        for layer, name in _planned_resources(ctx.module_plan)
    ]
    result.failures.extend(planned_objects.shared_create_failures(planned, ctx.spec))
    return result


def _patch_siblings(update: str, spec: Spec) -> list[str]:
    """PATCH operations on the planned PUT's own path, in its API, that take its body.

    The API is compared too, because a schema name is only unique inside one
    document.
    """
    chosen = operation_refs.resolve(update, spec)
    if chosen is None or chosen.http_method != "PUT" or not chosen.request_schema:
        return []
    api = _api_of(chosen)
    return sorted(
        operation_refs.cited(operation)
        for operation in spec.every_operation()
        if operation.http_method == "PATCH"
        and operation.normalized_path == chosen.normalized_path
        and operation.request_schema == chosen.request_schema
        and _api_of(operation) == api
    )


def _api_of(operation: api_spec.Operation) -> str:
    """The API a dotted identifier names first, or "" for a flat one."""
    head, dot, _ = operation.operation_id.partition(".")
    return head if dot else ""


def _planned_resources(module_plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (layer_name, resource)
        for layer_name, layer in (module_plan.get("layers") or {}).items()
        for resource in (layer.get("resources") or [])
        if isinstance(resource, dict)
    ]


def _scope_siblings(create: str, spec: Spec) -> set[str]:
    """Other operations that create the same body under a different parent.

    Matched on the API, the collection and the method -- the last two segments --
    and on the body, because `iam.projects.serviceAccounts.keys.create` and
    `iam.locations.workforcePools.providers.keys.create` end alike and are not
    the same subject. The body is what settles it.
    """
    if not create.startswith(OPERATION_PREFIX):
        return set()
    identifier = create[len(OPERATION_PREFIX) :]
    chosen = spec.operations.get(identifier)
    parts = identifier.split(".")
    if chosen is None or len(parts) < 3 or not chosen.request_schema:
        return set()
    return {
        OPERATION_PREFIX + other
        for other, operation in spec.operations.items()
        if other != identifier
        and other.split(".")[0] == parts[0]
        and other.split(".")[-2:] == parts[-2:]
        and operation.request_schema == chosen.request_schema
    }


def _creating_evidence(coverage: dict[str, Any], spec: Spec) -> list[tuple[str, str, api_spec.Operation]]:
    """Return `(where, ref, operation)` for every cited operation that creates.

    `ext:` is declared to be outside the NCP's API, so it names no operation
    here. See `operation_refs.creates` for what counts as a creation.
    """
    found: set[tuple[str, str]] = set()
    for mapping in coverage["mappings"]:
        for where, ref in mapping_evidence(mapping):
            if not ref.startswith(EXTERNAL_PREFIX):
                found.add((where, ref))
    creating: list[tuple[str, str, api_spec.Operation]] = []
    for where, ref in sorted(found):
        operation = operation_refs.resolve(ref, spec)
        if operation is not None and operation_refs.creates(operation, spec):
            creating.append((where, ref, operation))
    return creating


def _named_operations(coverage: dict[str, Any], module_plan: dict[str, Any]) -> set[str]:
    """Every operation the artifact names, whatever it goes on to say about it.

    Cited as evidence, planned, offered as a scope alternative, or excused all
    count. The only question here is whether the analysis looked at it.
    """
    named = {ref for mapping in coverage["mappings"] for _, ref in mapping_evidence(mapping)}
    for layer in module_plan.get("layers", {}).values():
        for resource in layer.get("resources", []) or []:
            named.update((resource.get("operations") or {}).values())
            named.update(resource.get("scope_alternatives") or [])
    named.update(
        entry["operation"]
        for entry in module_plan.get("unplanned_operations") or []
        if isinstance(entry, dict) and entry.get("operation")
    )
    return named


def _excused_operations(module_plan: dict[str, Any]) -> set[str]:
    """Return the operations the plan deliberately leaves unplanned."""
    declared = module_plan.get("unplanned_operations") or []
    return {
        entry["operation"]
        for entry in declared
        if isinstance(entry, dict) and entry.get("operation") and entry.get("reason")
    }


@dataclass
class Context:
    requirements: dict[str, dict[str, Any]]
    coverage: dict[str, Any]
    #: None when no specification was supplied. See `resolve_evidence`.
    spec: Spec | None
    #: Where the artifact was read from.
    coverage_path: Path
    #: Every specification this run was given, unmerged. `spec` is what they
    #: mean together; this is which files said it, so a gate can check the
    #: analysis against the documents rather than against a claim about them.
    spec_files: tuple[Path, ...]
    #: The `module_plan` section of `coverage.yaml`. One artifact holds both
    #: the per-requirement analysis and the resource projection it justifies,
    #: so a resource can cite a part in its own document and the NCP and spec
    #: revision are stated once.
    module_plan: dict[str, Any] | None
    #: Each supplied specification read alone, so a gate can say which
    #: document a reference resolves into.
    documents: dict[Path, Spec]


#: Declared order. `check` stops at the first failure.
GATES: tuple[tuple[str, Callable[[Context], GateResult]], ...] = (
    ("coverage-schema", gate_coverage_schema),
    ("spec-documents", gate_spec_documents),
    ("evidence", gate_evidence),
    ("layers", gate_layers),
    ("module-plan", gate_module_plan),
    ("plan-completeness", gate_plan_completeness),
    ("requirement-types", gate_requirement_types),
    ("parent-completeness", gate_parent_completeness),
    ("nested-completeness", gate_nested_completeness),
    ("scope-choice", gate_scope_choice),
    ("update-choice", gate_update_choice),
    ("own-object", gate_own_object),
    ("own-methods", gate_own_methods),
    ("method-conditions", gate_method_conditions),
    ("open-fields", gate_open_fields),
    ("shared-create", gate_shared_create),
)


def build_context(args: argparse.Namespace) -> Context:
    try:
        # Several documents merge into one. An NCP is not always one API:
        # GCP spreads the four layers across Compute, GKE, IAM and KMS.
        documents = {Path(item): api_spec.load(Path(item)) for item in args.spec}
        spec = api_spec.merge(list(documents.values()))
    except FileNotFoundError as error:
        raise InputError(f"no such file: {error.filename}") from error
    except (ValueError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise InputError(f"cannot read specification: {error}") from error
    requirements = Path(args.requirements) if args.requirements else BUNDLED_REQUIREMENTS
    if not requirements.is_file():
        raise InputError(f"no requirement catalog at {requirements}; pass --requirements to name one")
    catalog = load_requirements(requirements)
    coverage = _load_yaml(Path(args.coverage))
    return Context(
        requirements=catalog,
        coverage=coverage,
        spec=spec,
        coverage_path=Path(args.coverage),
        spec_files=tuple(Path(item) for item in args.spec),
        module_plan=coverage.get("module_plan"),
        documents=documents,
    )


def run_gates(ctx: Context, *, only: str | None = None) -> list[GateResult]:
    results: list[GateResult] = []
    for name, gate in GATES:
        if only and name != only:
            continue
        result = gate(ctx)
        results.append(result)
        if not result.ok:
            break
    return results


def command_check(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    results = run_gates(ctx, only=args.gate)
    for result in results:
        if result.ok:
            print(f"PASS  {result.gate}")
            for note in result.notes:
                print(f"        note: {note}")
            continue
        print(f"FAIL  {result.gate}")
        for note in result.notes:
            print(f"        note: {note}")
        for failure in result.failures:
            print(f"        {failure}")
        print(f"\n{result.gate} failed with {len(result.failures)} issue(s). Later gates did not run.")
        return 1
    print(f"\nAll {len(results)} gate(s) passed.")
    for result in results:
        for note in result.notes:
            print(f"  {result.gate}: {note}")
    return 0


#: The file the `report` command writes beside the coverage report. The
#: coverage answer and the plan that implements it have different readers.
MODULE_PLAN_FILE = "module-plan.md"
#: The module plan's heading for the readings that the gates checked. They
#: need no review line by line, so they follow the sections that need one.
REFERENCE = "Reference: readings checked against the documents"


def _report_header(coverage: dict[str, Any], title: str, caveats: bool = True) -> list[str]:
    """The title and the documents the report was read from. The caveats
    qualify the coverage answer, so the module plan points to them and keeps
    its decisions near the top."""
    lines = [
        f"# {title}: {coverage_ncp(coverage) or '?'}",
        "",
        f"- spec: {coverage.get('spec_source', '?')}",
        f"- spec revision: {coverage.get('spec_revision', '?')}",
    ]
    if caveats:
        lines += [f"- caveat: {caveat}" for caveat in coverage.get("caveats", []) or []]
    elif coverage.get("caveats"):
        lines += ["", "Caveats: see `coverage.md`."]
    return [*lines, ""]


def module_plan_lines(ctx: Any) -> list[str]:
    """The module plan report: what the operator approves before any Terraform
    is generated, in the order to review it."""
    planned = [resource for _, resource in _planned_resources(ctx.module_plan)]
    resources = replaced_types.provider_resources(ctx.coverage, ctx.spec_files)
    replaced = replaced_types.derive(ctx.coverage, ctx.spec_files, resources)
    lines = _report_header(ctx.coverage, "Module plan", caveats=False)
    lines += [
        "This file is the plan that the provider, the modules and the tests implement. "
        "Approve it before any Terraform is generated. Review two sections: Decisions to approve "
        "and Changes that replace. They hold the choices that cost money, downtime or data, and no "
        "script can check them for you. Each fact there says where it comes from: a sentence of the "
        "document, a derivation from the request shapes, your own entry, or an assumption. An "
        "assumption is where a wrong fact can hide. The sections under Reference are readings of the "
        "documents that the coverage gates checked against them, so you need not check them line by "
        "line. When a plan fact is unclear or wrong, the provider, scaffold and module steps read the "
        "documents again and propose the correction to you. This file and `coverage.md` are rendered "
        "from `coverage.yaml`: change that file, never this one.",
        "",
    ]
    reached = assumed_fields.live_changes(ctx.coverage, ctx.spec_files)
    silent = {
        str(resource.get("terraform_type")): set(open_fields.silent(resource).get("fields") or [])
        for resource in planned
    }
    unbuilt = requirement_types.report_lines(ctx.coverage, ctx.module_plan)
    if unmentioned := unmentioned_creations.report_lines(ctx.coverage, ctx.module_plan, ctx.documents, ctx.spec):
        unbuilt = [*unbuilt, "", *unmentioned] if unbuilt else unmentioned
    stated = own_methods.replacements(planned, ctx.module_plan, replaced, ctx.spec) if ctx.spec is not None else []
    lines.extend(plan_decisions.report_lines(planned, ctx.module_plan, replaced, unbuilt, stated))
    lines.extend(
        [
            *replaced_types.report_lines(replaced),
            *assumed_fields.review_lines(replaced, reached, silent),
            *silent_update.report_lines(planned, ctx.spec),
        ]
    )
    reference = [
        *method_conditions.report_lines(planned),
        *resource_shape.report_lines(planned, ctx.spec),
        *assumed_fields.reference_lines(replaced, reached, silent),
        *planned_objects.report_lines(ctx.module_plan, ctx.spec),
        *own_methods.report_lines(planned, ctx.module_plan),
        *deprecated_plan.report_lines(resources),
    ]
    if reference:
        lines += [
            "",
            f"## {REFERENCE}",
            "",
            "What the plan read from the documents. The gates checked each reading against them.",
            *(f"#{line}" if line.startswith("## ") else line for line in reference),
        ]
    return lines


def command_report(args: argparse.Namespace) -> int:
    """Write the coverage report, and the module plan report beside it."""
    ctx = build_context(args)
    mappings = {entry["req_id"]: entry for entry in ctx.coverage["mappings"]}
    coverage = ctx.coverage

    lines = _report_header(coverage, "API coverage")
    lines.append("This file answers which requirements the API covers, and which layers can be built.")
    if ctx.module_plan:
        lines[-1] += (
            f" The plan that implements it, with the decisions that you approve "
            f"before any Terraform is generated, is in `{MODULE_PLAN_FILE}` beside "
            f"this file."
        )
    lines.append("")

    totals: dict[str, int] = dict.fromkeys(STATUSES, 0)
    served = requirements_catalog.served(ctx.requirements)
    sections: dict[str, list[str]] = {}
    for req_id, requirement in served.items():
        sections.setdefault(requirement.get("section", "(no section)"), []).append(req_id)

    for section, req_ids in sections.items():
        lines.append(f"## {section}")
        lines.append("")
        lines.append("| req_id | status | layers | area |")
        lines.append("|---|---|---|---|")
        for req_id in req_ids:
            mapping = mappings.get(req_id)
            status = mapping["status"] if mapping else "unmapped"
            totals[status] = totals.get(status, 0) + 1
            layers = ", ".join(mapping_layers(mapping)) if mapping else ""
            requirement = served[req_id]
            area = requirement.get("area") or requirement.get("subsection") or ""
            lines.append(f"| {req_id} | {status} | {layers} | {area} |")
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    for status in ("present", "partial", "absent", "not_applicable"):
        lines.append(f"- {status}: {totals.get(status, 0)}")
    if totals.get("unmapped"):
        lines.append(f"- unmapped: {totals['unmapped']}")
    if ctx.module_plan:
        lines.append(
            f"- creations that the plan does not mention: "
            f"{unmentioned_creations.count(coverage, ctx.module_plan, ctx.documents, ctx.spec)}, "
            f"listed in `{MODULE_PLAN_FILE}`"
        )

    checkable = totals["present"] + totals["partial"] + totals["absent"]
    if checkable:
        score = (totals["present"] + 0.5 * totals["partial"]) / checkable
        lines.append("")
        lines.append(
            f"API-shape coverage score, excluding not_applicable and unmapped: "
            f"{score:.0%} of {checkable} checkable requirements."
        )

    unserved = requirements_catalog.unserved(ctx.requirements)
    if unserved:
        lines.append("")
        lines.append("## Requirements no Terraform integration can answer")
        lines.append("")
        lines.append(
            f"{len(unserved)} requirement(s) ask for a practice -- ticketing, "
            f"escalation, on-call -- rather than for anything an API creates. "
            f"They are listed so the count is visible, and they are outside "
            f"both the analysis above and the score."
        )
        lines.append("")
        lines.append("| req_id | section | area |")
        lines.append("|---|---|---|")
        for req_id, requirement in sorted(unserved.items()):
            lines.append(f"| {req_id} | {requirement.get('section', '')} | {requirement.get('area', '')} |")

    if ctx.module_plan:
        lines.append("")
        lines.append("## Layer verdicts")
        lines.append("")
        lines.append("| layer | verdict | resources | dependency resources | note |")
        lines.append("|---|---|---|---|---|")
        dependencies = 0
        for name, layer in ctx.module_plan.get("layers", {}).items():
            note = layer.get("gap") or layer.get("reason") or ""
            resources = [r for r in layer.get("resources", []) or [] if isinstance(r, dict)]
            needed = sum(1 for r in resources if is_dependency_resource(r))
            dependencies += needed
            lines.append(
                f"| {name} | {layer.get('verdict', 'no module' if name == 'other' else '?')} | "
                f"{len(resources) - needed} | "
                f"{needed} | {note} |"
            )
        if dependencies:
            lines.append("")
            lines.append(
                f"{dependencies} dependency resource(s) are planned because another "
                f"resource's create needs them. No requirement names them, so they "
                f"are counted apart and add nothing to the coverage above."
            )

    texts = ["\n".join(lines) + "\n"]
    if ctx.module_plan:
        texts.append("\n".join(module_plan_lines(ctx)) + "\n")
    if not args.out:
        print("\n".join(texts), end="")
        return 0
    paths = [Path(args.out), Path(args.out).with_name(MODULE_PLAN_FILE)]
    for path, text in zip(paths, texts):
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    return 0


def command_methods(args: argparse.Namespace) -> int:
    """Print each method on each planned object's own path, with what the
    document says of it and where the plan records it, as YAML."""
    ctx = build_context(args)
    if ctx.module_plan is None or ctx.spec is None:
        raise InputError("coverage has no 'module_plan' section to list the methods of")
    wanted = set(args.resource or ())
    planned = [
        (layer, resource)
        for layer, resource in _planned_resources(ctx.module_plan)
        if not wanted or resource.get("terraform_type") in wanted
    ]
    print(
        yaml.safe_dump(
            {"resources": own_methods.methods(planned, ctx.module_plan, ctx.spec)}, sort_keys=False, allow_unicode=True
        ),
        end="",
    )
    return 0


def command_fields(args: argparse.Namespace) -> int:
    """Print, as YAML, each planned type's update with what limits it, and
    each field that the plan leaves open, with its description and the
    descriptions of the fields inside it."""
    ctx = build_context(args)
    if ctx.module_plan is None or ctx.spec is None:
        raise InputError("coverage has no 'module_plan' section to list the open fields of")
    listed = open_fields.listing(ctx.coverage, ctx.spec, ctx.spec_files, set(args.resource or ()))
    print(yaml.safe_dump({"resources": listed}, sort_keys=False, allow_unicode=True, width=100), end="")
    return 0


def command_stamp(args: argparse.Namespace) -> int:
    """Record which skill produced this artifact.

    Written by the script rather than by the agent that wrote the analysis. An
    agent-authored stamp is one that can be copied stale from a previous file,
    which is the failure the stamp exists to catch.
    """
    path = Path(args.coverage)
    if not path.is_file():
        raise InputError(f"no such file: {path}")
    stamp = provenance.current()
    path.write_text(
        provenance.apply_stamp(path.read_text(encoding="utf-8"), stamp),
        encoding="utf-8",
    )
    print(f"stamped {path}: {stamp.skill} v{stamp.version} {stamp.digest}")
    return 0


def command_operations(args: argparse.Namespace) -> int:
    """List the operations a specification defines, with their SDK symbols.

    This is how an analyst finds the identifier to cite, and how the identifier
    is turned back into the SDK method the provider will call. Both directions
    matter: the evidence has to be checkable, and it has to be usable by whoever
    writes the Terraform provider afterwards.
    """
    if not args.spec:
        raise InputError("--spec is required to list operations")
    spec = api_spec.load_many([Path(item) for item in args.spec])
    if not spec.has_operation_ids:
        print(
            f"this {spec.kind} specification defines no operation identifiers; cite evidence as 'METHOD /path'",
            file=sys.stderr,
        )
        return 3

    needle = (args.find or "").lower()
    matched = [
        operation
        for operation in spec.operations.values()
        if needle in operation.operation_id.lower() or needle in operation.path.lower()
    ]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "evidence": f"{OPERATION_PREFIX}{operation.operation_id}",
                        "http_method": operation.http_method,
                        "path": operation.path,
                        "sdk_symbol": sdk_symbol(operation.operation_id),
                    }
                    for operation in sorted(matched, key=lambda item: item.operation_id)
                ],
                indent=2,
            )
        )
        return 0 if matched else 1

    for operation in sorted(matched, key=lambda item: item.operation_id):
        print(f"{OPERATION_PREFIX}{operation.operation_id}")
        print(f"    {operation.http_method} {operation.path}")
        symbol = sdk_symbol(operation.operation_id)
        if symbol:
            print(f"    sdk: {symbol}")
    print(f"\n{len(matched)} of {len(spec.operations)} operation(s).", file=sys.stderr)
    return 0 if matched else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api_coverage_report",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("check", "run the gates in order and stop at the first failure"),
        ("report", "write the human-readable coverage report"),
        ("operations", "list the operations a specification defines"),
        ("methods", "list each method on each planned object's own path"),
        ("fields", "list each update and each field that the plan leaves open"),
        ("stamp", "record which skill produced this coverage file"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument(
            "--coverage",
            required=name != "operations",
            help="path to coverage.yaml",
        )
        child.add_argument(
            "--requirements",
            help="requirement catalog (default: the copy bundled with this skill)",
        )
        child.add_argument(
            "--spec",
            action="append",
            required=name != "stamp",
            metavar="SPEC",
            help=(
                "path to an NCP API specification, OpenAPI 3.x or a Google "
                "Discovery document; repeat for an NCP that spans several APIs. "
                "Required: the specifications are supplied to this skill, never "
                "found by it"
            ),
        )
        if name == "check":
            child.add_argument("--gate", choices=[gate for gate, _ in GATES], help="run one gate only")
        if name == "report":
            child.add_argument("--out", help="write here instead of standard output")
        if name in ("methods", "fields"):
            child.add_argument("--resource", action="append", help="one planned terraform_type; repeatable")
        if name == "operations":
            child.add_argument("--find", help="substring to match against the id or the path")
            child.add_argument("--json", action="store_true", help="emit JSON instead of a listing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            return command_check(args)
        if args.command == "report":
            return command_report(args)
        if args.command == "operations":
            return command_operations(args)
        if args.command == "methods":
            return command_methods(args)
        if args.command == "fields":
            return command_fields(args)
        if args.command == "stamp":
            return command_stamp(args)
    except InputError as error:
        print(f"api_coverage_report: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
