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
"""A case's peer: a second resource of the type under test, which the
resource under test needs to be usable.

A VPN tunnel is ready only when a tunnel at its far end names it back. That
counterpart is of the case's own type, so a `dependencies` entry for it
resolves the case again. As a dependency it would also be created first, and
it is ready only when the resource under test exists. The provider's create
waits until its resource is ready, so the two are created in the same apply,
with no dependency between them, and each create can wait for the other.

The peer is the case's create fixture with `values` laid over it at every
level. A value may name a dependency resource of the case, so each side names
the other side's gateway. The readiness, the update and the read-back of the
case's values stay on the resource under test.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The case field, and the Terraform label of the peer's block.
FIELD = "peer"

#: The configuration that the request check applies: the create without the
#: peer. The check asserts the first request that arrives, and with the peer in
#: the configuration that request can be the peer's.
REQUEST = "request"

#: The Go variable that keeps the peer's id from the create step to the
#: teardown, after Terraform has removed the peer from state.
ID_VARIABLE = "peerID"

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

#: Where a live check line sits in a rendered step.
INDENT = "\t" * 5


def declared(case: dict[str, Any]) -> bool:
    return FIELD in case


def values(case: dict[str, Any]) -> dict[str, Any]:
    """The peer's values, or none when the entry is malformed: `render`
    resolves a case before `check` refuses it."""
    peer = case.get(FIELD)
    given = peer.get("values") if isinstance(peer, dict) else None
    return dict(given) if isinstance(given, dict) else {}


def entry(case: dict[str, Any]) -> dict[str, Any]:
    """The peer as the dependency renderer names a created resource: the
    case's fixture with the values laid over it, named for the case and `peer`."""
    return {"variable": FIELD, "values": values(case), "case": case}


def address(case: dict[str, Any]) -> str:
    kind = str((case.get("resource") or {}).get("terraform_address") or "").partition(".")[0]
    return f"{kind}.{FIELD}"


def refusals(case: dict[str, Any]) -> list[str]:
    """Each way the case's peer cannot be rendered, empty when it can or when
    the case declares none."""
    if not declared(case):
        return []
    contract = str(case.get("contract") or "")
    if contract != "resource":
        return [
            f"a {contract} case has no peer; a module or a blueprint test applies "
            "the operator's configuration, and nothing renders a peer there"
        ]
    peer = case[FIELD]
    if not isinstance(peer, dict):
        return ["peer is not a mapping of `why` and `values`"]
    found = []
    if not str(peer.get("why") or "").strip():
        found.append(
            "peer gives no why; a peer creates more resources and costs more, and "
            "a reviewer cannot tell one that nothing needs from one the operator chose"
        )
    given = peer.get("values")
    if not isinstance(given, dict) or not given:
        found.append("peer holds no values; a peer with none is a second copy of the resource under test")
        given = {}
    create = (case.get("fixture") or {}).get("create") or {}
    found += [
        f"peer.values.{path} is not a field of fixture.create; the peer is the "
        "case's create fixture with its values laid over it"
        for path in _unknown(given, create)
    ]
    dependencies = [entry for entry in case.get("dependencies") or [] if isinstance(entry, dict)]
    known = {str(entry.get("variable") or "") for entry in dependencies}
    found += [
        f"peer.values names {{{name}}}, which the case's dependencies do not declare; nothing creates or supplies it"
        for name in sorted(set(_PLACEHOLDER.findall(json.dumps(given))) - known - {"run_id"})
    ]
    if not any(entry.get("resource") for entry in dependencies):
        found.append(
            "peer: the case declares no dependency resource; the render writes the "
            "peer beside the dependency resources that the test creates"
        )
    return found


def _unknown(given: dict[str, Any], create: dict[str, Any], prefix: str = "") -> list[str]:
    """The keys of `given` that `create` does not hold, at every level where
    both hold a mapping."""
    found = []
    for key, value in given.items():
        if key not in create:
            found.append(prefix + str(key))
        elif isinstance(value, dict) and isinstance(create[key], dict):
            found += _unknown(value, create[key], f"{prefix}{key}.")
    return found


def create_checks(case: dict[str, Any]) -> list[str]:
    """The create step's checks of the peer: the NCP has it, and its id is
    kept for the destroy check. Its values and its readiness are not checked:
    the peer is the means, and the resource under test is the subject."""
    operation = str(((case.get("verification") or {}).get("create") or {}).get("operation") or "")
    peer = json.dumps(address(case))
    lines = [f'{INDENT}checkRead({peer}, {json.dumps(operation)}, "", nil, nil),'] if operation else []
    return [*lines, f"{INDENT}rememberID({peer}, &{ID_VARIABLE}),"]


def teardown_check(case: dict[str, Any], subject: str, operation: str, identifier: str, expect: str) -> str:
    """The teardown step's check: the subject's destroy, and the peer's, read
    the same way."""
    arguments = ", ".join(json.dumps(text) for text in (operation, identifier, expect))
    peer = f"checkGoneByID({json.dumps(address(case))}, &{ID_VARIABLE}, {arguments})"
    return f"resource.ComposeAggregateTestCheckFunc(\n{INDENT}{subject.removesuffix(',')},\n{INDENT}{peer},\n\t\t\t\t),"
