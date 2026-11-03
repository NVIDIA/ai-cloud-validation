---
name: ncp-tf-scaffold
description: Lays out one tier of an NCP's Terraform integration - provider, layer module or blueprint - and writes the tests that tier must pass, before its code exists. An operator names the tier; the contract flag names what its tests assert, and the two differ - provider is --contract resource, blueprint is --contract composition, module is --contract module. Use for scaffolding a provider, module or blueprint directory, test generation, cassette replay, composition tests.
version: 0.1.0
metadata:
  author: NVIDIA Corporation
  tags: [ncp, terraform, scaffold, testing, lifecycle]
---

# NCP Terraform Scaffold

## Purpose

Bring one tier of an NCP's Terraform integration into existence, and write the
tests it must pass before any of its code is written.

Run once per tier, and once per layer for the module tier, because each lands in
a different directory:

| Tier | What this lays out | What it writes there | Pass |
|---|---|---|---|
| provider | the provider's own tree | a case per resource type | `--contract resource` |
| module | one layer's module | that layer's case | `--contract module` |
| blueprint | the blueprint that composes the layers | the composition case | `--contract composition` |

**The operator names the tier; the flag names the contract, and for two of the
three they are different words.** The provider's tier holds a case per
resource, so its contract is `resource`; the blueprint's tier holds one case
over the composed layers, so its contract is `composition`. Translate before
passing the flag. This table is the only map.

A tier's directory begins as the tests its implementation must satisfy: a true
claim about what the code owes, not a placeholder for work nobody started.

## Prerequisites

- `uv`, for the scripts below.
- An approved `coverage.yaml` whose `module_plan` names the resources, the layer
  that owns each, and the operations behind each.
- The pinned specifications, for offline cases only.

## The cases are instantiated, not written

Almost all of a case is a function of reviewed inputs. The contract fixes the
lifecycle, the verification rule and the cleanup; the module plan fixes the
resource, its operations and its address. Each contract names what is left.

| Comes from the contract | Comes from the module plan | Needs judgement |
|---|---|---|
| the lifecycle, and its order | the resource and its layer | the fixture values |
| which check proves each step | the operation behind each step | an update that really round-trips |
| the cleanup semantics and timeout | the Terraform address | what counts as ready |
| | | what a destroyed resource reads back as |

**Review the judged fields. The structure around them was authored by nobody**:
reviewing it is reviewing the script, which is reviewed once, not every run.

The contract fixes that a destroy is proved by reading the API. It cannot fix
what that read comes back with: some APIs stop answering it, and some keep
answering it with a marker. Some identity resources soft-delete, so a case that expects absence fails on a destroy that worked.

Some resources show a destroy only on another resource: a key that cannot be
deleted has its versions destroyed. Then add `via` under `verification.destroy`;
[references/what-counts-as-proof.md](references/what-counts-as-proof.md) has the
shape, and what `check` refuses.

Each case repeats its judged fields, and the entries a reviewer should still
look at, under `review`; [references/contracts-and-modes.md](references/contracts-and-modes.md)
says why.

## What is under test, and what the requests reach

Two independent choices, made at two different times. The **contract** says what
is under test, and it shapes the case. The **mode** says what the requests reach,
and it shapes the run. **The case does not name a mode**, so the same case runs
offline today and live tomorrow.

Three contracts ship in `references/contracts/`: `resource`, `module` and
`composition`. Three modes ship in `references/modes/`: `offline`, `replay` and
`live`. [references/contracts-and-modes.md](references/contracts-and-modes.md)
says what each contract asserts and each mode proves, and why a failure in each
contract indicts one thing.

**`live` is never a fallback.** No missing input, no absent recording and no
failed offline run may cause a live call. Entering that mode is something a
person does on purpose.

**Offline means no call to the NCP API.** An offline run sends no request to the
API under test, uses no credentials, and needs no NCP address -- do not ask the
operator for one. It may fetch dependencies, for example Go modules or a
provider from a registry or a mirror.

**Replay is unavailable until something is recorded.** Say so to the operator
rather than offering replay as a choice they cannot take.

## Instructions

### 1. Establish the scope before instantiating anything

Three choices. **Most of them are already decided — derive them, state what you
derived, and ask only for what the repository cannot tell you.** A menu of
options the operator has no basis to choose between is not a question; it moves
the decision to the person with less information.

**Ask for this step's inputs only.** `--provider-source` and
`--provider-version` belong to step 4, where a workspace is written and
Terraform needs an address to initialise one. Instantiating cases needs neither,
and a question asked three steps early is answered with a guess that then looks
settled.

Where the tier's directory goes is the one thing no default can supply. Ask that.

**Not the mode.** Instantiating takes no mode. The case states the whole
lifecycle and the mode is chosen at step 4, when the tests are rendered, so
nothing here has to be decided before it is known.

When the operator is short of time or permission, drop work in this order:
`composition` live first, then `module` live, then `resource` live. Never drop
an offline run — it costs nothing and it is the only thing that still works when
there is no account at all.

**Which contract**, because it decides what is under test. The operator asks for
a tier — the provider, a layer module, the blueprint — and the flag takes the
contract that tier's tests assert. Translate with the table in **Purpose**; only
`module` is the same word in both.

Never pass the operator's word through unread. `--contract provider` and
`--contract blueprint` name no contract and the run is refused, which is the
intended outcome: a tier silently accepted as a contract would write the wrong
tests into the right directory.

**How much of it — ask, and never assume.** Scope is the operator's decision and
`instantiate` refuses to start without one. Every case it writes carries judged
fields that you fill and the operator reviews, so the size of a run is a
commitment of their review time, not a detail. Do not assume the account can create everything the
plan names, and do not assume it cannot. A scope narrows; it is not a switch:

| The operator says | Pass |
|---|---|
| all of it | `--all` |
| only these layers | `--layer <name>`, repeated |
| only these resources | `--resource <type>`, repeated |
| only this function of the cloud | `--section <heading>`, repeated |

Every flag repeats, and every one is refused when it names something the plan
does not carry, so a typo stops the run instead of quietly producing a clean
result over nothing. A module run reads `--layer` or `--all`, and a composition
run reads `--all`; another flag is refused, because a module case covers a
whole layer and a composition case covers every buildable layer.

**`--section` takes a requirement heading, not a layer**: a function such as
IAM, in the requirement catalog's words. Ask which they mean when both a layer
and a heading would fit. [references/scope.md](references/scope.md) says how it
matches.

**Which one to run next is a question the scan answers.** Run `progress` (below)
first, every time, and show the operator what each section already has behind
it. Then ask which to take. A menu with no state beside it moves the decision to
the person with less information, and no menu at all moves it to you.

**A narrowed run and a full run must not report the same green.** Report the
resource run's `selection.yaml`, and each layer a blueprint run names in its
output as one it cannot compose. A skip is never a pass.
[references/scope.md](references/scope.md) has what each run writes, and why.

### 2. Instantiate the cases

```bash
uv run scripts/case_builder.py instantiate \
  --contract resource [--account-scope '<the scope the provider render was given>'] \
  --coverage <path>/coverage.yaml --out <provider>/tests/cases [--provider-schema <path>/provider-schema.json] [--provider-data <the provider's per-resource data>]
```

One run per layer for the module contract, so each layer's case lands in the
module it tests:

```bash
uv run scripts/case_builder.py instantiate \
  --contract module --layer <name> \
  --coverage <path>/coverage.yaml --out <module>/tests/cases
```

**Re-running refreshes each reviewed case and keeps its answers.** Run it after
a kit update too: `check` and `render` refuse a case that the current kit
derives differently, because the tests come from the kit. Run it after the provider's schema review too,
with `--provider-schema` and the reviewed `provider-schema.json`: a field that the review says names the object then replaces it, and the live update does not owe it. Once the provider records its request paths, add `--provider-data` and the per-resource data file that holds its `update_mappings`: the live update then owes each field that the update request sends at another path. It backs the cases
up, writes each selected case again, carries each value that its `judged` list
names, and prints a report. Show the operator that report and settle it with
them: [references/refresh.md](references/refresh.md). `--overwrite` discards every answer, and is never the answer to a message you did not expect.

A resource run writes `selection.yaml`. Write in it only a reason the operator
stated, and ask for each other one: [references/scope.md](references/scope.md).

**The specifications come from the coverage, not from you.** The script reads
each document that `coverage.yaml` records in `spec_documents`, so every case
carries the request each operation must issue, which a replay run matches on.
`--spec` overrides it when the documents have moved. A coverage that records
none is refused: re-run the coverage step. A document whose digest differs is
refused too: the cases would read another API than the analysis.

The script refuses rather than guesses. A layer or resource the module plan does
not name, an operation the documents do not define, a composition with fewer
than two buildable layers: each stops with what is wrong. **The documents are
the ground truth; the plan is a reviewed reading of them.** When a plan fact a
case rests on (an operation, a request field, a field the update changes) is
unclear or the document contradicts it, read the document. Write no value it
does not confirm: nothing is better than something wrong. Tell the operator what
each says, propose the fix to `coverage.yaml` (`field_overrides` where no
condition states it), apply it on their word, and rerun the check and this step.

### 3. Fill the judged fields — and only those

Each case lists them. They are empty and present, because a reviewer has to see
that a decision is owed, and a placeholder value is a decision nobody made that
would nonetheless run. You fill each except `readiness`, also when the resource costs money: a cost is never a reason to leave a field empty. A second case of one type is the operator's call: [references/several-cases.md](references/several-cases.md). The operator decides spending when they choose the resources of a live run, not in a field of the case.

| Field | What to decide | What makes it wrong |
|---|---|---|
| `fixture.create` | which values create this resource | a combination the API rejects, or one that needs a quota the account lacks |
| `fixture.update` | a new value for every field in the case's `update_fields.change`, each at its path (`version_template.algorithm` sits inside `version_template`); `check` refuses one that leaves a field out. The plan updates these fields in place, and for most of them no sentence of the documents says they can change: this step is the first that tests it. A field you cannot give a valid changed value goes under `not_exercised`, with the reason, and `not_exercised` into `judged`. `left_out` says why the plan takes a field out | a field left out or one the API ignores: the step passes while a call that does not change it stays untested. A field dropped without a `not_exercised` reason: a wrong "updates in place" reaches a real user first |
| `proved_by` | for each requirement in `resource.requirements`, the fixture paths whose values exercise it, such as the machine type for a requirement to choose the node type. `check` refuses a requirement with no path, and a path that the fixture does not set. When no value of the resource can exercise a requirement, propose the correction of the plan to the operator: [references/what-counts-as-proof.md](references/what-counts-as-proof.md) | a path whose value the requirement does not depend on: the API's default answers the requirement, and the record reads it as proved live |
| `readiness` | which state of the resource's own read means usable. The operator decides it: [references/troubleshooting.md](references/troubleshooting.md) | "created" for a resource that is not yet serving; the next step fails instead of this one. A field of the create's operation, such as `done`: the read never returns it |
| `expected_requests.<verb>.headers` | which operations need a header that a document states for many operations, such as an account header. Read every document in the specification directory, in any format. In your reply, list each operation with the document's sentence, and ask the operator to confirm. Record the header only after the answer: [references/provider-headers.md](references/provider-headers.md) | a header that no case lists: the provider's switch stays off in a live test, and the API's refusal reads as a missing permission. A header recorded before the operator confirmed it |

Read the specification for each. The required fields and the enumerations constrain the answer without determining it. Add each other field that you write or change, such as `dependencies`, `peer` or a corrected `naming.name_pattern`, to the case's `judged` list: a refresh carries only judged values, and drops the rest.

**Only what a surviving step will use is asked for.** A case owes each judged
field that a surviving step uses, whatever mode it is later rendered in,
because one case serves every mode and `render` refuses a case with an empty
owed field. A case whose update step was dropped — the API offers none — is not
asked for an update value. Asking for a value no step will use spends the one
review that matters.

**Do not read the provider to decide any of them.** These cases are the
provider's only proof of behaviour, and a test shaped to the code it checks
proves only that the code does what it does. You may generate workspace
configuration; you may not derive an assertion by inspecting the provider.

**A value that names another resource is a dependency, and the case says so.**
When `fixture.create` needs the identifier of a resource that must exist first --
a firewall's network, an address's subnetwork -- write the value as a
placeholder, such as `network: '{network}'`, and declare it under
`dependencies`. With `TFKIT_VAR_<VARIABLE>` set, the case uses that existing
resource: the value is its name, or for an `attribute` dependency the
attribute's value. Unset, the test creates the dependency resource from its own
reviewed case, and destroys it after. `check` refuses a value that the case's `derived_dependencies` names and no entry declares: declare it, also after a refresh, without waiting for the operator, and name its new `TFKIT_VAR_` value in your final reply. [references/dependencies.md](references/dependencies.md)
has every shape an entry takes, how to declare a derived one, what `render` refuses, and why no name is built
from `{run_id}` alone. **A counterpart of the resource's own type is a `peer`, not a dependency, and only the operator decides whether the case has one.** A VPN tunnel is ready only when a tunnel at its far end names it back. A peer creates more resources and costs more, and a request to fix the test or to make it pass does not choose that spend. When a live run or the document shows such a need, do not add a peer or its dependency resources: show the evidence, ask the operator, and change no case before the answer. [references/peer.md](references/peer.md) has when, the shape and what `render` does.

### 4. Render the filled cases

```bash
uv run scripts/case_builder.py render \
  --cases <path>/tests/cases --out <path>/tests/workspaces \
  --mode offline \
  --provider-source <the address the target repository states>
```

**This is where the mode is decided**, and the only place it appears. The same
filled cases render again with `--mode live` when an account exists; nothing is
regenerated and no decision made here is lost.

The render also writes `RUN_VARIABLES.md` beside the workspaces: every value a
live run has to supply, which case needs it, and where in the fixture it goes.
Do not restate it -- an operator who has to learn the set by running the tests
and reading what refuses pays for everything each case does before it stops.
**The render is the only writer of what it writes, and the installed kit is not yours to edit:** an install does not update a local script that renders or patches a rendered test, so that script drifts, and the install overwrites an edit of a kit file. Before you run or keep such a script, or change a kit script or a rendered check, read [references/rendering.md](references/rendering.md#the-render-is-the-only-writer).

**`--runner-out` gives the artifact one command per mode.** Point it at the
directory the artifact owns: the provider's own directory for a resource render,
the module's own directory for a module or blueprint render. Give
`--provider-mirror` with it, and `--provider-export` for a module or blueprint
render. The render writes `.env.example` and `tools/run-tests.sh` there, and for
a resource or module render its section of the `README.md`. **The provider
README is this step's:** write its layout section beside that run section, and
write no review into it ([references/rendering.md](references/rendering.md#the-provider-readme)). A module's README needs nothing from you ([references/rendering.md](references/rendering.md#the-module-readme)).
An operator then runs `cp .env.example .env` and `./tools/run-tests.sh offline`.

**A run id is generated; a supplied value is not.** When `TFKIT_RUN_ID` is
unset, a live run generates one and prints it. A missing `TFKIT_VAR_<NAME>`
stays a refusal, because a guessed value builds a resource against the wrong
parent. [references/rendering.md](references/rendering.md) has what each entry
point does, the mirror, what each `.env` holds, and the order the offline tier
runs in.

**Done means the offline entry point exits 0.** After any change to the
provider's code or to a render, `run-tests.sh offline` exits 0 in each
directory the change reaches, before a commit and before any report that the
work is done. A provider change reaches every module and the blueprint: run the
provider's `build`, then each one's `offline`, because a module that no longer
matches the provider fails there and not in the provider's own run.
A tool it needs and cannot find stops the work, and is reported; it is not
skipped. Running some of its steps by hand is not a pass: `gofmt`, `go vet`,
`go build` and one `terraform test` can all succeed while the linter, the tests
beside the code or the request checks fail, and those are where a defect shows
first. This is the one statement of the rule; other skills point here.

**Which linter runs is the repository's decision**, made by a `.golangci.yml`.
The entry point ends with a summary that counts the cases that refused to start,
failed and passed. A test that started and has no result is counted as failed;
[references/rendering.md](references/rendering.md) says why.

| The operator says | Pass |
|---|---|
| no account, nothing recorded — I am building locally | `--mode offline` |
| no account, but the exchanges were recorded earlier | `--mode replay --cassettes <dir>` |
| I have an account and I mean to use it | `--mode live` |

Assume `offline` and say so. A first render has nothing recorded, so `replay` is
refused with the reason above, and `live` is never entered except on purpose.

**Ask for the provider's source address; do not choose one.** It is the
`source` line of each workspace's `required_providers` block. Read it from the
repository, say where you read it, and have it confirmed. Until a namespace is
chosen, propose a host that is obviously not a namespace, such as
`terraform.local`. [references/rendering.md](references/rendering.md) says why a
Go module path is not a source address, and why a value found in the tree is
not a confirmed one.

Each workspace's `versions.tf` requires Terraform `>= 1.9.5`, the same floor as
every Terraform file the kit writes.

What it writes depends on the contract and the mode:

| Contract | Mode | Rendered |
|---|---|---|
| `resource` | `offline` | a workspace, a Terraform test that plans and stops, and a request check |
| `resource` | `live` | a Go acceptance test, plus the independent-check file |
| `resource` | `replay` | a Go test that drives the lifecycle against the recordings, for each case whose create, read and delete were recorded; any other case is reported, not skipped |
| `module` | `offline` or `live` | a Terraform test, run from the module itself |
| `composition` | `offline` | a Terraform test on the blueprint that plans and asserts nothing |
| `composition` | `live` | a Terraform test on the blueprint that applies, with one assertion per wiring pair |

The mode cuts the lifecycle here, where the case kept all of it: an offline
render stops before apply, and a live render of the same case does not.

**The request check is the offline tier's second half.** It applies the create
configuration against a transport that refuses every request, and asserts the
method, the path and the body fields the document requires. It needs no account
and no recording. A live test references what the provider must supply: its
provider factories, and a reader for the independent check. `TFKIT_RUN_ID` must
be set for a live run; the test refuses to start without one. `render` refuses a
case whose judged fields are still empty.
[references/rendering.md](references/rendering.md) has the seams the provider
fills, where `--out` points, and why the module and blueprint tiers need no Go.

### 5. Check, report, then stop

Every document the run writes declares what it is: a `case`, or the run's
`selection`. A directory holding anything else stops the check rather than being
graded as a case.

```bash
uv run scripts/case_builder.py check --cases <path>/tests/cases \
  --provider-export <provider>/provider-schema-export.json
```

Run it against each directory the flow wrote to, not only the last one. It
refuses an empty judged field, because a template that runs reports a pass. It
also refuses a `readiness` whose field the export does not give the resource.

For a module case, also give the provider's reviewed resource cases and the
module's `references.yaml`:

```bash
uv run scripts/case_builder.py check --cases <module>/tests/cases \
  --resource-cases <provider>/tests/cases --references <module>/references.yaml
```

It then refuses each value the module entry sends, its own or from a shared
input, that the reviewed resource case sets differently or sets and the entry
leaves out. A name, a reference to another resource and a placeholder are the
module's own, so they are not compared. Copy the reviewed value. When the module
needs another value, keep it and list it under `differs_from_resource_case`
with the resource, the dotted path and why; the check refuses an entry with no
why, and one that no longer differs.

Report the cases written, the judged fields filled, and everything the run
left out, with its reason: `selection.yaml` for a resource run, the output for
a blueprint run. Then stop. A person reviews
and commits the cases before the code they check is written.

## How a result is proved

Terraform's own view and **a direct call to the NCP's API** must agree. Each
verification is one call to the read operation the module plan names, never
through the provider. **Only `live` verifies.**
[references/what-counts-as-proof.md](references/what-counts-as-proof.md) has the
reasoning.

## Where the cases go, and who owns the directory

**A test lives with the thing it checks**, which is where both mature reference
providers keep theirs and where the standard module template already puts a
module's own `tests/`. So the cases scatter by contract rather than pooling:

| Contract | Written into |
|---|---|
| `resource` | the provider's tree, beside the provider's own tests |
| `module` | that layer module's `tests/`, one run per layer |
| `composition` | the blueprint's `tests/` |

**Write a module entry from the reviewed resource case of the same resource.**
Its fixture holds what that resource's own runs found, and an API document
calls some fields optional that the API refuses to go without. A module case
written apart from it repeats none of that, and only the module's live run
finds each value again, one refusal at a time. Copy each value, and change one
only for a reason the module has.

**A value the API needs and the document does not state is found by a live
run.** When a live module or blueprint test is refused for a field that no
reviewed resource case holds, report the field and the refusal to the
operator, and ask for the value. Do not guess one: a guessed value that the API
accepts hides the question.

**Ask where the tier goes; do not choose.** The location is the operator's
decision. Ask for one path per run, and run once per layer for the module tier
so each layer's case lands in its own module.

**The layout is the repository's, not this skill's.** The target repository
states what lays out a provider and what lays out a module — a project template,
or an existing provider or sample module to follow. Read that, create the
directory at the path the operator gave, and copy the template in. Do not invent
a layout. A repository with none of these has only the kit's layout: use what
the kit's steps write at that path, say in your answer that the repository gave
no template, and do not ask the operator for a layout.

**The blueprint tier needs a purpose before it needs a path.** Ask what the
blueprint is for; there is no sensible default.
[references/layout.md](references/layout.md) has why, how a resource case is
filed under its requirement, where a dependency resource's case goes, and what
a tier still only names.

## What has to exist first

| Input | Where it comes from | Why it comes first |
|---|---|---|
| the approved module plan | the coverage step | it names every resource, layer and operation a case is built from |
| the sweeper | its own step | it must exist before anything can leak, and it reads the naming convention below |
| the cassettes | recorded by hand against the API | a cassette captured from a generated provider would record the provider rather than the API, and could not then be evidence the provider is right |

## Naming

Every resource a run creates is named `tfkit-{run_id}-<discriminator>` and
carries the same parts as labels wherever the API supports them, because a label
survives what a truncated name cannot. The script emits the pattern and refuses
one that would overflow. `references/naming.md` explains the parts and the limits.

## Scripts

| Script | Command | Purpose |
|---|---|---|
| `scripts/case_builder.py` | `instantiate` | emit cases for one contract from the module plan |
| `scripts/case_builder.py` | `check` | check filled cases against their contracts |
| `scripts/case_builder.py` | `render` | turn filled cases into runnable tests, and with `--runner-out` the one command that runs them |
| `scripts/case_builder.py` | `progress` | what each requirement has behind it, scanned now and stored nowhere |

`instantiate` reads `references/contracts/` for what is under test; `render`
reads `references/modes/` for what a run's requests reach. Both are the kit's:
do not edit them, and do not hand-edit the cases it produces.

```bash
uv run scripts/case_builder.py progress \
  --cases <provider>/tests/cases [--provider-dir <provider>]
```

`progress` reports each requirement as **missing**, **test-only** or **done**,
and writes nothing. [references/scope.md](references/scope.md) says what each
word means.

## Rules that decide the content

A step with no operation behind it is dropped, and the case says so. The
independent check calls the API, not Terraform. Cleanup is reported even when a
run failed early. [references/contracts-and-modes.md](references/contracts-and-modes.md)
gives the reason for each.

## Examples

[references/examples.md](references/examples.md) answers what operators ask
most: one layer only, an account that cannot create a resource, which fixture to
use, no account at all, and replay.

## What to report when the step finishes

Read [references/reporting.md](references/reporting.md) before you report: it
sets what comes first and the order of the rest. Anything you changed in an
artifact this step did not generate comes first. **Give the review in your
answer, and write no review file**: the answers go into each case, so a file
of them is stale at the first answer.

**Do not hand the operator a command to run.** They ask you for the next step,
in their own words, and you run it.

## Limitations and troubleshooting

[references/troubleshooting.md](references/troubleshooting.md) lists what this
step cannot do, and the cause and fix for each message it prints.
