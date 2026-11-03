---
name: ncp-tf-module
description: Generates one NCP layer module (iam, network, cluster, or compute) from the provider's exported schema and the module plan. Use for layer module generation, variables.tf/outputs.tf derivation, module scaffolding.
version: 0.1.0
metadata:
  author: NVIDIA Corporation
  tags: [ncp, terraform, module, codegen, hcl]
---

# NCP Terraform Module Generate

## Purpose

Generate one canonical Terraform layer module for an NCP -- `iam`, `network`,
`cluster`, or `compute` -- from two inputs that are already reviewed: the
`module_plan` section of `coverage.yaml`, and the provider's own
`provider-schema-export.json`.

This is the step `ncp-tf-provider` names and does not perform: the module
generator that its export, "the seam", feeds.

## Prerequisites

- `uv`, to run the bundled script.
- **An approved `coverage.yaml` with a `module_plan` section**, the same
  contract `ncp-tf-provider` read. It names this layer's resources; nothing
  here may add one. The pinned API documents it records are the ground truth:
  when the plan is unclear about a resource or a field this module exposes, or
  a document contradicts it, read the document, expose nothing it does not
  confirm, and tell the operator what each says. Propose the fix to
  `coverage.yaml` (`field_overrides` where no condition states it); apply it
  on approval, rerun the check. Nothing is better than something wrong.
- **`provider-schema-export.json`**, produced by `ncp-tf-provider`'s own last
  step (`terraform providers schema -json`), and **the provider's reviewed
  schema files**, which its schema step wrote (`provider-schema.json`, or one
  file for each resource). Read these and nothing else about the provider. Do
  not read the provider's Go source, for the same reason the provider skill
  does not read the acceptance tests: code written by reading what it will be
  checked against, or what it wraps, can be shaped to fit rather than shown to
  be right.
- **A provider that already builds and passes its own self-checks.** If
  `provider-schema-export.json` is missing entirely, stop and say the
  provider step has not run at all -- a module wrapped around nothing
  satisfies nothing.
  **If it is only missing some of what `module_plan` names for this layer**,
  that is not a stop. Generate a module for the resources the provider does
  implement; `derive` names the rest on stderr and in its section of the
  module's `README.md`: ship the supported subset, state the gap. A provider
  that lags an approved plan is the ordinary state of an NCP built one
  section at a time, not a failure.
- **`references.yaml`, optional.** Absent, it generates a complete module
  with every cross-resource attribute left as an opaque, caller-wired
  string. Present, it names which attributes refer to which sibling
  resource type in this same layer -- see step 3 -- and this skill generates
  the key's resolution to an id and a worked example from each entry, not
  only a variable description.

**The module tree already exists.** `ncp-tf-scaffold` laid it out and wrote
the module-tier test case (the
`module` contract: `variables_accepted`, `outputs_resolve`, `plan`) into it.
Write into that tree; do not create a second one. If no such tree exists,
stop and say the scaffold step has not run.

Read the case there before writing a line. Its `judged.variables` and
`judged.outputs` name the values a reviewer chose to exercise the module's own
wiring; a module whose variable names or types do not match that case fails
the case, not the review.

### Where the HCL goes

The layout is fixed: every layer module this kit generates has the same shape,
and a blueprint composes modules by that shape, not by their internals.

```text
main.tf          resource blocks, one per resource type, for_each over its variable
variables.tf     one map(object(...)) variable per resource type
outputs.tf       one output per resource type, the whole resource object, same keys
versions.tf      required_providers, pointed at the provider's placeholder source
examples/default/  the wiring of each confirmed reference, as comments; only with a confirmed reference
```

No file here is generated freehand. Each traces to `provider-schema-export.json`
(the attributes) or to `module_plan` (which resource types exist at all). **When the generator writes a file that Terraform refuses, report it to the operator as a kit defect, name the script and the message, and stop.** Do not edit its output by hand, run it through a script of yours that changes what it does, or edit the installed kit: the next generation or install undoes each one without a sign, and the defect stays hidden. If the operator refuses a kit change, say that the step waits for the kit fix.

## Half of this module is written once; the rest is generated

The same split `ncp-tf-provider` draws, at the HCL layer instead of the Go
layer.

| Fixed -- written once for the repository, not per layer | Derived -- generated per NCP, per layer |
|---|---|
| the map-of-object variable convention (`terraform-aws-vpc`'s shape, not a per-resource variable) | the object type of each map, from the resource's own attributes |
| `for_each` as the only way a resource block is written | which resource types this layer holds, from `module_plan` |
| whole-object outputs, keyed the same as their variable | which attributes are visible on each, from the schema export |
| `versions.tf`'s `required_providers` block shape | the provider's placeholder source address, read from the schema export's `provider_schemas` key |
| `.terraform-docs.yml`, CI, semver -- the scaffold's project template | nothing; the template is not regenerated per layer |

The less that is generated, the less can be generated wrong. This module
never re-derives an attribute's `Required`/`Optional`/`Computed` status --
that judgment was already made once, by the provider skill, and lives in
`provider-schema-export.json` and in the reviewed schemas behind it. Reading
it twice, differently, is how a module and its provider disagree about a field
neither document changed.

## Instructions

### 1. Read the export and the reviewed schemas, not the API specification

```bash
uv run scripts/module_generator.py derive \
  --module-plan <path>/coverage.yaml \
  --schema-export <path>/provider-schema-export.json \
  --provider-schema <path>/provider-schema.json [<path>/...] \
  --layer <iam|network|cluster|compute> \
  --references <path>/references.yaml \
  --out <path>
```

`--references` is optional. Omit it and every cross-resource attribute is
generated as an opaque string with no key resolution and no worked wiring --
correct, and strictly less generated than with it, never wrong.

`derive` runs `terraform fmt` on the files it writes. When Terraform is not on
the path, it writes a note on stderr and leaves the output unformatted. The
`terraform fmt -check` of step 5 then fails, so install Terraform and run
`derive` again.

`provider-schema-export.json` is Terraform's own schema output: it already
states `Required`/`Optional`/`Computed` per attribute, because Terraform
computed that once, correctly, when the provider was built. Re-deriving it
from the API specification here would let this module and its provider
disagree about a field neither one changed on purpose.

`--provider-schema` takes every reviewed schema file that holds this layer's
resources. The export cannot say which fields an item of a list or object
must hold when the API fills that value: inside it, the provider marks every
field optional and computed. A module input has no read, so a caller that
writes an item must write each field that the API requires in it. `derive`
reads that from each attribute's `behaviour`, and refuses a resource that no
file holds.

Take only the resources `module_plan.layers.<layer>.resources` names.
A resource the export carries but the plan does not assign to this layer is
not this layer's to wrap -- it belongs to whichever layer's plan does name
it, or to none, and either way this skill leaves it alone.

### 2. One variable per resource type -- a map, never a singleton

**Name the variable as the sample modules name the concept.** The sample
modules are the hand-written layer modules in the operator's Terraform group.
They use one name for a common concept on every cloud. Where a resource is one of
these concepts, propose the name in `references.yaml`, and have the operator
confirm it:

| Concept | Variable and output |
|---|---|
| a network, as a map | `virtual_private_clouds` |
| a subnet | `subnets` |
| a node pool | `node_pools` |
| a NAT gateway | `nat_gateways` |
| a route table | `route_tables` |
| a security group | `security_groups` |

```yaml
variables:
  example_subnetwork: subnets
```

Any other resource keeps the plural of its logical kind. `derive` uses the
name for the variable, the `for_each`, the output and the example, and refuses
two resource types under one name.

```hcl
variable "virtual_private_clouds" {
  type = map(object({
    auto_create_subnetworks = optional(bool)
    routing_mode            = optional(string)
    description             = optional(string)
  }))
  default = {}
}
```

Only `Required` and `Optional` attributes appear in the object type.
`Computed` attributes never do -- a caller cannot set what the API assigns,
and a variable that accepted one would let a plan disagree with itself the
same way an always-sent header would.

**The map key is the caller's, not derived.** It is how a consumer names one
instance among several -- `module.network.virtual_private_clouds["primary"]`. The module
reads it into one attribute only: the name default below, where the entry sets
no name. Do not read it into any other attribute.

**An entry that sets no name is named `<prefix>-<key>`, or `<key>` when
`prefix` is unset.** The sample modules in the operator's Terraform group do
this, so a caller names the run once and not in every entry. Which attribute
names a resource is a reading of the NCP, not a fact in the export: one NCP
names a service account by `account_id` and derives its `name` from it.
Propose the attribute for each resource in `references.yaml`, and have the
operator confirm it:

```yaml
name_attribute:
  example_service_account: account_id
  example_network: name
  example_cluster: cluster.name   # inside the nested object `cluster`
```

`derive` then adds a `prefix` input, makes the attribute optional in the
variable, and writes the default. A resource with no attribute that names it
is left out of the section. Where the NCP's rules for a name do not fit the
default, for example a length limit or a character the API refuses, choose a
form that works, write it in the module case, and report it to the operator.
Do not stop the module for it.

**A scope that several resources take is one module input, which an entry
overrides.** The sample modules take a project, a region or a location once, as
a default. Propose each scope in `references.yaml`, with the attribute it
fills in each resource, and have the operator confirm it. The attribute can
have another name in another resource:

```yaml
shared:
  region:
    example_subnet: region
    example_router: region_name
    example_cluster: cluster.location
```

`derive` adds the input, makes each attribute optional, and writes the entry's
own value, else the input. A path may pass through a list of objects, for
example `network_interfaces.network`: every element then takes the default. It
refuses an input whose attributes have more than one type, or an input with
the name of a resource variable. A default inside an optional object applies
only when the entry sets that object: writing it into an object the entry left
out would create the object, and change the request. Share only a value
that means the same thing in each resource: a `parent` that is a project for
one resource and an organization for another is two values, not one scope.

### 3. One resource block per type, `for_each` and nothing else

```hcl
resource "ncp_network" "default" {
  for_each = var.virtual_private_clouds

  auto_create_subnetworks = each.value.auto_create_subnetworks
  routing_mode            = each.value.routing_mode
  description             = each.value.description
}
```

Every argument comes from `each.value.<attribute>`, one line per attribute
the variable's object type carries. Do not add an argument the export does
not list for this resource, and do not omit one the variable declares --
the two are generated from the same source in the same step, so they cannot
drift from each other without the script being wrong rather than the
input.

**A dependency resource is created first, by `depends_on` from the plan.**
Where a planned resource in this layer names another in `dependency_of`, the
block of the resource it serves carries `depends_on` on it. Some dependency
resources are named by no field of what they serve: a VPN tunnel names its
gateway and never the forwarding rules the gateway needs, so no reference
orders them, and a tunnel sent first is refused. The order comes from
`module_plan`, which the operator already reviewed, not from a name.

**A reference to a sibling resource type in the same layer is named once, by
a person, and generated from their answer -- the same discipline the
composition contract's `wiring` already uses elsewhere in this kit.** Guessing from an attribute's name is still
refused: a field called `network` is an arbitrary label on one API and a
strict foreign key on another, and this skill does not know which without
being told. What changes from a naive reading of "the caller's to wire" is
that *being told, once,* is enough to generate the wiring itself -- not just
document that it exists.

```yaml
# references.yaml, beside the module -- reviewed once, read every run
references:
  - resource: ncp_subnetwork
    attribute: network
    refers_to: ncp_network
```

**Where a `references.yaml` entry exists for an attribute, generate three
things from it, not one:**

1. The variable's object type keeps the attribute's declared type: the API
   still takes an id, not a map key. The map variable's description gains one
   generated sentence for the attribute: `network names a ncp_network: a key
   of virtual_private_clouds in this module, or one from elsewhere.` An
   attribute inside an HCL object type has no description of its own, so the
   sentence goes in the variable's description.
2. The resource argument resolves a key before it reaches the API. A key is
   the convenience the variable offers, and the API takes an id, so `main.tf`
   writes `network = try(ncp_network.default[each.value.network].id,
   each.value.network)`: a key of `var.virtual_private_clouds` becomes that network's id,
   and anything else passes through as the reference it already is.
   Where the field takes a value the target was assigned rather than its
   id, the entry says which, as `takes: <attribute>`: a forwarding rule's
   `ip_address` takes the address's `address`. Sent the id, the API assigns
   another value, and the read disagrees with the configuration. An
   attribute the target does not have, or one of another type than the
   field, is refused, and the variable's description names what it takes.
3. `examples/default/main.tf`'s worked wiring for this attribute, generated
   rather than left for a reviewer to write by hand. It is written as comments,
   because a runnable instantiation needs a value for every required field and
   no document states those values here.

**Do not generate a `validation` block for a reference.** A value that is not
a sibling key may be an id from elsewhere, and no NCP-neutral rule tells that
from a mistyped key: ids are paths on one NCP, URLs or opaque strings on
another. A check that guesses the shape refuses real ids. A mistyped key passes
through `try()` unchanged, and the API refuses it at apply as a resource that
does not exist.

**An attribute absent from `references.yaml` stays exactly what step 2
already declares: an opaque string, wired by the caller with no help from
this module.** The file is
additive and optional per attribute -- silence is not a refusal to generate,
it is simply nothing having been reviewed yet. `ncp-tf-scaffold`'s module
case does not gate on it; a layer with no `references.yaml` at all still
generates a complete, valid module, just one where every cross-resource
wire is entirely the caller's to make.

**A resource of another layer is an id input.** An attribute that names a
resource another layer creates is a `shared` input, named for what it takes,
for example `network_id` or `firewall_ruleset_id`. The sample modules take it
this way, and the blueprint wires the other layer's output into it, by the
composition contract's `wiring`. `references:` names only resource types this
same run generates. A `refers_to` of another layer is refused, not guessed at
as an id input.

### 4. One output per resource type, the whole object, same keys

```hcl
output "virtual_private_clouds" {
  value       = ncp_network.default
  description = "The virtual_private_clouds this module manages, keyed as declared in var.virtual_private_clouds."
}
```

The whole resource object, not a hand-picked subset of attributes. Every
`Computed` attribute is then reachable by a consumer that needs it --
`module.network.virtual_private_clouds["primary"].self_link` -- without this skill
guessing in advance which ones will be. This is the same shape
`terraform-aws-vpc` and `terraform-aws-iam` already use; a blueprint composes
against it without needing to know this NCP's resources by name.

A `Sensitive` attribute on the underlying resource makes the whole output
sensitive. Do not destructure a sensitive resource into a non-sensitive
output to work around it -- that is the provider's judgment, carried
forward, not this skill's to reverse.

### 5. Run the self-checks, then stop

None of these need an account. All of them read only what this skill wrote
or was given.

Before a commit or a report of done, the module's offline entry point must exit
0; the rule and why are in `ncp-tf-scaffold` ("Done means the offline entry
point exits 0").

| Check | Passes when |
|---|---|
| `terraform fmt -check` | it reports nothing |
| `terraform validate` (against the provider's local mirror) | the module's HCL is internally consistent |
| `terraform-docs markdown table --output-mode inject --output-file README.md .` changes nothing | the table between the `BEGIN_TF_DOCS` markers matches the checked-in variables and outputs |
| the scaffolded module-tier case's `init`/`validate`/`plan_create` steps | Terraform accepts the judged `variables` fixture and produces a plan |

**The module-tier case's `apply_create`/`refresh`/`destroy` steps need a real
account**, because an output resolving to something other than `(known after
apply)` is not provable by a plan -- only an apply shows whether the wiring
inside the module, not just the provider underneath it, produced the value a
consumer would read. Running those steps is the operator's decision, the
same way running any live test is; this skill's own self-checks stop at
`plan_create`.

**Do not edit the module-tier case, and do not read its assertions.** Read
only its `judged.variables` and `judged.outputs`. `ncp-tf-scaffold` wrote it
before this module existed, from the same discipline the provider tests are
protected by: a module written to satisfy a case it has read can be shaped
to that case rather than shown to fit it.

## Rules that decide the content

**Generate only the resource types `module_plan` assigns to this layer.** A
resource the export carries but the plan does not name for this layer stays
out, however completely the provider implements it.

**Never invent an attribute, a default, or a validation rule the export does
not carry.** The export already encodes every judgment call the provider
skill made -- `Required`/`Optional`/`Computed`, `Sensitive`, which name is
`api_name` versus the Terraform-facing `name`. This skill reads that
encoding, and the reviewed schemas for the fields an item must hold; it does
not re-derive either and does not second-guess them.

**A layer scored `partial` in `module_plan` generates a module for the
resources it does build.** **The module's `README.md` is the scaffold's.** Its
render writes the run, the layout and `module_plan.layers.<layer>.gap`; `derive`
and `terraform-docs` fill their own markers. Write nothing else into it: your
report goes in your answer. Change the render's section only for a part that
cannot work, and say in your answer what you changed and why.

**A `not_buildable` layer gets no module directory at all.** An empty
`terraform-<ncp>-<layer>/` implies capability that is not there.
This skill refuses to run against a `not_buildable` layer rather than
produce an empty or placeholder one.

**The provider's source address is a placeholder, and this module states
that too, not a real one.** `versions.tf`'s `required_providers` block
carries the address that the schema export names: the single key of its
`provider_schemas`. Terraform resolved that address when it exported the
schema, so it cannot drift from the provider. Do not choose a namespace here,
and do not state it as settled.

**The provider's local name is the resource types' prefix.** Terraform gives a
resource the provider named by its type up to the first underscore:
`my_isv_instance` belongs to `my`. `versions.tf` declares that name, so the
module requires exactly the provider its resources use. A name taken from the
NCP's own spelling is one no resource refers to, and Terraform then looks for
a second provider of the prefix's name. A layer whose types have more than one
prefix is refused.

**`versions.tf` requires Terraform `>= 1.9.5`.** Every Terraform file the kit
writes requires the same floor, from `scripts/terraform_floor.py`.

**Do not read the module-tier test case's assertions before generating.**
Read its `judged.variables`/`judged.outputs` only to confirm the variable
and output *names and shapes* this skill is about to emit will satisfy what
a reviewer already committed to needing -- not to write the module in a way
that merely passes what the case checks.

## Scripts

| Script | Command | Purpose |
|---|---|---|
| `scripts/module_generator.py` | `derive` | Writes `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`, and only its own section of `README.md`, for one layer from `module_plan`, a schema export and the reviewed schemas (`--provider-schema`), and `examples/default/main.tf` when a reference is confirmed. `--provider-version` (default `0.1.0`) is the provider's version constraint in `versions.tf`. `--layer` is required; refuses a layer absent from `module_plan` or scored `not_buildable`. |
| `scripts/module_generator.py` | `diff` | Compares the settable attributes and `timeouts` of each planned resource type in two schema exports (`--old-schema-export`, `--new-schema-export`), and reports each added, removed and retyped one. It says which export holds a type that only one holds. It reads no `references.yaml` and writes no file. Exits 0 for no change, 1 for a change, 2 for a refusal. Run this before regenerating an already-reviewed module. |
| `scripts/module_generator.py` | `references init` | Scans `provider-schema-export.json` for settable string attributes whose name holds a sibling resource type's `logical_kind` in this layer as whole words (`address_group_id` holds `address_group`), and writes them into `references.yaml` as **unanswered** entries (`refers_to: null`) for a person to confirm or reject. It never writes a `refers_to` value itself -- that is still the one thing this skill refuses to guess. |

## Examples

**A layer with a reviewed within-layer reference.** `network`'s plan names
both `ncp_network` and `ncp_subnetwork`; the export shows `ncp_subnetwork`
takes a `network` string argument. `references init` notices the name
collision and proposes an unanswered entry; a reviewer confirms
`refers_to: ncp_network`, and names the two variables `virtual_private_clouds`
and `subnets`. From that confirmed line, `derive` generates
all three of: the sentence on `network` in the description of `subnets`, the
`try()` that turns a `var.virtual_private_clouds` key into that network's id,
and `examples/default/main.tf`, which shows as comments that
`subnets.*.network` may name a key of `virtual_private_clouds` -- none of
them hand-written.

**The same layer, before anyone confirms the reference.** With the entry
still `refers_to: null`, `derive` produces the identical resource blocks,
variables and outputs, minus the generated sentence, the key resolution,
and the example file -- a complete, valid module either way,
just one where that attribute is an unremarked opaque string.

**A partial layer.** `compute`'s plan builds `ncp_instance`, `ncp_image`,
`ncp_disk`, and `ncp_reservation`, and states that bare-metal and BMC
resources are absent. The module has four resource blocks, four variables and
four outputs; its README states the gap, as the scaffold's render wrote it.

## Limitations

- **A module that validates is not a module that applies correctly.** This
  skill's own checks stop at `plan_create`. Whether an output actually
  resolves to a working value, and whether a within-layer reference actually
  wires to a live resource, is proven only by the module-tier case's live
  run -- which this skill does not run, per the same ordering discipline
  `ncp-tf-provider` follows for its own acceptance tests.
- **A within-layer reference nobody has confirmed in `references.yaml` is
  generated as an opaque string, correctly, and with nothing extra.**
  `references init` proposes candidates from name collisions, but a
  candidate is not a confirmation -- an entry with `refers_to: null` is
  read the same as no entry at all. A real dependency between two resource
  types in the same layer that `init` did not happen to name (a reference
  by an attribute whose name matches nothing, for instance) still surfaces
  only when a human reviews the module's variables against the API's own
  documentation and adds the line by hand.
- **No account is used**, so nothing here proves the module works, only that
  it is internally consistent.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `layer '<x>' has no module_plan entry` | The coverage artifact's `module_plan` does not name this layer. Run `ncp-tf-coverage` first, or check the `--layer` spelling. |
| `layer '<x>' is not_buildable` | Correct refusal. There is nothing to wrap; do not ask this skill to produce a placeholder. |
| `not found: <path>` | The file at `<path>` does not exist. For the schema export, `ncp-tf-provider` has not exported it yet, or the path is wrong. |
| `note: N planned resource(s) not yet in the provider, generating the rest` | Not an error. `module_plan` names more than the provider currently implements; the module was generated for what does exist, and the rest is listed in `derive`'s section of its `README.md`. When `derive` also notes `wrote no README`, the scaffold's render has not written the README: render the module's case with `--runner-out`, then run `derive` again. |
| `no provider schema holds [<type>, ...]` | `--provider-schema` names no file that holds a resource this layer wraps. Pass each reviewed schema file that the provider's schema step wrote. |
| `none of layer '<x>'s planned resources are in the schema export` | The provider step has not started for this layer at all -- there is nothing yet to wrap, unlike the partial case above. |
| `Error: Invalid value for input variable` in the module-tier case's `terraform test` | A value in the case's `judged.variables` does not fit the variable type this run generated. `terraform test` does not report a variable name that the module does not declare, so also compare the names in `judged.variables` with `variables.tf`. Do not change the case to match -- it was reviewed first. Check whether `module_plan` or the schema export changed since the case was written. |
| `references.yaml: '<resource>.<attribute>': this layer does not generate <resource>` | The entry names a resource of another layer, or one the provider does not implement yet. The module has no block to carry the reference. Correct the `resource`, or delete the entry. |
| `references.yaml: '<resource>.<attribute>': <attribute> is not a settable top-level attribute of <resource>` | The attribute is misspelled, computed only, or inside a nested object, or the export no longer has it. `main.tf` resolves a reference only on a top-level argument. Correct the `attribute`, or delete the entry. |
| `references.yaml: '<resource>.<attribute>' refers to '<x>', which this layer does not generate` | Either a typo, or the entry describes a resource of another layer. Take that resource as an id input in `shared`, and let the blueprint wire it. |
