---
name: ncp-tf-verify-report
description: Records what a live run proved against an account and rolls it up to modules and requirements. Use for Terraform verification status, live test results, and what is supported but unverified.
version: 0.1.0
metadata:
  author: NVIDIA Corporation
  tags: [ncp, terraform, verification, live-test, reporting]
---

# NCP Terraform Verification Report

## Purpose

The coverage report answers what this NCP's API *can* support. It is true before
anything runs.

This answers a different question: **what has been shown against a real
account.** A requirement can be supported and never once exercised, and until
those two live in separate documents they read the same.

## Prerequisites

- An approved `coverage.yaml` with a `module_plan`. It is the list of what must
  be verified.
- The output of a live run, as `go test -json`. Not a log somebody read.

## The one rule that makes this artifact worth having

**A status is recorded, never asserted.** It comes from a run's own
machine-readable output. You do not decide that a resource passed; a run
reported it.

An agent that reads a log and writes "this passed" has produced a document
nothing can contradict. A hand-written status also decays: a resource can stay in a
report as blocked by a permission after the permission is granted, and nothing
refuses the claim because nothing can.

**What you *do* write is why.** No run reports why it was refused in terms a
person can act on. That is the one judgement here, and it goes in a blocker.

## The four statuses

| Status | Means |
|---|---|
| `live` | a full create–update–destroy ran against the account and passed |
| `offline` | its live lifecycle failed or was skipped, or its offline test passed and no live run has reported it |
| `unverified` | it has a case, and no run has reported it |
| `not_selected` | the operator chose not to build it; its evidence is the operator's reason |

**`offline` means not passed live.** Its live lifecycle failed, or no live run
has reported it. It is not a weaker pass: every resource that later passed live
also passed offline while the provider could not create anything at all.

**It is recorded, not inferred.** Pass `--offline` with the workspaces'
`terraform test -json`, which the scaffold's entry point writes in one file
with `--json`. Without it, a resource whose offline test passed and was
never run live reads `unverified` -- the same as one with no tests at all -- and
an offline test that *failed* is `unverified` too, because it has shown nothing.

`offline` obliges a blocker. `unverified` does not — nothing was attempted, so
there is no refusal to explain, and demanding one buys an invented sentence.

**`not_selected` obliges the operator's reason.** A requirement needs the type,
so the report must say why nobody will prove it. `record` reads
`selection.yaml` in the cases directory. A type that it skips as `not_selected`
takes the selection's `why` as its evidence, and its `source`. When the
selection gives no reason, `record` writes none and `check` refuses the entry.
Ask the operator why the type is not built and who or what decided. Write the
answer in the selection, and record again. Do not write a reason the operator
did not give.

## Blockers

A blocker is stated once and cited by id. Many resources can share a few
reasons; restating each inline invites slightly different versions of the same
reason, which is how a report starts disagreeing with itself.

Each carries an `id`, a `category`, a `statement`, and `clears_when`, and
`intermittent: true` where one pass does not settle it (below). Nothing else: a
key the renderer does not know is dropped in silence, so the gate refuses it
rather than letting a written field read as a recorded one.

**`clears_when` is what makes it a work list rather than a complaint.** Write
what would actually change the status — a grant, a policy change, an answer from
a person, a fix in our own code.

**The record keeps what is true now, and nothing else.** A blocker exists while
its cause holds. When the cause is gone, delete the blocker. If the resource is
still not `live`, it cites what still holds it; when it passes, recording drops
its citations, except a citation of an intermittent blocker. A fix is not marked in the record: the commit that made it
is where it is described, and the next run is what shows it worked.

**A blocker names a root cause, and a failure that follows from it is not a
new blocker.** The root cause is a grant, a policy, an account limit, an answer
that a person can give, a service outage, or a defect in our code. When a test cannot run because
a dependency resource cannot be created, or because an input must name a
resource that the account cannot create or hold, the entry cites the blocker
that holds that resource. Add the child's evidence to that blocker's
`statement`, not a second blocker. A blocker for each symptom files one cause
several times, and a symptom filed as `awaiting-approval` sends the reader to
wait for an answer that a missing permission blocks. Choose the category from
the root cause. When our defect and an account refusal both hold an entry, cite
both, and say in each statement which one a run must clear first.

| Category | Use when |
|---|---|
| `our-defect` | the generated code is wrong; nothing about the account is in question |
| `permission-not-held` | the operation needs a scope the credential does not have |
| `policy-refuses` | the permission **is** held and a policy forbids the act |
| `scope-exceeds-account` | it would act outside the account set aside for testing |
| `awaiting-approval` | a person can answer the question, has been asked, and has not answered |
| `feature-not-enabled` | a service or setting is off and could be turned on |
| `service-unavailable` | the service failed the call with an error that names no cause the account can change, for example HTTP 503 |
| `quota-exhausted` | an account quota or limit is used up |

The set is closed. Five of these look alike from a distance and are not:

- **`our-defect` is the only one we can clear ourselves**, and the only one that
  is a bug. Filing one as `permission-not-held` stops the asking — a refusal
  that describes the wrong cause costs more than one that says nothing, because
  a clear wrong answer ends the investigation.
- **`policy-refuses` is not `permission-not-held`.** The permission is held. A
  grant would change nothing, and asking for one wastes the ask.
- **`scope-exceeds-account` is not a technical limit at all.** The call would
  work. It is a decision about blast radius, and it belongs to a person.
- **`quota-exhausted` is not `scope-exceeds-account`.** The call did not work.
  A quota raise, or the release of what holds the quota, clears it.
- **`service-unavailable` is not `feature-not-enabled`.** Nothing is off, and
  nothing can be turned on. A later retry can clear it.

## The roll-up

    resource  →  module  →  requirement

A requirement is `live` only when every resource carrying it is. Anything else
takes the weakest status among its members and names the member holding it.

There is no partial status. An average would report something as mostly
verified, which is not a thing it can be.

A requirement cut into parts rolls up under the requirement, not per part: one
unproven part leaves the requirement unproven.

**Each case of a resource type is a member.** A type with a further, named case
has the entry `<type>[<name>]` beside `<type>`. Its lifecycle test is
`TestAcc<Type>_<name>_lifecycle`, and its offline subject is `<type>_<name>`.
It counts for each requirement of its type, so a requirement is `live` only when
every case of its type passed live. The layer and the headline count the type
entries only, because a layer module uses one configuration of each resource.

### A module needs two things, and they are not the same

**Green resources are necessary and not sufficient.** A module adds variables,
outputs and wiring on top of the resources it builds, so:

- it can **fail while every resource passes** — an output bound to the wrong
  attribute reaches no consumer, and no resource test would ever see it;
- it can **pass while a resource is unverified** — its own test says nothing
  about a resource it does not exercise.

So the record keeps them apart. `modules.<name>.status` is the module's own
`terraform test` result; the resource tally is derived. A module is verified
when both hold, and the report shows both columns rather than merging them.

Module results come from `terraform test -json` through `--module`, not from the
Go harness: the two tiers have two runners, so they have two inputs.

**A module's status follows the mode its test ran in, not only the result.** The
file name states the mode — `<layer>_offline.tftest.hcl`, `_replay`, `_live`. A
test that plans with no account passes exactly as one that applied, so the same
rule as for a resource holds:

| Module test | Status |
|---|---|
| `live` passed | `live` |
| `live` failed | `offline`, and it owes a blocker |
| only `offline` or `replay`, passed | `offline`, and it owes a blocker |
| only `offline` or `replay`, failed | `unverified` — it has shown nothing |

The blueprint tier composes modules and will need the same treatment. It is not
built yet, and nothing here pretends otherwise.

## Running it

```bash
# 1. record — a run's output becomes statuses. No judgement.
uv run scripts/verification_report.py record \
  --coverage <the approved coverage.yaml> \
  --cases <the provider's resource cases directory> \
  --live <the go test -json output> \
  --offline <the workspaces' terraform test -json output> \
  --module <the modules' terraform test -json output, when they exist> \
  --out <the verification record> \
  --provider-revision <the commit> --account <the account>

# 2. write the blockers by hand, and cite them from each offline resource.

# 3. check — refuses a record that says less than it appears to.
uv run scripts/verification_report.py check \
  --verification <the verification record> \
  --coverage <the approved coverage.yaml>

# 4. report — render it. Add --layer to report on one layer only.
uv run scripts/verification_report.py report \
  --verification <the verification record> \
  --coverage <the approved coverage.yaml> \
  --out <the rendered report>

# 5. before a commit, check the report against a render of the record.
uv run scripts/verification_report.py check \
  --verification <the verification record> \
  --coverage <the approved coverage.yaml> \
  --report <the rendered report>
```

**The report is the `report` render, and nothing else.** Do not write it or
edit it by hand: a reader takes it as the kit's render of the record, and a
hand-written line can state what the record does not. To make it say more,
change the record and render again. Give `check --report` each `--layer` that
you gave `report`.

**`record` and `check` always cover the whole plan; only `report` narrows.** A
resource dropped from the record is a resource that leaves the report. Narrowing
is a reading of the same record, so a report about one layer states nothing
about another — a blocker only another layer cites does not appear in it.

**Dependency resources are counted apart.** A resource the plan lists with
`dependency_of` and no `requirements` serves no requirement. The headline and
the per-layer table count requirement resources only, and a separate line gives
the dependency resources' own tally. A module still needs them: a module builds
its dependency resources, so they hold its status like any other resource.

**The headline is per layer on purpose.** One fraction across every layer
answers nothing: a layer nobody has built for is not a layer that failed
verification. A type the operator chose not to build reads `not_selected`, not
`unverified`, because a reader who cannot tell them apart reads untouched work
as a gap.

Produce the live output with `go test -json` from the workspaces directory. The
provider README states the environment a live run needs, and `RUN_VARIABLES.md`
beside the workspaces lists every value it must be given.

**An offline pass never replaces what a live run recorded.** It shows less than
a live pass, and less than a live failure that owes a reason, so a resource or
module a live run set keeps that status and its tier. `record` reads the
entry's `tier` for this: a status with `tier: offline` is replaced by the later
offline pass, because an earlier offline pass shows no more. An offline failure
does replace it: a subject that no longer plans is not live, whatever passed
before.

**`record` reads the cases, and refuses to run without them.** Without them, a
requirement that two cases of one type cite reads `live` from one of them. A
named case whose type the plan does not carry is a warning, and is not recorded.

**Recording again never erases a blocker.** A run reports what happened and has
nothing to say about why, so re-recording keeps what a person wrote. A resource
that starts passing does drop its citation — the run just disproved it.

**A pass does not clear an intermittent defect.** Mark a blocker
`intermittent: true` when the same test both failed and passed with no change
between the runs. A pass then keeps that citation: the resource reads `live`,
and the report still shows the open defect, because the run that passed is the
same evidence as the run that failed. Delete the blocker only when its cause is
found and fixed, and name the cause in the commit that deletes it.

## What `check` refuses, and why each one

| Refusal | Why |
|---|---|
| a planned resource is unrecorded | a record built from what ran holds exactly what somebody remembered; silence is how a resource leaves a report |
| an `offline` resource cites no blocker | not naming a reason has to cost something |
| a `not_selected` resource has no evidence or no `source` | a requirement needs it, and the report would show it unproved without saying why |
| a cited blocker is not declared | the reason is a dangling reference |
| a blocker nothing cites | it records a limit that no longer applies |
| a blocker with no `clears_when` | it is a complaint |
| a blocker with a field nothing renders | it reads as recorded and appears nowhere |
| a `live` or `offline` status that names no tier | a failed live lifecycle and an offline pass both read `offline`, and only the tier says which one a later offline pass may replace |
| a `run_id` or `recorded` key | a run's output is not committed, so a run id names nothing a reader can open; git history says when each line changed |
| a category outside the set | one invented for a single run is one nobody can act on |
| no stamp | a pass with no revision and account outlives the code that earned it |

## The stamp

Two fields, both required: `provider_revision` and `account`. The stamp
describes the latest recording, and holds only that recording's values.
`record` refuses a recording that does not give both, and writes nothing.

**Each entry names the tier that set its status.** A record keeps the latest
status of every subject, and a run overwrites only what it reports, so one
record holds results from several runs — an IAM layer proved live in one run and
a network layer planned offline in another. `record` writes `tier` on every
entry the run reported. `tier` is `live` when a lifecycle or a live module test
set the status, and `offline` when an offline or replay result set it. An entry
no run has reported names no tier.

**The record names no run and no date.** A run's output is not committed, so a
run id names nothing that a reader can open. The record is a committed file, so
its git history says when each line changed. Write evidence and blocker
statements the same way: name the test and what it observed, not the run.

A verification result is true of one provider revision and one account.
Without the stamp, a reader cannot tell a current result from one the provider
has since changed underneath. A resource stays wrong in a report
after the code that blocked it is fixed.

**`account` is the tenancy boundary the run was made in, whatever this NCP calls
it.** A project on one cloud, an account on another, a subscription on a third:
the word here is neutral because the record is, and the value is whatever the
provider's own setting names. Write the NCP's own word beside it if the two
differ, because a reader who knows the NCP reads the field as that NCP's term
and a mismatch reads as the wrong value rather than a neutral one.

It is the one deployment-specific value a record carries, and it carries it
because a result that does not say where it was taken is not a result — the same
reason a CI run records its runner. Nothing else about the deployment belongs
here: no credentials, and no identifiers for resources the run did not create.

## Where the output goes

**Ask, and put it where the operator says.** The repository's own layout
document says which directory holds a generated artifact, and it is theirs to
change. Read it rather than assuming a directory, and never create one the
operator did not name.

A reasonable default to propose: beside the rendered coverage report, because
the two answer halves of the same question and a reader wants them together.

## Common refusals

| Message | What to do |
|---|---|
| `is offline and cites no blocker` | write the reason as a blocker and cite its id. If you do not know the reason yet, that is the finding. |
| `is not_selected and states no reason` | ask the operator why the type is not built and who or what decided. Write the answer in `why` and `source` of its entry in `selection.yaml`, and record again. `record` prints the same line as a warning. |
| `is cited by no resource` | the limit was cleared, or the id is misspelt. Remove it or cite it. |
| `fixed is not a field a blocker has` | delete the blocker; its cause is gone. Cite what still holds the resource, if anything does. |
| `names no tier; record the run again` | record the run again; `record` did not write this status. |
| `names run_id` or `names recorded` | delete the key, from the stamp and from each entry. `record` refuses a previous record that holds one. If a blocker statement names a run, write the test and what it observed instead. |
| `names no tier, so an offline pass cannot tell what it may replace` | `record` refuses the previous record. Add `tier` to each named entry from its evidence: `live` for a lifecycle or a live module test, `offline` for an offline or replay test. Record again. |
| `is planned and unrecorded` | record the run again over the current plan. The plan grew. |
| `holds no resource case` | `record` read a directory without resource cases. Give `--cases` the provider's cases directory. |
| `category ... is not one of` | pick from the closed set. If none fits, the set is wrong and that is a decision, not a field. |

One message is a warning, not a refusal. `record` writes it to stderr, exits 0,
and records every planned result. Read stderr after each `record`: exit 0 does
not say that every test matched.

| Warning | What to do |
|---|---|
| `names no resource in the module plan` | a test exists for something the plan does not name, and `record` did not record its result. Fix the plan or the test name. Record the run again. |
