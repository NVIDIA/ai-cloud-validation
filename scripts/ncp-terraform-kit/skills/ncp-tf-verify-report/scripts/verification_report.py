#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""What a run proved, as against what the API can support.

`coverage.yaml` is true before anything runs. This is true of one account, one
provider revision and one day, so every record here is taken from a run's own
machine-readable output rather than from a reading of a log.

Three subcommands, and the split between them is the point:

    record   a run's output becomes a status. No judgement.
    check    refuses a record that says less than it appears to.
    report   renders it, and rolls it up to modules and requirements.

Why a resource is blocked is the one thing no run reports, so it is the one
thing written by hand -- once, in a blocker a resource cites by id.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import case_results
import intermittent
import module_table
import not_selected
import observed
import rendered_report
import tier

API_VERSION = "terraform.ncp/v1"
KIND = "verification"
#: The plan's group for the types that no layer describes. Its types are
#: verified like any other, and no module holds them, so it owes no module status.
NO_MODULE = "other"
STAMP_FIELDS = ("provider_revision", "account")

#: Strongest first. A roll-up takes the weakest of what it holds, because a
#: module cannot be partly verified -- it either has an unproven resource or it
#: does not.
STATUSES = ("live", "offline", "unverified", not_selected.STATUS)

#: Why a resource is not live. The set is closed: a category invented for one
#: run is a category nobody can act on, and merging two of them loses the
#: action. `our-defect` is deliberately separate from `permission-not-held` --
#: it is the only one we can clear ourselves, and filing it as a permission
#: limit stops the asking.
CATEGORIES = {
    "our-defect": "the generated code is wrong; nothing about the account is in question",
    "permission-not-held": "the operation needs a scope this credential does not have",
    "policy-refuses": "the permission is held and a policy forbids the act",
    "scope-exceeds-account": "it would act outside the account set aside for testing",
    "awaiting-approval": "a person has been asked and has not answered",
    "feature-not-enabled": "a service or setting is off and could be turned on",
    "service-unavailable": "the service failed the call with an error that names no cause the account can change",
    "quota-exhausted": "an account quota or limit is used up",
}

#: `go test -json` names a lifecycle test for the Go form of the resource type.
LIFECYCLE_TEST = re.compile(r"^TestAcc(?P<name>\w+)_lifecycle$")


class InputError(Exception):
    """Something the caller gave is wrong, and saying which is the whole job."""


@dataclass
class Resource:
    """One planned resource, and where the plan puts it."""

    terraform_type: str
    layer: str
    requirements: list[str] = field(default_factory=list)
    #: For a dependency resource, the planned resources whose create needs it.
    dependency_of: list[str] = field(default_factory=list)

    @property
    def is_dependency(self) -> bool:
        """Planned because another resource needs it; no requirement names it."""
        return not self.requirements and bool(self.dependency_of)


def load_document(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InputError(f"no such file: {path}") from error
    except yaml.YAMLError as error:
        raise InputError(f"{path} is not readable as YAML: {error}") from error
    if not isinstance(document, dict):
        raise InputError(f"{path} does not hold a mapping")
    return document


def requirement_ids(requirements: Any) -> list[str]:
    """The requirement each entry names, however the plan spells it.

    A plan writes a requirement as a bare id, or as a mapping carrying a
    `part_id` when the requirement was cut into parts. The roll-up answers "is
    this requirement verified", so it groups by the requirement and not by the
    part -- a requirement with one unproven part is not verified, and splitting
    the row would report it as partly so.
    """
    found: list[str] = []
    for entry in requirements or []:
        if isinstance(entry, dict):
            name = str(entry.get("req_id") or "").strip()
        else:
            name = str(entry).strip()
        if name and name not in found:
            found.append(name)
    return found


def planned_resources(coverage: dict[str, Any]) -> dict[str, Resource]:
    """Every resource the module plan names, by Terraform type.

    The plan is the list of what should be verified. A record built from a run's
    output alone would hold exactly the resources somebody remembered to run,
    which is how a resource leaves a report without anyone deciding to drop it.
    """
    layers = ((coverage.get("module_plan") or {}).get("layers")) or {}
    if not isinstance(layers, dict) or not layers:
        raise InputError("the coverage holds no module_plan.layers to verify against")
    planned: dict[str, Resource] = {}
    for layer, body in layers.items():
        for entry in (body or {}).get("resources") or []:
            kind = str(entry.get("terraform_type") or "")
            if not kind:
                continue
            planned[kind] = Resource(
                kind,
                str(layer),
                requirement_ids(entry.get("requirements") or []),
                [str(name) for name in entry.get("dependency_of") or []],
            )
    if not planned:
        raise InputError("the module plan names no resources")
    return planned


def lifecycle_results(
    lines: str, planned: dict[str, Resource], named: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """What `go test -json` says about each lifecycle test.

    Only the top-level lifecycle test counts. A subtest passing says a step
    passed, and a resource is verified by the whole lifecycle or not at all.
    `named` maps the Go name of each named case's test to its entry.
    """
    by_go_name = {case_results.go_name(kind): kind for kind in planned} | (named or {})
    results: dict[str, str] = {}
    unknown: list[str] = []
    for line in lines.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        action, test = event.get("Action"), event.get("Test")
        if action not in {"pass", "fail", "skip"} or not isinstance(test, str):
            continue
        matched = LIFECYCLE_TEST.match(test)
        if not matched:
            continue
        kind = by_go_name.get(matched.group("name"))
        if kind is None:
            unknown.append(test)
            continue
        # A run repeated in one output: a later failure outranks an earlier pass.
        if results.get(kind) != "fail":
            results[kind] = action
    return results, sorted(set(unknown))


#: The scaffold names a `terraform test` file for its subject and its mode --
#: `iam_live.tftest.hcl`, `gcp_group_offline.tftest.hcl` -- so a run over many
#: of them reports results that say what each one is about.
TFTEST_FILE = re.compile(r"^(?P<subject>.+)_(?P<mode>offline|live|replay)\.tftest\.hcl$")

#: The modes in which the API never answered. A pass in one of them is `offline`.
UNANSWERED_MODES = frozenset({"offline", "replay"})


def terraform_results(
    lines: str, subjects: set[str], modes: frozenset[str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """What `terraform test -json` says about each subject it names.

    Used for both tiers. A module is not the resources inside it: it adds
    variables, outputs and wiring, so it can fail while every resource passes --
    an output bound to the wrong attribute reaches no consumer, and no resource
    test would see it.

    `modes` keeps only the files whose name says one of those modes. A module
    test that plans with no account passes as easily as one that applied, so
    what the pass proves is in the file name and nowhere else.
    """
    results: dict[str, str] = {}
    unknown: list[str] = []
    for line in lines.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "test_file":
            continue
        body = event.get("test_file") or {}
        status = str(body.get("status") or "")
        matched = TFTEST_FILE.match(Path(str(body.get("path") or "")).name)
        if not matched or status not in {"pass", "fail", "error", "skip"}:
            continue
        if modes is not None and matched.group("mode") not in modes:
            continue
        subject = matched.group("subject")
        if subject not in subjects:
            unknown.append(subject)
            continue
        # A later failure outranks an earlier pass, as for a live result.
        if results.get(subject) not in {"fail", "error"}:
            results[subject] = status
    return results, sorted(set(unknown))


def module_entry(
    layer: str,
    kept: dict[str, Any] | None,
    live: str | None,
    unanswered: str | None,
    keep_cited: set[str] | None = None,
) -> dict[str, Any]:
    """A module's status, from the mode its test ran in as much as from the result.

    The same rule a resource follows. Only a test that applied against the
    account makes a module `live`; a plan with no account that passes makes it
    `offline`. A module test that plans reads "passed" exactly as one that
    applied does, so the result alone would record `live` for a module the
    account never saw.
    """
    entry = dict(kept or {})
    if live == "pass":
        entry["status"] = "live"
        entry["evidence"] = f"{layer}'s own live module test passed"
        intermittent.after_a_pass(entry, keep_cited or set())
    elif live in {"fail", "error", "skip"}:
        entry["status"] = "offline"
        entry["evidence"] = f"{layer}'s own live module test {case_results.PAST[live]}"
        entry.setdefault("blocked_by", [])
    elif unanswered == "pass" and tier.keeps(entry):
        pass  # the same rule as a resource: a plan shows less than an apply did
    elif unanswered == "pass":
        entry["status"] = "offline"
        entry["evidence"] = f"{layer}'s own module test passed without the account; no live module test has reported it"
        entry.setdefault("blocked_by", [])
    elif unanswered in {"fail", "error", "skip"}:
        entry["status"] = "unverified"
        entry["evidence"] = f"{layer}'s own module test {case_results.PAST[unanswered]}, so nothing has been shown"
    else:
        entry.setdefault("status", "unverified")
        entry.setdefault("evidence", "no module test has been run for this layer")
    return entry


def command_record(args: argparse.Namespace) -> int:
    """A run's output becomes a status, and an earlier record's reasons survive.

    Re-recording must not discard the blockers somebody wrote. A run reports
    what happened; it has nothing to say about why, so it may not overwrite why.
    """
    planned = planned_resources(load_document(Path(args.coverage)))
    out = Path(args.out)
    previous = load_document(out) if out.exists() else {}
    named_runs = tier.named_runs(previous)
    if named_runs:
        raise InputError(
            f"{out}: {', '.join(named_runs)}. Delete those keys and record again: a run's "
            f"output is not committed, and git history says when the record changed"
        )
    untiered = tier.untiered(previous)
    if untiered:
        raise InputError(
            f"{out}: {', '.join(untiered)} names no {tier.FIELD}, so an offline pass "
            f"cannot tell what it may replace. Add `{tier.FIELD}: live` where a lifecycle "
            f"or a live module test set the status, `{tier.FIELD}: offline` where an "
            f"offline or replay result set it, and record again"
        )
    kept = previous.get("resources") if isinstance(previous.get("resources"), dict) else {}
    try:
        subjects, unknown = case_results.subjects(Path(args.cases), set(planned))
        skipped = not_selected.skipped(Path(args.cases))
    except ValueError as error:
        raise InputError(str(error)) from error

    results: dict[str, str] = {}
    printed: dict[str, str] = {}
    for source in args.live or []:
        text = Path(source).read_text(encoding="utf-8")
        found, strays = lifecycle_results(text, planned, case_results.named_tests(subjects))
        results.update(found)
        printed.update(observed.printed(text))
        unknown.extend(strays)

    offline: dict[str, str] = {}
    by_subject = {subject.offline: entry for entry, subject in subjects.items()}
    for source in args.offline or []:
        found, strays = terraform_results(Path(source).read_text(encoding="utf-8"), set(by_subject))
        offline.update({by_subject[name]: status for name, status in found.items()})
        unknown.extend(f"offline {name}" for name in strays)

    # A record keeps each subject's latest status, and a run overwrites only
    # what it reports, so one record can hold results from several runs. Each
    # entry names the tier that set it; the stamp is the latest recording.
    stamp = {name: getattr(args, name) for name in STAMP_FIELDS}

    keep_cited = intermittent.ids(previous.get("blockers") or [])
    resources: dict[str, Any] = {}
    for kind, subject in sorted(subjects.items()):
        entry = dict(kept.get(kind) or {})
        entry["layer"] = planned[subject.terraform_type].layer
        resources[kind] = case_results.resource_entry(
            entry,
            subject,
            results.get(kind),
            offline.get(kind),
            keep_cited,
            skipped.get(kind),
            printed.get(subject.test),
        )

    layers = module_layers(planned)
    module_kept = previous.get("modules") if isinstance(previous.get("modules"), dict) else {}
    module_live: dict[str, str] = {}
    module_unanswered: dict[str, str] = {}
    for source in args.module or []:
        text = Path(source).read_text(encoding="utf-8")
        for modes, outcomes in ((frozenset({"live"}), module_live), (UNANSWERED_MODES, module_unanswered)):
            found, strays = terraform_results(text, layers, modes)
            outcomes.update(found)
            unknown.extend(f"module {name}" for name in strays)

    modules: dict[str, Any] = {}
    for layer in sorted(layers):
        modules[layer] = module_entry(
            layer,
            module_kept.get(layer),
            module_live.get(layer),
            module_unanswered.get(layer),
            keep_cited,
        )
        kept_live = (
            not module_live.get(layer)
            and module_unanswered.get(layer) == "pass"
            and tier.keeps(module_kept.get(layer) or {})
        )
        if (module_live.get(layer) or module_unanswered.get(layer)) and not kept_live:
            modules[layer][tier.FIELD] = "live" if module_live.get(layer) else "offline"

    document = {
        "api_version": API_VERSION,
        "kind": KIND,
        "stamp": stamp,
        "blockers": previous.get("blockers") or [],
        "modules": modules,
        "resources": resources,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(document, sort_keys=False, width=100), encoding="utf-8")
    for stray in sorted(set(unknown)):
        print(f"warning: {stray} names no resource in the module plan", file=sys.stderr)
    for kind in not_selected.unstated(document):
        print(f"warning: {kind} is not_selected and states no reason; {not_selected.ASK}", file=sys.stderr)
    for label, kinds in split_by_role(planned).items():
        if not kinds and label != "planned":
            continue
        tally = {s: sum(1 for k in kinds if resources[k]["status"] == s) for s in STATUSES}
        print(
            f"{out}: {tally['live']} live, {tally['offline']} offline, {tally['unverified']} "
            f"unverified, {tally[not_selected.STATUS]} not selected, of {len(kinds)} {label}",
            file=sys.stderr,
        )
    return 0


def split_by_role(planned: dict[str, Resource]) -> dict[str, list[str]]:
    """Requirement resources, and dependency resources counted apart.

    A dependency resource serves no requirement. Counted with the rest, it would
    make the figure a requirement is judged by look further along than it is.
    """
    return {
        "planned": sorted(k for k, r in planned.items() if not r.is_dependency),
        "dependency resources": sorted(k for k, r in planned.items() if r.is_dependency),
    }


def gate_record_schema(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    broken = []
    if record.get("api_version") != API_VERSION:
        broken.append(f"api_version must be {API_VERSION}")
    if record.get("kind") != KIND:
        broken.append(f"kind must be {KIND}")
    if not isinstance(record.get("resources"), dict):
        broken.append("resources must be a mapping of terraform type to status")
    return broken


def gate_stamp(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    """A result is true of one provider revision and one account.

    Without the stamp a pass outlives the code that earned it, and a reader has
    no way to tell a current result from one the provider has since changed.
    """
    stamp = record.get("stamp")
    if not isinstance(stamp, dict):
        return ["stamp is missing: a result with no provider revision cannot be aged"]
    return [f"stamp.{name} is empty" for name in STAMP_FIELDS if not str(stamp.get(name) or "").strip()]


def gate_plan_completeness(record: dict[str, Any], planned: dict[str, Resource]) -> list[str]:
    """Every planned resource appears, said of or not.

    A record built only from what ran holds exactly the resources somebody
    remembered, and a resource that is never mentioned leaves the report without
    anyone deciding to drop it.
    """
    recorded = record.get("resources") or {}
    missing = sorted(set(planned) - set(recorded))
    unplanned = sorted(k for k in recorded if case_results.type_of(k) not in planned)
    broken = [f"{kind} is planned and unrecorded" for kind in missing]
    broken += [f"{kind} is recorded and the module plan does not name it" for kind in unplanned]
    return broken


def module_layers(planned: dict[str, Resource]) -> set[str]:
    """The planned layers that a module exists for."""
    return {resource.layer for resource in planned.values()} - {NO_MODULE}


def gate_module_completeness(record: dict[str, Any], planned: dict[str, Resource]) -> list[str]:
    """Every layer the plan names has a module entry, said of or not.

    A module that is absent from the record is a module nobody is waiting for,
    which is how a layer stops being work and starts being forgotten.
    """
    recorded = record.get("modules")
    if not isinstance(recorded, dict):
        return ["modules is missing: every layer owes a status, even 'not built'"]
    layers = module_layers(planned)
    broken = [f"{layer} is a planned layer with no module status" for layer in sorted(layers - set(recorded))]
    broken += [
        f"modules names {name}, which the module plan has no layer for" for name in sorted(set(recorded) - layers)
    ]
    broken += [
        f"modules.{name}: status {entry.get('status')!r} is not one of {', '.join(STATUSES)}"
        for name, entry in sorted(recorded.items())
        if isinstance(entry, dict) and entry.get("status") not in STATUSES
    ]
    return broken


def gate_status_known(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    return [
        f"{kind}: status {entry.get('status')!r} is not one of {', '.join(STATUSES)}"
        for kind, entry in (record.get("resources") or {}).items()
        if entry.get("status") not in STATUSES
    ]


def gate_status_has_a_tier(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    """A status a run set names the tier that set it.

    A failed live lifecycle and an offline pass both read `offline`, and only
    the tier tells which one a later offline pass may replace.
    """
    return [
        f"{name} names no {tier.FIELD}; record the run again so the status says which tier set it"
        for name in tier.untiered(record)
    ]


def gate_names_no_run(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    """A run's output is not committed, so a run id names nothing a reader can open."""
    return [
        f"{place}; delete it, because git history says when the record changed" for place in tier.named_runs(record)
    ]


def gate_blocker_cited(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    """A resource that was attempted and did not pass names why.

    Not naming a reason has to cost something. `unverified` is exempt: nothing
    was attempted, so there is no refusal to explain, and demanding one there
    would buy a sentence somebody invented.
    """
    known = {str(b.get("id")) for b in record.get("blockers") or [] if isinstance(b, dict)}
    broken = []
    subjects = [("", record.get("resources") or {}), ("module ", record.get("modules") or {})]
    for label, entries in subjects:
        for name, entry in sorted(entries.items()):
            if not isinstance(entry, dict) or entry.get("status") != "offline":
                continue
            cited = [str(i) for i in entry.get("blocked_by") or []]
            if not cited:
                broken.append(f"{label}{name} is offline and cites no blocker; say why it cannot run live")
                continue
            broken += [f"{label}{name} cites blocker {i}, which is not declared" for i in cited if i not in known]
    return broken


#: What a blocker may say, all of it required. A blocker exists while its cause
#: holds: when the cause is gone, the blocker is deleted, because the record
#: keeps what is true now and nothing else.
BLOCKER_KEYS = frozenset({"id", "category", "statement", "clears_when", intermittent.FIELD})


def gate_blocker_complete(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    """A blocker states its category, what is blocked, and what would clear it.

    Without the last one it is a complaint. With it the report is a work list.

    A key nothing reads is refused, because the report renders what it knows and
    drops the rest in silence. A field somebody wrote and nobody sees is worse
    than one nobody wrote: it reads as recorded.
    """
    broken = []
    for index, blocker in enumerate(record.get("blockers") or []):
        if not isinstance(blocker, dict):
            broken.append(f"blocker {index} is not a mapping")
            continue
        name = blocker.get("id") or f"blocker {index}"
        if not str(blocker.get("id") or "").strip():
            broken.append(f"blocker {index} has no id for a resource to cite")
        if blocker.get("category") not in CATEGORIES:
            broken.append(f"{name}: category {blocker.get('category')!r} is not one of {', '.join(sorted(CATEGORIES))}")
        for required in ("statement", "clears_when"):
            if not str(blocker.get(required) or "").strip():
                broken.append(f"{name}: {required} is empty")
        if intermittent.problem(blocker):
            broken.append(f"{name}: {intermittent.problem(blocker)}")
        for unknown in sorted(set(blocker) - BLOCKER_KEYS):
            broken.append(
                f"{name}: {unknown} is not a field a blocker has, so nothing "
                f"renders it. One of {', '.join(sorted(BLOCKER_KEYS))}."
                + (
                    " A fixed cause is not marked: delete the blocker, and cite what "
                    "still holds the resource, if anything does."
                    if unknown == "fixed"
                    else ""
                )
            )
    return broken


def gate_blocker_is_cited(record: dict[str, Any], _: dict[str, Resource]) -> list[str]:
    """A blocker nothing cites records a limit that no longer applies."""
    cited = {
        str(i)
        for entries in ((record.get("resources") or {}), (record.get("modules") or {}))
        for entry in entries.values()
        if isinstance(entry, dict)
        for i in entry.get("blocked_by") or []
    }
    return [
        f"blocker {b.get('id')} is cited by no resource; remove it or cite it"
        for b in record.get("blockers") or []
        if isinstance(b, dict) and str(b.get("id")) not in cited
    ]


GATES = (
    ("record-schema", gate_record_schema),
    ("stamp", gate_stamp),
    ("plan-completeness", gate_plan_completeness),
    ("module-completeness", gate_module_completeness),
    ("status-known", gate_status_known),
    ("status-has-a-tier", gate_status_has_a_tier),
    ("not-selected-states-its-reason", not_selected.gate),
    ("names-no-run", gate_names_no_run),
    ("blocker-cited", gate_blocker_cited),
    ("blocker-complete", gate_blocker_complete),
    ("blocker-is-cited", gate_blocker_is_cited),
)


def command_check(args: argparse.Namespace) -> int:
    record = load_document(Path(args.verification))
    coverage = load_document(Path(args.coverage))
    planned = planned_resources(coverage)
    gates = list(GATES)
    if args.report:
        report = _read_report(Path(args.report))
        rendered = render(*narrowed(record, planned, args.layer), coverage)
        gates.append((rendered_report.GATE, lambda *_: rendered_report.differences(rendered, report, args.report)))
    for name, gate in gates:
        broken = gate(record, planned)
        if broken:
            print(f"{name}: {len(broken)} finding(s)", file=sys.stderr)
            for line in broken:
                print(f"  - {line}", file=sys.stderr)
            return 1
        print(f"{name}: ok", file=sys.stderr)
    return 0


def _read_report(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise InputError(f"cannot read the report {path}: {error.strerror}") from error


def weakest(statuses: list[str]) -> str:
    """The weakest status in a group, because a module is not partly verified."""
    if not statuses:
        return "unverified"
    return max(statuses, key=lambda s: STATUSES.index(s) if s in STATUSES else len(STATUSES))


def rolled_up(record: dict[str, Any], key: str, planned: dict[str, Resource]) -> dict[str, Any]:
    """Status per module or per requirement, and which resource holds it back.

    Naming the holder is the difference between a status a reader can act on and
    one they have to go and look up. A named case counts for each requirement
    of its type, and not for its layer: a layer module uses one configuration
    of each resource.
    """
    groups: dict[str, list[str]] = {}
    for kind, entry in (record.get("resources") or {}).items():
        resource = planned.get(case_results.type_of(kind))
        if resource is None or (key == "layer" and case_results.is_named(kind)):
            continue
        names = [resource.layer] if key == "layer" else resource.requirements
        for name in names:
            groups.setdefault(name, []).append(kind)
    rolled = {}
    for name, kinds in sorted(groups.items()):
        statuses = {k: str((record["resources"][k]).get("status")) for k in kinds}
        verdict = weakest(list(statuses.values()))
        holding = sorted(k for k, s in statuses.items() if s == verdict) if verdict != "live" else []
        rolled[name] = {
            "status": verdict,
            "resources": len(kinds),
            "live": sum(1 for s in statuses.values() if s == "live"),
            "held_by": holding,
        }
    return rolled


def supported(coverage: dict[str, Any]) -> dict[str, str]:
    """What the analysis said each requirement's API support is.

    Read here rather than written into the coverage report, because coverage is
    generated before anything runs and must not depend on an artifact produced
    after it. A requirement cut into parts takes the weakest verdict among them,
    for the same reason a module does: one unsupported part is not a supported
    requirement.
    """
    order = ("present", "partial", "absent", "not_applicable")
    verdicts: dict[str, str] = {}
    for mapping in coverage.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        req_id = str(mapping.get("req_id") or "")
        if not req_id:
            continue
        found = [str(mapping.get("status") or "")]
        found += [str(part.get("status") or "") for part in mapping.get("parts") or [] if isinstance(part, dict)]
        known = [v for v in found if v in order]
        verdicts[req_id] = max(known, key=order.index) if known else str(mapping.get("status") or "?")
    return verdicts


def render(
    record: dict[str, Any],
    planned: dict[str, Resource],
    coverage: dict[str, Any] | None = None,
) -> str:
    """The record as a document a person reads."""
    stamp = record.get("stamp") or {}
    resources = record.get("resources") or {}
    roles = split_by_role(planned)
    counted = [k for k in roles["planned"] if k in resources]
    tally = {s: sum(1 for k in counted if resources[k].get("status") == s) for s in STATUSES}
    blockers = [b for b in record.get("blockers") or [] if isinstance(b, dict)]

    lines = [
        "# Verification report",
        "",
        "What a run proved. `coverage.md` says what this NCP's API can support;",
        "this says what has been shown against a real account. A requirement can",
        "be supported and never once exercised, and the difference is here.",
        "",
        "| | |",
        "|---|---|",
        f"| Provider revision | `{stamp.get('provider_revision', '—')}` |",
        f"| Account | `{stamp.get('account', '—')}` |",
        "",
        "That is the latest recording. A status below can come from an earlier",
        "recording at another provider revision. The git history of",
        "`verification.yaml` says which commit set it.",
        "",
        "## Where it stands",
        "",
        "Per layer, because one fraction across every layer answers nothing: a",
        "layer nobody has built yet is not a layer that failed verification.",
        "",
        "| layer | live | offline | unverified | not selected | planned |",
        "|---|---|---|---|---|---|",
    ]
    for layer in sorted({r.layer for r in planned.values()}):
        kinds = [k for k in counted if planned[k].layer == layer]
        per_layer = {s: sum(1 for k in kinds if resources[k].get("status") == s) for s in STATUSES}
        lines.append(
            f"| `{layer}` | {per_layer['live']} | {per_layer['offline']} | "
            f"{per_layer['unverified']} | {per_layer[not_selected.STATUS]} | {len(kinds)} |"
        )
    lines += [
        "",
        f"Across every layer: {tally['live']} live, {tally['offline']} offline, "
        f"{tally['unverified']} unverified, {tally[not_selected.STATUS]} not selected, "
        f"of {len(roles['planned'])} planned.",
        "",
    ]
    needed = [k for k in roles["dependency resources"] if k in resources]
    if roles["dependency resources"]:
        extra = {s: sum(1 for k in needed if resources[k].get("status") == s) for s in STATUSES}
        lines += [
            f"Dependency resources, counted apart: {extra['live']} live, "
            f"{extra['offline']} offline, {extra['unverified']} unverified, "
            f"{extra[not_selected.STATUS]} not selected, of "
            f"{len(roles['dependency resources'])}. Each is planned because another "
            f"resource's create needs it. No requirement names it, so none is in the "
            f"figures above.",
            "",
        ]
    lines += [
        "**`offline` means a resource that has not passed live.** Its live lifecycle",
        "failed, or no live run has reported it. Its blocker says why.",
        "",
        "**`unverified` means a resource that has a case and that no run has reported.**",
        "It is a verification gap. **`not_selected` means a resource that the operator",
        "chose not to build.** No run will report it. Its evidence is the operator's",
        "reason, and it counts here so the whole planned surface stays visible.",
        "",
    ]

    if blockers:
        lines += [
            "## Why the rest are not live",
            "",
            "Each blocker is stated once and cited by id, so six resources sharing",
            "one reason cannot grow six versions of it.",
            "",
            "| id | category | what is blocked | what would clear it |",
            "|---|---|---|---|",
        ]
        for blocker in blockers:
            lines.append(
                f"| `{blocker.get('id')}` | {intermittent.label(blocker)} | "
                f"{blocker.get('statement')} | {blocker.get('clears_when')} |"
            )
        lines.append("")

    lines += ["## Resources", ""]
    by_layer: dict[str, list[str]] = {}
    for kind in sorted(resources):
        if case_results.type_of(kind) in planned:
            by_layer.setdefault(planned[case_results.type_of(kind)].layer, []).append(kind)
    for layer, kinds in sorted(by_layer.items()):
        lines += [
            f"### `{layer}`",
            "",
            "| resource | status | blocked by | evidence |",
            "|---|---|---|---|",
        ]
        for kind in kinds:
            entry, resource = resources[kind], planned[case_results.type_of(kind)]
            cited = ", ".join(f"`{i}`" for i in entry.get("blocked_by") or []) or "—"
            serves = ", ".join(f"`{name}`" for name in resource.dependency_of)
            role = f" — dependency of {serves}" if resource.is_dependency else ""
            lines.append(f"| `{kind}`{role} | **{entry.get('status')}** | {cited} | {entry.get('evidence', '—')} |")
        lines.append("")

    layers = {n: b for n, b in rolled_up(record, "layer", planned).items() if n != NO_MODULE}
    lines += module_table.section(layers, record.get("modules") or {}, weakest)

    rolled = rolled_up(record, "requirements", planned)
    verdicts = supported(coverage or {})
    lines += [
        "## Requirements",
        "",
        "**Supported and verified are different claims, so they sit side by side.**",
        "`supported` is what the analysis found the API can do, and is true before",
        "anything runs. `verified` is what a run showed. A requirement that is",
        "`present` and `unverified` is not a gap in the API -- it is a gap in what",
        "we have proved, and the two read identically until they are printed",
        "together.",
        "",
        "A requirement is `live` only when every resource carrying it is. Anything",
        "else takes the weakest status among them, and names what holds it.",
        "",
        "| requirement | supported | verified | live / total | held by |",
        "|---|---|---|---|---|",
    ]
    for name, body in rolled.items():
        held = ", ".join(f"`{k}`" for k in body["held_by"]) or "—"
        lines.append(
            f"| `{name}` | {verdicts.get(name, '—')} | **{body['status']}** | "
            f"{body['live']} / {body['resources']} | {held} |"
        )
    lines.append("")
    gap = sorted(
        name
        for name, body in rolled.items()
        if verdicts.get(name) in {"present", "partial"} and body["status"] != "live"
    )
    if gap:
        lines += [
            f"**{len(gap)} requirement(s) the API supports and no run has proved:** "
            + ", ".join(f"`{name}`" for name in gap)
            + ".",
            "",
            "That number is the reason this report exists.",
            "",
        ]

    lines += not_selected.section(resources, planned)
    lines += [
        "---",
        "",
        "Generated by `ncp-tf-verify-report` from `verification.yaml`. Do not edit",
        "this file; record the run again instead.",
        "",
    ]
    return "\n".join(lines)


def narrowed(
    record: dict[str, Any], planned: dict[str, Resource], layers: list[str] | None
) -> tuple[dict[str, Any], dict[str, Resource]]:
    """The record and the plan, restricted to the layers asked for.

    `record` and `check` always run over the whole plan, because a resource
    dropped from either is a resource that leaves the report. Narrowing is a
    reading of the same record, so a report about one layer cannot claim a
    status for a resource outside it.
    """
    if not layers:
        return record, planned
    unknown = sorted(set(layers) - {r.layer for r in planned.values()})
    if unknown:
        raise InputError(
            f"the module plan has no layer named {', '.join(unknown)}; it has "
            f"{', '.join(sorted({r.layer for r in planned.values()}))}"
        )
    kept = {k: r for k, r in planned.items() if r.layer in layers}
    resources = {k: v for k, v in (record.get("resources") or {}).items() if case_results.type_of(k) in kept}
    modules = [e for n, e in (record.get("modules") or {}).items() if n in layers]
    cited = {
        str(i)
        for entry in [*resources.values(), *modules]
        if isinstance(entry, dict)
        for i in entry.get("blocked_by") or []
    }
    blockers = [b for b in record.get("blockers") or [] if isinstance(b, dict) and str(b.get("id")) in cited]
    return {**record, "resources": resources, "blockers": blockers}, kept


def command_report(args: argparse.Namespace) -> int:
    coverage = load_document(Path(args.coverage))
    record, planned = narrowed(
        load_document(Path(args.verification)),
        planned_resources(coverage),
        args.layer,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(record, planned, coverage), encoding="utf-8")
    print(f"{out}", file=sys.stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verification_report", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    child = subparsers.add_parser("record", help="turn a run's output into statuses")
    child.add_argument("--coverage", required=True, help="path to coverage.yaml")
    # Required, because a requirement that two cases of one type cite reads
    # `live` from one of them when the other is not known.
    child.add_argument("--cases", required=True, help="the provider's resource cases directory")
    child.add_argument("--out", required=True, help="path to verification.yaml")
    child.add_argument("--live", action="append", help="a `go test -json` output file; repeatable")
    child.add_argument(
        "--module",
        action="append",
        help="a `terraform test -json` output file for the layer modules; repeatable",
    )
    child.add_argument(
        "--offline",
        action="append",
        help="a `terraform test -json` output file for the resource workspaces; repeatable",
    )
    # Required, because the stamp holds this recording's values and no other.
    child.add_argument("--provider-revision", required=True, help="the provider commit under test")
    child.add_argument("--account", required=True, help="the account the run used")
    child.set_defaults(handler=command_record)

    child = subparsers.add_parser("check", help="refuse a record that says less than it appears to")
    child.add_argument("--verification", required=True, help="path to verification.yaml")
    child.add_argument("--coverage", required=True, help="path to coverage.yaml")
    child.add_argument("--report", help="the committed report; refused when the render differs")
    child.add_argument(
        "--layer",
        action="append",
        help="the layers the report was rendered for; repeatable. The gates still cover the whole plan.",
    )
    child.set_defaults(handler=command_check)

    child = subparsers.add_parser("report", help="render the record as verification.md")
    child.add_argument("--verification", required=True, help="path to verification.yaml")
    child.add_argument("--coverage", required=True, help="path to coverage.yaml")
    child.add_argument("--out", required=True, help="path to verification.md")
    child.add_argument(
        "--layer",
        action="append",
        help="report on one layer only; repeatable. The record still covers the whole plan.",
    )
    child.set_defaults(handler=command_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except InputError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
