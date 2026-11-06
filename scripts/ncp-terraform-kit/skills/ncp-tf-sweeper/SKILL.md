---
name: ncp-tf-sweeper
description: Use when a test run left resources in an NCP account — "what did the run leave", "leaked resources", "sweep", "clean up a killed run", "a live run stopped before destroy". Finds the resources that carry one run id, and deletes only what the operator approved.
version: 0.1.0
metadata:
  author: NVIDIA Corporation
  tags: [ncp, terraform, cleanup, live-test, sweeper]
---

# NCP Terraform Sweeper

## Purpose

A live run that is killed, or that fails before its destroy step, leaves real
resources in the account. They cost money, and they can block the next run.
A resource that a run creates carries the run id in its name, in a label or in
another field that the client chose (`ncp-tf-scaffold/references/naming.md`),
or it sits under a resource that does. This skill finds the resources of one
run id, and removes them only with the operator's word.

## The three rules

**1. Select by the run id. Never by the kit prefix alone.** A test account is
shared. Another person's run carries the same prefix while it is still in
progress, so a prefix-wide sweep deletes their resources. The sweeper refuses
to start without `--run-id`. The runner prints the run id after the tests of
each live run. When you do not know the run id, ask the operator for it.

**2. List first, and delete only what the operator approved.** Do not delete
before the operator approves the listing. A request to "clean up" asks for the
listing first: show what the run left, and ask. A delete is hard to undo, so it
is the operator's decision each time, even when an earlier sweep was approved.

**3. Delete only through the sweeper.** Do not delete with the NCP's CLI, its
console or a script of your own, even when the operator approved the listing.
The sweeper deletes only a resource that is in the approved record and that is
still there, in the order the plan derived. A command of your own has neither
check.

## Prerequisites

- The approved `coverage.yaml` with a `module_plan`, and the specification
  documents that it records.
- The provider's reviewed resource cases.
- The provider's schema export, when a list operation takes the account from a
  provider setting.
- The account scope that the provider's render was given, such as
  `projects/{project}`, when a list operation takes the account as a resource
  name.
- Go, and the NCP's Go SDK or HTTP client.

## Steps

### 1. Render the sweeper

```bash
uv run scripts/sweeper.py render \
  --coverage <the approved coverage.yaml> \
  --cases <the provider's resource cases directory> \
  --provider-export <the provider's schema export> \
  --account-scope <the account scope that the provider's render was given> \
  --out <the sweeper's directory>
```

Ask the operator where the sweeper goes, the first time. The render writes
`sweep.yaml`, the sweep plan with the source of each fact, the Go core, and a
`go.mod` when the directory has none. It never writes `adapter.go`, and it
keeps a `go.mod` that is there.

The sweeper is a Go module of its own. Inside the provider's module, its
`main` package is a second one, and the provider's build refuses two. Its
`go.mod` holds the NCP's Go SDK that the adapter uses.

The provider fills a parameter that names the account, such as
`projects/<project>`, from its account scope. The sweeper must list where the
provider creates, so give it the same account scope. Read it from the
provider's own record of its render. When you find none, ask the operator for
it. Do not write the scope into a reviewed case to make the render pass: the
operator reviewed the case, and a new value there changes what the provider's
tests send.

The render stops when a list operation takes a parameter that nothing gives:
not the reviewed case, not a declared dependency, not a provider setting, and
not the account scope. Show the operator the message. Do not guess the value: a
sweeper that lists in the wrong scope finds nothing, and an empty listing reads
as clean.

### 2. Show the operator `sweep.yaml`

Say which types the sweeper lists, and in which scope. Say each type with
`run_fields`: the sweeper selects it by that field, because its name does not
hold the run id. Say each type with `by_parent`: its resources carry no run id
of their own, so the sweeper selects one only under a resource of that type
that the run made, and never one under a supplied parent. Say each type under
`cannot_list`: the sweeper cannot see a leak of that type, so a listing that
finds nothing says nothing about it. The reason `carries_no_run_id` means that
nothing in its resources says which run made them. Say each type under
`cannot_delete`: the sweeper lists it and never deletes it. Say each type under
`not_built`: no reviewed case builds it, so no run creates it, and the sweeper
does not list it. To sweep it, the operator first reviews a case that builds it.
Ask the operator whether to scaffold that case with `ncp-tf-scaffold`, and
render again after the review. Do not write the case yourself, and do not edit
the installed kit or the rendered files to list the type: a case that the
operator did not review changes what the provider's tests send, and the next
install or render drops an edit.

End this report with a question when a recording is missing. Each reviewed
case names the cassette of each operation under `expected_requests`. When the
provider's cassette directory does not hold one of them, ask the operator
whether to record the missing cassettes with `ncp-tf-cassette`. Ask even when
the operator told you to stop after the render: the question starts nothing.
With the recordings, replay runs the whole lifecycle with no account. The
question comes here and not earlier, because each recording session ends with
this sweeper's listing.

### 3. Write `adapter.go` once

`adapter.go` is the only file that reaches the NCP. It is `package main`, and it
defines `NewClient(ctx context.Context) (Client, error)`. Its client implements
the two methods of `Client` in `sweeper.go`:

- `List` calls the type's list operation in the given scope. It returns each
  resource with its relative resource name: the name that the API's own
  operation paths take, which is also the scope value for a type nested under
  it. Never return a URL, such as a link to the resource: a delete and a nested
  list built from a URL address nothing. Return the labels too. For each field
  that the type's `ListedFields()` names, as a dotted path such as `key.id`, it
  puts the value that the API returned in `Fields`, and a value that is not a
  string as its JSON text, such as `true`. When the NCP answers that the scope
  does not exist, such as with HTTP 404, return an error that wraps the core's
  `ErrNotFound` (`%w`). Under a resource that the NCP keeps as deleted, the
  core reads only that error as "nothing is there". Any other error is a
  failed list.
- `Delete` calls the type's delete operation. It returns when the NCP reports
  the resource gone, or with the NCP's error. The next delete in the order must
  not meet a resource that is still in use.

The adapter decides nothing about which run a resource belongs to. Do not write
a run id into a resource's labels or fields, and do not skip or refuse a
planned type by its name. The core selects by `sweep.yaml`, and its unit tests
hold that selection; an adapter that marks a resource selects it for deletion.
The core never calls `List` for a type under `cannot_list`. When the plan
cannot select a type that a run creates, show the operator: the fix belongs in
the kit or in the case, not in the adapter. The adapter may leave out a
resource that the NCP manages itself and that no client can delete, with the
list's own filter where the documents give one.

The adapter imports no package of the provider. The sweeper exists before any
provider does, and a defect in the provider must not hide a leak.

Add `Create`, with the signature of `Creator` in `live_check_test.go`, only for
the live check in step 6.

### 4. Run the unit tests

Run `go test` in the sweeper's directory. The tests use a fake client and need
no account. They hold the selection on a plan of their own: near-miss names, a
name that the API cut, the order, and a delete that the NCP refuses. They also
hold that your plan deletes each type before the type that it is listed under.

### 5. List, show, ask, then delete

```bash
go run . --run-id <the run id> > sweep-<the run id>.json
```

The sweeper reads the `TFKIT_VAR_<NAME>` values and the provider settings from
its environment, as the run did. Without one, it cannot list where the run
created: it records each type that it could not list under `cannot_list`, and
exits with status 3.

The record on stdout is JSON, which every YAML reader reads. Show the operator
what is under `found`, `cannot_list` and `cannot_delete`. Ask whether to delete
the resources under `found`. Stop until the answer comes.

After the operator approves that listing:

```bash
go run . --run-id <the run id> --delete sweep-<the run id>.json
```

Report what is under `removed`, `failed` and `left`. A resource that appeared
after the approval stays, and is under `found`. The exit status is 1 when a
delete failed or a resource is left.

### 6. The live check, only with the operator's word

The live check creates one resource of the run and one of another run, sweeps,
and asserts that only the first is gone. It creates and deletes real resources,
so run it only when the operator asks for it:

```bash
TFKIT_SWEEPER_LIVE=true TFKIT_RUN_ID=<a new run id> \
  TFKIT_SWEEPER_LIVE_TYPE=<a planned type> go test -run TestSweeperLiveCheck .
```

## The sweep record

| Key | What it holds |
|---|---|
| `run_id` | the run id the sweep selected by |
| `scope` | each type and the scopes it was listed in |
| `found` | each resource of the run: its type, name, labels, run `fields`, scope, and whether its `name`, a `field`, a `label` or its `parent` matched |
| `deleted` | each resource of the run that the NCP deleted and still lists: its field holds the value of its type's `deleted_when`. It is not in `found`, so a delete never holds it |
| `cannot_list` | each type the sweep could not list, and why: no list operation, an unset value, or a refused list |
| `cannot_delete` | each type that has no delete operation |
| `removed`, `failed`, `left` | after `--delete`: what went, what the NCP refused with its error, and what a listing after the deletes still found, other than what went |

## A live run ends with the listing

When the scaffold's `render` is given `--sweeper <the sweeper's directory>`,
each live entry point runs the listing for the run's id after the tests,
whatever their result. Read it after each live run, and report what the run
left. The provider's offline entry point then also runs the sweeper's `go vet`,
its linter and its unit tests.

## Limitations

- A type under `cannot_list` is not swept. Name it in the report. For a type
  with `carries_no_run_id`, the operator decides what to do with a resource of
  it, because no record says which run made it.
- A type under `not_built` in `sweep.yaml` is not in the sweep record. Render
  the sweeper again after a case for it is reviewed.
- The sweeper deletes only planned types. A resource that the NCP made for a
  planned one, such as the VMs of a node pool, goes with it, or is under `left`.
- A name the API cut short is found only by its label.
