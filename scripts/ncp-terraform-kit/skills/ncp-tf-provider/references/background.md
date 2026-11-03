# Background for `ncp-tf-provider`

Why the fixed rules are fixed, and what a generated provider does not prove.
Read this when a rule in the skill looks arbitrary.

## Contents

- [The package layout](#the-package-layout)
- [Fixed and derived are not the module layers](#fixed-and-derived-are-not-the-module-layers)
- [The mirror and the test harness](#the-mirror-and-the-test-harness)
- [The unit tests](#the-unit-tests)
- [Provider settings](#provider-settings)
- [Examples](#examples)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)

## The package layout

- **`main.go` at the module root.** `tfplugindocs` and GoReleaser both build the
  module root with no package argument. A `cmd/<name>/` entry point is ordinary
  Go and fails both -- `tfplugindocs` reports "no Go files", which names the
  directory and not the cause.
- **The provider under `internal/`.** Provider code is not a library and has no
  API stability, and a package outside `internal/` can be imported by anything.
  HashiCorp's scaffolding and the official google provider both put it in a
  subdirectory; `internal/` is how Go states the rest.
- **Embedded data beside the code that embeds it.** `go:embed` cannot reach a
  parent directory, so generated schemas live inside `internal/provider/` or
  they cannot be embedded at all.

## Fixed and derived are not the module layers

(These are not the four module layers — `iam`, `network`, `cluster`, `compute`.
Those describe which module a resource belongs to. This is about which half of
the provider's code a thing comes from.)

## The mirror and the test harness

`terraform init` must succeed against the local mirror, because that is how a
real consumer resolves a provider and how the module and blueprint tests resolve
this one — they run a real `terraform` against real configuration.

The provider's own acceptance tests do not use the mirror. That harness runs the
provider in the same process as the test and hands Terraform a reattach address,
so it never resolves or downloads the provider at all. Both paths have to work,
and passing one proves nothing about the other.

## The unit tests

The bulk of them cover **expanders and flatteners**, which is what both mature
reference providers call the two translation directions, and what HashiCorp's
own guidance names as the common subject of a provider unit test:

| Function | Direction | What a test catches |
|---|---|---|
| **expand** | configuration → API request | a field silently dropped from the request |
| **flatten** | API response → resource state | a value written to the wrong attribute, or lost |

These are where a generated provider goes wrong most often, because they are the
only place the two shapes are reconciled by hand, and because a mistake surfaces
as a wrong value rather than an error.

Four more, each a single decision worth isolating:

| What | Why it is worth a test |
|---|---|
| deciding an operation reached a terminal state | the enumeration holds more terminal values than the success one |
| extracting the identifier that an operation names, from its first answer and from the completed operation | wrong here means a resource that cannot be refreshed or destroyed |
| building the import identifier, and parsing it back | the round trip must return what it started with |
| mapping an API error to a diagnostic | a swallowed error is reported as success |

Each takes a value in and returns a value out, so none needs a network, a
credential, or a running Terraform.

**These tests will be mutated.** A later step alters the generated provider — a
poll that returns early, a dropped required field, a call never issued — and
every mutation must make some test fail. A surviving mutation means the tests
are decorative.

That constrains the code, not only the tests. **Keep each behaviour in one
place.** A poll loop written once in the fixed runtime can be mutated once and
proven once; the same logic copied into every resource cannot, because
mutating one copy proves nothing about the others.

## Provider settings

Where some creates sit under no account, `SKILL.md` requires the provider to
expose the account as a setting and send it: a value, and a switch that sends
it, each with an environment fallback. Without this, a resource outside the account path fails at create in a way that
names neither the setting nor the resource. The refusal is easy to read as a
permission problem or an unavailable service, and a run that reads it that way
records a limit it does not have and stops asking.

A switch without an environment fallback cannot be turned on by a test. The
account is then named and never sent, every live test runs with the header off,
and the API's refusal reads as a missing permission.

`provider_schema.py settings` refuses an optional provider-block attribute whose description states no
`Falls back to <VARIABLE>`. It reads descriptions, because Terraform's schema carries
no field for a fallback -- so it cannot see one the code has and the description
omits. That is the same defect from the operator's side: nothing then tells the
operator the variable exists.

## Examples

**A first provider for an NCP whose API is mostly asynchronous.**
`measure` reports that most mutating operations return an operation handle.
Derive the schemas, generate the resources with the shared poller wired in,
compile, load the schema, validate, and report — naming every resource where
expansion stopped at a schema cycle or the depth bound.

**A specification that has moved on.**
Re-run `measure` and `schema` against the new revision, regenerate, and diff the
schema export. A new attribute is additive; a removed one is a breaking change
to every module built on it, and belongs in the report rather than in a silent
regeneration.

## Limitations

- **A complete schema is not a working one.** Every field the document defines
  is present, which does not mean the API accepts any particular combination of
  them. Conflicting fields, values valid only in some regions, and quota limits
  are all invisible here. Only a live run finds them.
- **A document that marks nothing output-only, and returns what it was sent,
  yields nothing `Computed`.** A specification is free to set neither signal,
  and an API whose create answers with the schema it received adds nothing the
  request did not already carry. Every field then becomes `Optional`, which is
  what the document says rather than what the API means: a practitioner could
  try to set a server-assigned identifier and only find out when the API rejects
  it. When a resource derives with no computed attributes at all, read the
  document before trusting the result. A marker that
  qualifies a case rather than the field — output-only for one type, settable for
  another — is read as settable, because the other reading refuses a value the
  API accepts.
- **No account is used**, so nothing here proves the provider works.
- **Readiness is a judgement.** Which status value means usable is read from a
  document written for people, and it is the field most worth reviewing.
- **The expansion bound is a policy, not a fact.** A schema truncated at depth
  is narrower than the API, and the notes say exactly where.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `has no 'module_plan' section` | The coverage artifact carries no resource projection. Run `ncp-tf-coverage` first. |
| `create operation ... is not in the supplied documents` | The plan cites an operation no supplied specification defines. A document is missing from `--spec`, or the plan is stale. Check the path template too: a plan and a specification that spell the same parameter differently do not meet. |
| `This reads OpenAPI 3.x, and Google Discovery` | The document is in another language — Swagger 2.0, or an OpenAPI file with no `openapi` key. Read it and derive the answers yourself, and record that you derived them rather than the script. |
| `cycle: X already open` | Expected on self-referencing schemas. The attribute is emitted without children rather than dropped. Confirm the omission is acceptable. |
| `depth: stopped at N` | The expansion bound was reached. Raise `--max-depth` if the missing attributes matter, and re-review the size. |
| A resource reports 0 required attributes | Its create method declares no required parameters. That is common for body-only creates and is not an error. |
| `returns a field of that name` | A required parameter and a returned field share one name. The parameter is kept. Read the document and decide where the returned value belongs; it is not in the schema. |
| `describes the operation rather than the resource` | The create is long-running, so its response was not read. Expected, and the reason this resource has fewer computed attributes than a synchronous one. |
