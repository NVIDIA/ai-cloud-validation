# A peer of the case's own type

How a case declares a counterpart of the resource under test, and when.
`SKILL.md` step 3 points here.

**When a resource needs one.** Some resources are usable only with a
counterpart of their own type. A VPN tunnel is ready only when a tunnel at its
far end names it back and holds the same key. The two sides of a network
peering are another example. A live run shows it: the read stops at a state
that waits for the other end, such as a first handshake or a wait for the peer,
and the readiness names a later state. The document can show it too, in the
description of that state or of the field that names the other end.

**The operator decides.** A peer creates a second resource of the type under
test, and often its own dependency resources, so it costs more. A request to
fix the failed test, or to make it pass, is not this decision: it does not say
which resources the operator agrees to pay for. Ask even when the operator
asked for a fix. Before the operator answers, add no peer, and no dependency
resource for one. Do not set the field that names the other end to an address
or a resource that you chose.

1. Show the operator the evidence: the run id, the test, and the state that the
   read returned.
2. End your answer with one question, for example "The tunnel reads
   `<state>` and needs a tunnel back to reach `<ready state>`. Should the test
   create a second gateway and a return tunnel?". A statement that the case
   waits is not a question.
3. Do not change the readiness to the state that the read returned. That
   readiness passes a resource that is not usable.

**A peer is not a dependency.** A `dependencies` entry of the case's own type
resolves the case again, and `render` stops with `dependency cycle`. A
dependency resource is also created before the resource under test, and the
counterpart is ready only when the resource under test exists. The provider's
create waits until its resource is ready, so the first create waits for a
resource that does not exist yet.

## The shape

```yaml
fixture:
  create:
    gateway: '{gateway}'                 # this side
    remote_gateway: '{peer_gateway}'     # the far end
dependencies:                            # the dependency resources of both sides
- variable: gateway
  resource: <ncp>_vpn_gateway
  reference: <..., ending tfkit-{run_id}-{case}-gateway>
- variable: peer_gateway
  resource: <ncp>_vpn_gateway
  values: {network: '{peer_network}'}    # the far end's own network
  reference: <..., ending tfkit-{run_id}-{case}-peer-gateway>
# ... the networks, the addresses and the rules of each side
peer:
  why: <why the resource under test needs it, and that the operator chose it>
  values:                                # laid over fixture.create at every level
    gateway: '{peer_gateway}'
    remote_gateway: '{gateway}'
```

- The peer is this case's `fixture.create` with `values` laid over it at every
  level. A field that `values` does not set keeps the case's value, for example
  the shared key.
- A `values` entry can hold a `{variable}` of this case's own `dependencies`.
  Declare each dependency resource of the far end there too, with `values` that
  set it apart. An entry whose `values` name another dependency resource is
  created after that resource.
- The peer is named `tfkit-{run_id}-<case>-peer`.

## What the render does

- The setup step creates the dependency resources of both sides. Then one apply
  creates the resource under test and the peer, with no dependency between the
  two, so Terraform creates both at the same time.
- The live test reads the peer back after the create, and proves its destroy
  with this case's destroy read. The readiness, the update and the read-back of
  the values stay on the resource under test. The update does not change the
  peer.
- The offline plan asserts the peer's values. The request check applies the
  create without the peer, because it asserts the first request that arrives.

`check` refuses a peer with no `why` or no `values`, a `values` key that is not
a field of `fixture.create`, a `{variable}` that the case's `dependencies` do
not declare, a peer in a case that declares no dependency resource, and a peer
in a module or composition case.
