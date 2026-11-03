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
"""The stand-in that the request check gives an `attribute` dependency.

The request check creates nothing, so each supplied value is a stand-in. A
provider can refuse a stand-in before it sends a request: a path parameter
whose document states the pattern `^groups/[^/]+$` takes no bare word. A
`reference` already gives its value's form. An `attribute` gives none, so its
stand-in takes the form that the case's derived dependency states for the
field: the Discovery pattern, the stated name format, or one segment of an
OpenAPI path. The account scope that `derived_from` records fills the front
of a form that starts with it, as the provider fills it from its settings.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import dependency_graph
import fixture_values

_WHOLE = re.compile(r"^\{([a-z_][a-z0-9_]*)\}$")
_NAMED = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
#: The form of a value that is one segment of a path.
_SEGMENT = "{}"


def field_form(entry: dict[str, Any]) -> str:
    """The form that a derived dependency's field takes, with `{}` for each value."""
    evidence = str(entry.get("evidence") or "")
    if entry.get("rule") == dependency_graph.STATED_FORMAT:
        return dependency_graph.template_shape(evidence.strip("`"))
    if evidence.startswith("^"):
        # A Discovery path parameter holds the whole name that its pattern states.
        return dependency_graph.pattern_shape(evidence)
    # An OpenAPI path parameter is one segment of the create's path.
    return _SEGMENT


def _forms(case: dict[str, Any]) -> dict[str, set[str]]:
    """Each attribute dependency that fills a derived dependency's field, with the forms."""
    attributes = {
        str(e.get("variable")) for e in case.get("dependencies") or [] if isinstance(e, dict) and e.get("attribute")
    }
    stated = {str(e.get("field")): field_form(e) for e in case.get(dependency_graph.FIELD) or []}
    forms: dict[str, set[str]] = {}
    create = (case.get("fixture") or {}).get("create")
    for path, value in fixture_values.leaves(create):
        whole = _WHOLE.match(value) if isinstance(value, str) else None
        here = ".".join(part for part in path if not part.isdigit())
        if whole and whole.group(1) in attributes and here in stated:
            forms.setdefault(whole.group(1), set()).add(stated[here])
    return forms


def stand_ins(case: dict[str, Any], stub: Callable[[str], str]) -> dict[str, str]:
    """The stand-in of each attribute dependency whose field states a form other
    than one segment, which the flat stand-in `stub` already is."""
    scope = str((case.get("derived_from") or {}).get("account_scope") or "")
    head = dependency_graph.template_shape(scope) + "/" if scope else ""
    out: dict[str, str] = {}
    for variable, forms in _forms(case).items():
        if len(forms) != 1 or (form := next(iter(forms))) == _SEGMENT:
            continue
        filled = form.replace("{}", stub(variable))
        if head and form.startswith(head):
            filled = (
                _NAMED.sub(lambda found: stub(found.group(1)), scope)
                + "/"
                + (form.removeprefix(head).replace("{}", stub(variable)))
            )
        out[variable] = filled
    return out


def supplied_forms(case: dict[str, Any]) -> dict[str, str]:
    """The pattern that a supplied value of each attribute dependency must match:
    the one form, other than one segment, that its field states. The test sends
    the value as it is, so a value in another form reaches the API."""
    return {
        variable: "^" + "[^/]+".join(re.escape(part) for part in form.split("{}")) + "$"
        for variable, forms in _forms(case).items()
        if len(forms) == 1 and (form := next(iter(forms))) != _SEGMENT
    }


def refusals(case: dict[str, Any]) -> list[str]:
    """Each attribute dependency that fills fields of different stated forms: one
    stand-in cannot take both, so the request check would send one that a
    provider refuses."""
    return [
        f"dependency {variable!r} is an `attribute` in fields whose documents state "
        f"different forms ({', '.join(sorted(forms))}). One value cannot take both, so the "
        "request check has no stand-in that the provider sends. Read the documents of each "
        "field: one of them names another resource, or takes the value in another form"
        for variable, forms in sorted(_forms(case).items())
        if len(forms) > 1
    ]
