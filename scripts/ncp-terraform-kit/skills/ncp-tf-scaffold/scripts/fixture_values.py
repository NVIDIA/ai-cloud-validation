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
"""The values a fixture configures, at the paths a live test checks them.

A fixture is a map of Terraform arguments, and an argument can hold a map or a
list of maps. Terraform's state names a nested value by its keys joined with
dots, a list's length as `#`, and an element by its index. The live test checks
each value at that path in state, and again in what the NCP's read returns: the
provider keeps a configured value in state when its read omits it, so state
alone passes for a value the NCP dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import provider_settings

#: The segment Terraform's state names a list's length with.
LENGTH = "#"

#: The marker a document uses for a field the read never returns. The same
#: anchored form as `ncp-tf-provider`'s output-only marker: it opens the
#: description or follows a sentence, and closes at one, so `Input only for
#: type PARTNER` qualifies a case and does not match.
INPUT_ONLY = re.compile(r"(?:^|[.;:(]\s*)\[?input[ _-]?only\s*(?:[.)\]]|$)", re.IGNORECASE)

KeyPath = tuple[str, ...]

#: The argument that holds the resource's operation timeouts. Terraform reads
#: it and the provider sends none of it, so no request and no read holds it.
TIMEOUTS = "timeouts"


def api_values(values: dict[str, Any]) -> dict[str, Any]:
    """The values that a request carries: all of them but the timeouts."""
    return {key: value for key, value in values.items() if key != TIMEOUTS}


def leaves(values: Any, path: KeyPath = ()) -> list[tuple[KeyPath, Any]]:
    """Each scalar a fixture sets, and each list's length, with its path."""
    if isinstance(values, dict):
        return [leaf for key, value in values.items() for leaf in leaves(value, (*path, str(key)))]
    if isinstance(values, list):
        found: list[tuple[KeyPath, Any]] = [((*path, LENGTH), len(values))]
        for index, item in enumerate(values):
            found += leaves(item, (*path, str(index)))
        return found
    if isinstance(values, (str, int, float, bool)):
        return [(path, values)]
    return []


def read_export(named: str | None, runner_out: str | None) -> dict[str, Any]:
    """The provider's schema export the render was given or finds, or nothing."""
    path = Path(named) if named else (Path(runner_out) / provider_settings.EXPORT_NAME if runner_out else None)
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resource_block(export: dict[str, Any], terraform_type: str) -> dict[str, Any]:
    """One resource type's schema block in the export, or an empty one."""
    for _, schema in sorted((export.get("provider_schemas") or {}).items()):
        found = ((schema or {}).get("resource_schemas") or {}).get(terraform_type)
        if found:
            return found.get("block") or {}
    return {}


def unreturned(block: dict[str, Any], path: KeyPath) -> bool:
    """Whether the read cannot return this value.

    A secret comes back masked or not at all, and an input-only field does not
    come back. A path the block does not describe, such as a key in a map, is
    returned as far as the export says.
    """
    attributes = block.get("attributes") or {}
    for segment in path:
        if segment == LENGTH or segment.isdigit():
            continue
        attribute = attributes.get(segment)
        if not isinstance(attribute, dict):
            return False
        if attribute.get("sensitive") or INPUT_ONLY.search(str(attribute.get("description") or "")):
            return True
        attributes = (attribute.get("nested_type") or {}).get("attributes") or {}
    return False


def merged_fixture(create: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """The whole body the update step sends, not the change on its own.

    The update fixture is a delta by contract, and several APIs update through a
    request that replaces the whole body, so the delta has to be laid over the
    create rather than sent alone. It is laid over at every level: a shallow
    merge replaced a nested object outright, and one case's update of a single
    field discarded the eight others its create had set.

    A list is replaced rather than merged. A document that asks for a list of
    rules means the rules, and half of one set is not a shorter list.
    """
    body = dict(create)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key] = merged_fixture(body[key], value)
        else:
            body[key] = value
    return body


def update_fixture(case: dict[str, Any]) -> dict[str, Any]:
    """The update delta that the case states, or nothing."""
    return dict((case.get("fixture") or {}).get("update") or {})


def step_values(case: dict[str, Any], create: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The values each applying step checks: the create's, and after the update
    the create's with the update laid over them, which the update applies."""
    return {"create": create, "update": merged_fixture(create, update_fixture(case))}


def wrapper(values: dict[str, Any]) -> str:
    """The argument that may wrap the whole resource, or nothing.

    A create request can wrap the resource under one field beside scalars such
    as its parent, and the read returns the resource itself. Only an argument
    that is the step's one map, with no list beside it, has that shape.
    """
    nested = [key for key, value in api_values(values).items() if isinstance(value, (dict, list))]
    if len(nested) == 1 and isinstance(values[nested[0]], dict):
        return str(nested[0])
    return ""


def _loose(name: str) -> str:
    """A name with case, underscores and hyphens ignored: the request check's
    rule, by which `project_id` is the parameter `projectId`."""
    return re.sub(r"[_-]", "", name).lower()


def _create_parameters(case: dict[str, Any]) -> frozenset[str]:
    """The required parameters of the create request, as `_loose` spells them.

    `instantiate` writes them from the specification: the request sends each
    in its path or its query, and the resource's read does not return it.
    """
    for entry in case.get("expected_requests") or []:
        if isinstance(entry, dict) and entry.get("verb") == "create":
            return frozenset(_loose(str(name)) for name in entry.get("required_parameters") or [])
    return frozenset()


def read_check(
    address: str,
    operation: str,
    values: dict[str, Any],
    block: dict[str, Any],
    case: dict[str, Any],
    *,
    segment: Callable[[str], str],
) -> str:
    """The rendered `checkRead` call: each value's path the read must hold,
    and then each path whose configured value is its type's zero value.

    A parameter of the case's create request is one value, so only a
    top-level scalar can be one. `segment` writes one path segment as a Go
    string expression, because a map key can hold a value the run supplies.
    """
    parameters = _create_parameters(case)
    found = [
        (path, value)
        for path, value in leaves(api_values(values))
        if not unreturned(block, path) and not (len(path) == 1 and _loose(path[0]) in parameters)
    ]
    held = _go_paths([path for path, value in found if value], segment)
    zero = _go_paths([path for path, value in found if not value], segment)
    return (
        f"\t\t\t\t\tcheckRead({json.dumps(address)}, {json.dumps(operation)}, "
        f"{json.dumps(wrapper(values))}, {held}, {zero}),"
    )


def _go_paths(paths: list[KeyPath], segment: Callable[[str], str]) -> str:
    """A Go `[][]string` of paths, or `nil`."""
    if not paths:
        return "nil"
    rows = "".join("\t\t\t\t\t\t{" + ", ".join(segment(part) for part in path) + "},\n" for path in paths)
    return "[][]string{\n" + rows + "\t\t\t\t\t}"


#: The standard-library packages `READ_WALKER` uses. It uses nothing else, so
#: a kit test can compile and run it with no module to fetch.
READ_WALKER_IMPORTS = ("fmt", "net/url", "strconv", "strings")

#: The checkRead function. It joins the independent check file, which imports
#: what it needs besides `READ_WALKER_IMPORTS`.
CHECK_READ = """
// checkRead asks the NCP for the resource Terraform reported, and holds each
// value the step configured to what the NCP returns. The provider keeps a
// configured value in state when its read omits it, so state alone passes for
// a value the NCP dropped. Each path names the configuration's arguments, and
// state holds the expected value at the same path. wrapper names the argument
// that may wrap the whole resource, or is empty. zero holds the paths whose
// configured value is the zero value of its type.
func checkRead(address, operation, wrapper string, paths, zero [][]string) resource.TestCheckFunc {
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
		attributes := state.RootModule().Resources[address].Primary.Attributes
		differences, unconfirmed, extra := readDifferences(body, id, wrapper, attributes, paths, zero)
		for _, path := range unconfirmed {
			fmt.Fprintf(os.Stderr, "%s (%s): the read omits %s, so the check cannot confirm the configured zero value\\n",
				address, id, path)
		}
		for _, list := range extra {
			fmt.Fprintf(os.Stderr, "%s (%s): %s\\n", address, id, list)
		}
		if len(differences) > 0 {
			return fmt.Errorf("the NCP's read of %s (%s) does not hold what the configuration set:\\n%s",
				address, id, strings.Join(differences, "\\n"))
		}
		return nil
	}
}
"""

READ_WALKER = """
// readDifferences holds each configured value to the read, and says how the
// read differs. attributes are state's. A path in zero holds the zero value
// of its type. Some APIs leave such a field out of a read and some do not, so
// a read that omits it neither shows the value nor contradicts it: the path
// is returned as unconfirmed, and it fails nothing. extra names each list in
// which the read holds more entries than the configuration set.
func readDifferences(body map[string]any, id, wrapper string, attributes map[string]string,
	paths, zero [][]string) (differences, unconfirmed, extra []string) {
	all := append(append([][]string{}, paths...), zero...)
	read := readWalk{id: id, wrapper: wrapper, attributes: attributes, paths: all, zero: map[string]bool{}}
	for _, path := range zero {
		read.zero[strings.Join(path, ".")] = true
	}
	for index, path := range all {
		key := strings.Join(path, ".")
		want, held := attributes[key]
		if !held {
			differences = append(differences, key+": state holds no value")
			continue
		}
		difference, absent, more := read.walk(body, path, 0, want)
		switch {
		case difference == "":
		case absent && index >= len(paths):
			unconfirmed = append(unconfirmed, key)
		default:
			differences = append(differences, difference)
		}
		if more != "" {
			extra = append(extra, more)
		}
	}
	return differences, unconfirmed, extra
}

// readWalk finds configured values in one read. paths are all the configured
// paths, and zero names those whose value is the zero value of its type.
type readWalk struct {
	id, wrapper string
	attributes  map[string]string
	paths       [][]string
	zero        map[string]bool
}

// walk says how the read differs from one configured value, from path[from]
// on, or nothing when the read holds it. absent is true when the read has no
// field on the path. more names a list in which the read holds more entries
// than the configuration set.
//
// A path segment matches the read's field of the same name. An argument name
// also matches a field whose name is the same with case, underscores and
// hyphens ignored, as the request check matches a request field; a map key,
// which is not an argument name, matches only itself. Two more places hold a
// top-level value, as they do in a request: the wrapper argument, which a
// create request can put around the whole resource, holds what the read
// returns at its top; and a value the request sent in its path is in the id
// the read was made with.
//
// An API can add its own entries to a list that the configuration set, so a
// read list may be longer than the configured one, and its entries may come
// in another order. Each configured entry is the read entry that holds all of
// its configured values.
func (r readWalk) walk(current any, path []string, from int, want string) (difference string, absent bool, more string) {
	shown := strings.Join(path, ".")
	for depth := from; depth < len(path); depth++ {
		segment := path[depth]
		switch value := current.(type) {
		case map[string]any:
			key, ambiguous := readKey(value, segment)
			switch {
			case ambiguous:
				return fmt.Sprintf("%s: more than one field of the read corresponds to %q", shown, segment), false, ""
			case key != "":
				current = value[key]
			case depth == 0 && len(path) > 1 && segment == r.wrapper:
				// The read returns the wrapped resource itself.
			case depth == 0 && len(path) == 1 && want != "" && strings.Contains(r.id, want):
				return "", false, ""
			default:
				return fmt.Sprintf("%s: the read returns no %s", shown, strings.Join(path[:depth+1], ".")), true, ""
			}
		case []any:
			list := strings.Join(path[:depth], ".")
			if segment == "#" {
				configured, err := strconv.Atoi(want)
				switch {
				case err != nil || len(value) < configured:
					return fmt.Sprintf("%s: the read's list holds %d entries, and the configuration set %s",
						shown, len(value), want), false, ""
				case len(value) > configured:
					return "", false, fmt.Sprintf("the read's %s holds %d entries, and the configuration set %d, "+
						"so the check does not hold the others", list, len(value), configured)
				}
				return "", false, ""
			}
			index, found := r.entry(value, path[:depth+1])
			if !found {
				return fmt.Sprintf("%s: no entry of the read's %s holds the values of the configured entry %s",
					shown, list, strings.Join(path[:depth+1], ".")), false, ""
			}
			current = value[index]
		default:
			return fmt.Sprintf("%s: the read holds %q where the configuration has fields", shown, readText(current)), false, ""
		}
	}
	if current == nil {
		return fmt.Sprintf("%s: the read returns null, and the configuration set %q", shown, want), false, ""
	}
	if got := readText(current); got != want && !sameReadReference(want, got) {
		return fmt.Sprintf("%s: the read returns %q, and the configuration set %q", shown, got, want), false, ""
	}
	return "", false, ""
}

// entry finds the read entry of list that is the configured entry prefix: the
// first one that holds each of its configured values and is not an earlier
// configured entry. A read that omits a zero value still holds the entry,
// as readDifferences treats that value.
func (r readWalk) entry(list []any, prefix []string) (int, bool) {
	position, err := strconv.Atoi(prefix[len(prefix)-1])
	if err != nil {
		return 0, false
	}
	taken := map[int]bool{}
	for earlier := 0; earlier < position; earlier++ {
		sibling := append(append([]string{}, prefix[:len(prefix)-1]...), strconv.Itoa(earlier))
		if index, found := r.entry(list, sibling); found {
			taken[index] = true
		}
	}
	for index, candidate := range list {
		if !taken[index] && r.holds(candidate, prefix) {
			return index, true
		}
	}
	return 0, false
}

// holds says whether one read entry holds each configured value under prefix.
func (r readWalk) holds(candidate any, prefix []string) bool {
	for _, path := range r.paths {
		if len(path) < len(prefix) || strings.Join(path[:len(prefix)], ".") != strings.Join(prefix, ".") {
			continue
		}
		key := strings.Join(path, ".")
		want, held := r.attributes[key]
		if !held {
			continue
		}
		difference, absent, _ := r.walk(candidate, path, len(prefix), want)
		if difference != "" && (!absent || !r.zero[key]) {
			return false
		}
	}
	return true
}

func readKey(fields map[string]any, name string) (key string, ambiguous bool) {
	if _, exact := fields[name]; exact {
		return name, false
	}
	if strings.Trim(name, "abcdefghijklmnopqrstuvwxyz0123456789_") != "" {
		return "", false
	}
	wanted := readName(name)
	for candidate := range fields {
		if readName(candidate) != wanted {
			continue
		}
		if key != "" {
			return "", true
		}
		key = candidate
	}
	return key, false
}

func readName(name string) string {
	return strings.ToLower(strings.NewReplacer("_", "", "-", "").Replace(name))
}

// readText spells a read value as Terraform's state spells it.
func readText(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case bool:
		return strconv.FormatBool(typed)
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case int:
		return strconv.Itoa(typed)
	}
	return fmt.Sprintf("%v", value)
}

// sameReadReference accepts the spelling a provider keeps as the configured
// one: a full URL whose path ends with the relative name the configuration set.
func sameReadReference(configured, returned string) bool {
	link, err := url.Parse(returned)
	if configured == "" || err != nil || !link.IsAbs() || link.Host == "" {
		return false
	}
	path := strings.Trim(link.Path, "/")
	wanted := strings.Trim(configured, "/")
	return path == wanted || strings.HasSuffix(path, "/"+wanted)
}
"""
