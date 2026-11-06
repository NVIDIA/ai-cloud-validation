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
"""Derive one NCP layer module from a provider's own exported schema.

`ncp-tf-provider` states the seam and does not cross it: it writes
`provider-schema-export.json` (`terraform providers schema -json`) for exactly
this reason. Everything here reads that export and `module_plan` -- never the
provider's Go source, never the pinned API specification again. Re-deriving a
field's Required/Optional/Computed status from the specification here would let
this module and its provider disagree about a field neither one changed. The one
fact the export cannot carry, a field that the API requires in an item of a
value the read fills, comes from the provider's reviewed schemas
(`required_items`).

`derive` emits variables.tf, main.tf and outputs.tf for one layer: one
map(object(...)) variable per resource type, one for_each resource block, one
whole-object output. `diff` compares two schema exports for the resources one
layer holds, without writing. `references init` proposes -- never answers --
candidate within-layer attribute references, from name collisions alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import module_diff
import module_files
import module_shape
import required_items

#: The one block_types key this version understands. Anything else present on
#: a resource's block is a nested block this generator has no rule for yet,
#: and it stops rather than guess a shape for it.
KNOWN_BLOCK_TYPES = {"timeouts"}


#: The layers a module exists for.
MODULE_LAYERS = ("iam", "network", "cluster", "compute")
#: The plan's group for the types that no layer describes. It is accepted as a
#: `--layer` so that the refusal can say why it has no module.
NO_MODULE = "other"


def _fail(message: str) -> None:
    print(f"module_generator: {message}", file=sys.stderr)
    raise SystemExit(2)


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        _fail(f"not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> Any:
    if not path.exists():
        _fail(f"not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Reading module_plan and the schema export
# --------------------------------------------------------------------------


def layer_resources(module_plan: dict[str, Any], layer: str) -> list[dict[str, Any]]:
    """Return module_plan's resource entries for one layer, refusing what it must."""
    layers = module_plan.get("module_plan", {}).get("layers", {})
    if layer == NO_MODULE:
        _fail(f"'{NO_MODULE}' has no module: the provider and the tests build its types, and no module holds them")
    if layer not in layers:
        _fail(f"layer '{layer}' has no module_plan entry")
    entry = layers[layer]
    if entry.get("verdict") == "not_buildable":
        _fail(f"layer '{layer}' is not_buildable -- there is nothing to wrap")
    return entry.get("resources", [])


def schema_resources(schema_export: dict[str, Any]) -> dict[str, Any]:
    """Return {terraform_type: resource schema} from a `terraform providers schema -json` export."""
    return schema_provider(schema_export)[1]


def schema_provider(schema_export: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (source_address, {terraform_type: resource schema}).

    The source address is read from here -- the export's own top-level key --
    and nowhere else. It is the same placeholder the provider's own tree
    already uses; this never invents or chooses one.
    """
    provider_schemas = schema_export.get("provider_schemas", {})
    if not provider_schemas:
        _fail("schema export has no provider_schemas")
    if len(provider_schemas) != 1:
        _fail(f"schema export names {len(provider_schemas)} providers; expected exactly one")
    ((address, schema),) = provider_schemas.items()
    return address, schema.get("resource_schemas", {})


# --------------------------------------------------------------------------
# Attribute derivation -- the part that must not guess
# --------------------------------------------------------------------------


@dataclass
class Field:
    name: str
    hcl_type: str
    required: bool


def type_to_hcl(t: Any) -> str:
    """Convert a Terraform JSON type constraint into an HCL type-constraint string."""
    if isinstance(t, str):
        # The JSON schema spells an unconstrained type "dynamic"; HCL spells it any.
        return "any" if t == "dynamic" else t
    if not isinstance(t, list) or not t:
        _fail(f"unsupported type shape: {t!r}")
    kind = t[0]
    if kind in ("list", "set"):
        return f"{kind}({type_to_hcl(t[1])})"
    if kind == "map":
        return f"map({type_to_hcl(t[1])})"
    if kind == "object":
        parts = ", ".join(f"{k} = {type_to_hcl(v)}" for k, v in t[1].items())
        return "object({ " + parts + " })"
    if kind == "tuple":
        parts = ", ".join(type_to_hcl(x) for x in t[1])
        return f"tuple([{parts}])"
    _fail(f"unsupported type shape: {t!r}")
    raise AssertionError("unreachable")


def extract_fields(attributes: dict[str, Any]) -> list[Field]:
    """Return the settable (Required or Optional) fields of an attribute map.

    A Computed-only attribute is excluded: a caller cannot set what the API
    assigns, and a variable that accepted one would let a plan disagree with
    itself. This is recursive -- a nested_type attribute's own fields are
    filtered by the same rule, at every depth.
    """
    fields: list[Field] = []
    for name, defn in attributes.items():
        required = bool(defn.get("required"))
        optional = bool(defn.get("optional"))
        if not required and not optional:
            continue  # Computed-only: not settable, so not in this list at all.

        if "nested_type" in defn:
            nested = defn["nested_type"]
            mode = nested.get("nesting_mode", "single")
            inner = extract_fields(nested.get("attributes", {}))
            obj = render_object_type(inner)
            if mode == "single":
                t = obj
            elif mode in ("list", "set", "map"):
                t = f"{mode}({obj})"
            else:
                _fail(f"unsupported nesting_mode '{mode}' on attribute '{name}'")
        else:
            if "type" not in defn:
                _fail(f"attribute '{name}' has neither type nor nested_type")
            t = type_to_hcl(defn["type"])

        if optional and not required:
            t = f"optional({t})"

        fields.append(
            Field(
                name=name,
                hcl_type=t,
                required=required,
            )
        )
    return sorted(fields, key=lambda f: (not f.required, f.name))


def extract_block_types(block: dict[str, Any]) -> Field | None:
    """Return the resource's `timeouts` field, derived from its own block_types.

    `timeouts` is the one block_types key this generator understands -- it is
    part of the fixed half of a provider (`ncp-tf-provider`'s own fixed
    column), declared as a nested block rather than loose fields, and every
    resource that has one declares it the same way. Any other block_types key
    is a nested block this generator has no rule for, and it stops rather
    than guess a shape for it.
    """
    block_types = block.get("block_types", {})
    unknown = set(block_types) - KNOWN_BLOCK_TYPES
    if unknown:
        _fail(f"resource declares block_types this generator has no rule for: {sorted(unknown)}")
    if "timeouts" not in block_types:
        return None
    timeouts = block_types["timeouts"]
    inner = extract_fields(timeouts.get("block", {}).get("attributes", {}))
    return Field(
        name="timeouts",
        hcl_type=f"optional({render_object_type(inner)})",
        required=False,
    )


def render_object_type(fields: list[Field], indent: str = "    ") -> str:
    if not fields:
        return "object({})"
    inner = "\n".join(f"{indent}{f.name} = {f.hcl_type}" for f in fields)
    close_indent = indent[:-2] if len(indent) >= 2 else ""
    return "object({\n" + inner + f"\n{close_indent}}})"


def derive_resource(terraform_type: str, schema: dict[str, Any]) -> tuple[list[Field], Field | None]:
    block = schema.get("block", {})
    fields = extract_fields(block.get("attributes", {}))
    timeouts = extract_block_types(block)
    return fields, timeouts


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------


def provider_local_name(resources: list[dict[str, Any]]) -> str:
    """The local name Terraform gives the provider these resource types belong to.

    Terraform takes it from a resource type's prefix, up to the first
    underscore: `my_isv_instance` belongs to `my`. A local name taken from
    anywhere else -- the NCP's own name, say -- is one no resource in the module
    refers to, and Terraform looks for a provider of the prefix's name instead.
    """
    prefixes = sorted({str(r["terraform_type"]).split("_", 1)[0] for r in resources})
    if len(prefixes) != 1:
        _fail(f"the layer's resource types name {len(prefixes)} providers ({prefixes}); a module wraps one")
    return prefixes[0]


def pluralize(word: str) -> str:
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "sh", "ch", "x", "z")):
        return word + "es"
    return word + "s"


# --------------------------------------------------------------------------
# HCL rendering
# --------------------------------------------------------------------------

_HEADER = (
    "# Generated by ncp-tf-module. Do not hand-edit -- rerun `derive` and let\n"
    "# module_plan or the schema export change instead.\n\n"
)


#: `{terraform_type: {attribute: referred terraform_type}}`, from the confirmed
#: entries of references.yaml.
Confirmed = dict[str, dict[str, str]]
#: (resource, attribute) -> the attribute of the referred resource it takes,
#: where that is not the id.
Takes = dict[tuple[str, str], str]


def render_variables_tf(
    resources: list[dict[str, Any]],
    schemas: dict[str, dict[str, Any]],
    confirmed: Confirmed | None = None,
    takes: Takes | None = None,
    shape: module_shape.Shape | None = None,
) -> str:
    shape = shape or module_shape.Shape()
    out = [_HEADER, *module_shape.module_variables(shape, type_to_hcl)]
    plurals = {r["terraform_type"]: module_shape.variable_of(shape, r, pluralize) for r in resources}
    for r in resources:
        tt = r["terraform_type"]
        plural = plurals[tt]
        fields, timeouts = derive_resource(tt, schemas[tt])
        all_fields = fields + ([timeouts] if timeouts else [])
        refs = (confirmed or {}).get(tt, {})
        description = f"Map of {tt} keyed by caller-chosen name."
        for attribute, target in sorted(refs.items()):
            taken = (takes or {}).get((tt, attribute))
            description += (
                f" {attribute} names a {target}: a key of {plurals[target]} in this module, or one from elsewhere."
                if not taken
                else f" {attribute} takes a {target}'s {taken}: a key of {plurals[target]} "
                f"in this module, or that value from elsewhere."
            )
        out.append(f'variable "{plural}" {{')
        out.append(f"  description = {json.dumps(description)}")
        out.append(f"  type        = map({render_object_type(all_fields, indent='    ')})")
        out.append("  default     = {}")
        # No validation: a value that is not a sibling key may be an id from
        # elsewhere, and nothing NCP-neutral tells that from a mistyped key. A
        # check that guesses an id's shape refuses real ids; a mistyped key is
        # refused by the API instead.
        out.append("}")
        out.append("")
    return "\n".join(out)


def render_main_tf(
    resources: list[dict[str, Any]],
    schemas: dict[str, dict[str, Any]],
    confirmed: Confirmed | None = None,
    takes: Takes | None = None,
    shape: module_shape.Shape | None = None,
) -> str:
    shape = shape or module_shape.Shape()
    out = [_HEADER]
    waits = waits_for(resources)
    for r in resources:
        tt = r["terraform_type"]
        plural = module_shape.variable_of(shape, r, pluralize)
        fields, timeouts = derive_resource(tt, schemas[tt])
        refs = (confirmed or {}).get(tt, {})
        out.append(f'resource "{tt}" "default" {{')
        out.append(f"  for_each = var.{plural}")
        if waits.get(tt):
            out.append("  depends_on = [" + ", ".join(f"{d}.default" for d in waits[tt]) + "]")
        out.append("")
        for f in fields:
            value = module_shape.entry_value(shape, tt, f.name)
            if f.name in refs:
                # A key of the sibling map is the convenience the variable
                # offers; the API takes an id, so the key is resolved here.
                # Anything else is already a reference and passes through.
                taken = (takes or {}).get((tt, f.name), "id")
                out.append(f"  {f.name} = try({refs[f.name]}.default[{value}].{taken}, {value})")
            else:
                out.append(f"  {f.name} = {value}")
        if timeouts:
            out.append("")
            out.append('  dynamic "timeouts" {')
            out.append("    for_each = each.value.timeouts != null ? [each.value.timeouts] : []")
            out.append("    content {")
            declared = schemas[tt]["block"]["block_types"]["timeouts"].get("block", {})
            operations = [f.name for f in extract_fields(declared.get("attributes", {}))]
            width = max(map(len, operations), default=0)
            out.extend(f"      {op.ljust(width)} = timeouts.value.{op}" for op in operations)
            out.append("    }")
            out.append("  }")
        out.append("}")
        out.append("")
    return "\n".join(out)


def waits_for(resources: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The layer's resources each resource must be created after, from the plan.

    A dependency resource exists because another resource's create needs it,
    and the plan names that resource in `dependency_of`. Some are named by no
    field of what they serve -- a VPN tunnel names its gateway and never the
    forwarding rules the gateway needs -- so a reference cannot order them,
    and a create sent first is refused. Only this layer's resources are
    ordered here; across layers the composition's wiring does it.
    """
    here = {r["terraform_type"] for r in resources}
    waits: dict[str, set[str]] = {}
    for r in resources:
        for served in r.get("dependency_of") or []:
            if served in here and served != r["terraform_type"]:
                waits.setdefault(str(served), set()).add(r["terraform_type"])
    return {served: sorted(needed) for served, needed in waits.items()}


def refuse_a_cycle(waits: dict[str, list[str]], confirmed: Confirmed) -> None:
    """Stop when the create order this module states has no first resource.

    Terraform refuses a cycle only at plan time, naming its own addresses; the
    plan or the reviewed references are what hold it, so they are named here.
    """
    after: dict[str, set[str]] = {served: set(needed) for served, needed in waits.items()}
    for resource, refs in confirmed.items():
        after.setdefault(resource, set()).update(refs.values())

    def visit(node: str, path: list[str]) -> None:
        if node in path:
            loop = " -> ".join([*path[path.index(node) :], node])
            _fail(
                f"the create order has a cycle: {loop}. It comes from module_plan's "
                "`dependency_of` and references.yaml; one of them is wrong"
            )
        for nxt in sorted(after.get(node, ())):
            visit(nxt, [*path, node])

    for start in sorted(after):
        visit(start, [])


def attributes_carry_sensitive(attributes: dict[str, Any]) -> bool:
    """Recursively check every attribute -- settable or computed-only -- for `sensitive`.

    The whole-resource output exposes every attribute the resource has, not
    only the settable ones `extract_fields` returns, so a Computed-only
    sensitive field (an API-issued secret, most often) still has to make the
    output sensitive even though it never reaches a variable or a resource
    argument.
    """
    for defn in attributes.values():
        if defn.get("sensitive"):
            return True
        if "nested_type" in defn and attributes_carry_sensitive(defn["nested_type"].get("attributes", {})):
            return True
    return False


def render_outputs_tf(
    resources: list[dict[str, Any]],
    schemas: dict[str, dict[str, Any]],
    shape: module_shape.Shape | None = None,
) -> str:
    shape = shape or module_shape.Shape()
    out = [_HEADER]
    for r in resources:
        tt = r["terraform_type"]
        plural = module_shape.variable_of(shape, r, pluralize)
        sensitive = attributes_carry_sensitive(schemas[tt].get("block", {}).get("attributes", {}))
        out.append(f'output "{plural}" {{')
        out.append(f"  value       = {tt}.default")
        out.append(f'  description = "The {plural} this module manages, keyed as declared in var.{plural}."')
        if sensitive:
            out.append("  sensitive   = true")
        out.append("}")
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# references.yaml -- proposed, never answered, here
# --------------------------------------------------------------------------


def find_reference_candidates(
    resources: list[dict[str, Any]], schemas: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Propose candidate within-layer references from name collisions alone.

    A candidate is a settable string-typed attribute whose name equals or
    contains a sibling resource's logical_kind. This never writes a
    `refers_to` answer -- only the candidate's existence and the collision
    that suggested it. See SKILL.md step 3: guessing the answer is refused;
    surfacing that a question exists is not.
    """
    sibling_kinds = {r["logical_kind"] for r in resources}
    candidates: list[dict[str, Any]] = []
    for r in resources:
        tt = r["terraform_type"]
        fields, _ = derive_resource(tt, schemas[tt])
        for f in fields:
            if f.hcl_type not in ("string", "optional(string)"):
                continue
            for kind in sorted(sibling_kinds):
                if kind == r["logical_kind"]:
                    continue
                # A whole run of words, not one word: `address_group` is in
                # `address_group_id`, and splitting the name first loses it.
                if f"_{kind}_" in f"_{f.name}_":
                    candidates.append(
                        {
                            "resource": tt,
                            "attribute": f.name,
                            "refers_to": None,
                            "reason": f"attribute name matches sibling logical_kind '{kind}'",
                        }
                    )
    return candidates


def taken_attributes(references: list[dict[str, Any]], schemas: dict[str, dict[str, Any]]) -> Takes:
    """Where a reference takes an attribute of its target other than the id.

    Some fields name another resource by a value it was assigned rather than
    by its id: a forwarding rule's `ip_address` takes the address's IP. Sent
    the id, the API assigns another value, and the read disagrees with the
    configuration. The attribute must exist on the target, or the module
    names nothing.
    """
    found: Takes = {}
    for ref in references:
        taken, target = ref.get("takes"), ref.get("refers_to")
        if not taken or not target:
            continue
        attributes = (schemas.get(target) or {}).get("block", {}).get("attributes", {})
        where = f"references.yaml: '{ref['resource']}.{ref['attribute']}' takes '{taken}'"
        if taken not in attributes:
            _fail(f"{where}, which {target} does not have")
        field = (schemas.get(ref["resource"]) or {}).get("block", {}).get("attributes", {}).get(ref["attribute"]) or {}
        if attributes[taken].get("type") != field.get("type"):
            _fail(f"{where}, a {attributes[taken].get('type')}, for a field of type {field.get('type')}")
        found[(ref["resource"], ref["attribute"])] = str(taken)
    return found


def confirmed_references(
    resources: list[dict[str, Any]],
    references: list[dict[str, Any]],
    schemas: dict[str, dict[str, Any]],
) -> Confirmed:
    """The confirmed entries of references.yaml, refusing one this layer cannot hold.

    An unconfirmed entry (`refers_to: null`) is read the same as no entry at all.
    `main.tf` resolves a reference only on a top-level argument of a resource
    it generates, so an entry anywhere else would be a reviewed answer lost.
    """
    by_type = {r["terraform_type"] for r in resources}
    confirmed: Confirmed = {}
    for ref in references:
        refers_to = ref.get("refers_to")
        if not refers_to:
            continue  # unanswered: read as absent.
        where = f"references.yaml: '{ref['resource']}.{ref['attribute']}'"
        if ref["resource"] not in by_type:
            _fail(f"{where}: this layer does not generate {ref['resource']}; it generates {sorted(by_type)}")
        settable = {f.name for f in derive_resource(ref["resource"], schemas[ref["resource"]])[0]}
        if ref["attribute"] not in settable:
            _fail(f"{where}: {ref['attribute']} is not a settable top-level attribute of {ref['resource']}")
        if refers_to not in by_type:
            _fail(
                f"references.yaml: '{ref['resource']}.{ref['attribute']}' refers to "
                f"'{refers_to}', which this layer does not generate. Take a resource "
                "of another layer as an id input, in `shared`, and let the blueprint wire it"
            )
        confirmed.setdefault(ref["resource"], {})[ref["attribute"]] = refers_to
    return confirmed


def render_example(
    resources: list[dict[str, Any]],
    confirmed: Confirmed,
    takes: Takes | None = None,
    shape: module_shape.Shape | None = None,
) -> str:
    """The worked wiring for each confirmed reference, as a reader follows it.

    Written as comments: a runnable instantiation needs a value for every
    required field of every resource, and no document states those values here.
    """
    shape = shape or module_shape.Shape()
    plurals = {r["terraform_type"]: module_shape.variable_of(shape, r, pluralize) for r in resources}
    lines = [module_files.EXAMPLE_HEADER, "#"]
    for tt, refs in sorted(confirmed.items()):
        for attribute, target in sorted(refs.items()):
            lines += [
                f"# {plurals[tt]}.*.{attribute} may name a key of {plurals[target]}.",
                f"# The module resolves it to {target}.default[<key>].{(takes or {}).get((tt, attribute), 'id')}:",
                "#",
                f"#   {plurals[target]} = {{ primary = {{ ... }} }}",
                f'#   {plurals[tt]} = {{ primary = {{ {attribute} = "primary", ... }} }}',
                "#",
            ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def render_module(args: argparse.Namespace) -> tuple[dict[str, str], str | None, list[str], int, int]:
    """Every file `derive` owns, with every refusal run before one is written.

    Returns the files by name, the example (None without a confirmed
    reference), the planned types the export lacks, and the count of wrapped
    resource types and of confirmed references.
    """
    module_plan = _load_yaml(Path(args.module_plan))
    schema_export = _load_json(Path(args.schema_export))
    all_resources = layer_resources(module_plan, args.layer)
    if not all_resources:
        _fail(f"layer '{args.layer}' has a module_plan entry with no resources")
    provider_address, schemas = schema_provider(schema_export)

    resources = [r for r in all_resources if r["terraform_type"] in schemas]
    schemas = required_items.with_required_items(
        schemas,
        [r["terraform_type"] for r in resources],
        required_items.required_paths(_load_json(Path(p)) for p in args.provider_schema),
        _fail,
    )
    not_yet_implemented = [r["terraform_type"] for r in all_resources if r["terraform_type"] not in schemas]

    if not resources:
        _fail(
            f"none of layer '{args.layer}'s planned resources are in the schema export "
            f"({not_yet_implemented}) -- the provider step has not started for this layer"
        )

    if not_yet_implemented:
        print(
            f"note: {len(not_yet_implemented)} planned resource(s) not yet in the provider, "
            f"generating the rest: {not_yet_implemented}",
            file=sys.stderr,
        )

    confirmed: Confirmed = {}
    takes: Takes = {}
    shape = module_shape.Shape()
    if args.references:
        refs = _load_yaml(Path(args.references)) or {}
        confirmed = confirmed_references(resources, refs.get("references", []), schemas)
        takes = taken_attributes(refs.get("references", []), schemas)
        shape = module_shape.read_shape(refs, resources, schemas, pluralize, _fail)
        schemas = module_shape.with_defaults(shape, schemas)
    refuse_a_cycle(waits_for(resources), confirmed)
    files = {
        "variables.tf": render_variables_tf(resources, schemas, confirmed, takes, shape),
        "main.tf": render_main_tf(resources, schemas, confirmed, takes, shape),
        "outputs.tf": render_outputs_tf(resources, schemas, shape),
        "versions.tf": module_files.render_versions(
            _HEADER, provider_local_name(resources), provider_address, args.provider_version
        ),
    }
    example = render_example(resources, confirmed, takes, shape) if confirmed else None
    return (files, example, not_yet_implemented, len(resources), sum(len(refs) for refs in confirmed.values()))


def cmd_derive(args: argparse.Namespace) -> int:
    files, example, not_yet_implemented, wrapped, references = render_module(args)
    out = Path(args.out)
    notes = module_files.write(out, files, example, not_yet_implemented)
    formatted = module_files.format_module(out)
    for note in [*notes, *([formatted] if formatted else [])]:
        print(note, file=sys.stderr)
    print(f"wrote {out}/variables.tf, main.tf, outputs.tf, versions.tf ({wrapped} resource types)")
    if example is not None:
        print(f"wrote {out}/examples/default/main.tf ({references} confirmed reference(s))")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    module_plan = _load_yaml(Path(args.module_plan))
    old_schemas = schema_resources(_load_json(Path(args.old_schema_export)))
    new_schemas = schema_resources(_load_json(Path(args.new_schema_export)))
    report = module_diff.compare(layer_resources(module_plan, args.layer), old_schemas, new_schemas, derive_resource)
    for note in report.notes:
        print(note, file=sys.stderr)
    print("\n".join(report.changes or [f"no attribute changes for layer '{args.layer}'"]))
    return 1 if report.changes else 0


def cmd_references_init(args: argparse.Namespace) -> int:
    module_plan = _load_yaml(Path(args.module_plan))
    schemas = schema_resources(_load_json(Path(args.schema_export)))
    # The same rule `derive` follows: a planned resource the provider does not
    # implement yet is not in the module, so nothing can refer to it there.
    resources = [r for r in layer_resources(module_plan, args.layer) if r["terraform_type"] in schemas]
    candidates = find_reference_candidates(resources, schemas)

    out_path = Path(args.out)
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    prior: dict[str, Any] = {}
    if out_path.exists():
        prior = _load_yaml(out_path) or {}
        for ref in prior.get("references", []):
            existing[(ref["resource"], ref["attribute"])] = ref

    merged = []
    for c in candidates:
        key = (c["resource"], c["attribute"])
        merged.append(existing.get(key, c))
    # Keep any already-confirmed entries this scan didn't rediscover, rather than drop a review.
    for key, ref in existing.items():
        if key not in {(c["resource"], c["attribute"]) for c in candidates} and ref.get("refers_to"):
            merged.append(ref)

    out_path.write_text(
        # The other sections are reviewed answers this scan has no opinion on.
        yaml.safe_dump({**prior, "references": merged}, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    unanswered = sum(1 for r in merged if not r.get("refers_to"))
    print(f"wrote {out_path} ({len(merged)} candidate(s), {unanswered} unanswered)")
    if unanswered:
        print("review each: confirm refers_to, or delete the entry if the collision is coincidental.")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_derive = sub.add_parser("derive", help="emit variables.tf/main.tf/outputs.tf for one layer")
    p_derive.add_argument("--module-plan", required=True)
    p_derive.add_argument("--schema-export", required=True)
    p_derive.add_argument(
        "--provider-schema", required=True, nargs="+", help="the provider step's reviewed schema files"
    )
    p_derive.add_argument("--layer", required=True, choices=[*MODULE_LAYERS, NO_MODULE])
    p_derive.add_argument("--references", default=None)
    p_derive.add_argument("--provider-version", default="0.1.0", help="version constraint for versions.tf")
    p_derive.add_argument("--out", required=True)

    p_diff = sub.add_parser("diff", help="report attribute changes between two schema exports, for one layer")
    p_diff.add_argument("--module-plan", required=True)
    p_diff.add_argument("--layer", required=True, choices=[*MODULE_LAYERS, NO_MODULE])
    p_diff.add_argument("--old-schema-export", required=True)
    p_diff.add_argument("--new-schema-export", required=True)

    p_refs = sub.add_parser("references", help="propose within-layer reference candidates")
    refs_sub = p_refs.add_subparsers(dest="references_command", required=True)
    p_refs_init = refs_sub.add_parser("init", help="scan for name collisions and write unanswered candidates")
    p_refs_init.add_argument("--module-plan", required=True)
    p_refs_init.add_argument("--schema-export", required=True)
    p_refs_init.add_argument("--layer", required=True, choices=[*MODULE_LAYERS, NO_MODULE])
    p_refs_init.add_argument("--out", required=True)

    args = parser.parse_args(argv)

    if args.command == "derive":
        raise SystemExit(cmd_derive(args))
    if args.command == "diff":
        raise SystemExit(cmd_diff(args))
    if args.command == "references" and args.references_command == "init":
        raise SystemExit(cmd_references_init(args))
    parser.error("unhandled command")


if __name__ == "__main__":
    main()
