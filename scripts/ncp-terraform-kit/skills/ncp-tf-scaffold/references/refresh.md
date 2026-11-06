# Refresh a reviewed case

A case holds two kinds of fields. `instantiate` derives most of them from the
contract, the module plan and the documents. A person writes the others, and
the case lists them under `judged`. A kit update changes the derived fields,
so the case must be written again. The answers must stay.

## When

- After each kit update, before you render. `check` and `render` derive each
  case again from the documents that its `derived_from` names, and refuse a
  case that the current kit derives differently. `render` then writes no test.
- When `check` or `render` says `stale`. The message names the differing
  paths.
- When the plan gains a resource. The same run adds its case beside the
  others.

## Before the refresh

A refresh carries only the values whose paths the case lists under `judged`.
It drops every other field that `instantiate` does not write. When `check`
says that a refresh drops a field, such as `dependencies` or `peer`, and a
person wrote it, add the field to the case's `judged` list first.

## Run it

Run `instantiate` with the case's `contract`, the coverage that its
`derived_from` names, the case directory as `--out`, and a scope that selects
the stale cases. A run over a type also refreshes that type's named cases.
When `derived_from` names a `provider_schema`, or the provider's schema review
is done, give that reviewed file as `--provider-schema`. Without it, the
update owes again each field that the review makes replace.

The run copies the case directory to a new backup directory outside it, and
prints the path. Then it writes each selected case again and carries each
judged value. It prints the report on stdout.

## The report

Show the operator the whole report. Then go through each entry that needs a
decision with them, and wait for the answer before you change a case.

| List | What it holds | What you do |
|---|---|---|
| `carried` | each judged value that the new case keeps | Report it. Nothing is to decide. |
| `refused` | a carried value that a current check refuses, with the check's message | Tell the operator the value, the rule, the reason for the rule and what the rule accepts. Wait for the operator's decision. Do not change the value yourself: the case stays refused until a person changes it. One exception: a message that names a dependency resource that no `dependencies` entry gives. Declare it as [dependencies.md](dependencies.md#the-dependencies-that-the-documents-state) says, and name its new `TFKIT_VAR_` value in your final reply. |
| `lost` | a judged value that has no place in the new case: the contract no longer lists its field, or the derivation no longer has its operation | Show the value and the reason. Ask where the answer goes now, or whether the operator answers the question again. |
| `differs` | a field where the backup and the new derivation disagree | A judged field keeps the backup value; another field takes the derived value. Either side can be a kit correction or a hand fix, and only the operator knows which. Ask the operator to keep or to take each one. |
| `kept` | a case whose inputs no longer build, with the reason | The case is unchanged, and `check` and `render` refuse it. Report the reason. |

To keep a value that the refresh replaced, copy it from the backup into the
case, and add its path to `judged`. Copy the value, not the file: an old file
is stale again.

## After the refresh

Run `check`, then `render`. A case that the operator has not settled yet stays
refused, and that is correct: report it, and do not render around it.
