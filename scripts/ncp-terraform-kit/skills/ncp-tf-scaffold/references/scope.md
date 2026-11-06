# Scope

How a run is narrowed, what it writes about what it left out, and what
`progress` reports. `SKILL.md` step 1 points here.

**`--section` takes a requirement heading, not a layer.** An operator asks for
a function -- IAM, key management, virtual networking -- and that is the
requirement catalog's vocabulary rather than Terraform's. It matches a
case-insensitive substring of either heading, so `--section IAM` reaches
*Identity and Access Management (IAM)*. Ask which they mean when both a layer
and a heading would fit; they are different cuts and they overlap only by
accident.

**A narrowed run and a full run must not report the same green.** A resource
run writes `selection.yaml` beside its cases, naming what it covers and what it
does not, each skipped resource with the reason `not_selected` or
`not_buildable`. A `not_selected` resource also has `why` and `source`: the
operator's reason, and who or what decided. A requirement needs each planned
type, so `check` refuses either one empty, and a rerun keeps them. Write only a
reason that the operator stated. The scope of the request is not a reason: it
says what is left out, not why. For each other type, leave its fields empty and
end your report with a question to the operator: why is it not built, and who
decided? A pending item in a status line is not a question, and the operator
does not answer it. A step that a case
drops is recorded under that case's
`narrowed`. Report that file. A module or blueprint run writes none: a module
case lists the whole layer it covers, and a composition case every buildable
layer, so a record would only repeat the case. A blueprint run names in its
output each layer it cannot compose; report that line. A skip is never a pass, and a run
over part of the plan must not read like a run over all of it.

## What `progress` reports

`progress` reports each requirement as **missing**, **test-only** or **done**.
Without `--provider-dir` nothing reads as done: a written test is not an
implementation, and calling it one would report a cloud as finished the day its
tests were scaffolded. With it, a requirement is done when a file under that
tree is named after the resource -- which is a reading of a filename, not a
check that the code is right, and the report says so.

It writes nothing. Run it as often as you like; it cannot go stale, because
there is nothing stored to go stale.

## What lands beside the cases

**What lands beside a resource run's cases.** `selection.yaml`, naming what the
cases in the directory cover and what they do not, each skip with its reason. It opens with a comment saying
what it is, so a reader who finds it needs nothing else to read it.

**No guidance document is written.** What a reviewer must decide is inside each
case, under `review`, next to the field it asks about — which is where they
already are when the question arises. A separate document restates it, is read
once, and then sits in the repository going stale.
