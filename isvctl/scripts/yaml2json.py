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

# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pyyaml>=6.0.2,<7.0.0",
# ]
# ///
"""Convert YAML file to JSON output.

Usage:
    uv run yaml2json.py <yaml_file>

Outputs JSON to stdout. Used as fallback when yq is not available.
"""

import json
import sys

import yaml


def main() -> int:
    """CLI entry point to convert a YAML file to JSON on stdout."""
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <yaml_file>", file=sys.stderr)
        return 1

    yaml_file = sys.argv[1]

    # Load YAML file
    try:
        with open(yaml_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Error: File not found: {yaml_file}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Error: Cannot read file: {e}", file=sys.stderr)
        return 1
    except yaml.YAMLError as e:
        print(f"Error: Invalid YAML: {e}", file=sys.stderr)
        return 1

    # Handle empty YAML (safe_load returns None)
    if data is None:
        print("{}")
        return 0

    # Serialize to JSON (use default=str to handle datetime and other types)
    try:
        print(json.dumps(data, indent=2, default=str))
        return 0
    except (TypeError, ValueError) as e:
        print(f"Error: Cannot serialize to JSON: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
