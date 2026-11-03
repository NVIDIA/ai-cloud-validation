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
"""The values an offline test holds the plan and the create request to, at
every depth.

A case's proving values can sit inside a map or a list: a node pool's machine
type is inside its `config`. The offline plan and the request check are the
two places that see such a value without an account, so each checks it at the
path the fixture wrote.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import fixture_values

#: A path segment that HCL can name with a dot. Any other segment, such as a
#: label key with a dot or a slash, is named as an index.
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

KeyPath = fixture_values.KeyPath


def hcl_path(address: str, path: KeyPath, configured: dict[str, Any]) -> str:
    """The HCL expression that reaches one value of a resource's plan.

    The configured value says what each segment indexes: a list takes a number,
    and a map takes its key, also a key spelled with digits alone.
    """
    text, node = address, configured
    for segment in path:
        if isinstance(node, list):
            text += f"[{segment}]"
            node = node[int(segment)]
            continue
        text += f".{segment}" if IDENTIFIER.fullmatch(segment) else f"[{json.dumps(segment)}]"
        node = node.get(segment) if isinstance(node, dict) else None
    return text


def plan_checks(
    address: str,
    configured: dict[str, Any],
    spell: Callable[[Any], str],
) -> list[tuple[KeyPath, str, Any]]:
    """Each value the plan must hold: its path, the expression, and the value.

    A top-level scalar compares by its value and type. A nested scalar compares
    as a string, because a provider can declare a number of the document as a
    string attribute, and Terraform's `==` does not convert. A list compares by
    its length.
    """
    checks: list[tuple[KeyPath, str, Any]] = []
    for path, value in fixture_values.leaves(configured):
        if len(path) == 1:
            checks.append((path, f"{address}.{path[0]}", value))
        elif path[-1] == fixture_values.LENGTH:
            checks.append((path, f"length({hcl_path(address, path[:-1], configured)})", value))
        else:
            checks.append((path, f"tostring({hcl_path(address, path, configured)})", spell(value)))
    return checks


def nested_request_check(values: list[tuple[KeyPath, str]]) -> str:
    """The rendered `checkNestedValues` call, or nothing when the configuration
    sets no nested value and no list."""
    if not values:
        return ""
    rows = "".join(
        "\t\t{[]string{" + ", ".join(json.dumps(segment) for segment in path) + "}, " + json.dumps(value) + "},\n"
        for path, value in values
    )
    return (
        "\t// Every nested value and list the configuration sets, at its path in the body.\n"
        "\tcheckNestedValues(t, observed, []nestedValue{\n" + rows + "\t})\n\n"
    )


#: The checkNestedValues function. It joins the request check file, which
#: declares the observer it reads.
CHECK_NESTED = """
// nestedValue is one value the configuration set below the top level, or one
// list, with the argument names and map keys that reach it.
type nestedValue struct {
	path []string
	want string
}

// checkNestedValues asserts that each nested value the configuration set
// arrives in the body, at the same path. A query parameter or a path segment
// holds one scalar, so only the body can hold a map or a list.
func checkNestedValues(t *testing.T, observed *requestObserver, want []nestedValue) {
	t.Helper()
	got, sent := observed.first()
	if !sent {
		return // checkRequest has already said so
	}
	for _, value := range want {
		if difference := bodyDifference(got.body, value.path, value.want); difference != "" {
			t.Errorf("%s. Body: %s", difference, render(got.body))
		}
	}
}
"""

#: The standard-library packages `REQUEST_WALKER` uses. It uses nothing else,
#: so a kit test can compile and run it with no module to fetch.
REQUEST_WALKER_IMPORTS = ("fmt", "strconv", "strings")

#: The walker `checkNestedValues` uses, joined into the request check file.
REQUEST_WALKER = """
// bodyDifference says how the body differs from one configured value, or
// nothing when the body holds it. A segment matches the field of the same
// name. A segment spelled as an argument name also matches a field whose name
// is the same with case, underscores and hyphens ignored; a segment with a
// character no argument name has, such as a label key with a hyphen, matches
// only itself. A list compares by its length and element by element, in the
// order the configuration wrote.
func bodyDifference(body map[string]any, path []string, want string) string {
	shown := strings.Join(path, ".")
	var current any = body
	for depth, segment := range path {
		switch value := current.(type) {
		case map[string]any:
			key, ambiguous := bodyKey(value, segment)
			if ambiguous {
				return fmt.Sprintf("%s: more than one field of the body corresponds to %q", shown, segment)
			}
			if key == "" {
				return fmt.Sprintf("%s: the body has no %s", shown, strings.Join(path[:depth+1], "."))
			}
			current = value[key]
		case []any:
			if segment == "#" {
				current = len(value)
				continue
			}
			index, err := strconv.Atoi(segment)
			if err != nil || index < 0 || index >= len(value) {
				return fmt.Sprintf("%s: the body's list holds %d elements", shown, len(value))
			}
			current = value[index]
		default:
			return fmt.Sprintf("%s: the body holds %q where the configuration has fields", shown, bodyText(current))
		}
	}
	if current == nil {
		return fmt.Sprintf("%s: the body holds null, and the configuration set %q", shown, want)
	}
	if got := bodyText(current); got != want {
		return fmt.Sprintf("%s: the body holds %q, and the configuration set %q", shown, got, want)
	}
	return ""
}

func bodyKey(fields map[string]any, name string) (key string, ambiguous bool) {
	if _, exact := fields[name]; exact {
		return name, false
	}
	if strings.Trim(name, "abcdefghijklmnopqrstuvwxyz0123456789_") != "" {
		return "", false
	}
	for candidate := range fields {
		if flatten(candidate) != flatten(name) {
			continue
		}
		if key != "" {
			return "", true
		}
		key = candidate
	}
	return key, false
}

// bodyText spells a body value as Terraform's state spells it.
func bodyText(value any) string {
	if number, ok := value.(float64); ok {
		return strconv.FormatFloat(number, 'f', -1, 64)
	}
	return fmt.Sprintf("%v", value)
}

func flatten(name string) string {
	return strings.ToLower(strings.NewReplacer("_", "", "-", "").Replace(name))
}
"""
