# What a render writes, and how it runs

The detail behind `SKILL.md` step 4: the entry points, the provider source
address, and the request check.

## Contents

- [The entry point](#the-entry-point)
- [The provider README](#the-provider-readme)
- [The module README](#the-module-readme)
- [The render is the only writer](#the-render-is-the-only-writer)
- [The provider source address](#the-provider-source-address)
- [The request check, and the upper tiers](#the-request-check-and-the-upper-tiers)

## The entry point

**`--runner-out` gives the artifact one command per mode.** Point it at the
directory the artifact owns -- the provider's own directory for a resource
render, the module's own directory for a module or blueprint render. The render
writes `.env.example` there and the entry point inside it:

```bash
uv run scripts/case_builder.py render \
  --cases <path>/tests/cases --out <path>/tests/workspaces \
  --mode offline \
  --provider-source <the address the target repository states> \
  --runner-out <path> \
  --provider-mirror <the mirror directory>
```

An operator then copies one file and runs one command:

```bash
cp .env.example .env
./tools/run-tests.sh offline
```

The provider's entry point builds the provider into a temporary filesystem
mirror, writes the CLI configuration that points at it, runs every workspace,
and removes both. Those are exactly the acts a README otherwise asks a person to
perform, and the CLI configuration is the sharpest of them: it cannot be derived
from the tree, so it is the one file a newcomer has to author before anything
runs at all.

`--runner-out` is the artifact's directory, which is the operator's answer, not
the kit's. Omitted, neither file is written and nothing else changes.

**Each layer module and the blueprint run their own tests.** A module directory
can move to a repository of its own, so its tests read nothing from another
directory but the provider build. Render a module or blueprint case with
`--runner-out` naming that module's directory, and it gets its own
`tools/run-tests.sh` and `.env.example`. A render that holds both the
provider's cases and a module's is refused: each directory gets one entry point.

**The provider build reaches a module through a filesystem mirror.**
`--provider-mirror` is the mirror's directory. It is the operator's answer, and
every render of one provider gives the same one. Each entry point holds it as a
path relative to its own directory, so a clone anywhere still finds it, and
`TFKIT_PROVIDER_MIRROR` in that directory's `.env` overrides it. The provider's
`run-tests.sh build` writes the build there; a module's entry point refuses to
start until that build exists, and says how to make it.

| Directory | Modes | `.env` holds |
|---|---|---|
| the provider | `offline`, `live`, `build` | the run id, the mirror, the provider's settings, what the resource cases need |
| a layer module or the blueprint | `offline`, `live` | the run id, the mirror, the provider's settings, what its own live test needs |

A module's `offline` runs only its `*_offline.tftest.hcl` files, and `live` only
its `*_live.tftest.hcl`, because `terraform test` runs every file it finds and
the live one applies. Render the case with `--mode live` for the live file,
beside the offline one. It plans, applies and asserts each output, then plans
refresh-only and asserts each output again, so every resource is read back from
the API. `terraform test` destroys what the file created when it ends, and a
destroy that fails fails the test. A test file declares no variables, so the
names carry `${var.tfkit_run_id}` and each `{name}` a case holds becomes
`${var.tfkit_var_<name>}`; the entry point passes `TFKIT_RUN_ID` and every
`TFKIT_VAR_<NAME>` as those, quoted, and names every value the test lacks
before it starts. The live render writes `RUN_VARIABLES.md` beside the module's
tests, and the module's `.env.example` lists each value its live file reads. A
test file cannot assert that a second plan is empty; the resource acceptance
tests carry that check.

Every `.env.example` also lists the provider's own settings, because each
directory's tests configure the provider through the environment. The render
reads them from `provider-schema-export.json`, where `ncp-tf-provider` writes it:
each setting's description names the variable it falls back to. A provider
render reads the export in its own `--runner-out`; before the provider exports
its schema, the section says so, and a render after the export fills it. A
module or blueprint render needs `--provider-export`, the path to that file.

**A run id is generated; a supplied value is not.** A run id names what does
not exist yet, so it only has to be unique and visible. When `TFKIT_RUN_ID` is
unset, a live run generates one, and prints it when it starts and after its
tests. A `TFKIT_VAR_<NAME>` value names what must already exist, and a guessed
value builds a resource against the wrong parent, so a missing one stays a
refusal.
A value set in the environment wins over `.env`, so one run can override the
file without an edit.

`--json FILE` makes either mode write its machine-readable stream to one file,
which is what a verification record reads.

**The offline tier is the cheap gate, and it runs in this order:** `gofmt -l`,
`go vet`, the linter if the repository has chosen one, the tests beside the
code, the request checks, then each workspace. Formatting first because it is
the cheapest; the tests beside the code before the generated ones because that
is where a provider defect is caught soonest.

**Which linter, and which rules, is the repository's decision.** A
`.golangci.yml` is how it is made: present, the linter runs and the tool is
required; absent, it does not run and the entry point says so. A check that
skips itself in silence is the one that gets trusted while doing nothing.

**It ends with a summary, because `go test` has one word for a test that did not
pass.** A case that refused to start attempted nothing, and printing that as
`FAIL` makes a run where every case behaved correctly look broken, because
`go test` prints a refusal and a failure the same way. The summary
counts three things, and only the first is a conclusion the entry point is
entitled to draw:

- **refused to start**, which is the guards' own wording and so is this script's
  to recognise;
- **failed**, counted and not diagnosed. Whether a refusal by the API is a limit
  or a defect is a judgement, and the verification record is where a person
  writes it down. A test that started and has no result is failed too: `go
  test` writes no result line for a test that its timeout or a panic stopped;
- **passed**.

When every failure carries the same message it says so, because that is one
broken harness rather than N broken tests, and the reader who cannot tell opens
N investigations. When the package failed and no test failed, it says that the
package failed, so the run does not read as clean.

**`live` gives `go test` a timeout long enough for the whole package.** The
`go test` default stops the whole package after 10 minutes, and one lifecycle
can take longer. A test that is stopped before its destroy step leaves what it
created in the account. A `-timeout` argument from the operator replaces the
rendered one.

## The provider README

**The provider's `README.md` belongs to this step.** It says how to run the
tests and what the provider project looks like. `ncp-tf-provider` writes the
code so that each command in it works, and changes it only for a part that it
cannot make work.

**The render writes the run section.** A resource render with `--runner-out`
writes it between two `ncp-tf-scaffold` markers, from the entry point that it
writes in the same run: the commands of each mode, each offline tier in its
order with the command that runs one of its tests alone, how to select
resources, how a resource gets a replay test, the run values, what each
refusal means, where the source address is stated, and the manual path that
builds the mirror. The same inputs give the same section. Do not edit it: a
render writes it again. Text outside the markers stays.

**Write the layout section yourself, outside the markers.** The layout is a
reading of the repository's template or sample provider, so no script can
write it. Say where the provider's code goes, as the template lays it out, and
which directories the kit writes: `tests/cases`, the workspaces,
`tools/run-tests.sh` and `.env.example`. A person changes those only through
the cases and a render. When the repository has no template and no sample
provider, describe the kit's layout: the package layout that `ncp-tf-provider`
fixes, and the directories above. Say in your answer that the repository gave
no template. Do not ask the operator for a layout. The operator reviews the
section.

**The README states what is true now.** Do not write the scaffold's review into
it, or any other file: the filled values, the sizes and costs, the readiness
proposals and the open questions go in your answer. The operator's answers go
into each case, so a written review is wrong at the first answer. Do not list
what one run rendered, and do not state the provider's source address: the run
section says where the address is stated.

## The module README

**A layer module's `README.md` belongs to this step too.** A module render with
`--runner-out` writes its section between the same two `ncp-tf-scaffold`
markers, from the inputs of the module's entry point: the commands of each
mode, the mirror that both modes read, the run values, the module's layout with
the writer of each part, and the layer's `module_plan` `gap` text verbatim. The
module's layout is the kit's own, so the render writes it, and you write no
layout section.

The render also adds two reserved places once, and keeps what is between them:
an `ncp-tf-module` section, which `derive` fills with the planned resources
that the provider does not implement yet, and the `terraform-docs` markers
`<!-- BEGIN_TF_DOCS -->` and `<!-- END_TF_DOCS -->`, between which
`terraform-docs` writes the inputs and outputs. Render the module's case before
`derive` runs: `derive` writes no README without its section.

## The render is the only writer

The render is the only writer of the files that it writes: the workspaces, the
Go tests, the request check, the independent check and `RUN_VARIABLES.md`. The
first line of each Go test of a case starts with `// Generated from the case`
and ends with `Edit the case, not this file.` To change a test, change the case,
and render again.

A local script that renders a case again, or patches a rendered test, drifts.
The install replaces the skills, and it does not update that script. The
script calls code inside the skill, or finds its place in the render by its
text. A later render changes that code or that text. The script then breaks
without a sign, or it writes over a check that the render added.

These show such a script: a rendered test whose first line names another
writer, a README step that runs a script after the render, and a file beside
the rendered tests that only the script's patches call. When you find one:

1. Compare each check that the script and its helper files add with the checks
   that the installed render writes for the same case. A helper file is a file
   that the script writes, or that only its patches call.
2. If the installed render holds each of those checks, remove the script, its
   helper files and each instruction that runs it. Then render again in each
   mode that wrote the patched files. A live render runs nothing. Tell the
   operator what you removed, and which rendered check replaces each removed
   check.
3. If the render does not hold a check that the case needs, do not write a
   patch. Report the missing check to the operator as a kit defect: name the
   case, the check and the local file that holds it. Ask the operator whether
   to keep the script until the kit renders that check.

**Do not edit the installed kit.** The skills, their scripts and their
references are the kit's. The next install overwrites an edit there. An edit
that weakens a check also hides the defect that the check found. When a kit
script or a rendered check blocks a case that you think is correct, do not
change the script or the check. Report it to the operator as a kit defect: name
the script or the check, the case and the message.

## The provider source address

**What the source address is, and where it comes from.** It is the `source` line
of the workspace's `required_providers` block — what `terraform init` resolves
to find the provider. It is needed here and nowhere earlier, because nothing
before this step writes Terraform.

The provider does not exist in a registry while it is being built, so this is a
local address the repository chooses once and every workspace then repeats. Read
it from the repository: the provider's own directory, its `go.mod`, or a
`dev_overrides` block already in the operator's Terraform CLI configuration.
Propose what you found and have it confirmed. Ask cold only when the repository
states nothing, and say then that the value binds every rendered workspace.

**Know the shape before proposing one.** It is `<host>/<namespace>/<type>`, and
`type` is the suffix of the binary Terraform looks for: `terraform-provider-`
plus the type. So the type is the provider's, not the NCP's directory name, and
the namespace is a group that will own *that provider*.

**A Go module path is not a source address, even when it starts the same way.**
A provider whose `main` package sits in a subdirectory has a module path ending
in `terraform-provider-<type>` and a source address that stops one segment
earlier, so one string cannot be both. Read the module path for the host, and
check what the remaining segments claim: if the namespace already owns something
else -- the layer modules, a template repository -- then the address says this
provider lives there, and that is a claim on someone else's group rather than a
fact about this one.

**A value found in the tree is a value somebody typed,
not a value anybody checked.** Say where you read it and what it would mean, so
the confirmation is a decision rather than an echo.

**Until a namespace is chosen, use one that is obviously not a namespace.** A
host like `terraform.local` claims nothing and reads as local at a glance.
Putting a real group's name in the address makes every generated file assert
that the provider is published there, and that assertion then appears in fifteen
configurations, in the mirror layout, and on every line of Go test output — where
a reader has no way to tell it from a fact. Propose a placeholder and say it is
one; do not propose a group's name because the tree happens to contain it.

## The request check, and the upper tiers

**The request check is the offline tier's second half, and the more useful
one.** A plan proves the configuration and the schema agree. It proves nothing
about what would go on the wire, so a resource can be configured correctly and
still put a value in the field next to the one the API wants — and only a run
with credentials sees it. The check applies the same configuration against a
transport that answers every request with a refusal, then asserts what arrived:
the method, the path the operation declares, and every field the document
requires in the body. It also holds each value the configuration sets to what
arrived. A top-level scalar can arrive in the body, the query or the path. A
nested value or a list arrives in the body, at the same path: a map by its
keys, and a list by its length and each element. The match of names is the
read's rule (see the live test below).

It needs no account and no recording. **Refusing rather than answering is the
point**: inventing a plausible response means deciding what the API returns,
which is exactly what an offline test may not know, and needing a real one is
what makes replay wait for an account.

It asks two things of the provider, and both are already required of it — the
transport is substitutable so requests can be observed, and the provider's own
test setup fills the seam:

| The check declares | The provider assigns it |
|---|---|
| `requestProviderFactories` | provider factories built on the transport it is handed |
| `requestEnvironment` | its own settings, as stubs, for one test |

Without the second, a provider that cannot resolve its project refuses before it
builds anything, and the check reports that no request was sent — true, and no
use to anyone. What those settings are called is the provider's to know.

A case whose specification defined no create operation gets no request check: an
empty expectation passes against any request at all.

**The upper two tiers need no Go and no workspace.** `terraform test` treats a
module's own directory as the root, so there is nothing to wrap it in, and
neither tier carries an independent NCP check — the resource contract already
proved this provider's create and delete reach the NCP for every resource type,
and what is left is a Terraform-level claim Terraform asserts itself. A resource
either tier leaks is the sweeper's to find, by the run id in its name.

Point `--out` at the directory that holds the subject: the provider's tests for
a resource case, that layer module's `tests/` for a module case, the blueprint's
for a composition case. Two cases that would write one file stop the run.

It refuses a case whose judged fields are still empty — rendering one would
produce a resource with no configuration — and it names any case whose contract
and mode have no renderer yet rather than skipping it in silence.

**A live test references what the provider must supply**: its provider
factories, and a reader for the independent check. Those are the interface the
provider has to meet, which is the whole reason the tests are written first. The
check file defines the seam and nothing in it knows how this NCP authenticates.
The reader's `Read` returns the resource's own object, as the read operation
returns it, outside any envelope the API puts around it.

**The live test checks each value a step configured, at every depth.** A nested
map is checked by its keys, and a list by its length (`#`) and each element in
the order the case wrote. The update step configures the create's values with
the update laid over them, so it checks all of them: a create value that the
update does not set must still hold. Each value is checked in state, and then
in the independent read (`checkRead`). The provider keeps a configured value in state
when its read omits it, so only the read shows a value the NCP dropped. An API
can add its own entries to a list, so the read can hold more entries than the
step configured, in any order: each configured entry must be one read entry
with all its values, and the test output names a list that holds more. The read
matches an argument to the field whose name is the same with case, underscores
and hyphens ignored, and a map key only to itself. It also finds a value in
three other places: when a step's values hold one map and no list, that map can
wrap the whole resource and its values can be at the top of the read; a
top-level value the request sent in its path is in the read's id; a full URL
that ends with the configured relative name is the same reference.

The offline plan asserts the same values, at every depth: a list by its
`length`, and a nested value by its `tostring`. A provider can declare a number
as a string attribute, and Terraform's `==` does not convert.

**The live steps follow the lifecycle.** Each `plan_no_change` is a plan-only
step with the configuration of the step before it, so the update is planned
too. A `readiness` in the form `<field> == <value>` is checked in the read
after create and after update (`checkReady`). The field is the read's own,
with dots for each object it sits inside. `render` and `check --provider-export`
refuse a field that the schema export does not give the resource, at its top
or under the argument that wraps it. A readiness in prose names no
field, so no step checks it. A test that creates a dependency resource proves
its destroy at the end (`CheckDestroy`), with the destroy read and `expect` of
that resource's own case.

**A case with a `peer` creates it in the same apply as the resource under
test**, with no dependency between the two, because each is ready only when the
other exists. The live test reads the peer back after the create, and proves
its destroy at the teardown. The readiness, the update and the read-back of the
values stay on the resource under test. The offline plan asserts the peer's
values. The request check applies the create without the peer, because it
asserts the first request that arrives. Whether a case has a peer is the
operator's decision: [peer.md](peer.md).

A value under an attribute that the provider's schema export marks `sensitive`,
or describes as input only, cannot come back from the read. The read leaves it
out, and state still checks it. Render with `--runner-out` or `--provider-export`
so that the render reads the export. Without one, the read compares every
value, and such a value fails the step. A top-level value that the create
request sends as a parameter, in its path or its query, is not a field of the
resource either. The case lists these parameters in `required_parameters` of
its `create` request. The read leaves them out, and state still checks them.

`TFKIT_RUN_ID` must be set for a live run. The test refuses to start without
one, because a run whose resources cannot be found afterwards must not create
any. The entry point sets one before it starts the test.

**Ask for the provider's source address; do not choose one.** It is what the
target repository states, and the generated configuration is where it becomes a
requirement the provider has to meet.

Rendered files are canonical as written, so `terraform fmt -check` and
`gofmt -l` both pass without a formatting step — including the configuration
embedded inside the Go test.
