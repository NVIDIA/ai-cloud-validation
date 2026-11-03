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
"""The next step after a render that wrote the provider's entry point, for stderr."""

from __future__ import annotations

import json
import os
from pathlib import Path

import provider_settings

#: The entry point's mode that needs no account. `provider_runner` accepts it.
OFFLINE_MODE = "offline"
#: The column of `supplied_rows` and `optional_rows` that holds the variable.
VARIABLE = 1
#: The provider's Go module file. The entry point's offline mode runs `go vet
#: ./...` in `--runner-out`, which fails while this file does not exist.
PROVIDER_MODULE = "go.mod"


def text(
    runner_out: Path,
    script: Path,
    run_variables: Path,
    export: Path,
    required_rows: list[tuple[str, str, str]],
    optional_rows: list[tuple[str, str, str]],
) -> str:
    """What the operator does next, and what the agent says and asks.

    The values are the rows that `.env.example` and `RUN_VARIABLES.md` are
    written from, so the three cannot disagree. Required values and provider
    settings are named, because the agent asks for each one by name. Optional
    values are counted: each may stay empty, and `RUN_VARIABLES.md` names them
    with their cases.
    """
    command = f"./{Path(os.path.relpath(script, runner_out)).as_posix()} {OFFLINE_MODE}"
    table = Path(os.path.relpath(run_variables, runner_out)).as_posix()
    required = sorted({row[VARIABLE] for row in required_rows})
    optional = sorted({row[VARIABLE] for row in optional_rows})
    settings = (
        [name for name, _ in provider_settings.settings(json.loads(export.read_text(encoding="utf-8")))]
        if export.is_file()
        else []
    )
    named = ", ".join(required) if required else "none"
    provider = f" The provider settings in .env.example: {', '.join(settings)}." if settings else ""
    start = f"`cp .env.example .env`, then `{command}`"
    # A render at the scaffold step comes before the provider, and the offline
    # run builds the provider, so it can only fail there.
    if (runner_out / PROVIDER_MODULE).is_file():
        first = f"In {runner_out}, the operator runs {start}; the offline run reads no value."
    else:
        first = (
            f"{runner_out} holds no provider ({PROVIDER_MODULE}), and the offline run "
            f"builds it, so the next step is the provider. Do not run the offline "
            f"tier now. After the provider exists, the operator runs {start} in "
            f"{runner_out}; the offline run reads no value."
        )
    # The agent reads this text just before it reports. A summary that names
    # no actor leaves `.env` out of the report, and the operator who reads only
    # the report does not know the next step. A run without a value stops
    # before it creates anything; a guessed one builds against the wrong
    # resource.
    return (
        f"\nNext: the agent tells the operator this step in its answer. {first} "
        f"A live run needs .env to hold {len(required)} required "
        f"value(s): {named}.{provider} {len(optional)} optional value(s) name an existing "
        f"dependency resource; each may stay empty, and then each case creates its "
        f"dependency resource. {table} lists each value with its case. "
        f"Before a live run, ask the operator for each value by name. "
        f"Never guess a value, and never print one."
    )
