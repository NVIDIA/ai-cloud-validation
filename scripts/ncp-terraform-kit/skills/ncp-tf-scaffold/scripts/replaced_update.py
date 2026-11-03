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
"""Whether the provider replaces a field that the case's update owes in place.

`update_fields.change` names each field that a planned update or update call
carries at its path, so the plan changes it in place. The provider's schema
export states a replacement in the attribute's description, with the sentence
that the provider's derivation writes. A provider that replaces the object for
such a change destroys it and its data where the update would have kept it,
so the pair is refused. The export shows no plan modifier, so a replacement
whose description lacks the sentence is not seen here.
"""

from __future__ import annotations

from typing import Any

import case_identity
import fixture_values
from replacement import SENTENCE


def _replacing(attributes: dict[str, Any], segments: list[str]) -> str:
    """The path of the first attribute on the way to the field that says a
    change replaces the object, or "" when none does or the export stops."""
    walked: list[str] = []
    for segment in segments:
        attribute = attributes.get(segment)
        if not isinstance(attribute, dict):
            return ""
        walked.append(segment)
        if SENTENCE in str(attribute.get("description") or ""):
            return ".".join(walked)
        attributes = (attribute.get("nested_type") or {}).get("attributes") or {}
    return ""


def refusals(case: dict[str, Any], export: dict[str, Any]) -> list[str]:
    """One refusal for each attribute that replaces a field the update owes."""
    if case.get("contract") != "resource":
        return []
    terraform_type = case_identity.terraform_type(case)
    attributes = fixture_values.resource_block(export, terraform_type).get("attributes") or {}
    owed: dict[str, list[str]] = {}
    for field in (case.get("update_fields") or {}).get("change") or []:
        if replaced := _replacing(attributes, str(field).split(".")):
            owed.setdefault(replaced, []).append(f"`{field}`")
    return [
        f"update_fields.change owes {', '.join(fields)} an in-place update, and {terraform_type}'s "
        f"`{replaced}` in the provider's schema export forces a new resource for that change. A "
        f"planned update carries the field, and a wrong replacement destroys the object and its data. "
        f"When no document says that a change of the field replaces the object, remove the "
        f"replacement from the provider. When a document says so, quote it to the operator, ask for "
        f"a `replaces` field override in the plan, and derive the cases again"
        for replaced, fields in owed.items()
    ]
