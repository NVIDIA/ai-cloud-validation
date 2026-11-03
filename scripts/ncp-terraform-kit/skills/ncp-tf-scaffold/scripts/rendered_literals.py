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
"""A case value as a literal in rendered Terraform and Go.

A case is text that a person or an agent wrote. In an HCL string, `${` and
`%{` open a template that Terraform evaluates, and a template can call
`file()`. In a Go raw string, a backtick ends the literal, and what follows it
is Go code. So each value is escaped where it lands.
"""

from __future__ import annotations

import json
import re

#: The one template that the kit writes into a string itself: `{run_id}` and
#: each supplied placeholder become `${var.<name>}`. A Terraform variable holds
#: a value and calls nothing, so this form stays a reference.
_KIT_REFERENCE = re.compile(r"\$\$(\{var\.[a-z_][a-z0-9_]*\})")


def template_escaped(value: str) -> str:
    """`value` with each `${` and `%{` doubled, which is how HCL spells the brace."""
    return value.replace("${", "$${").replace("%{", "%%{")


def hcl_value(value: str) -> str:
    """A string as an HCL literal: a template in it is text, and a kit reference is a reference."""
    return json.dumps(_KIT_REFERENCE.sub(r"$\1", template_escaped(value)))


def go_raw(text: str, *, argument: bool = False) -> str:
    """`text` as a Go string expression of raw literals.

    A raw literal cannot hold a backtick, so each backtick is a quoted string
    between two raw literals. The joined constants are still one format
    string to `fmt.Sprintf`. gofmt writes no space around `+` in an argument
    of a call with more than one argument, and a space elsewhere.
    """
    plus = "+" if argument else " + "
    return "`" + text.replace("`", f'`{plus}"`"{plus}`') + "`"
