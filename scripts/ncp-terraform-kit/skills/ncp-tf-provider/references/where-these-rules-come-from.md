# Where the fixed half comes from

Read this when a rule in the fixed column looks arbitrary, or when deciding
whether something new belongs in it.

## The published guidance, and the three gaps in it

`terraform-plugin-framework` is HashiCorp's Go library for building a Terraform
provider: it defines the provider, resource and schema types a provider
implements, and the plugin protocol Terraform speaks to it. Use it, not
`terraform-plugin-sdk/v2` — HashiCorp's own guidance recommends the framework
for a new provider and has stopped most SDKv2 feature development.

A resource implements four methods — `Create`, `Read`, `Update`, `Delete` —
which Terraform calls to make the NCP match the configuration. `Read` fetches
the resource's current condition from the API so Terraform can tell what has
changed since last time.

**Use the NCP's own SDK when it publishes one.** Nothing here requires a
hand-written HTTP client, and an SDK usually brings authentication, retries and
pagination already solved. What is required is narrower: the client's HTTP
transport must be **substitutable**, so a test can see the request the provider
issued without a live call. Most generated SDKs allow this — pass a custom
`*http.Client` at construction — and one that does not cannot be tested offline,
which is worth knowing before it is chosen rather than after.

Watch one consequence: in at least one major SDK, supplying a custom client
takes precedence over every other option, authentication included. A test that
substitutes the transport must therefore carry whatever the request needs
itself, rather than expecting the SDK's own credential path to still apply.

`ImportState` is a fifth, behind `terraform import`. It takes an identifier for
a resource that **already exists** in the NCP and records enough about it for
`Read` to work, so an operator can bring existing infrastructure under Terraform
instead of destroying and recreating it. HashiCorp's requirement is exactly
that: leave enough for `Read`, or return an error.

The contract is fixed and the same everywhere. What is derived is the
**identifier format** — which fields a resource needs to be located again, often
more than its name, since a name is rarely unique across an NCP's whole account.

Everything in the fixed column follows HashiCorp's published provider guidance,
**except three items it does not cover**:

- **the poll loop.** The framework documents timeouts but publishes no wait or
  poll helper.
- **the repeat of a transient answer.** The framework leaves transport to the
  provider. An API answers 429 or a 5xx now and then, and a call sent once
  fails the resource on that answer. A failed delete leaves billed resources
  behind. The repeat is safe only as the same request: a create sent again
  with a new de-duplication token is a second create.
- **what to record when a create half-succeeds.** Terraform keeps *state*: its
  record of what it believes exists, which it compares against the API on the
  next run. HashiCorp says both that returning an error does not stop Terraform
  writing that record, and that a create error marks the resource for
  replacement on the next run. Neither says what to write when the API made
  something but the call then failed — and writing nothing loses the only handle
  by which it could later be destroyed. The kit records the id that the API
  named, as soon as it names it, and keeps it on every later failure. An
  operation that ends in an error does not prove that nothing exists. A kept id
  of a resource the API never made costs one call that answers not-found.

Those three are this kit's choices. Do not present them as Terraform practice.
