# Where the cases go

How cases are filed inside a tier. `SKILL.md` keeps the rules an agent must
follow; this holds the rest.

**Why the layout is the repository's.** A generated tier laid out differently
from a hand-written one is harder to review and harder to adopt. The parts a
template carries — CI, linting, `terraform-docs`, release — are exactly the
parts nothing here generates. A repository with no template, no provider and no
sample module states no layout, so the only layout is what the kit's steps
write: the cases, the rendered tests, the entry point, `.env.example` and the
README of this step, and the files that `ncp-tf-provider` and `ncp-tf-module`
define. That is not an invented layout. The operator has no basis to choose
another one, so a question about it stops the run for an answer nobody can
give.

The recordings a replay run reads belong with the provider's cases, since they
are what the provider's requests are matched against.

**Inside that directory, a resource case is filed under the requirement it
exists for:**

    <tier>/tests/cases/<section>/<subsection>/<resource>.yaml

The headings are the requirement catalog's, lowercased and hyphenated, and they
come from the plan's own `requirements` for that resource -- this skill chooses
nothing. A resource serving requirements under several headings is still one
case, filed under the first the plan lists.

A dependency resource — one the plan lists with `dependency_of` and no
`requirements` — is filed with the resource it serves, and a `--section` run
keeps it with that resource. Its case carries `dependency_of`, and its
`requirements` are those of the resource it serves. The progress scan counts it
towards no requirement, because it is not what the requirement asked for.

A module case covers a whole layer and a composition covers every layer, so
neither belongs under one heading and both stay at the root.

A module case's `variables`, and a composition case's, may hold a placeholder,
such as `organizations/{organization_id}`. The offline test fills it with a
stand-in, as a resource's offline test does. A test that applies reads it from
the run as `TFKIT_VAR_<NAME>`, and the render lists it in `RUN_VARIABLES.md`
beside the tests and in the module's own `.env.example`.

**The blueprint tier needs a purpose before it needs a path.** A blueprint
composes layers *for* something, and what it is for decides which layers it
wires and what the composition case asserts. Ask; there is no sensible default.

**What it still only names.** A workspace declares the provider's local source
address and the resource type it configures; a module case declares the module
path it applies. None of those exist yet. They are the interface the later steps
must satisfy — a provider that publishes a different source address or resource
type does not fail its own checks, it fails these.

**A resource run's `selection.yaml` sits beside its own cases.** A module case
names its layer and a composition case its layers, so reading what a whole flow
covered means reading that record and those cases.
