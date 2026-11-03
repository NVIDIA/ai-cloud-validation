# Limitations and troubleshooting

## Limitations

- **Nothing here records a cassette.** `ncp-tf-cassette` records them, against
  the API directly. Replay renders a resource only when its create, read and
  delete were recorded, and a module or a blueprint has no replay.
- **A rendered live test is not compiled here.** It is checked as Go, not built:
  the provider it references does not exist when it is written.
- **Nothing here contacts an NCP account or runs Terraform.**
- **A specification may state no required body fields at all.** Where the
  document marks required-ness only on method parameters, an offline case can
  assert the body's schema and not its contents. The case shows an empty list,
  which is what the document says rather than a resource that demands nothing.
- **The fixture is a judgement, and a wrong one passes locally.** A value the
  API accepts but that does not exercise the resource produces a green run. The
  update field is where this happens most.
- **Readiness is the field most worth reviewing.** It is read from prose written
  for people, and a resource that exists is not always usable. Which state
  means usable is the operator's call.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `stale: the current kit derives this case differently` | The kit changed since the case was written, or its documents did. Refresh the case with `instantiate`, show the operator the report, and settle it with them: [refresh.md](refresh.md). Do not edit the derived fields by hand, and do not pass `--overwrite`, which discards every answer. |
| `stale: the case records no derived_from` | The case has no `derived_from` record, so nothing names the coverage it came from. Refresh it as the row above says, with the coverage that the cases were written from. |
| `is a tier, not a contract` | A tier was passed where a contract belongs. The message names the contract that tier's tests use; pass that. The table in **Purpose** is the whole map. |
| `coverage has no module_plan.layers` | The coverage step has not run, or its output was not approved. Run it first. |
| `is not defined in the supplied specifications` | The plan and the documents disagree about an operation. Fix the plan or supply the missing document; do not drop the case. |
| `composition needs two buildable layers` | Fewer than two layers are buildable, so there is no seam to assert. Expected, not a failure. |
| `check` fails every case | Expected before the judged fields are filled. Fill them, then check again. |
| `readiness names ..., and ... has no attribute ...` | The resource's read does not return that field. A field of the operation that the create returns, such as `done`, is not the resource's. Which state means usable is the operator's call, so do not choose one. End your answer with one question for each refused case, for example "Which state of `<type>` means usable?". A statement that the value is pending is not a question. Do not report the work as done while a case is refused. |
| A live test fails with `the read returns no <field>, so it cannot show the readiness` | The resource's schema has the field, and the live read left it out. Which state means usable is the operator's call. Show the operator the run's evidence and ask, as [below](#when-the-operator-and-a-live-read-disagree) says. |
| A live test fails with `<address> (<id>) is not ready: <field> is "<value>", and ready means "<value>"` | The resource exists, and the read shows a state before the one that means usable. When that state waits for a counterpart of the resource's own type, such as a tunnel back to a VPN tunnel, the case needs a `peer`, and the operator decides: [peer.md](peer.md) says what to show and what to ask. Do not change the readiness to the state that the read returned, and do not add the counterpart as a dependency. |
| A replay expectation has empty `required_fields` | The document states required-ness on parameters only. The schema is still asserted; cover the body in `live`. |
| A case carries an `undetermined` list | The document declares security this script cannot map to a header. Derive the header from the document and have a person review it. |
| `replay is not available yet` | Expected until something is recorded. A recording takes an account once, against the API directly rather than through the provider; the cases are already correct and change none of it. `ncp-tf-cassette` records it. |
| `no replay test -- no_cassette for ...` | The named operations of that case were not recorded. Without the create, the read and the delete, no later step can run. Record them with `ncp-tf-cassette`. |
| `the update step -- no_cassette` | The replay test of that case has no update step, because the update was not recorded. |
| A replay test fails with `no recording matches` | The provider sent a request that differs from the recording: the message prints both. A real difference is a defect of the provider or of the fixture; an update mask that names an attribute the configuration does not set is one. |
| `the case names a mode` | A hand-edited case froze a run-time choice. Remove the field: the mode belongs to `render`, and a case that fixes one cannot be rerun any other way. |
| `records no spec_documents` | The coverage predates this step or was hand-written. Re-run the coverage step; do not name the documents by hand, or the cases and the analysis can disagree about which API they read. |
| `its digest is ..., and ... records ...` | The file it names is not the document the coverage read: the document changed after the coverage step, or a copy with the same name sits beside `coverage.yaml`. Remove a stale copy. If the document changed on purpose, re-run the coverage step on it. |
| A resource has no update steps | The module plan names no update operation. The case records the drop with reason `no_operation`. |

## When the operator and a live read disagree

A live read shows what the NCP returns. A document shows what it should
return. The operator can state, from the document, that a read returns a field
that the run's read did not return: the field of a readiness, or a value of an
update.

A field that the read left out is not a defect to find. The independent
reader returns what the NCP returned, and the field is absent, not at another
value, so no wait in the provider and no retry makes it appear. Do not look
for the cause in the provider or in the NCP, and do not ask for their code.
The next step is the operator's answer:

1. Show the operator the evidence: the run id, the test, and the failure line
   that names what the read left out. That line is the whole evidence: a live
   test keeps no response body, so do not ask for a recording of the read.
2. End your answer with one question that names the evidence, for example
   "Run `<run id>` read `<type>` and the read returned no `<field>`. Which state
   of `<type>` means usable?". A statement that you kept the case is not a
   question. Change the case only on the operator's answer to that evidence,
   and do not report the work as done while the question is open.
3. Do not change a check to make the case pass: not a rendered test, and not a
   kit script. A check that you think is wrong is a kit defect. Report it to
   the operator.
