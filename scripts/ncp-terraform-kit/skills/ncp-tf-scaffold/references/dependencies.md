# Dependency resources

How a case declares a resource that must exist before its own, and what
`render` refuses. `SKILL.md` step 3 points here.

## Contents

- [Declare a dependency](#declare-a-dependency)
- [The dependencies that the documents state](#the-dependencies-that-the-documents-state)
- [More shapes](#more-shapes)

## Declare a dependency

**A value that names another resource is a dependency, and the case says so.**
When `fixture.create` needs the identifier of a resource that must exist first —
a firewall's network, an address's subnetwork — write the value as a placeholder
and declare it under `dependencies`:

```yaml
fixture:
  create:
    network: '{network}'
dependencies:
- variable: network                  # the placeholder, and TFKIT_VAR_NETWORK
  resource: <ncp>_network            # the case that creates one
  reference: <the identifier the created one has, e.g. .../networks/tfkit-{run_id}-{case}-net>
```

- `TFKIT_VAR_<VARIABLE>` set: the case uses that existing resource. The value
  is its name, not a path. Each test puts the name where its `reference` holds
  the created resource's name, so one value serves cases whose APIs take
  different forms: a path for one, a full URL for another. The live test
  refuses a value with a `/`. An `attribute` dependency takes the attribute's
  value as it is.
- Unset: the workspace creates the dependency resource from its own reviewed
  case, as `setup_<variable>`, before the resource under test, and destroys it
  after. The live test does this as its own named steps, so a failure there
  reads as a setup failure. The destroy read of that case proves the
  dependency resource is gone. The offline test plans both paths.
- A created dependency resource is named for the case that creates it, so two
  cases in one run never ask for the same name. The name goes into the value of
  its own case's fixture that starts with `tfkit-{run_id}`, in `name` or in
  another field such as an id or an email. When that field is short, a tag of
  four characters stands for this case. `{case}` in `reference` is what the
  created name carries for this case, so write `{case}`, not this case's name.
- Never build a dependency resource's name from `{run_id}` alone. No test
  creates it, and no supplied value can name it, because the run id is new
  every run.

## The dependencies that the documents state

`instantiate` records them in the case under `derived_dependencies`. Each entry
holds the `field` of `fixture.create`, the `resource` type that the plan holds,
and the `rule` and the `evidence` from the documents: a create that posts under
another resource's item (`path_nesting`), or a field whose description states
another resource's name format (`stated_format`). Give `instantiate` the
provider's `--account-scope`, so that the account is not taken for one.

`check` and `render` refuse a case whose fixture sets such a field to anything
but a whole placeholder that a `dependencies` entry with that `resource`
declares. A plain value and a `required: true` entry are refused, because the
provider can create the resource. When `check` refuses one, also right after a
refresh, declare it. Do not wait for the operator: the operator already chose a
supplied dependency resource first and a created one as the fallback, and the
entry keeps the supplied path.

1. Read the dependency resource's reviewed case. When that case chooses the
   resource's name, write a `reference` that holds the created name
   (`tfkit-{run_id}-{case}-...`) in the form that the field takes. When the API
   assigns the value, write an `attribute`.
2. Write the field as the entry's placeholder, whole. A prefix such as
   `networks/` goes into the `reference`.
3. Add `dependencies` to `judged`.
4. In your final reply, name the new `TFKIT_VAR_<VARIABLE>` of each entry. A
   value that the operator set under the old name no longer reaches the test,
   and with the new one unset, the test creates the resource.

The rules are a floor. A value that names another resource is a dependency
also when no rule derives it, and also when it is not the name field, such as
a membership's member. Do not build such a value from `{run_id}` unless the
same test creates that resource.

## More shapes

Three more shapes cover what a single placeholder cannot:

```yaml
dependencies:
- variable: esp_rule                 # nothing in this fixture names it
  resource: <ncp>_rule
  values: {protocol: ESP}            # laid over that case's fixture.create
  reference: <the identifier, ending tfkit-{run_id}-{case}-esp-rule>
  reason: <why the API refuses this resource's create until it exists>
- variable: address                  # the fixture writes '{address}'
  resource: <ncp>_address
  attribute: address                 # in place of reference: what the API assigns
```

- **Several of one type.** When the resource needs more than one dependency
  resource of the same type, write one entry for each, each with its own
  `variable` and the `values` that set it apart. An entry with `values` is
  named for its variable, `tfkit-{run_id}-{case}-<variable>` with hyphens, so
  entries from one case never share a name.
- **A variant of a reviewed case.** `values` also adapts one resource to what
  this resource needs, without changing that resource's own case -- an address
  made external for a forwarding rule. A field set to `null` is left unset. A
  dependency the case fills that field from is then not created.
- **One that nothing names.** When the API refuses the create until a resource
  exists, but no field of this fixture names it, declare it anyway and give the
  `reason`. The workspace creates it first and the resource under test waits
  for it. Without a `reason`, an entry nothing names is refused, because it is
  more often a misspelt placeholder.
- **A value the API assigns.** When the field takes something the API only
  knows once the dependency resource exists -- an address's IP, a
  server-generated id -- write `attribute` in place of `reference`. Unset, the
  resource receives the created resource's attribute; set,
  `TFKIT_VAR_<VARIABLE>` supplies the value. A reference to a value like this is
  a guess, and a field the read returns in another form differs on every plan.
  The offline plan cannot know the value, so only the supplied path asserts it.
- Read the resource's documentation, and the provider it is modelled on if
  there is one, for what has to exist before a create. A dependency missed here
  is found by a live create that waits or is refused.

When the provider has no resource that creates the dependency, the only path is
a supplied value. Write `required: true` with a `reason` in place of `resource`
and `reference` or `attribute`; the guard then asks for it on every run. Look for the
dependency resource in the plan first: the coverage step plans one with
`dependency_of` when the specification can create it.

`render` refuses a dependency that names no case, one with neither or both of
`reference` and `attribute`, a `reference` that does not name the resource the
entry creates, a cycle, `values`
that are not a non-empty mapping, two entries that would create one name, a
dependency placeholder inside a longer value (only a whole value becomes the
created resource's reference or attribute), and a variable nothing uses when
the entry gives no `reason`.
