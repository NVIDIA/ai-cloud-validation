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

"""Released validation test manifest helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "released_tests.json"
INCLUDE_UNRELEASED_ENV = "ISVTEST_INCLUDE_UNRELEASED"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def release_manifest_path() -> Path:
    """Return the source-tree path used when refreshing the manifest."""
    return Path(__file__).with_name(MANIFEST_FILENAME)


def load_release_manifest(manifest_path: Path | None = None) -> dict[str, Any]:
    """Load the released test manifest.

    Args:
        manifest_path: Optional explicit manifest path. When omitted, reads the
            packaged ``isvtest/released_tests.json`` resource.

    Returns:
        Parsed manifest dictionary.

    Raises:
        FileNotFoundError: If the manifest cannot be found.
        ValueError: If the manifest format is invalid.
    """
    if manifest_path is None:
        try:
            text = files("isvtest").joinpath(MANIFEST_FILENAME).read_text()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"released test manifest not found: {MANIFEST_FILENAME}") from exc
    else:
        text = manifest_path.read_text()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid released test manifest JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("released test manifest must be a JSON object")

    version = data.get("version")
    tests = data.get("tests")
    if not isinstance(version, str) or not version:
        raise ValueError("released test manifest requires non-empty string field 'version'")
    if not isinstance(tests, list) or not all(isinstance(name, str) and name for name in tests):
        raise ValueError("released test manifest requires 'tests' to be a list of non-empty strings")

    return data


def load_released_tests(manifest_path: Path | None = None) -> set[str]:
    """Return the set of released validation names."""
    manifest = load_release_manifest(manifest_path)
    return set(manifest["tests"])


def include_unreleased_tests_enabled() -> bool:
    """Return whether unreleased validations should be included."""
    return os.environ.get(INCLUDE_UNRELEASED_ENV, "").strip().lower() in _TRUE_ENV_VALUES


def load_released_test_filter(manifest_path: Path | None = None) -> set[str] | None:
    """Return released validation names, or None when the release filter is disabled."""
    if include_unreleased_tests_enabled():
        return None
    return load_released_tests(manifest_path)


def write_release_manifest(version: str, tests: Iterable[str], manifest_path: Path | None = None) -> None:
    """Write a deterministic released test manifest.

    Args:
        version: Version these tests are released under.
        tests: Validation names to release.
        manifest_path: Optional output path. Defaults to the source-tree
            manifest next to this module.
    """
    target = manifest_path or release_manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": version,
        "tests": sorted(set(tests)),
    }
    target.write_text(json.dumps(payload, indent=2) + "\n")
