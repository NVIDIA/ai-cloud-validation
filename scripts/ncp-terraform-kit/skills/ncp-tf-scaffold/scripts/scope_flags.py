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
"""Which scope flags each contract reads, and the refusal for one it does not.

A flag that a run does not read is never checked against the plan, so a typo
in it falls into a run over the whole plan instead of stopping the run.
"""

from __future__ import annotations

import argparse

#: Each scope flag, and the `instantiate` argument that holds it.
FLAGS = {"--all": "all", "--layer": "layer", "--resource": "resource", "--section": "section"}

#: The scope flags a run of each contract reads. A resource run reads them all.
READS = {
    "module": ("--all", "--layer"),
    "composition": ("--all",),
}

#: What the subject of a case is, which is why the other flags select nothing.
SUBJECT = {
    "module": "a module case covers a whole layer",
    "composition": "a composition case covers every buildable layer",
}


def unread(args: argparse.Namespace) -> str | None:
    """The refusal for each given flag that the run's contract does not read, if any."""
    reads = READS.get(args.contract)
    if reads is None:
        return None
    ignored = [flag for flag, name in FLAGS.items() if getattr(args, name) and flag not in reads]
    if not ignored:
        return None
    return (
        f"--contract {args.contract} does not read {', '.join(ignored)}: "
        f"{SUBJECT[args.contract]}. Pass {' or '.join(reads)}."
    )
