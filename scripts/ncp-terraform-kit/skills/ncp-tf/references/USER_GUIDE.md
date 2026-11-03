# NCP Terraform Kit — user guide

How to take one NCP from "we have an API" to a Terraform integration, in order.

This guide covers **what to do and what to decide**. It does not explain how the
skills work inside.

It travels with the `ncp-tf` skill, so it is wherever your agent's skills are —
`.claude/skills/ncp-tf/references/USER_GUIDE.md` for Claude Code,
`.agents/skills/ncp-tf/references/USER_GUIDE.md` for Codex.

## Contents

- [What you get](#what-you-get)
- [How you use it](#how-you-use-it)
- [Before you start](#before-you-start)
- [The steps](#the-steps)
  - [Step 1 — Coverage report](#step-1--coverage-report)
  - [Step 2 — Scaffold each tier, and the tests it must pass](#step-2--scaffold-each-tier-and-the-tests-it-must-pass)
  - [Asking what is left](#asking-what-is-left)
  - [Step 3a — Provider](#step-3a--provider)
  - [Step 3b — Verification report](#step-3b--verification-report)
  - [Step 3c — Find what a run left](#step-3c--find-what-a-run-left)
  - [Step 3d — Record the exchanges (optional; needs an account, once)](#step-3d--record-the-exchanges-optional-needs-an-account-once)
  - [Step 4 — Layer modules](#step-4--layer-modules)
  - [Not built yet](#not-built-yet)
- [The order, and why it does not bend](#the-order-and-why-it-does-not-bend)
- [What you decide, and what the kit decides](#what-you-decide-and-what-the-kit-decides)
- [Rules the kit holds itself to](#rules-the-kit-holds-itself-to)
- [When something looks wrong](#when-something-looks-wrong)
- [Where the output lives](#where-the-output-lives)

## What you get

A Terraform integration for one NCP, generated from that NCP's own API
specification.

| | What it is | What you do with it |
|---|---|---|
| **Provider** | a Terraform provider carrying a resource type for everything the API can create, read, update and delete | write `resource "<ncp>_instance"` and `terraform apply` reaches the NCP, the same way it reaches any cloud |
| **Layer modules** | one module for each layer the API can support — `iam`, `network`, `cluster`, `compute` | configure a layer without knowing which resources sit under it |
| **Blueprint** | one configuration that composes the layers in order | stand up a whole environment from a single root module |
| **Tests** | a test for each of the three, run offline or against a live account | know the above works before anything depends on it |

A layer the API cannot support is reported as such, with the missing operations
named. The kit never invents a resource to fill a gap.

## How you use it

Ask for one step, in plain language. Each step writes **one file**, you review
that file, and the next step reads it.

There is no orchestrator, no run to start and none to resume. The files on disk
are the whole handoff, so you can stop after any step, come back days later, and
continue from what is committed.

## Before you start

You need three things, and the kit will not guess any of them.

1. **The NCP's API specifications, on disk.** They are an input you supply, not
   something a run goes looking for. Fetch them once and pin them.
2. **A place for the output.** A directory beside the NCP's existing Terraform.
3. **Time to read.** Every step ends with you reading its output. A step you
   skim is a step that did not happen.

## The steps

Steps 1 to 4 exist today. The rest are designed and not built; the router says
so plainly if you ask for one.

### Step 1 — Coverage report

**Ask:** *"Produce the coverage report for `<NCP>`."*

**You get:** `coverage.yaml`, and two readable files beside it: `coverage.md`,
the coverage, and `module-plan.md`, the plan that you approve.

The report answers two questions: which of the four layers — `iam`, `network`,
`cluster`, `compute` — can be built at all, and which resources each one owns.
Its `module_plan` section is the contract for everything after it. Nothing later
may add a resource it does not contain.

**What to review:**

| Look at | Ask yourself |
|---|---|
| Each layer's verdict | Does the stated reason match what you know of this NCP? |
| Requirements marked `partial` | Is the note specific about what is missing, or vague? |
| Requirements marked `absent` | Is the capability genuinely missing, or was the API just not read? |
| The resource list per layer | Could you build the layer from these and nothing else? |
| The first two sections of `module-plan.md` | Do you accept each handling (`refuse`, `forget`, `stop_and_start`) and each replacement? The sections under Reference are readings that the gates checked against the documents. |
| A field fact you know and the plan cannot state | Ask the agent for a `field_overrides` entry: the field, `replaces`, `in_place` or `leave_out`, and your reason, such as an update that a live run refused. You can ask for one at any later step too. |

**The one thing worth checking hardest:** a requirement about *behaviour* — a
timing bound, a throughput figure, a certification — should never be `present`.
This analysis reads an API's shape and cannot prove behaviour.

**Before you go on — this is the decision the report exists for.**

Count the layers that carry **at least one resource**. A layer carries resources
only when its verdict is `buildable` or `partial`: the gate refuses a
`not_buildable` or `not_applicable` layer that lists any, so the count is not a
judgement call.

| Layers with resources | What to do |
|---|---|
| **0** | **Stop.** There is nothing to generate — no provider, no module, no test. Before you conclude the NCP supports nothing, check which specifications you supplied. A document missing from the input looks exactly like a capability missing from the API. |
| **1** | **Go.** One layer is enough for a provider, that layer's module, and tests for both. The blueprint composes layers, so it needs two to have a seam between them; that step stays out of reach until a second layer appears. |
| **2 or more** | **Go**, with the blueprint available as well. |

**`partial` is a go, not a hold.** A partial layer is one the API supports in
part, with the gap stated, and it generates a module for the resources that do
exist. Waiting for every layer to reach `buildable` is waiting for an API that
may never ship the rest.

**Then:** commit `coverage.yaml`, `coverage.md` and `module-plan.md`. They are
the reviewed contract, and everything downstream cites them.

### Step 2 — Scaffold each tier, and the tests it must pass

One step, run once per tier, because each tier lands in a different directory
and only you know where. Each run lays out that directory from the repository's
own project template, then writes the tests the code there will have to pass.

| Ask | Where it goes |
|---|---|
| **2a.** *"Scaffold the Terraform provider at `<path>`."* | the provider's own tree |
| **2b.** *"Scaffold the `<layer>` module at `<path>`."* | that layer's module — once per buildable layer |
| **2c.** *"Scaffold the blueprint at `<path>`, for `<purpose>`."* | the blueprint that composes the layers |

**2c needs a purpose, not just a path.** A blueprint composes layers *for*
something — a training environment, an inference environment — and what it is
for decides which layers it wires and what the test asserts. There is no
sensible default, so you will be asked.

**You get,** in each directory: the layout the template defines, and a case for
each subject of that tier — a resource, a layer module, or the composition.
Each case carries the lifecycle it will run, what it will assert, and what it
will remove afterwards.

**A resource's case is filed under the requirement it exists for**, so the
directory reads as what the cloud does rather than as a list of types:

    <tier>/tests/cases/<section>/<subsection>/<resource>.yaml

**You can ask for one function at a time.** *"Scaffold the provider at `<path>`,
for IAM only."* The heading comes from the requirement catalog, not from the
four layers, and the two are different cuts — a layer can hold several
functions, and one function can reach into several layers. A heading that
matches nothing stops the run rather than quietly covering everything.

**Asking again keeps your review.** A second run, and a run after a kit update,
writes each case again from the current kit and carries every answer you gave.
It keeps a backup, and shows you what it carried, what it could not carry, and
each value where your case and the new kit disagree. You decide each of those.
After a kit update the step does not write tests from a case that the new kit
would write differently: it asks to refresh the case first.

**What you review is the `review` block in each case, and nothing else.** It
names each decision the case owes you, the question behind it, what a wrong
answer costs, and the shape to write — sitting beside the field it asks about.
For a fixture it also names the schema that describes the body. Everything else
in the file was derived from inputs already reviewed.

**Your answer goes at the field the entry names, in the same file.**
`fixture.create` is the `create:` key under `fixture:`; `readiness` is the key of
that name. Write it in the shape the entry gives. Do not write the answer under
`review`: that block is generated from the contract, and nothing reads it back.
The top of every case says the same, so you need this page open for none of it.

For the provider's cases, `selection.yaml` beside them says what the run did
not cover, and why. For each type that you choose not to build, the agent asks
you why and who decided, and records your answer there. The verification
report shows the type as `not_selected` with that reason. A module case covers
its whole layer, and the blueprint's every buildable layer, so neither has one.

### Asking what is left

**Ask:** *"Show me the build progress."*

You get a list of every requirement, grouped by section, each marked **missing**,
**test-only** or **done**. Name the generated provider's directory as well and a
requirement is promoted to done when a file there is named after its resource.

Nothing is written, and nothing is remembered between runs. The answer is read
off the directories each time you ask, so it cannot drift from what is on disk —
and there is no file to commit, to review, or to forget to update.

Two things it does not claim. **Done means a file exists with the right name**,
not that the code behind it is correct; only the tests say that. And the
requirements that ask for a practice — ticketing, escalation, on-call — are left
out entirely, because no Terraform integration can answer them and listing them
would put rows nobody can act on at the top of the queue.

**Nothing is implemented yet, and that is the point.** The directory holds what
the code owes, not the code. A test written by reading finished code proves only
that the code does what it does.

**What to review — four fields per case, and nothing else:**

| Look at | Ask yourself |
|---|---|
| The fixture values | Would the API actually accept this combination, with the quota this account holds? The entry names the `schema` that describes the body; read it in the specification. The case copies no part of it, because a part read as the whole is worse than the trip. A name here that holds `{run_id}` is the name the test sends, and `check` holds it to the kit's prefix, the run id and the API's limit. When the fixture holds no `{run_id}`, `naming.name_pattern` supplies the name. |
| The update value | Does this change really round-trip, or does the API accept it and ignore it? A test that updates an ignored field passes while proving nothing. |
| The readiness condition | Does this state mean usable, or only created? Where the read returns no state at all, say what the next step may depend on instead. |
| What proves each requirement | Does each fixture value that `proved_by` names for a requirement really exercise it, or does the API's default answer the requirement? |

For a layer module the judged fields are its variables and outputs instead; for
the blueprint, the layer order and the wiring between layers.

Everything else in a case comes from the contract or from the module plan you
already approved. Reviewing it is reviewing the script.

**Running what it rendered is two commands.** The render also writes
`.env.example` beside the artifact and an entry point inside it, so the setup is
a copy and a command. For the provider, it also writes these commands, each
test tier and what each refusal means into the provider's `README.md`. For a
layer module, it writes these commands, the module's layout and the layer's gap
into the module's `README.md`:

```text
cp .env.example .env
./tools/run-tests.sh offline
```

An offline run needs no account, no credentials and no project. Copy the example
unchanged and it is green. A live run is the same file with real values in it,
and `.env.example` says what each one names. Add `--json <file>` and the run
writes the stream that step 3b reads.

**The provider, each layer module and the blueprint have their own entry point
and their own `.env`.** A module's tests read nothing from another directory
but the provider build, so a module can move to a repository of its own. The
build goes into a filesystem mirror; you say where once, and each entry point
holds that path relative to its own directory. `TFKIT_PROVIDER_MIRROR` in a
directory's `.env` overrides it.

```text
<provider>/tools/run-tests.sh build      # the build every module reads
<module>/tools/run-tests.sh offline
<module>/tools/run-tests.sh live         # applies the module, then destroys it
```

In the provider, `run-tests.sh live` runs the resource acceptance tests. In a
layer module or the blueprint, it applies the whole configuration, which creates
several resources at once and costs more.

**A run id is generated; a supplied value is not.** With no `TFKIT_RUN_ID`, a
live run generates one, and prints it when it starts and after its tests:
everything the run creates carries it, and that is how you find what a run
left. A case with a missing `TFKIT_VAR_<NAME>` stops before it creates
anything, and names every value it needs rather than the first. A value set
in the environment wins over `.env`.

**Then:** commit the scaffolded directories and the cases in them.

### Step 3a — Provider

**Ask:** *"Generate the Terraform provider from the approved module plan."*

**You get:** the provider source, written **into the tree step 2a laid out**, a
derived schema for each resource, and a measurement of how much of the API is
asynchronous. This step lays out nothing; if the tree is missing it will say so
rather than create a second one. It writes the code so that each command in the
provider's `README.md` works. It changes the README only for a part that it
cannot make work, and tells you what it changed and why.

**What to review:**

| Look at | Ask yourself |
|---|---|
| The asynchrony measurement | If most operations are long-running, does the provider actually wait for them? |
| The truncation notes | The schema stops expanding at recursive types and at a depth bound. Are the omitted parts ones you need? |
| Readiness handling | "Created" and "usable" are different states for most cloud resources. Does the provider wait for the second? |
| The `naming` entries of the schema | Does each entry name the field that names the object, with the document's words? `schema` lists each open entry. |

The resource schemas themselves are derived from the specification, not chosen.
You do not need to read every attribute — read the rules and the notes instead.

### Step 3b — Verification report

**Ask:** *"Record what the live run proved for `<NCP>`."*

**You get:** `verification.yaml` and a readable `verification.md`, written where you say.

Step 1 said what the API can support. This says what has been shown against a
real account, and the difference between the two is the point. A requirement can
be supported and never once exercised, and in one document those read the same.

It takes the output of a run — `go test -json` for the live lifecycles,
`terraform test -json` for the offline workspaces, not a log you read — and
turns it into a status per resource, then rolls that up to requirements. It
prints what the API **supports** beside what a run has **verified**, because
those two read identically until they sit in one table.

**A module is reported on twice, and needs both.** Every resource it builds must
be verified, *and* its own `terraform test` must pass. Those are different
facts: a module adds variables, outputs and wiring, so an output bound to the
wrong attribute fails the module while every resource passes. Module results
come from `terraform test -json`, a separate input from the resource run.

Until the layer modules exist, every module reads `unverified` because no module
test has run — not because one failed, and the report says so.

**The one thing you write by hand is why.** A resource that ran and did not pass
must cite a *blocker*: a reason stated once, given an id, and reused by every
resource that shares it. Each blocker says what would clear it, which turns the
report into a work list. Categories are a closed set, and three of them look
alike and are not — a defect in the generated code, a permission that is not
held, and a permission that **is** held while a policy forbids the act. Asking
for a grant fixes only the middle one.

**Then:** commit both files. The record is stamped with the provider revision
and the account, because a result is true of those and not in general. Git
history says when each line changed.

### Step 3c — Find what a run left

**Ask:** *"What did run `<run id>` leave?"* or *"Clean up after the killed run."*

**You get:** a listing of the resources that carry that run id. Nothing is
deleted until you approve the listing.

A live run that is killed, or that fails before its destroy step, leaves real
resources in the account. Every one of them carries the run id, and the entry
point prints the run id after the tests. The sweeper is rendered once from the
module plan, the documents and the cases, and the agent writes the one file that
calls the NCP. It selects by the run id only, never by the kit prefix, because
another person's run carries the same prefix. It also names each type it cannot
list, because a leak there is unknown, not absent.

**Then:** read the listing, and say which of it to delete. The sweeper deletes
only what you approved and what is still there, in an order that removes a
resource before the network or group it sits in, and then lists again. Render
the provider's entry point with the sweeper named, and each live run ends with
that listing.

### Step 3d — Record the exchanges (optional; needs an account, once)

**Ask:** *"Record the replay cassettes for `<resource>`."*

**You get:** one cassette for each operation of the resource, in the directory
you name. Nothing changes in the account before you say yes to that change.

**Whether you need it.** The three modes answer different questions, and only
one of them needs recordings:

| Your situation | The mode to use | Need this step |
|---|---|---|
| No account, now or later | **offline** — plans, applies nothing | no |
| An account available whenever tests run | **live** — the whole lifecycle against the NCP | no |
| An account once, but not in CI | **replay** — the whole lifecycle against what the API really returned | **yes** |

Replay needs no account *now*. It needs one to have existed once, when someone
recorded the exchange, and that is what makes it stronger than a fixture
somebody wrote: replaying is itself a request assertion, because the recording
is matched on method, URL and body.

**Where it sits.** After **3c**, because a recording needs what the earlier
steps make. The cases from 2a name the recording each operation needs. The
script reads the schema that the provider exported in 3a, to refuse a secret.
Each session ends with the sweeper's listing from 3c, because a refused delete
leaves the resource. The provider's offline entry point runs the replay test.
When the sweeper is rendered and a case names a recording that does not exist,
the agent asks whether you want to record.

    2a  scaffold provider  ──▶  3a  provider  ──▶  3c  sweeper  ──▶  3d  record,
                                                                     against the
                                                                     API itself
                                                                         │
                                     run 2a's own cases in replay mode ◀─┘

**The cases are never regenerated for a mode.** One set of cases runs offline
today, replay when the recordings land, and live when an account does. The mode
is chosen when the tests are rendered, not when they are written.

**It never goes through the provider.** A recording taken through the
generated provider records the provider rather than the API, and could not then
be evidence the provider is right. The agent builds each request from the case,
and sends it to the API directly.

**Recording is never something a test run does.** A run that can record is a run
that can create resources nobody expected, so a missing recording skips that
step and says so — it never triggers one.

**Each create, update and delete waits for your word.** Each one creates or
changes a real resource. The agent shows you the request and asks. The session
ends with the sweeper's listing for its run id, because a refused delete leaves
the resource.

**A cassette holds no credential and no account value.** The credential is sent
and never written, and the run id, the supplied values and the account values
you name become placeholders. Read each cassette before you commit it.

**Until a recording exists, replay is reported unavailable.** Asking for it
names what is missing, and the answer is a recording, not a change to the
cases. With the create, the read and the delete of a resource recorded, the
offline entry point runs its replay test after the request checks.

### Step 4 — Layer modules

**Ask:** *"Generate the `<layer>` module."*

**You get:** the layer module, written **into the directory step 2b laid out**:
one map variable and one output for each resource type the layer holds, and an
example that uses them. It reads two inputs only: the module plan, and the
schema the provider exported in step 3a. A resource the plan names and the
provider does not implement yet is listed in the module's `README.md`, not
generated. The step writes only that list into the README that step 2b wrote.
It changes the rest only for a part that it cannot make work, and tells you
what it changed and why.

**What to review:**

| Look at | Ask yourself |
|---|---|
| `references.yaml`, if you wrote one | Does each entry name the sibling resource the attribute really refers to? |
| The module case check | Does each value the module case sends match the reviewed resource case, or say why it differs? |
| The module's own `run-tests.sh offline` | Does it exit 0? The step is not done until it does. |

### Not built yet

The remaining step is designed:

- the **blueprint** generation, written into the directory step 2c laid out.
  Today a person or an agent writes the blueprint, against the composition case.

Ask for one today and the router will tell you it does not exist. That is the
correct answer, not a failure.

## The order, and why it does not bend

    coverage report           what the API supports, and what to build
        ↓   you review
    scaffold + tests          one per tier, in the directory you name
        ↓   you review
    provider, modules,        the implementation, written into those
    blueprint                 directories

**Tests come before the implementation.** A test written by reading finished
code proves only that the code does what it does. A test written first, from the
specification, cannot be bent to fit the code. When the later steps land, they
will ask you to review tests before any implementation exists — that is the
point, not an oversight.

## What you decide, and what the kit decides

| You decide | The kit decides |
|---|---|
| Which NCP, and which specifications | Whether an operation resolves in them |
| Where the output goes | What the output contains |
| Whether a verdict is believable | What verdict the evidence supports |
| Whether to run anything against a live account | Nothing — it never does this on its own |
| What to commit, and when | Nothing — it commits only when you ask, and only after the offline entry point exits 0 |

## Rules the kit holds itself to

- **It never invents a resource.** A layer whose operations are missing is
  reported as not buildable, with the gap stated.
- **No step contacts a live account.** The tests a step writes do, when you
  choose to run them against one. Generating and running are separate acts,
  and the second is yours.
- **It commits only when you ask.** You review, and you decide what to commit.
  Before a commit, the offline entry point (`run-tests.sh offline`) must exit 0
  in each directory that the change reaches.
- **It stops at one artifact.** No step runs the next one for you.
- **It reads only the specifications you supply.** Not the web, and not its own
  recollection of an API.

## When something looks wrong

| Symptom | What it usually means |
|---|---|
| A whole capability is marked `absent` | A specification is missing from the input set. Check what was supplied before believing the NCP lacks the feature. |
| The report contradicts a previous one | Compare the specification revisions first. A changed input explains more than a changed judgement. |
| A gate fails | Read the message and fix the artifact. Do not silence the gate; it is the only thing checking the work. |
| A skill asks you a question | Answer it. Skills name no NCP and no path by design, and a guess is worse than a question. |

## Where the output lives

In **your** repository, beside the Terraform it explains. The kit publishes
nothing, creates no repository of its own, and keeps no state between commands.

If you want to keep a run for comparison, copy the artifacts somewhere before
regenerating. Nothing is versioned automatically.
