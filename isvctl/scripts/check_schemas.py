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

"""Check that JSON schema files are in sync with Pydantic models.

This script compares the committed JSON schema files against freshly generated
schemas from the Python Pydantic models. It can also regenerate the schemas.

Usage (from repo root):
    # Check if schemas are in sync (exits non-zero if out of sync)
    uv --directory=isvctl run python scripts/check_schemas.py

    # Regenerate schema files
    uv --directory=isvctl run python scripts/check_schemas.py --generate
"""

import argparse
import json
import sys
from pathlib import Path


def get_schema_dir() -> Path:
    """Get the path to the schemas directory."""
    return Path(__file__).parent.parent / "schemas"


def generate_schemas() -> dict[str, str]:
    """Generate JSON schemas from Pydantic models.

    Returns:
        Dict mapping filename to generated JSON content.
    """
    from isvctl.config.schema import CommandOutput, RunConfig

    return {
        "config.schema.json": json.dumps(RunConfig.model_json_schema(), indent=2) + "\n",
        "command_output.schema.json": json.dumps(CommandOutput.model_json_schema(), indent=2) + "\n",
    }


def check_schemas() -> bool:
    """Check if committed schemas match generated schemas.

    Returns:
        True if all schemas are in sync, False otherwise.
    """
    schema_dir = get_schema_dir()
    generated = generate_schemas()
    all_in_sync = True

    for filename, expected_content in generated.items():
        schema_path = schema_dir / filename
        if not schema_path.exists():
            print(f"FAIL: {filename} does not exist")
            all_in_sync = False
            continue

        actual_content = schema_path.read_text()
        if actual_content != expected_content:
            print(f"FAIL: {filename} is out of sync")
            all_in_sync = False
        else:
            print(f"OK: {filename} is in sync")

    if not all_in_sync:
        print("\nTo regenerate schemas, run:")
        print("  uv --directory=isvctl run python scripts/check_schemas.py --generate")

    return all_in_sync


def write_schemas() -> None:
    """Write generated schemas to disk."""
    schema_dir = get_schema_dir()
    generated = generate_schemas()

    for filename, content in generated.items():
        schema_path = schema_dir / filename
        schema_path.write_text(content)
        print(f"OK: Generated {filename}")


def main() -> int:
    """Run the schema check or generation."""
    parser = argparse.ArgumentParser(description="Check or generate JSON schemas")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Regenerate schema files instead of checking",
    )
    args = parser.parse_args()

    if args.generate:
        write_schemas()
        return 0
    else:
        return 0 if check_schemas() else 1


if __name__ == "__main__":
    sys.exit(main())
