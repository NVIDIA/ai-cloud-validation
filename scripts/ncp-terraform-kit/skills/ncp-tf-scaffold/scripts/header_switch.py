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
"""A header that the provider sends only when a setting is on.

A prose document can state that some operations need a header, such as an
account header for a resource outside every account path. The agent records it
in the case after the operator confirms it, with `source: provider` and the
environment variable that turns the provider's switch on. A generated test
configuration has no `provider` block, so the environment is the only way a
test sets the switch. With the switch off, the API refuses the request, and the
refusal reads as a missing permission. So a live test that sends such a request
stops before its first step, and names the variable.
"""

from __future__ import annotations

import json
from typing import Any

import provider_settings

SOURCE = "provider"

GUARD = """
// headerSwitchOn stops a live test before its first step when the provider
// would not send a header that the test's requests need. The provider sends it
// only when the setting is true. Without it, the API refuses the request, and
// the refusal reads as a missing permission.
func headerSwitchOn(t *testing.T, variable, header string) {
	t.Helper()
	on, err := strconv.ParseBool(os.Getenv(variable))
	if err != nil || !on {
		t.Fatalf("%s is off; set it to true. The provider sends %s only when it is on, "+
			"and this test's requests need that header", variable, header)
	}
}
"""


def _entries(case: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each header with `source: provider`, with the verb of its operation."""
    return [
        (str(request.get("verb") or ""), header)
        for request in case.get("expected_requests") or []
        if isinstance(request, dict)
        for header in request.get("headers") or []
        if isinstance(header, dict) and header.get("source") == SOURCE
    ]


def switches(case: dict[str, Any]) -> list[tuple[str, str]]:
    """The settings, with their headers, that a live test of the case needs on.

    The test sends the operations of its own case, and the setup step sends the
    create of each dependency resource that the test creates.
    """
    created = [
        entry["case"]
        for entry in case.get("_resolved_dependencies") or []
        if entry.get("resource") and isinstance(entry.get("case"), dict)
    ]
    return sorted(
        {
            (str(header["setting"]), str(header.get("name") or ""))
            for subject in (case, *created)
            for _, header in _entries(subject)
            if header.get("setting")
        }
    )


def calls(case: dict[str, Any]) -> str:
    """The guard statements of a live test, one for each switch it needs."""
    return "".join(
        f"\theaderSwitchOn(t, {json.dumps(setting)}, {json.dumps(header)})\n" for setting, header in switches(case)
    )


def turned_on(case: dict[str, Any]) -> str:
    """The statements that turn on each switch of the case, for a test that
    asserts its headers with no account. The environment is the only way a
    test sets the switch, and the provider's stub settings do not name it."""
    return "".join(f'\tt.Setenv({json.dumps(setting)}, "true")\n' for setting, _ in switches(case))


def stated(export: dict[str, Any]) -> set[str]:
    """Every environment variable that a provider setting falls back to."""
    found: set[str] = set()
    for _, schema in sorted((export.get("provider_schemas") or {}).items()):
        block = ((schema or {}).get("provider") or {}).get("block") or {}
        for attribute in (block.get("attributes") or {}).values():
            fallback = provider_settings.FALLBACK.search(str((attribute or {}).get("description") or ""))
            if fallback:
                found.update(provider_settings.ENVIRONMENT_VARIABLE.findall(fallback.group(1)))
    return found


def refusals(case: dict[str, Any], export: dict[str, Any]) -> list[str]:
    """Why a live render cannot guard the case's provider headers, or nothing.

    The live render runs once the provider exists, so a header entry must name
    its setting, and a setting that the provider's export does not state turns
    no switch on.
    """
    broken = []
    known = stated(export) if export else None
    for verb, header in _entries(case):
        name, setting = header.get("name"), header.get("setting")
        if not setting:
            broken.append(
                f"the {verb} header {name} has source provider and no setting; name the "
                "environment variable that makes the provider send it"
            )
        elif known is not None and setting not in known:
            broken.append(
                f"the {verb} header {name} names the setting {setting}, which no provider "
                f"setting falls back to; the schema export states {', '.join(sorted(known)) or 'none'}"
            )
    return broken
