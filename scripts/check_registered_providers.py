#!/usr/bin/env python3
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

"""Fetch every registered provider and check it against this checkout.

For each entry in ``isvctl/configs/providers-registry/``, except ``deprecated``
ones: fetch the pinned commit, then dry-run each declared suite, which loads and
validates the provider's configs against the current suites without running any
script. ``demo`` entries also run each suite, since their scripts need no
credentials. Needs network access.

Usage:
    uv run python scripts/check_registered_providers.py              # every entry
    uv run python scripts/check_registered_providers.py acme other   # only these
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from isvctl.config.provider_registry import ProviderRegistryError, RegistryEntry, load_registry
from isvctl.config.suite_resolution import CONFIGS_ROOT

ISVCTL = [sys.executable, "-m", "isvctl.main"]


def _commands(entry: RegistryEntry) -> list[list[str]]:
    """Return the isvctl arguments that check one entry, in order."""
    commands = [["provider", "fetch", entry.name]]
    for suite in entry.suites:
        run = ["test", "run", "--provider", entry.name, "--suite", suite]
        commands.append([*run, "--dry-run"])
        if entry.status == "demo":
            commands.append(run)
    return commands


def main(argv: list[str] | None = None, configs_root: Path = CONFIGS_ROOT) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="Entries to check (default: all).")
    args = parser.parse_args(argv)

    try:
        entries = load_registry(configs_root)
    except ProviderRegistryError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    if args.names:
        unknown = sorted(set(args.names) - {entry.name for entry in entries})
        if unknown:
            sys.stderr.write(f"Not in the registry: {', '.join(unknown)}\n")
            return 1
        entries = [entry for entry in entries if entry.name in args.names]

    failed = []
    for entry in entries:
        if entry.status == "deprecated":
            print(f"Skipping {entry.name}: deprecated.")
            continue
        for isvctl_args in _commands(entry):
            print(f"$ isvctl {shlex.join(isvctl_args)}", flush=True)
            if subprocess.run([*ISVCTL, *isvctl_args], check=False).returncode != 0:
                failed.append(entry.name)
                break

    if failed:
        sys.stderr.write(f"FAILED: {', '.join(failed)}\n")
        return 1
    print(f"OK: {len(entries)} registered providers checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
