# What counts as proof

Read this when deciding whether a check is evidence or a restatement.

## Two agreeing, and only one is Terraform

Two independent things have to agree, and only one of them is Terraform.

| Layer | What it reads | What it catches |
|---|---|---|
| Terraform's own view | the state, the outputs, the plan | the provider wrote the wrong value into state, or a plan does not settle |
| the independent check | **the NCP's API, directly** | the provider reported a success the NCP never performed |

**The independent check never goes through the provider.** Terraform's view of a
resource is the thing under test, so it cannot also be the evidence. Each check
is one call to the read operation the module plan names, and the case says what
that call must come back with:

| After | The read must | Which catches |
|---|---|---|
| create | return the resource, holding each value the create configured, at every depth, and the field `readiness` names at its value | a provider that reports success without calling the API, a value the API dropped or replaced while state kept it, and a resource that is not usable yet |
| update | come back **changed**: holding each value the update configured, each create value it did not set, and still ready | an update the API accepted and ignored, and one that reset or replaced what the create set |
| destroy | return what `verification.destroy.expect` names: not-found, or the marker of a soft delete. The same holds for each dependency resource the test created | a delete that returned success and removed nothing |

The update check is the one that earns its place most often. A provider can
issue a perfectly well-formed update that the API accepts and discards, and
every Terraform-side assertion still passes.

**Terraform's state is not a second opinion, and neither is a refresh.** Exported
state is Terraform's own belief written down, so reading it back proves only that
it believes what it believed. A refresh does reach the API, but it reaches it
*through the provider's read path* — which is part of what is under test. The
classic provider defect is a read that treats a not-found as success, and a
refresh-based check passes for a resource that was deleted out from under it.
The independent check exists to have one path to the truth that the provider is
not on.

**Only `live` verifies.** A recording cannot say whether a resource exists now,
and an offline run created nothing, so offline and replay runs do not reach the
verification steps. The case keeps them for a live run. A resource the API
offers no read for cannot be verified at all in any mode, and the case records
that rather than implying a check happened.

## Which values prove a requirement

A case cites the requirements it exists for, under `resource.requirements`. A
pass proves a requirement only when a value of the case exercises it: a node
pool case that cites "specify the node type" and sets no machine type tests the
API's default. So `proved_by` names, for each cited requirement, the fixture
paths whose values exercise it. `check` and `render` refuse a cited requirement
with no path, and a path that the fixture does not set. Whether a set value
really exercises the requirement is the review. When no value of the resource
can, do not name another value: propose to the operator the correction of the
plan that ties the resource to the requirement.

## What a replay matcher compares, and what it leaves to the case

Replaying is itself a request assertion: the matcher compares the method, the
URL and the body, and the run fails when nothing matches. Two things it does not
compare, which the case must therefore carry:

| Not compared by the matcher | So the case carries it |
|---|---|
| any header | `headers`, derived from the document, and a header that only a prose document states, which the agent proposes and the operator confirms ([provider-headers.md](provider-headers.md)) |
| the body of a request that has none — a read, a delete | its method and path, which is all there is |

Headers come from two places in a document, and the script reads both: a
parameter declared to sit in the header, and the security the document declares.
Neither language this reads puts credentials in a parameter, so declared
security is the usual source. **A scheme the script cannot map to a header is
reported in the case's `undetermined` list rather than assumed away** — derive
it from the document and have a person review it.

**A missing recording skips its step, and the skip is reported; it never falls
back to recording one.** This is the rule of `modes/replay.yaml`. `render` has
no replay renderer yet: it reports that and renders nothing. A replay library
may treat an absent cassette as permission to record, and the run would then
reach the live API without saying so. The absence is decided before anything
runs, which is what `--cassettes` is for, and the script refuses a replay run
without it.

## A destroy shown on another resource

Some resources show a destroy only on another resource: a key that cannot be
deleted has its versions destroyed, and the key's own read says nothing. Then
add `via` under `verification.destroy`, and write `expect` for that read:

```yaml
verification:
  destroy:
    expect: state == DESTROY_SCHEDULED
    via:
      operation: <the other resource's read, as op:...>
      id: '{id}/<the path to it>'   # {id} is the destroyed resource's id
      why: <why this resource's own read cannot show the destroy>
```

`check` refuses a `via` without `{id}` in its `id`, because a fixed identifier
reads the same resource whatever the run destroyed. It refuses one without a
`why`, because a reviewer cannot tell it from a read of the wrong resource.
