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

"""Validate the entries in ``isvctl/configs/providers-registry/``.

Each entry must match ``isvctl/schemas/provider-registry.schema.json``, be named
after its file, and declare only suites that exist in ``isvctl/configs/suites/``.
This is offline: it does not check that the pinned commit can be fetched.

Usage:
    python3 scripts/validate_provider_registry.py
    python3 scripts/validate_provider_registry.py --check   # exit 1 on violations
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from isvctl.config.provider_registry import ProviderRegistryError, load_registry
from isvctl.config.suite_resolution import CONFIGS_ROOT


def main(argv: list[str] | None = None, configs_root: Path = CONFIGS_ROOT) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 if any registry entry is invalid.")
    args = parser.parse_args(argv)

    try:
        entries = load_registry(configs_root)
    except ProviderRegistryError as exc:
        if args.check:
            sys.stderr.write(f"{exc}\n")
            return 1
        print(exc)
        return 0

    print(f"OK: {len(entries)} provider registry entries are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
