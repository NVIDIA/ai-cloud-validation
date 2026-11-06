# Artifact shapes

One artifact: `coverage.yaml`, written into the operator's repository and
reviewed by a person before anything downstream reads it.

It holds two sections. `mappings` is the per-requirement analysis — can this
NCP's API satisfy each requirement. `module_plan` is the resource projection
that analysis justifies — which Terraform resources exist, and which layer owns
each one. They live together because they describe one NCP at one spec revision:
stated once, they cannot disagree, and a resource cites a requirement part in
its own document.

## Contents

- [coverage.yaml](#coverageyaml)
  - [Finding an identifier, and mapping it back to the SDK](#finding-an-identifier-and-mapping-it-back-to-the-sdk)
  - [`spec_documents`: the documents this analysis read](#spec_documents-the-documents-this-analysis-read)
  - [`status`: what the four values mean](#status-what-the-four-values-mean)
  - [`generator`: which skill wrote this](#generator-which-skill-wrote-this)
  - [`note`: required where no evidence can carry the claim](#note-required-where-no-evidence-can-carry-the-claim)
  - [Requirement parts](#requirement-parts)
- [The `module_plan` section](#the-module_plan-section)
  - [`unplanned_operations`: a creation you chose not to plan](#unplanned_operations-a-creation-you-chose-not-to-plan)
  - [`not_planned`: a requirement that no type serves](#not_planned-a-requirement-that-no-type-serves)
  - [A deprecation: what the plan leaves out](#a-deprecation-what-the-plan-leaves-out)
  - [A child you cite names its parents](#a-child-you-cite-names-its-parents)
  - [A dependency resource: one another resource's create needs](#a-dependency-resource-one-another-resources-create-needs)
  - [Why the operations are recorded](#why-the-operations-are-recorded)
- [Layers](#layers)

## coverage.yaml

Per requirement: does this NCP's API expose a surface that could satisfy it, and
which layer or layers does that surface belong to.

```yaml
ncp: <name>                       # established with the operator, recorded here
spec_source: <url>
spec_revision: <immutable revision>
spec_documents:                   # every supplied document; see below
  <api>:
    file: <repository-relative path>
    digest: sha256:<hex>
caveats:
  - <anything a reader must know before trusting this file>

mappings:
  - req_id: CNP01
    status: present               # present | partial | absent | not_applicable
    layers: [compute, network]    # zero or more of iam network cluster compute other
    evidence:
      - "POST /api/v1/instance-operations/launch"
      - "schema:Instance.status"
```

`evidence` takes three forms, in this order of preference.

| Form | Example | Use when |
|---|---|---|
| `op:<identifier>` | `op:compute.instances.updateNetworkInterface` | The specification names its operations. **Prefer this.** |
| `METHOD /path` | `POST /api/v1/instance-operations/launch` | The specification defines no identifiers. |
| `schema:Name[.field]` | `schema:Instance.status` | The evidence is a field, not a call. |
| `ext:METHOD /path` | `ext:POST /api/v1/namespaces/{ns}/services` | The endpoint is real but outside the NCP's own API. |

Every reference must resolve against a specification you were given. The
specifications are an input: if a capability you believe the NCP has is not in
them, say so in a note rather than citing it from memory. `ext:` is for
endpoints that are genuinely outside the NCP's API, not a way to record one you
could not find.

**`ext:` is a declaration, not an escape hatch.** A managed-Kubernetes
requirement may genuinely be satisfied through the Kubernetes API, and a boot
requirement through the instance metadata server. Neither is in the NCP's
specification, so neither can be resolved, and pretending otherwise would make
the gate lie. Marked, the claim survives and the gate reports it rather than
passing it in silence.

The marker is load-bearing in one place: **a `module_plan` resource may never
cite `ext:` evidence.** A generated provider resource cannot create a Kubernetes
object, so a plan resting on one would be generated and then fail. The
module-plan gate rejects it.

**Prefer `op:` whenever the specification supports it.** A REST path is three
unstable things at once: an HTTP verb you can get wrong, a path template with
several legal spellings, and a version prefix. The identifier has one spelling
and carries no verb, so a whole class of mistake cannot be written down. Two
real examples, both impossible to express as an identifier:
`compute.instances.updateNetworkInterface` is `PATCH`, not the
`POST` its name suggests, and `asymmetricSign` sits on `cryptoKeyVersions`, not
on `cryptoKeys` where its neighbour `encrypt` does.

Where the identifier comes from depends on what the NCP publishes — the skill
never assumes a vendor:

| The NCP publishes | The identifier is | Example |
|---|---|---|
| OpenAPI 3.x | `operationId` | `op:firewallRulesSet` |
| Google Discovery | the method `id` | `op:compute.networks.updatePeering` |
| Neither | — | use `METHOD /path`, or record the SDK symbol in a caveat |

### Finding an identifier, and mapping it back to the SDK

Do not guess an identifier. List them:

```bash
uv run scripts/api_coverage_report.py operations \
  --spec <path>/<ncp>-spec.json --find networkInterface
```

It prints the evidence string to paste, the real method and path, and the
generated SDK symbol:

```text
op:compute.instances.updateNetworkInterface
    PATCH /compute/v1/projects/{project}/zones/{zone}/instances/{instance}/updateNetworkInterface
    sdk: InstancesService.UpdateNetworkInterface
```

That last line is why the identifier is the right unit of evidence rather than
merely a safer one. The provider is written against the SDK, and the identifier
is what the SDK is generated from, so one string serves the coverage analysis
and the implementation that follows it. `--json` emits the same thing for a
script.

A flat `operationId` has no service type to derive, so no `sdk:` line is
printed. That is deliberate: a fabricated symbol is worse than none.

A requirement may carry several layers. Some requirements — stable identifiers,
for instance — apply to more than one.

### `spec_documents`: the documents this analysis read

Record every document you were given with `--spec`, also when there is only
one. Use one key for each API. Each entry has two fields:

- `file`: the path of the document, relative to the repository root.
- `digest`: `sha256:` and the hex SHA-256 of that file.

The `spec-documents` gate computes the digest of each supplied file again and
compares it. The next step finds every document through this block, and it
refuses an analysis without it.

### `status`: what the four values mean

Apply these definitions rather than inventing them per run. Judgements
drift on the `present`/`partial` boundary unless it is defined. The same
requirement, API and evidence then get different statuses on different runs.

| Status | Means |
|---|---|
| `present` | The API exposes every control the requirement names, and each is cited. |
| `partial` | Some named controls exist and others do not, **or** the requirement includes a guarantee an API shape cannot prove. |
| `absent` | The API exposes none of them. Searched, found nothing. |
| `not_applicable` | No API could satisfy it — it is a document, a physical property, or a contractual commitment. |

**The tie-breaker for `present` against `partial`.** A requirement naming a
*behavioural* guarantee — a timing bound, a throughput figure, an availability
percentage, conformance, a certification, "without reboot", "within five
percent" — is never `present` here. This analysis reads an API shape and runs
no account, so it cannot prove behaviour. Such a requirement is `partial` when
the control surface exists and `absent` when it does not. This single rule
decides most of the boundary.

**The tie-breaker for `absent` against `not_applicable`.** `absent` says this
NCP could expose the capability and does not — another NCP might.
`not_applicable` says no NCP's API could, because the requirement is not about
an API-managed capability at all. SOC 2 coverage is `not_applicable`; a missing
quota endpoint is `absent`.

**Structural presence only.** `present` is a claim about the API surface, never
about the service behind it. It does not assert that a capability works, is
quota-permitted, or is available in a region.

A `present` status must cite evidence — a claim with nothing behind it is
checked by the schema gate. `absent` normally cites none, since a search that
found nothing has nothing to cite; where an inherited analysis cites related
endpoints under `absent`, read it as "these exist and do not satisfy the
requirement" and prefer `partial` if they partly do.

### `generator`: which skill wrote this

Written by `api_coverage_report.py stamp`, never by hand and never by the agent
— an agent-authored stamp is one that can be copied stale from a previous file,
which is the failure it exists to catch.

```yaml
generator:
  skill: ncp-tf-coverage
  version: 0.1.0
  digest: sha256:c808b2ebc2d88f7ba7...
  generated: 2026-09-12
```

The digest is the part that matters. A version does not have to change when
behaviour changes. The digest changes whenever any shipped file does, so
`check` can tell you that an artifact was generated by a different skill than
the one now checking it. That has already happened once — a report was generated against an
installed copy predating two gates, its output looked clean, and only a
directory diff revealed it.

A mismatch is reported, never failed. Re-checking an archived artifact against
a newer skill is a normal thing to do.

### `note`: required where no evidence can carry the claim

`partial` and `not_applicable` must each carry a `note`. Both assert something
the evidence list cannot express: `partial` says something is **missing**, and
what is missing is by definition not among the references cited;
`not_applicable` says the requirement does not apply to this NCP at all, which
is a judgement about the requirement rather than about the API.

`absent` needs none — a search that found nothing has nothing to cite and
nothing to explain. `present` is carried by its evidence.

**Write the note to be falsifiable, not to clear the gate.** The gate can only
check that a note exists; whether it says anything is on you. A useful note
names the surface searched and what it lacked:

> Filestore does not expose filesystem-wide default and per-UID/GID quota APIs.

> NFS clients provide locking, but mount-time flock behavior is not configured
> through the Filestore resource API.

Both can be checked by a reader against the specification. Compare a note that
passes the gate and informs nobody:

> The API does not fully support this requirement.

If the honest note is "I could not determine this", write that. It is worth
more than a confident sentence with no content, and it tells the next reader
where to look.

### Requirement parts

When one requirement covers several concerns, split it into parts instead of
flattening it. Each part has its own layer, status, and evidence:

```yaml
mappings:
  - req_id: CNP01
    status: partial
    parts:
      - part_id: node-lifecycle
        layer: compute
        status: partial
        evidence:
          - "POST /api/v1/instance-operations/launch"
      - part_id: network-configuration
        layer: network
        status: partial
        evidence:
          - "GET /api/v1/firewall-rulesets"
      - part_id: security-configuration
        layer: iam
        status: absent
        evidence: []
```

Rules:

- `parts` and `layers` are mutually exclusive. A split requirement's layers are
  exactly its parts' layers, so a second copy of that fact can only drift.
- `part_id` is unique within the requirement, and is the name the `module_plan`
  section cites. A plan may only cite a part this file declares.
- Most requirements need no split, and keep the flat `layers` list.

One requirement covering compute, network, and iam cannot have one status: the
node lifecycle can be supported while the security side is absent. That is what
parts are for.

**`layers` is a judgement written here.** The layer table in `SKILL.md` says
which catalog headings each layer covers, and nothing cross-checks the choice.
It decides where a resource is filed, not whether it exists or which API calls
implement it, so a wrong layer moves a resource between modules and changes
nothing else.

## The `module_plan` section

Which Terraform resources exist for this NCP, and which layer owns each one.
This is what Phase 3 reads, and the only section it may read to decide what to
generate.

It carries no header of its own. The NCP, the specification, and its revision
are already stated at the top of the file.

**`<ncp>` here is the NCP's name converted to a Terraform identifier**, not the
name as `ncp:` spells it. The name is whatever the operator calls the NCP and
several carry a hyphen; a Terraform identifier carries `a-z`, `0-9` and `_`.
Every other character becomes an underscore, and the result is lowercased, so
`ncp: my-isv` declares `my_isv_instance`. One spelling, because the test
generator builds a Go identifier from the same string and `my-isv_instance`
renders `TestAccMy-IsvInstance`, which Go will not parse. The module-plan gate
checks the converted prefix and names it in the failure.

```yaml
module_plan:
  layers:
    compute:
      verdict: buildable            # buildable | partial | not_buildable | not_applicable
      resources:
        - logical_kind: instance
          terraform_type: <ncp>_instance
          # Only create, read, update and delete. `module-plan` refuses any
          # other verb here, and any key of a resource that this page does
          # not define.
          operations:
            create: "POST /api/v1/instance-operations/launch"
            read:   "GET /api/v1/instances/{id}"
            update: "PATCH /api/v1/instances/{id}"
            delete: "POST /api/v1/instance-operations/terminate"
          # Present only where another call changes the object: each call,
          # and the fields of the object that it changes, or a path into one
          # such as metadata.items. The provider sends a change of those
          # fields through that call alone. Calls that change the same field
          # each give the `value` of the field that they set, such as
          # `value: true` for a disable. The request carries each field: by
          # its name, as a body of the field's own schema, or under the name
          # that `carried_as` gives, such as `carried_as: {disk_size_gb:
          # size_gb}`, where a description says that request field sets it.
          update_calls:
            - operation: "POST /api/v1/instances/{id}/change-type"
              fields: [instance_type_name]
          # Present only where a method's description, a field of its
          # request, the schema that a request field names, or a query
          # parameter limits its call: the method, the kind, and the sentence
          # that says so. Kinds:
          # changes_only, cannot_change, one_way, blocks_delete and
          # create_only name `fields`, and one_way its `direction`, increase or decrease;
          # create_only quotes the sentence that each listed field's own
          # description states, in the request of the operation, and names a
          # nested field by its path there, such as
          # versionTemplate.protectionLevel; needs_stopped
          # takes a handling of refuse or stop_and_start, which names the
          # object's `stop` and `start` operations, and `stopped_when`, the
          # object's field and its value while the object is stopped;
          # marked_deleted, on a delete after which the read still returns
          # the object, names `deleted_when`, the object's field and its
          # value once deleted, and may quote a field that the read returns;
          # one_field_per_request, on an update whose request takes at most
          # one changed field, names nothing else; cannot_change, on the
          # update, names each field that its request carries and that its
          # description says it cannot change, and that field replaces.
          conditions:
            - operation: "PATCH /api/v1/instances/{id}"
              kind: changes_only
              fields: [name]
              quote: <the sentence from the description>
            - operation: "POST /api/v1/instances/{id}/change-type"
              kind: needs_stopped
              handling: refuse
              stopped_when: {field: state, value: STOPPED}
              quote: <the sentence from the description>
          # Present only where the operator states a field fact that no
          # condition can, such as an update that a live run refused: the
          # field or a path into one, replaces, in_place or leave_out, and
          # the operator's reason. The provider applies it after its own
          # derivation.
          field_overrides:
            - field: <a field of the object, or a path into one>
              decision: replaces
              reason: <the operator's reason>
          # Present only where the API never deletes the object: the plan has
          # no delete, or `quote` is the delete's sentence that refuses it.
          # forget removes it from the state; refuse fails the destroy.
          # on_destroy: {handling: forget, reason: <why it is never deleted>}
          # Where the documents state no fact, after the agent read them.
          # `update`: the update's description, request field schemas and
          # query parameters name no limit, so the plan has no changes_only,
          # cannot_change or one_field_per_request on it. It holds the texts that `fields` prints
          # for the update, word for word: `description` (empty when the
          # document has none), and `request_field_schemas` when `fields`
          # prints one. The gate checks them against the document, and the
          # module plan shows them to the operator. `fields`:
          # each field that `check` names as open (only the update's body
          # lets it change, or it replaces beside an unmanaged call) whose
          # descriptions state no sentence about its fate. A conditional
          # limit, such as "only while the state is OFF", states none.
          documents_silent:
            update:
              description: <the update's description, as `fields` prints it>
              request_field_schemas:
                <request field>: <the description of the schema it names>
            fields: [labels, rotationPeriod]
          # A fact about the resource that no other field holds.
          note: <one or two sentences>
          # Present only where the API offers the same body under another
          # parent. Every parent not chosen, and why this one: a choice
          # recorded without a reason is indistinguishable from an oversight.
          scope_alternatives:
            - op:<the parent not chosen>
          scope_note: <why this parent>
          # Present only where `update` is a PUT and the same path takes a
          # PATCH of the same body: why the whole-object replacement.
          update_note: <why the PUT>
          # Present only where the API adds, changes or removes entries inside
          # the object one at a time: each such operation, and how the
          # resource manages those entries.
          entry_methods:
            - op:<an operation on one entry>
          entries_note: <in the create body and the update, by replacement, or not managed>
          # Each other method on the object's own path that the resource
          # does not send, and why. `own-methods` asks about every one.
          unmanaged_methods:
            - operation: op:<the method>
              reason: <why the resource does not send it>
          requirements:
            - req_id: CNP01
              part_id: node-lifecycle

    network:
      verdict: partial
      gap: No create or delete operation for a firewall ruleset.
      resources:
        - logical_kind: firewall_ruleset
          terraform_type: <ncp>_firewall_ruleset
          operations:
            read: "GET /api/v1/firewall-rulesets"
          requirements:
            - req_id: CNP01
              part_id: network-configuration

    cluster:
      verdict: not_buildable
      reason: The API exposes no Kubernetes surface.
      resources: []
  # One reason that covers a method name on every planned type.
  unmanaged_methods:
    - method: setIamPolicy
      reason: <why no type sends it>
```

Read it this way:

- One requirement can appear in two layers. That is the layer tagging in
  `coverage.yaml`, seen from the resource side.
- Four operations on one object make **one** resource, not four.
- A `not_buildable` layer keeps an explicit verdict and an empty resource list.
  An absent capability has to be visible, and must not become a fabricated
  resource.
- A `partial` verdict carries `gap`, because the module ships a subset and the
  report has to say what is missing.
- A `partial` status on a requirement limits what its resource promises. It does
  not delete the resource.

### `unplanned_operations`: a creation you chose not to plan

Every creating operation cited as evidence in `mappings` must be claimed by a
planned resource. The `plan-completeness` gate refuses the rest. It resolves
each `op:` and `METHOD /path` reference to the operation first, so one
operation in two spellings is one operation. An operation creates when:

- its identifier is dotted and its last segment is `create` or `insert`, or
- it has no dotted identifier and it is a `POST` on a collection path: the path
  ends in a literal segment, and the same path plus one parameter segment is
  also in the specification.

An action that makes an object, such as `POST /api/v1/instance-operations/launch`,
has no item path below it, so the gate does not see it as a creation. Read the
document, and plan or excuse it yourself.

Not every creation deserves a resource of its own. A key version belongs to its
key, and a DNS change is an edit to a zone. Say so, per operation:

```yaml
module_plan:
  unplanned_operations:
    - operation: op:cloudkms.projects.locations.keyRings.cryptoKeys.cryptoKeyVersions.create
      reason: A key version is a lifecycle state of its crypto key, not a separate resource.
```

The reason is required. An omission with no stated reason cannot be told apart
from an oversight, and this exception exists to record a decision — not to
provide a quieter way past the gate than adding the resource.

### `not_planned`: a requirement that no type serves

A `present` or `partial` requirement, or part, whose evidence names an object
that the documents create needs a planned type that names it under
`requirements`. Its evidence names such an object when it cites the creation, a
call on an item that a creation makes, or a field of a creation's body. An
excused creation does not serve the requirement: the excuse says why the object
is no resource, not why the requirement gets nothing. A type planned for
another requirement does not serve it either, until it names it. A part needs
a type that names the part.

When nothing is to be built for it, state the reason on the requirement or the
part. The module plan lists it under Decisions to approve:

```yaml
mappings:
  - req_id: SEC08
    status: partial
    layers: [iam]
    evidence: ["op:logging.projects.sinks.create"]
    note: Export exists. Retention for 30 days needs live evidence.
    not_planned: The operator exports audit logs with a tool outside Terraform.
```

The `requirement-types` gate refuses a requirement that names a created object
and has neither, an empty reason, and a reason beside a type that names it.

### A deprecation: what the plan leaves out

The API is retiring a deprecated method, field or type, so the plan does not
plan it. The pinned document marks one with `deprecated: true`, or with a
`Deprecated.`, `Deprecated:` or `[Deprecated]` marker that opens a clause of
its description. The scripts read that mark:

- The provider gives a marked field no attribute, at any depth. The module
  plan lists each one under "Fields the documents mark deprecated".
- `module-plan` refuses a marked operation, update call or entry method. Plan
  the call that replaces it. Record a marked method under `unmanaged_methods`,
  or a marked creation under `unplanned_operations`, with the reason.
- `module-plan` refuses a `leave_out` override on a marked field, because the
  provider already leaves the field out.

The API can still need a marked field: for example, a live create is refused
without it, and no field that the plan keeps replaces it. That is the
operator's decision. Propose a `replaces` or `in_place` override on the field,
with the reason that the API still needs it, and record it when the operator
approves. The provider then keeps that one field, decided as the override
says, and still leaves out every other marked field.

```yaml
field_overrides:
  - field: <the marked field>
    decision: replaces
    reason: <what the live run showed that the API needs, and why no other field states it>
```

A deprecation that no mark states is the operator's correction: a sentence in
other words, such as "This field has been deprecated", or another source, such
as a release note. Propose the correction with the sentence or the source, also
when the operator told you of the deprecation, and record it only after the
operator approves the correction:

- For a field, a `leave_out` override, with the sentence or the source as the
  reason.
- For a type, take it out of `module_plan`. List its create under
  `unplanned_operations`, and give each requirement or part that it served
  `not_planned`, each with the source in the reason. Do not keep the type for
  a later step to skip: the type stays planned, and nobody builds it.

```yaml
module_plan:
  unplanned_operations:
    - operation: op:<the type's create>
      reason: The vendor's release notes of <date> deprecate this API. The pinned document does not say so yet.
```

### A child you cite names its parents

Citing `a.b.c.create` says the collection `a.b` exists. Where the specification
creates `a.b` too, `parent-completeness` asks the artifact to name that creation
as well — as evidence, as a planned resource, or under `unplanned_operations`.
Without a dotted identifier, a creation on `P/{x}/C` names `P` when `P` is a
collection with a `POST`, and the walk stops at the first level that is not a
collection, so a version prefix such as `/v1` asks for nothing.

The mention is the obligation, not the resource. A parent may be excused with a
reason like any other creation. What it may not be is absent: `plan-completeness`
reads only the operations the analysis cites, so never writing a name is cheaper
than omitting it, and a whole resource family can leave without a word against
it.

### A dependency resource: one another resource's create needs

Some resources are planned because no requirement asks for them but a planned
resource cannot be created without them: a tunnel needs its gateway, a ruleset
its address group. Leave one out and the resource that needs it can be tested
only against an object somebody made by hand, and often cannot be used either.

Plan it like any other resource, and name the resources it serves in place of
`requirements`:

```yaml
        - logical_kind: address_group
          terraform_type: <ncp>_address_group
          operations:
            create: "POST /api/v1/address-groups"
            read:   "GET /api/v1/address-groups/{id}"
            delete: "DELETE /api/v1/address-groups/{id}"
          dependency_of:
            - <ncp>_firewall_ruleset
```

- `dependency_of` lists the `terraform_type` of each planned resource whose
  create needs this one. Name what the specification shows: a required field of
  the served resource's create body that holds this resource's identifier.
- Every name must be planned, and following `dependency_of` must reach a
  resource that `requirements` explains. A dependency resource that serves only
  another unexplained one explains nothing.
- A resource a requirement names keeps `requirements`, even when another
  resource needs it too. `dependency_of` is then a note, and the resource counts
  as a requirement resource.
- A dependency resource is counted apart in the report. It serves no
  requirement, so it adds nothing to the coverage figure.

### Why the operations are recorded

The verdict is a claim; the operations are its evidence. You cannot call a layer
buildable without having found the operations that build it, so recording them
costs nothing and lets a reviewer check the claim instead of trusting it.

They are also the input the next two steps need: the provider generator emits
resource schemas and API calls from them, and the test generator writes its
blackbox NCP API checks against them.

## Layers

`iam`, `network`, `cluster`, `compute`. The design document fixes this set and
its names.

`other` holds what no layer's catalog headings describe, such as a file
system. A requirement or a part can take it. A type under `layers.other` in the
`module_plan` section is planned like any other: the provider and the tests
build it, and no module holds it. `other` takes no verdict, because no module
is generated for it.

The `module_plan` section must carry an explicit verdict for **all four** layers. A
plan that simply omits `iam` reads as "not considered", and an absent capability
has to be visible.
