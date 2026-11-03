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
"""Field names that Go cannot hold.

The agent copies `api_name` into struct tags and string literals. A quote, a
backtick, a backslash, whitespace or a control character ends the tag or the
literal, and the rest of the name becomes code in a file that `go test` runs.
"""

from __future__ import annotations

from typing import Any


def _unholdable(name: str) -> bool:
    return any(c in "\"'`\\" or c.isspace() or not c.isprintable() for c in name)


def unholdable(node: Any) -> tuple[str, str] | None:
    """The first property or parameter name of a document that Go cannot hold,
    with its kind, or None. Every property and parameter is read, so no path
    that turns a name into an `api_name` skips the check."""
    if isinstance(node, list):
        return next((found for item in node if (found := unholdable(item))), None)
    if not isinstance(node, dict):
        return None
    for key, value in node.items():
        if key in ("properties", "parameters") and isinstance(value, dict):
            for name in value:
                if isinstance(name, str) and _unholdable(name):
                    return "field", name
        elif key == "parameters" and isinstance(value, list):
            for item in value:
                name = item.get("name") if isinstance(item, dict) else None
                if isinstance(name, str) and _unholdable(name):
                    return "parameter", name
        if found := unholdable(value):
            return found
    return None
