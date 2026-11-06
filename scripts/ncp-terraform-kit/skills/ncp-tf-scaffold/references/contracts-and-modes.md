# Contracts and modes

What each contract puts under test, and what each mode lets the requests reach.
`SKILL.md` names them; this says why each is shaped as it is.

## Contents

- [The two choices](#the-two-choices)
  - [Offline means no call to the NCP API](#offline-means-no-call-to-the-ncp-api)
  - [What replay asserts, and what it does not](#what-replay-asserts-and-what-it-does-not)
- [Which checks belong here](#which-checks-belong-here)
- [Rules that decide the content](#rules-that-decide-the-content)
- [What `review` carries](#what-review-carries)

## The two choices

Two independent choices, made at two different times. The **contract** says what
is under test, and it shapes the case. The **mode** says what the requests reach,
and it shapes the run.

**The case does not name a mode.** It states the whole lifecycle the API can
support, the check that proves each step and the cleanup they oblige, and a run
reads the part it reaches. So the same case runs offline today and live
tomorrow, and widening it is a render rather than a rebuild.

**Three contracts** — the subject. All ship in `references/contracts/`.

| Contract | Applies | Asserts |
|---|---|---|
| `resource` | a minimal root module holding one resource | the whole lifecycle, with an independent check at each step |
| `module` | the generated layer module | its variables are accepted, and its outputs resolve |
| `composition` | the blueprint | each layer's outputs satisfy the next layer's inputs |

Each is scoped so that **a failure indicts one thing**. `resource` drives a
minimal root module, so a failure there is the provider and nothing else — which
is exactly why it cannot also test the module. Without `module`, a defect in a
module's variables or wiring first appears in `composition`, where a failure
cannot say whether the module or the composition broke.

**`composition` is not redundant once the other two pass.** A module case is
handed its variables as literals, so nothing in the first two contracts ever
connects one layer's output to the next layer's input. Four defects live only in
that seam: an output whose type or shape the next input cannot take, an output
not known until apply, a missing dependency so one layer applies before the
other is ready, and a blueprint output that does not resolve.

**Run `composition` in `offline` mode always; treat its `live` run as the first
thing to drop.** Offline validates and plans the whole blueprint with no account
and nothing created, and that alone catches the type and shape mismatches across
the seam. The live run is the expensive one — every layer applied, the longest
cleanup budget of the three — and it is what buys the other two defects. When
time or permissions are short, that is the run to skip, and the skip is reported
with its reason like any other.

**Three modes** — the transport, chosen when the tests are rendered and run.
All ship in `references/modes/`.

| Mode | Needs an account | Needs recordings | Proves |
|---|---|---|---|
| `offline` | no | no | the configuration and the contract hold |
| `replay` | no | yes | the provider issues the right request, against what the API really returned |
| `live` | yes | no | the NCP accepted it, and the resource exists |

**`offline` is not `replay` with the cassettes missing.** It is the mode for
someone with no account and nothing recorded — which is where most work starts.
It stops before apply, because applying is the thing that needs an API, and it
still proves a great deal: it formats, it initialises, it validates, the
provider's schema loads, the plan names the addresses the case expects, a
module's inputs and outputs line up, and no request reached the NCP API.

**`replay` needs an account to have existed once**, when a person recorded the
exchange. That is what makes it stronger than a fixture somebody wrote: the
recording proves the API returned that shape, rather than that someone believed
it would.

**`live` is never a fallback.** No missing input, no absent recording and no
failed offline run may cause a live call. Entering that mode is something a
person does on purpose.

### Offline means no call to the NCP API

An offline run sends no request to the NCP API, the server under test. It uses
no credentials and needs no NCP address — do not ask the operator for one. It
may fetch dependencies, for example Go modules or a provider from a registry or
a mirror. The `terraform` binary is supplied by path rather than downloaded.

### What replay asserts, and what it does not

Replaying is itself a request assertion — the matcher compares the method, the
URL and the body, and the run fails when nothing matches. It never compares a
header, so the case carries the headers the document demands, derived from its
header parameters and its declared security. A scheme the script cannot map
lands in the case's `undetermined` list for a person to resolve.

**A missing recording skips its step; it never falls back to recording one.**
The step stays in the case, because the API offers the operation and a later
recording has to revive it without the case being written again. What is absent
is the recording, and a skip is never a pass.

**Replay is unavailable until something is recorded, and the kit says so.**
Rendering a replay run against a directory holding no recording is refused with
the reason, not with a failure that reads like a defect in the cases:

    replay is not available yet: <directory> holds no recording for any of the
    N operations these cases assert. Record them with the ncp-tf-cassette
    skill: it sends each request of a case to the NCP's API directly, never
    through the provider -- one taken through the provider records the provider
    rather than the API. It needs an account once, and the provider's schema
    export, which marks each field that holds a secret. The cases need no
    change when the recordings arrive.

Say that to the operator rather than offering replay as a choice they cannot
take.

`references/what-counts-as-proof.md` has the rest.

## Which checks belong here

**Which checks belong here, and which do not.** A check that can be decided by
reading files — the artifact, and the specification it was derived from —
belongs to the step that produced it, and runs as that step's own gate. A check
that needs something to *run* before anyone can decide it belongs here:
Terraform, the provider, the NCP, or a recording of it.

That is the difference between a gate and a test, and it is why a case names a
lifecycle rather than a list of assertions. Nothing here re-checks what a gate
already settled.

## Rules that decide the content

**A step with no operation behind it is dropped, and the case says so.** A
resource the API offers no update for loses the update steps rather than
carrying one that fails for the wrong reason — a reader cannot tell that failure
from a real one. A resource with no delete keeps its cleanup obligation and
marks it manual, because nothing will remove it.

**The independent check calls the API, not Terraform.** Terraform's own view of
a resource is what is under test, so it cannot also be the evidence. Each check
is one call to the operation the module plan names.

**The assertion in an offline case is the request, not the response.** A
provider that reports success without calling the API fails, because no request
arrives.

**Cleanup is reported even when the run failed early.** A run that created
resources and then failed has more to remove than one that passed, not less.

## What `review` carries

A case also carries entries the script filled in and a reviewer should still
look at. `naming.name_pattern` is the one today: the convention joins its parts
with a hyphen, and an identifier that forbids one needs another spelling. These
are marked in `review` like any other question, and they block nothing, because
a value is already there.

Each case repeats its judged fields under `review`, with the question the
contract asks and what a wrong answer costs. That is the copy a reviewer sees,
so it travels in the case rather than staying in the contract.
