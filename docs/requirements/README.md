# Requirements <-> Test Reconciliation - Policy & Guide

Our test suite supports many overlapping goals and interests. This document
outlines how we make a single test plan, with unique IDs and labels, which
reconciles all of these different goals.

## Files

| File | Role |
| ---- | ---- |
| `../test-plan.yaml` | **Primary source of truth** for tests (lives one level up). |
| `offtake-requirements.yaml` | **Source of record** for offtake requirements (curated in-repo copy of the public "NVIDIA Requirements Guide for AI Clouds"). |
| `offtake-requirements.md` | Generated, publishable rendering of the offtake YAML (`make plan`). |
| `software-reference-requirements.yaml` | **Source of record** for the NSRG-derived reference requirements. |
| `software-reference-requirements.md` | Generated rendering of the reference YAML (`make plan`); one contributing requirements doc among several. |
| `test-requirements-matrix.yaml` | The **traceability matrix (index)**: which requirement(s) each test relates to, across documents (`source`). |
| `test-requirements-matrix.adoc` | Generated and committed traceability matrix, viewable in github (or renderable to html) |
| `../../scripts/reqtrace.py` | Integrity checks (`reqtrace validate`; `make reqcheck`). |
| `../../scripts/requirements_matrix_to_adoc.py` | Renders the matrix to AsciiDoc (`make plan`). |
| `../../scripts/requirements_source_to_md.py` | Renders a source-requirements YAML to publishable Markdown (`make plan`). |

## 1. Organization

- `test-plan.yaml` is the point of reconciliation and provides primary keys.
  The act of adding tests to this file implies that IDs and overlap with other
  tests have been reviewed and reconciled.
- The matrix is an index, a lookup from each test to the various upstream
  requirement documents. It is secondary/derived, not the authority, and should
  be updated as needed to match the `test-plan.yaml` file.
- There should be requirements from some source for each test, and when possible
  we should have the test id reflect the requirement ID. But this will not always
  be possible.

> Why this matters: upstream requirement docs are *inputs*. We deliberately
> "break rules" (renumber, re-prefix) to get from a state where the rules
> *cannot* all hold to one where they can - and we record the result as official
> here.

## 2. ID & naming rules

**Format.** A test id is `<req_id>-<seq>` (e.g. `BOOT01-01`, `OBS01-01`). A
requirement id is `<PREFIX><NN>` (offtake style, no internal separator). Because
a requirement can have several tests, `seq` disambiguates them; today most
reference reqs are 1:1, so `seq` is `01`.

**Uniqueness (primary keys).** `test_id`s and `req_id`s must each be **globally
unique across all documents**. A `test_id` must never equal a `req_id`. These
are meant to fill the role of primary keys, unique and unchanging, as much as
possible. We can add references to them from the code, and cross-reference them
from the upstream requirements. Ideally conflicts and collisions will be
detected and resolved before they get here, and if they get this far they
should become apparent at this point. If absolutely needed, changes may
be required, but we should always record the old id in the `legacy_ids` list.

**Prefix governance (anti-overload).** Every prefix maps to exactly one owning
concern. The `CP` overload (DDI/IP vs. control-plane) is the cautionary tale -
it was split into `IPAM` (DDI/IP) and `CP` (control plane). Before minting a new
prefix, check the registry below and add to it.

**Collision policy (shared prefixes).** When a new reference requirement shares a
prefix with offtake, **continue offtake's number space** rather than inventing a
distinguished prefix - edge overlap is a *feature* (two groups independently
validating the same idea). Concretely we did `SDN`+10, `K8S`+28, `BFX`+3 (offtake
maxes `SDN10`/`K8S28`/`BFX03`). Selectivity is allowed per case. Accepted cost:
this couples us to offtake's current max, so a future offtake addition could
collide - `reqtrace validate` is the backstop, and the map remains the lineage
record.

### Prefix registry

| Prefix | Owner | Concern |
| ------ | ----- | ------- |
| `CNP` `BOOT` `SDN` `K8S` `SEC` `BFX` `DIR` `HSS` `DMS` `STG` `NET` `CAP` `BM` | offtake | min-req domains (see offtake doc) |
| `IMG` | reference | image registry / golden images |
| `AUTH` | reference | key & secret mgmt |
| `IAM` | reference | identity & access mgmt |
| `IPAM` | reference | DNS/DHCP/IP address mgmt (was `CP-01/02`) |
| `CP` | reference | control plane & tenant lifecycle |
| `NETMGMT` | reference | network underlay / switch mgmt |
| `RESDB` | reference | resource database / inventory |
| `HWING` | reference | hardware ingestion |
| `ATTEST` | reference | boot / attestation |
| `BMAAS` / `VMAAS` | reference | bare-metal / VM compute services |
| `SDN` (11+) | reference | NVLink partitions + IMEX (continues offtake `SDN`) |
| `META` | reference | metadata service |
| `CTRL` | reference | cloud control plane API |
| `LB` | reference | load balancing |
| `STOR` `DATASVC` | reference | SDS / object / block / cache / vector / SQL / backup / model registry |
| `SLURM` | reference | Slurm control plane |
| `K8S` (29+) | reference | managed Kubernetes (continues offtake `K8S`) |
| `POWER` | reference | power policy mgmt |
| `APIGW` `AICP` `WEBUI` | reference | AI platform & user access |
| `OBS` `TELEM` | reference | observability collectors / data lakes |
| `BFX` (04+) | reference | break-fix health (continues offtake `BFX`) |
| `BENCH` | reference | exemplar benchmarking |

## 3. `legacy_ids`

We attempt to keep the test ID as permanent as possible. However, there have
already been complications that were best resolved by changing IDs. When an
ID must change, we keep the previous ID in a list in the `test-plan.yaml` file
in a field called `legacy_ids`. This list should only be appended to, never
removed from.

## 4. Labels & selective runs

The end goal is to run **subsets** of tests - an arbitrary tag, or *every test
belonging to a given requirements document*. We keep the data flexible for this:

- A test may carry many `labels` in `test-plan.yaml` (e.g. `min_req`); this list
  is open-ended and multi-valued.
- **Requirement-document membership is derivable from the index**: a test
  "belongs to" document *X* if any of its `requirements[].source == X` in
  `test-requirements-matrix.yaml`. So "run all offtake tests" = select tests
  with an `offtake`-source requirement; "run all NSRG/reference tests" = `reference`.
- Therefore membership is **derived, not duplicated** by default. Materialized
  convenience labels (e.g. a generated `doc:offtake` tag) may be produced later
  from the index, but the index stays the source of truth.

## 5. Onboarding a new requirements document (runbook)

When a new team's requirements document is blessed, reconcile it here:

1. **Register prefix(es)** for the new doc in the registry (sec. 2). Resolve
   any overload before proceeding (see the `CP` lesson).
2. **Assign IDs.** Prefer mirroring the upstream requirement IDs. On collision
   with an existing prefix, apply the collision policy (sec. 2): continue the
   number space, or (selectively) choose another resolution and record why.
3. **Add/adjust tests** in `test-plan.yaml` (the canonical truth). Use
   `legacy_ids` for any renames.
4. **Update the matrix** (`test-requirements-matrix.yaml`): add each
   test->requirement edge with the new `source`, plus `annotations`/`notes`.
5. **Validate**: `make reqcheck` must pass; **regenerate**: `make plan`.

> Kept as a subsection for now; promote to its own `ONBOARDING.md` if it grows.

## 6. Validation & enforcement

`reqtrace validate` (run via `make reqcheck`, and in CI through
`scripts/tests/test_reqtrace.py`) is the machine-checked guard.
