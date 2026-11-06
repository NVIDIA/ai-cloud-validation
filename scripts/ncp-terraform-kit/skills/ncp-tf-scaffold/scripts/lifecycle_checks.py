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
"""The checks a live test takes from a case's lifecycle, readiness and
dependency resources.

A case states more than its values: the order of its steps, which state means
usable, and which dependency resources its test creates. Each is a check the
live test makes, so that no operator writes it by hand beside the render.
"""

from __future__ import annotations

import json
import re
from typing import Any

import destroy_read

#: The lifecycle steps that apply a configuration, and which one each applies.
APPLIED = {"apply_create": "create", "apply_update": "update"}

#: The lifecycle step that plans the configuration applied before it and
#: expects no change.
PLAN = "plan_no_change"

#: A readiness the read can show: one field of the read, named with dots for
#: each object it sits inside, and the value that means usable. The contract
#: gives `status == READY` as the shape.
READINESS = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*==\s*(\S+)\s*")

#: Where a live check line sits in a rendered step.
INDENT = "\t" * 5


def steps(lifecycle: list[str]) -> list[tuple[str, str]]:
    """Each test step the lifecycle asks for, in its order.

    A step is ("apply", <configuration>) or ("plan", <configuration>). A plan
    before anything is applied has no configuration to plan, so it is no step.
    """
    found: list[tuple[str, str]] = []
    applied = ""
    for step in lifecycle:
        if step in APPLIED:
            applied = APPLIED[step]
            found.append(("apply", applied))
        elif step == PLAN and applied:
            found.append(("plan", applied))
    return found


def readiness(case: dict[str, Any]) -> tuple[str, str] | None:
    """The field and value the case's readiness names, or None for prose."""
    found = READINESS.fullmatch(str(case.get("readiness") or ""))
    return (found.group(1), found.group(2)) if found else None


def ready_check(address: str, operation: str, case: dict[str, Any]) -> list[str]:
    """The rendered `checkReady` call, or nothing when the readiness is prose."""
    found = readiness(case)
    if not found:
        return []
    field, value = found
    arguments = ", ".join(json.dumps(text) for text in (address, operation, field, value))
    return [f"{INDENT}checkReady({arguments}),"]


def dependency_destroys(entries: list[dict[str, Any]]) -> str:
    """The `CheckDestroy` value for the dependency resources a test creates,
    or nothing when it creates none that a read can prove destroyed.

    Each is read through its own case's destroy read. A run that supplies the
    dependency creates nothing, and `checkGone` passes what is not in state.
    """
    calls = []
    for entry in entries:
        case = entry.get("case") or {}
        destroy = (case.get("verification") or {}).get("destroy")
        if not entry.get("resource") or not isinstance(destroy, dict):
            continue
        operation, identifier = destroy_read.destroy_read(case)
        address = f"{entry['resource']}.setup_{entry['variable']}.0"
        arguments = (address, operation, identifier, str(destroy.get("expect") or ""))
        calls.append("checkGone(" + ", ".join(json.dumps(text) for text in arguments) + ")")
    if len(calls) < 2:
        return "".join(call + "," for call in calls)
    return "resource.ComposeAggregateTestCheckFunc(\n" + "".join(f"\t\t\t{call},\n" for call in calls) + "\t\t),"


#: The checkReady function. It joins the independent check file, which
#: imports what it needs.
CHECK_READY = """
// checkReady asks the NCP whether the resource is usable and not only created:
// its read must hold the field the case's readiness names, at the value it
// names. A provider that returns before the resource is usable passes every
// value check, and the next step that needs it fails instead.
func checkReady(address, operation, field, want string) resource.TestCheckFunc {
	return func(state *terraform.State) error {
		id, err := stateID(state, address)
		if err != nil {
			return err
		}
		body, found, err := independentReader.Read(context.Background(), operation, id)
		if err != nil {
			return fmt.Errorf("reading %s (%s) from the NCP: %w", address, id, err)
		}
		if !found {
			return fmt.Errorf("Terraform created %s, and the NCP does not have it", address)
		}
		difference, absent := readyDifference(body, field, want)
		if absent {
			return fmt.Errorf("%s (%s): %s", address, id, difference)
		}
		if difference != "" {
			return fmt.Errorf("%s (%s) is not ready: %s", address, id, difference)
		}
		return nil
	}
}
"""

#: The walker `checkReady` uses. It needs only the standard library and the
#: read's walker, so a kit test can compile and run the two together.
READY_WALKER = """
// readyDifference says how the read differs from the readiness the case
// states, or nothing when the read shows it. The field is the read's own,
// named with dots for each object it sits inside, and matches by the read's
// name rule. absent is true when the read has no such field: a schema can
// hold a field that the read leaves out, and only the operator can name the
// state that means usable instead.
func readyDifference(body map[string]any, field, want string) (difference string, absent bool) {
	var current any = body
	for _, segment := range strings.Split(field, ".") {
		fields, ok := current.(map[string]any)
		if !ok {
			return fmt.Sprintf("the read holds %q where the readiness names %s", readText(current), field), false
		}
		key, ambiguous := readKey(fields, segment)
		if ambiguous {
			return fmt.Sprintf("more than one field of the read corresponds to %q", segment), false
		}
		if key == "" {
			return fmt.Sprintf("the read returns no %s, so it cannot show the readiness the case names "+
				"(%s == %s). A read can leave out a field that the resource's schema has. Which state "+
				"means usable is the operator's call: ask the operator to restate the case's readiness, "+
				"and do not choose one", field, field, want), true
		}
		current = fields[key]
	}
	if current == nil {
		return fmt.Sprintf("the read returns null for %s, and ready means %q", field, want), false
	}
	if got := readText(current); got != want {
		return fmt.Sprintf("%s is %q, and ready means %q", field, got, want), false
	}
	return "", false
}
"""
