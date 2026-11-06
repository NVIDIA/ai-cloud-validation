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
"""The fields that only the read's response states, which the API assigns.

Terraform refreshes state from the read, so the read's answer is what state
holds. A create can answer with a confirmation alone, such as the identifiers
it made, and then states none of the resource's own fields. A field that the
read states, and that no request sends and no create answer states, is one the
API assigns.

Pure: plain data in, plain data out. The caller expands the read's response.
"""

from __future__ import annotations

from typing import Any

import terraform_flags


def at_the_top(attributes: list[Any], wrapper: str) -> set[str]:
    """The names the attributes hold where the read's fields sit.

    The read answers with the resource at its top, so a field deeper in, such
    as the `id` of an attached image, is another field. A create that wraps its
    payload under one field holds the resource's fields inside it.
    """
    found = {a.name for a in attributes}
    for attribute in attributes:
        if attribute.name == wrapper:
            found |= {child.name for child in attribute.attributes}
    return found


def only_the_read_states(
    operation: str,
    response: list[Any],
    response_notes: list[str],
    present: set[str],
    parameters: set[str],
) -> tuple[list[Any], list[str]]:
    """The read's fields that no attribute has yet, and the notes that explain them.

    `response` is the read's response, expanded. `present` holds the names that
    `at_the_top` gives. A field named like a create parameter is the
    parameter's, as for the create's answer.
    """
    response, _ = terraform_flags.inside_envelope(response, present, operation)
    fresh = [a for a in response if a.name not in present and a.name not in parameters]
    if not fresh:
        return [], []
    names = ", ".join(a.name for a in fresh)
    return fresh, [
        *response_notes,
        (
            f"{operation} states {len(fresh)} fields that no create answer states ({names}); "
            f"the read fills them, so each is a computed attribute"
        ),
    ]
