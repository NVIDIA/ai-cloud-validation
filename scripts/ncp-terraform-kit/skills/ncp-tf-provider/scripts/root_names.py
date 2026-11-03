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
"""The Terraform name of a top-level attribute, where Terraform reserves the name.

`terraform-plugin-framework` refuses a resource schema that declares one of
these names at its root, and then no resource of the provider can be used.
Each script that writes a configuration path applies this one rule, so the
schema, the cases and the sweeper name the attribute the same way.

Pure: no imports beyond the standard library, so each skill can load it.
"""

from __future__ import annotations

#: `ReservedResourceAttributeNames` of `terraform-plugin-framework`, in
#: `internal/fwschema/attribute_name_validation.go`: each is a meta-argument or
#: a block that Terraform reads inside a resource block. The framework checks
#: them only at the root, so a nested attribute keeps its name.
RESERVED = frozenset(
    {
        "connection",
        "count",
        "depends_on",
        "for_each",
        "lifecycle",
        "provider",
        "provisioner",
    }
)

#: No converted name ends with an underscore, so the new name meets no other
#: field. A loose name match, which ignores underscores, still finds the API
#: field.
MARK = "_"


def accepted(name: str) -> str:
    """The name a top-level attribute declares, for its converted `name`."""
    return f"{name}{MARK}" if name in RESERVED else name


def at_root(path: str) -> str:
    """A dotted configuration path with the rule applied to its first segment."""
    first, dot, rest = path.partition(".")
    return f"{accepted(first)}{dot}{rest}"
