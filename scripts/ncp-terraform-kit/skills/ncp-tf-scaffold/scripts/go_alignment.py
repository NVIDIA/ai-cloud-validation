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
"""Where gofmt starts a new alignment section in a composite literal.

gofmt aligns the values of consecutive single-line `key: value` entries into
one column. go/printer's `exprList` ends that column before an entry when the
key size and the key sizes of the section so far differ too much. The rule
reads key sizes only, so the kit applies it without the Go toolchain at render
time.
"""

from __future__ import annotations

import math

#: go/printer `exprList` `smallSize`: two consecutive keys of this size or
#: less stay in one section, whatever the ratio.
SMALL_KEY = 40
#: go/printer `exprList` `r`: a key whose size is this many times the geometric
#: mean of the section's key sizes, or this many times smaller, starts a new
#: section.
KEY_RATIO = 2.5


def starts_section(section: list[int], size: int) -> bool:
    """Whether a single-line entry with a key of `size` characters starts a new
    section after the single-line entries whose key sizes are `section`.

    A key size counts the key as written, with its quotes and without the colon.
    """
    if not section or (section[-1] <= SMALL_KEY and size <= SMALL_KEY):
        return False
    mean = math.exp(sum(math.log(n) for n in section) / len(section))
    ratio = size / mean
    return KEY_RATIO * ratio <= 1 or KEY_RATIO <= ratio
