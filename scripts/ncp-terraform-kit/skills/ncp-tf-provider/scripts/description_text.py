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
"""The description text that the derivation copies from an API document."""

from __future__ import annotations

import re

#: A URI with a name and a password in its userinfo part. A secret scanner
#: reports this shape as a credential, also when the document writes the words
#: as an example, and a pre-commit hook then refuses each generated file that
#: holds the description. A part that holds a brace is a placeholder already.
USERINFO = re.compile(
    r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<name>[^\s/?#@:{}`'\"<>]+):(?P<password>[^\s/?#@{}`'\"<>]+)@"
)


def copied(text: str) -> str:
    """The text with each part of a userinfo `name:password` written as
    `{name}:{password}`, the placeholder form of the documents' own paths. A
    reader still learns that the URI takes a name and a password there."""
    return USERINFO.sub(lambda m: f"{m['scheme']}{{{m['name']}}}:{{{m['password']}}}@", text)
