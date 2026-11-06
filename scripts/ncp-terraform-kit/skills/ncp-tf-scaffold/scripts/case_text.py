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
"""Case text that names a file.

The replay render reads `<directory>/<cassette>.yaml`, so a name with a
separator or a dot could name a file outside the directory.
"""

from __future__ import annotations

import re
from typing import Any

CASSETTE_NAME = re.compile(r"[a-z0-9_-]+")


def refusals(case: dict[str, Any]) -> list[str]:
    broken = []
    entries = case.get("expected_requests") or []
    for index, entry in enumerate(entries):
        name = entry.get("cassette") if isinstance(entry, dict) else None
        if name is not None and not CASSETTE_NAME.fullmatch(str(name)):
            broken.append(
                f"expected_requests[{index}] names the cassette {name!r}; "
                "a cassette name is lowercase letters, digits, `_` and `-` only"
            )
    return broken
