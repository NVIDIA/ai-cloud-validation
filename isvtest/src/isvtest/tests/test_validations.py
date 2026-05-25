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

import sys
from difflib import get_close_matches
from typing import TYPE_CHECKING, Any

import pytest

from isvtest.config.constants import RESOLVED_ENTRIES_FLAG
from isvtest.config.loader import ConfigLoader
from isvtest.core.discovery import discover_all_tests
from isvtest.core.resolution import ADAPTER_HANDLED_CATEGORIES, resolve_class_key
from isvtest.core.runners import LocalRunner
from isvtest.core.validation import BaseValidation, get_validation_labels, get_validation_markers
from isvtest.release_manifest import INCLUDE_UNRELEASED_ENV, load_released_test_filter

if TYPE_CHECKING:
    from isvtest.testing.subtests import SubTests

# In-memory storage for validation results (used by isvctl integration)
# This allows capturing detailed results without temp files
_validation_results: list[dict[str, Any]] = []


def get_validation_results() -> list[dict[str, Any]]:
    """Get captured validation results from the last pytest run."""
    return _validation_results.copy()


def clear_validation_results() -> None:
    """Clear captured validation results before a new pytest run."""
    _validation_results.clear()


def _pytest_marks_for_validation(
    config: pytest.Config,
    validation_class: type[BaseValidation],
) -> list[Any]:
    """Return pytest marks for a validation's labels and legacy markers."""
    labels = get_validation_labels(validation_class)
    markers = get_validation_markers(validation_class)
    mark_names = tuple(dict.fromkeys([*markers, *labels]))
    for mark_name in mark_names:
        config.addinivalue_line("markers", f"{mark_name}: Validation label")
    return [getattr(pytest.mark, mark_name) for mark_name in mark_names]


def _resolve_validation_class(
    validation_name: str,
    test_classes_map: dict[str, type[BaseValidation]],
) -> type[BaseValidation] | None:
    """Resolve a configured validation name to a discovered validation class."""
    key = resolve_class_key(validation_name, test_classes_map)
    return test_classes_map[key] if key is not None else None


def _is_released_validation(
    validation_name: str,
    validation_config: Any,
    target_class: type[BaseValidation],
    released_tests: set[str],
) -> bool:
    """Return whether a configured validation is allowed by the release manifest."""
    if validation_name in released_tests:
        return True

    return (
        target_class.__name__ in released_tests
        and isinstance(validation_config, dict)
        and "_category" in validation_config
        and validation_name.startswith(f"{target_class.__name__}-")
    )


def _suggest_similar_tests(name: str, available: list[str], max_suggestions: int = 3) -> list[str]:
    """Find test names similar to the given name."""
    # Try difflib for fuzzy matching
    matches = get_close_matches(name, available, n=max_suggestions, cutoff=0.4)
    if matches:
        return matches

    # Fallback: find tests with matching prefix (e.g., "Bm" -> BmDriverVersion, BmCudaVersion)
    prefix = ""
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            prefix = name[:i]
            break
    if prefix:
        prefix_matches = [t for t in available if t.startswith(prefix)][:max_suggestions]
        if prefix_matches:
            return prefix_matches

    return []


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Generate tests for all BaseValidation and BaseWorkloadCheck subclasses."""
    if "validation_class" in metafunc.fixturenames and "validation_config" in metafunc.fixturenames:
        test_classes = list(discover_all_tests())

        if not test_classes:
            return

        # Load configuration if available
        enabled_validations_config = {}
        cluster_inventory: dict[str, Any] = {}
        filtering_enabled = False
        resolution_preapplied = False
        show_skipped = False
        released_tests: set[str] | None = None

        try:
            config_file_arg = metafunc.config.getoption("--config", default=None)
            inventory_arg = metafunc.config.getoption("--inventory", default=None)

            if config_file_arg:
                filtering_enabled = True
                loader = ConfigLoader()
                cluster_config = loader.load_cluster_config(
                    config_file=config_file_arg,
                    inventory_path=inventory_arg,
                )
                # Extract inventory - contains dynamic info about resources created during setup
                # (e.g., instance IPs, SSH keys) needed by remote validations like AWS EC2
                cluster_inventory = cluster_config.get("inventory", {})
                resolution_preapplied = bool(cluster_config.get(RESOLVED_ENTRIES_FLAG))
                # Get all validation categories, excluding adapter-handled ones
                all_categories = list((cluster_config.get("validations") or {}).keys())
                standard_categories = [c for c in all_categories if c not in ADAPTER_HANDLED_CATEGORIES]
                enabled_validations_config = loader.get_all_validations(cluster_config, categories=standard_categories)
                # Check if we should show skipped tests
                show_skipped = cluster_config.get("settings", {}).get("show_skipped_tests", False)
        except (ImportError, FileNotFoundError, ValueError, AttributeError, OSError):
            pass

        if not resolution_preapplied:
            released_tests = load_released_test_filter()
            if released_tests is None:
                sys.stderr.write(
                    f"\n\033[33mInfo:\033[0m Including unreleased validations because {INCLUDE_UNRELEASED_ENV} is enabled.\n"
                )

        # Create parameters with markers
        params = []
        ids = []

        # Map class names to classes for easier lookup
        test_classes_map = {cls.__name__: cls for cls in test_classes}
        configured_classes: set[str] = set()
        unmatched_validations: list[str] = []

        if filtering_enabled:
            # 1. Process configured validations (including aliases/variants)
            for validation_name, validation_config in enabled_validations_config.items():
                target_class = _resolve_validation_class(validation_name, test_classes_map)
                if target_class is not None:
                    configured_classes.add(target_class.__name__)

                if target_class is not None and released_tests is not None and not resolution_preapplied:
                    if not _is_released_validation(validation_name, validation_config, target_class, released_tests):
                        sys.stderr.write(
                            f"\n\033[33mInfo:\033[0m Skipping unreleased validation: '{validation_name}'.\n"
                        )
                        continue

                if target_class:
                    pytest_marks = _pytest_marks_for_validation(metafunc.config, target_class)

                    # Merge inventory into validation config for validations that need it.
                    # Most validations (k8s, slurm, bare_metal) run commands locally and don't need inventory.
                    # Remote validations (e.g., AWS EC2) need inventory to get connection info
                    # (instance IP, SSH key path) for resources created during setup.
                    merged_config = {**validation_config, "inventory": cluster_inventory}
                    params.append(pytest.param(target_class, merged_config, validation_name, marks=pytest_marks))
                    ids.append(validation_name)
                else:
                    # Track unmatched validations for warning
                    unmatched_validations.append(validation_name)

            # Warn about configured validations that don't match any test class
            if unmatched_validations:
                available_tests = sorted(test_classes_map.keys())
                for validation_name in unmatched_validations:
                    similar = _suggest_similar_tests(validation_name, available_tests)
                    if similar:
                        hint = f"Did you mean: {', '.join(similar)}?"
                    else:
                        hint = "Check spelling or run without --config to see all available tests."
                    sys.stderr.write(f"\n\033[33mWarning:\033[0m Validation not found: '{validation_name}'. {hint}\n")

            # 2. Add skipped tests for classes NOT in config (if show_skipped)
            if show_skipped and not resolution_preapplied:
                for cls_name, cls in test_classes_map.items():
                    if released_tests is not None and cls_name not in released_tests:
                        continue

                    # If class was not configured by exact or variant match, treat it as skipped.
                    if cls_name not in configured_classes:
                        pytest_marks = _pytest_marks_for_validation(metafunc.config, cls)
                        pytest_marks.append(pytest.mark.skip(reason="Not configured in config YAML"))

                        # Include inventory even for skipped tests (for consistency)
                        merged_config = {"inventory": cluster_inventory}
                        params.append(pytest.param(cls, merged_config, cls_name, marks=pytest_marks))
                        ids.append(cls_name)
        else:
            # No filtering, run all discovered tests with empty config
            for cls in test_classes:
                if released_tests is not None and cls.__name__ not in released_tests:
                    continue

                pytest_marks = _pytest_marks_for_validation(metafunc.config, cls)
                params.append(pytest.param(cls, {"inventory": cluster_inventory}, cls.__name__, marks=pytest_marks))
                ids.append(cls.__name__)

        # Parametrize the test function with the discovered classes
        # Added validation_name to the parametrization to support variants
        if params:
            metafunc.parametrize("validation_class,validation_config,validation_name", params, ids=ids)
        else:
            # If no validations are configured/enabled, we must still parametrize arguments
            # to avoid "fixture not found" errors. We use a specific ID that will be
            # filtered out by pytest_collection_modifyitems in conftest.py.
            metafunc.parametrize(
                "validation_class,validation_config,validation_name",
                [(BaseValidation, {}, "no_validations")],
                ids=["NO_VALIDATIONS"],
            )


def test_validation(
    validation_class: type[BaseValidation],
    validation_config: dict[str, Any],
    validation_name: str,
    subtests: "SubTests",
) -> None:
    """Run an ISV validation test.

    Args:
        validation_class: The validation class to instantiate and run.
        validation_config: Configuration dictionary for the validation (includes inventory).
        validation_name: Display name for the validation (may include variant suffix).
        subtests: Subtests fixture for reporting nested test results.
    """
    # All validations use LocalRunner - they run commands on the local host
    # (even Kubernetes validations just run kubectl commands from outside the cluster)
    runner = LocalRunner()

    # Instantiate the validation (inventory is included in validation_config)
    validation = validation_class(runner=runner, config=validation_config)
    # Override name to match the configuration key (e.g. for variants like ValidationName-Variant)
    validation.name = validation_name

    # Inject subtests fixture for nested test reporting
    validation._subtests = subtests

    # Run the validation, capturing skips so they appear in the orchestration summary
    category = validation_config.get("_category", "")
    try:
        result = validation.execute()
    except pytest.skip.Exception as exc:
        skip_reason = str(exc)
        _validation_results.append(
            {
                "name": validation_name,
                "passed": True,
                "skipped": True,
                "message": skip_reason,
                "category": category,
                "duration": 0.0,
            }
        )
        raise

    _validation_results.append(
        {
            "name": validation_name,
            "passed": result["passed"],
            "skipped": False,
            "message": result["output"] if result["passed"] else result["error"],
            "category": category,
            "duration": result.get("duration", 0.0),
            "error_reason": result.get("error_reason"),
        }
    )

    assert result["passed"], f"Validation failed: {result['error']}\nOutput: {result['output']}"
