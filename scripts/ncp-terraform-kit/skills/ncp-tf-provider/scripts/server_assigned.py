# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
"""Whether an API document marks a field as one the server assigns.

The provider schema declares such a field Computed, and Terraform refuses a
configuration that sets it. Each script of the kit that decides whether a
request may set a field uses this one rule, so it never writes a value into a
field that the provider declares Computed.

Pure: no imports beyond the standard library, so each skill can load it.
"""

from __future__ import annotations

import re
from typing import Any

#: One document spells this marker more than one way -- a sentence prefix, a
#: bracketed tag, a trailing parenthetical -- and two schemas in the same API can
#: differ. Match every form: matching one silently reports a schema with no
#: computed fields, which looks like a valid result.
#:
#: The marker must be a clause of its own, so it is anchored on both sides. A
#: field is output-only when the marker opens the description or follows a
#: sentence or bracket, and closes at one. `[Output Only] the ID` means the field
#: is server-assigned; `for organization policies it is an [Output Only] ID` and
#: `[Output only for type PARTNER. Input only for ...]` qualify a case rather
#: than the field, and a provider that made either one read-only would refuse a
#: value the API accepts.
OUTPUT_ONLY = re.compile(r"(?:^|[.;:(]\s*)\[?output[ _-]?only\s*(?:[.)\]]|$)", re.IGNORECASE)

#: A second way a document says the same thing. Google's API guidance marks the
#: field holding a resource's own name `Identifier.` rather than `Output only.`:
#: the server assigns it, and a caller that sends one is ignored or refused.
#:
#: Anchored to the opening, because that is where the guidance puts it and
#: because the word appears in ordinary prose elsewhere -- "the identifier of the
#: pool" describes a field without claiming anything about who sets it.
IDENTIFIER = re.compile(r"^\[?identifier\s*[.)\]]", re.IGNORECASE)

#: A third way: Cloud Storage sets no `readOnly` flag and opens the description
#: of a time the server decides with "Server-determined value".
#:
#: Anchored to the opening, as `IDENTIFIER` is: a description that names "the
#: server-determined policy" later in a sentence can describe a field that the
#: caller sets.
SERVER_DETERMINED = re.compile(r"^server[- ]?determined\b", re.IGNORECASE)


def is_output_only(description: str | None) -> bool:
    """Whether the description marks this property as server-assigned.

    Searched, not anchored to the front. A document that wraps its descriptions
    puts the marker wherever the line break falls, and several write it as a
    closing parenthetical.
    """
    text = (description or "").strip()
    return bool(OUTPUT_ONLY.search(text)) or bool(IDENTIFIER.match(text)) or bool(SERVER_DETERMINED.match(text))


def is_server_assigned(prop: Any) -> bool:
    """Whether the document marks this property as server-assigned: the OpenAPI
    `readOnly` flag, or a marker in the description, where Discovery has no
    flag to set."""
    if not isinstance(prop, dict):
        return False
    return prop.get("readOnly") is True or is_output_only(prop.get("description"))
