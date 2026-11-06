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
"""The Terraform name of a field that an API document names.

The provider schema declares these names, and Terraform loads the provider
schema. Each script of the kit that names a document's field in a
configuration, a case or a check uses this one conversion, so it names an
attribute that the provider declares.

Pure: no imports beyond the standard library and `root_names`, so each skill
can load it.
"""

from __future__ import annotations

import re

import root_names

#: Two characters meeting where one word ends and another begins: the `I` in
#: `roleId`, and the `P` in `workloadIdentityPoolId`. A capital and a lowercase
#: run that a digit follows end an acronym, as the `Pv4` of `IPv4` does, so no
#: word starts there.
_WORD_BREAK = (re.compile(r"(.)([A-Z][a-z]+)(?![a-z0-9])"), re.compile(r"([a-z0-9])([A-Z])"))


def terraform_name(api_name: str) -> str:
    """The attribute name Terraform will accept for a field the API named.

    `terraform-plugin-framework` takes lowercase letters, digits and
    underscores, and refuses anything else when it validates the schema. Every
    document this kit reads names its fields in camelCase, so a schema emitted
    verbatim produces a provider that cannot start. The conversion is
    mechanical, and stating it once here is what lets a case and a provider
    agree on `included_permissions` without either of them guessing.
    """
    text = api_name
    for pattern in _WORD_BREAK:
        text = pattern.sub(r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def root_name(api_name: str) -> str:
    """The name a top-level attribute declares for a field the API named."""
    return root_names.accepted(terraform_name(api_name))


def configuration_path(api_path: str) -> str:
    """A document's dotted field path as a configuration writes it, from the
    resource's top level."""
    return root_names.at_root(".".join(terraform_name(part) for part in api_path.split(".")))
