---
name: ncp-tf
description: Entry point for building a Terraform integration for an API-first NCP. Use for how to use this kit, where its user guide is, the order the steps run in, and which step owns a goal.
version: 0.1.0
metadata:
  author: NVIDIA Corporation
  tags: [ncp, terraform, router, entry-point]
---

# NCP Terraform

## Purpose

Build a Terraform integration for an API-first NCP: one provider, one module for
each buildable layer, and a blueprint that composes them as the live test case.

This skill routes. It performs no analysis and generates nothing.

## Prerequisites

- `uv`, for the scripts the routed skills run.
- An NCP API or SDK specification, and the immutable revision identifying it.

## Instructions

**Open `references/USER_GUIDE.md` before answering what to do next, what to
review, or what a warning means.** It sits beside this file and holds what this
one only summarises. Answer from it, not from what a step like it usually
requires elsewhere.

### 1. Route by goal

| The operator wants | Use |
|---|---|
| To know whether an NCP's API can support Terraform at all, which layers are buildable, or what resources a layer would hold | `ncp-tf-coverage` |
| To lay out a provider, a layer module or a blueprint, and write the tests it must pass | `ncp-tf-scaffold` |
| To generate the NCP's Terraform provider from an approved module plan | `ncp-tf-provider` |
| To refresh that answer after a specification changed | `ncp-tf-coverage` |
| To see how far the build has got across the requirement sections, and what is left | `ncp-tf-scaffold` |
| To record what a live run proved, or to see what is supported and not yet verified | `ncp-tf-verify-report` |
| To generate a layer module from the provider's exported schema | `ncp-tf-module` |
| To find what a test run left in the account, or to remove it | `ncp-tf-sweeper` |
| To record the replay cassettes of a resource, or to make replay work | `ncp-tf-cassette` |
| To generate the blueprint | Not available yet. Say so plainly. |

When the goal matches no row, state what this kit does not do rather than
improvising a step. This kit is deliberately narrow.

### 2. Respect the order

    coverage.yaml                        what the API supports, and what to build
        ↓            human review
    NCP test cases                       written before the code they check
        ↓            human review
    provider, modules, blueprint         the implementation

Tests come before the implementation. A test written by reading an
implementation proves only that the implementation does what it does. A test
written first cannot be shaped to the code.

The coverage, test, provider, module, sweeper and cassette steps and the
verification report exist today. The blueprint step is designed and not built.

## How this kit works

**Nothing is hardcoded.** No skill here names an NCP, a specification path, or
an output location. Each is established by inspecting the repository and
confirming with the operator. A repository may hold several NCPs.

**One command, one output, one review.** Each skill produces one output, a file
or a few files that belong together, and stops. A person reviews it before the next step runs. There is no orchestrator,
no run identity and no resume — the artifacts on disk are the whole handoff
between steps.

**Progress is computed, never stored.** Asking how far the build has got reads
the case directories and the provider tree as they are now and prints the
answer. Nothing is written and nothing is remembered, so it cannot disagree with
what is on disk. A stored status is a second thing to keep true, and the first
time it differs from the directory it is the directory that is right.

**Done is the offline entry point exiting 0.** No change to generated code is
committed or reported as done until `run-tests.sh offline` passes in full in
each directory the change reaches; `ncp-tf-scaffold` states the rule.

**The artifact carries its own context.** `coverage.yaml` records the NCP and
its sources once, so a later invocation reads them back instead of asking again,
and the analysis and the resource projection cannot disagree about which NCP or
which spec revision they describe.

**The output belongs to this repository.** Generated Terraform, and the reports
that explain it, are committed here beside the modules. The kit publishes
nothing and creates no repository.

## The four layers

`iam`, `network`, `cluster`, `compute`. This set and its names are fixed by the
design standard, and no skill may add to it. `other` holds the types that no
layer describes: the provider and the tests build them, and no module holds
them.

## Examples

**"Can we build Terraform for this NCP?"** → `ncp-tf-coverage`. The layer
verdicts it produces are the answer.

**"The NCP published a new API version."** → `ncp-tf-coverage`. Its evidence
gate is what catches claims that have gone stale.

**"Generate the compute module."** → `ncp-tf-module`. It needs the provider's
exported schema, so the provider step comes first.

**"Write the tests for this NCP."** → `ncp-tf-scaffold`. It runs before the
implementation, not after it.

**"Scaffold the network module."** → `ncp-tf-scaffold`, module tier. Ask where it
goes; the same step writes that layer's tests into it.

**"How do I use this kit?"** → `references/USER_GUIDE.md`, beside this file. It
gives the order, the go/no-go after each step, and what to review. It ships
inside this skill so that copying the skills is the whole install.

**This skill is how that path is findable at all.** The kit writes no `AGENTS.md`
— that belongs to the operator, and a kit that edits it collides with whatever
else it carries. A skill description is therefore the
only thing the kit owns that an agent sees without being told where to look.

## Limitations

- The coverage, test, provider and module steps, the sweeper, the cassette
  recording and the verification report exist. Blueprint generation is designed
  and not built.
- No step contacts a live NCP account on its own. The scripts and the offline
  entry point run Terraform on this machine, and they need no account. The
  operator runs the live tier, the sweeper deletes only what the operator
  approved, and a recording changes the account only with the operator's word.
- This skill routes only; it must not perform the work of the skill it routes to.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `uv: command not found` | Install `uv`. Do not fall back to a bare `python3` and `pip install`; the scripts declare dependencies inline. |
| A gate warns or fails | Read the message and fix the artifact. Never edit a flag, a threshold or the gate itself to quiet one — a gate is the only thing checking the work. |
| A check refuses a file that an earlier version of the kit wrote | The format moved, not the work. Show the operator the change on one line: the file, the field, its current value and the value this kit needs. Ask to update it, or to re-run the step that writes it, and continue after the answer. Do not stop at the refusal, work around it, or edit the installed kit. |
