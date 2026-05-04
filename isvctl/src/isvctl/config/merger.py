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

"""YAML configuration merging utilities.

This module provides deep-merge functionality for combining multiple YAML
configuration files, similar to Helm's --values flag behavior.

Later files override earlier ones. The --set flag can override individual values.

Files may declare an ``import:`` key with a list of paths. The
``_resolve_import_path`` helper resolves imports relative to the importing file,
then searches the current working directory and its parents before falling back
to the current working directory. Imported files are loaded and merged as a base
before the importing file's own content is applied on top.
"""

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dictionaries.

    Values from `override` take precedence. Nested dicts are merged recursively.
    Lists are replaced entirely (not concatenated).

    Args:
        base: Base dictionary
        override: Dictionary with values to override

    Returns:
        Merged dictionary (new object, inputs not modified)
    """
    result = copy.deepcopy(base)

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Recursively merge nested dicts
            result[key] = deep_merge(result[key], value)
        else:
            # Override with new value (including None)
            result[key] = copy.deepcopy(value)

    return result


def parse_set_value(set_string: str) -> tuple[list[str], Any]:
    """Parse a --set value string into path and value.

    Supports dotted paths like 'context.node_count=8'.
    Values are parsed as YAML to support types (int, bool, list, etc.).

    Args:
        set_string: String in format 'key.path=value'

    Returns:
        Tuple of (path parts, parsed value)

    Raises:
        ValueError: If string format is invalid
    """
    if "=" not in set_string:
        raise ValueError(f"Invalid --set format: '{set_string}'. Expected 'key=value' or 'key.path=value'")

    key_path, value_str = set_string.split("=", 1)
    if not key_path:
        raise ValueError(f"Invalid --set format: '{set_string}'. Expected non-empty 'key=value' or 'key.path=value'")
    path_parts = key_path.split(".")

    # Parse value as YAML to handle types
    try:
        value = yaml.safe_load(value_str)
    except yaml.YAMLError:
        # Fall back to string if YAML parsing fails
        value = value_str

    return path_parts, value


def apply_set_value(config: dict[str, Any], path_parts: list[str], value: Any) -> None:
    """Apply a single --set value to a config dict (in-place).

    Args:
        config: Configuration dictionary to modify
        path_parts: List of keys representing the path (e.g., ['context', 'node_count'])
        value: Value to set
    """
    current = config
    for part in path_parts[:-1]:
        if part not in current:
            current[part] = {}
        elif not isinstance(current[part], dict):
            # Overwrite non-dict with empty dict
            current[part] = {}
        current = current[part]

    current[path_parts[-1]] = value


def _load_yaml_with_imports(
    path: Path,
    _visited: set[str] | None = None,
) -> dict[str, Any]:
    """Load a YAML file and recursively resolve its ``import:`` directives.

    Imported files are merged in order to form a base, then the importing
    file's own content is deep-merged on top. Paths in ``import:`` are resolved
    by ``_resolve_import_path`` relative to the importing file's directory, then
    by searching the current working directory and its parents before falling
    back to the current working directory.

    Args:
        path: Path to the YAML file.
        _visited: Tracks resolved paths to detect circular imports.

    Returns:
        Merged dictionary with ``import`` key stripped.

    Raises:
        FileNotFoundError: If the file or an imported file doesn't exist.
        ValueError: If a circular import is detected.
    """
    if _visited is None:
        _visited = set()

    resolved = str(path.resolve())
    if resolved in _visited:
        raise ValueError(f"Circular import detected: {path}")
    _visited.add(resolved)

    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, encoding="utf-8") as f:
        content = yaml.safe_load(f)

    if content is None:
        return {}
    if not isinstance(content, dict):
        raise ValueError(f"Configuration file must contain a YAML mapping, not {type(content).__name__}: {path}")

    import_list = content.pop("import", None)
    if not import_list:
        return content

    if not isinstance(import_list, list):
        import_list = [import_list]

    # Merge all imported files to form the base.
    # Pass a copy of _visited to each sibling so diamond dependencies
    # (two imports sharing a common base) are not falsely flagged as cycles.
    # Cycle detection still works because each recursive call sees its own
    # ancestors (the copy includes everything on the current call stack).
    base: dict[str, Any] = {}
    for imp in import_list:
        imp_path = _resolve_import_path(path, imp)
        logger.debug("Resolving import %s -> %s", imp, imp_path)
        imported = _load_yaml_with_imports(Path(imp_path), _visited.copy())
        base = deep_merge(base, imported)

    # The importing file's content wins over the base
    return deep_merge(base, content)


def _resolve_import_path(path: Path, imp: Any) -> Path:
    """Resolve an import path.

    Prefer paths relative to the importing file. If that does not exist, fall
    back to the current working directory or one of its parents so out-of-tree
    provider configs can import validation-suite files using checkout-root-relative
    paths even when commands run from a package subdirectory.
    """
    if not isinstance(imp, (str, Path)):
        raise ValueError(
            f"Import entries must be strings or paths in {path}, got {type(imp).__name__}",
        )

    expanded = Path(imp).expanduser()

    file_relative = (path.parent / expanded).resolve()
    if file_relative.exists():
        return file_relative

    if expanded.is_absolute():
        return expanded.resolve()

    cwd = Path.cwd().resolve()
    for root in (cwd, *cwd.parents):
        candidate = root / expanded
        if candidate.exists():
            return candidate.resolve()

    return (cwd / expanded).resolve()


def merge_yaml_files(
    file_paths: list[str | Path],
    set_values: list[str] | None = None,
) -> dict[str, Any]:
    """Merge multiple YAML files with optional --set overrides.

    Files are merged in order - later files override earlier ones.
    Each file may contain an ``import:`` key listing other YAML files
    to load as a base. Imports are resolved relative to the importing file,
    then by searching the current working directory and its parents before
    falling back to the current working directory.
    --set values are applied after all files are merged.

    Args:
        file_paths: List of paths to YAML files
        set_values: Optional list of --set strings (e.g., ['context.node_count=8'])

    Returns:
        Merged configuration dictionary

    Raises:
        FileNotFoundError: If a file doesn't exist
        yaml.YAMLError: If YAML parsing fails
    """
    result: dict[str, Any] = {}

    for file_path in file_paths:
        content = _load_yaml_with_imports(Path(file_path))
        if content:
            result = deep_merge(result, content)

    # Apply --set overrides
    if set_values:
        for set_string in set_values:
            path_parts, value = parse_set_value(set_string)
            apply_set_value(result, path_parts, value)

    return result
