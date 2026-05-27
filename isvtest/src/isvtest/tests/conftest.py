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

"""Global pytest fixtures and configuration for ISV tests."""

import os
from typing import Any

import pytest

from isvtest.config.loader import ConfigLoader
from isvtest.core.logger import setup_logger

# Register our custom subtests plugin - this provides the subtests fixture and hooks
pytest_plugins = ["isvtest.testing.subtests"]

logger = setup_logger()

# Validation labels pre-registered as pytest markers (registered here because
# pyproject.toml isn't shipped with the wheel). Validation classes also
# register their declared labels dynamically via _pytest_marks_for_validation,
# but pre-registering the known set keeps the PytestUnknownMarkWarning quiet
# during collection.
#
# NOTE: Do NOT add new entries here unless absolutely necessary. Prefer
# reusing an existing label on the validation class's `labels` attribute.
#
CUSTOM_MARKERS = [
    # Platform
    "bare_metal: Bare metal node validation tests",
    "kubernetes: Kubernetes cluster validation tests",
    "slurm: Slurm scheduler validation tests",
    "vm: Virtual machine validation tests",
    # Feature
    "gpu: GPU-related tests",
    "iam: IAM identity and access management tests",
    "network: Network and interconnect tests",
    "security: Security-related tests (SG, NACL, IAM)",
    "ssh: Tests requiring SSH access to instances",
    # Test type
    "unit: Unit tests for library code (run in development/CI)",
    "validation: Infrastructure validation tests (run on target systems)",
    "workload: Workload-based validation tests (longer running)",
    # Speed/scope
    "l2: Level 2 extended platform validation tests (longer running, e2e)",
    "slow: Tests that take longer than 5 minutes to run",
]


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers to avoid PytestUnknownMarkWarning."""
    for marker in CUSTOM_MARKERS:
        config.addinivalue_line("markers", marker)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command-line options."""
    parser.addoption(
        "--config",
        action="store",
        default=None,
        help="Path to cluster configuration file",
    )
    parser.addoption(
        "--inventory",
        action="store",
        default=None,
        help="Path to cluster inventory file (JSON or YAML). Can also be set via ISV_INVENTORY_PATH env var.",
    )
    parser.addoption(
        "--platform",
        action="store",
        default=None,
        choices=["bare_metal", "kubernetes", "slurm"],
        help="Override platform detection",
    )
    parser.addoption(
        "--step-outputs",
        action="store",
        default=None,
        help="Path to JSON file containing step outputs from isvctl orchestration",
    )


def _add_automatic_markers(items: list[pytest.Item]) -> None:
    """Add markers automatically based on directory structure.

    Auto-added markers:
    - validation: All tests in tests/

    Note: Platform-specific labels (bare_metal, kubernetes, slurm, etc.) are now
    set on the check classes themselves via the 'labels' class variable.

    Args:
        items: List of collected test items to mark
    """
    for item in items:
        path_str = str(item.fspath)

        # Add validation marker to all tests in tests/
        if "/tests/" in path_str and "/isvtest/tests/" in path_str:
            item.add_marker(pytest.mark.validation)


def _handle_test_exclusions(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Handle test exclusions based on cluster config exclude rules.

    Deselects tests based on:
    - Excluded platforms
    - Excluded labels (skipped if -k/-m is used, allowing explicit test selection)
    - Excluded test names
    - Excluded test file names

    `item_markers` here is the set of pytest mark names applied to a collected
    item. Validation labels are mirrored into pytest marks (see
    ``_pytest_marks_for_validation``), so excluding by label and matching by
    pytest mark name is the same thing.

    Args:
        config: Pytest config object
        items: List of collected test items to filter
    """
    config_file_arg = config.getoption("--config", default=None)
    inventory_arg = config.getoption("--inventory", default=None)

    if not config_file_arg:
        return

    # Check if -k or -m was used (explicit test selection bypasses label exclusions)
    keyword_expr = config.getoption("-k", default=None)
    marker_expr = config.getoption("-m", default=None)
    skip_label_exclusions = bool(keyword_expr) or bool(marker_expr)
    if keyword_expr:
        logger.info(f"Explicit test selection (-k '{keyword_expr}') - label exclusions bypassed")
    if marker_expr:
        logger.info(f"Explicit label selection (-m '{marker_expr}') - label exclusions bypassed")

    try:
        loader = ConfigLoader()
        cluster_config = loader.load_cluster_config(
            config_file=config_file_arg,
            inventory_path=inventory_arg,
        )

        # Look for exclude config at top level or under tests (for isvctl configs)
        exclude_config = cluster_config.get("exclude", {}) or cluster_config.get("tests", {}).get("exclude", {})

        if not exclude_config:
            return

        # Deselect based on exclusion rules
        deselected = []
        for item in items:
            should_exclude = False
            item_markers = {mark.name for mark in item.iter_markers()}

            # Exclude by platform (always applied)
            excluded_platforms = exclude_config.get("platforms", [])
            if any(platform in item_markers for platform in excluded_platforms):
                should_exclude = True

            # Exclude by label (skipped if -k/-m is used for explicit selection).
            if not skip_label_exclusions:
                excluded_labels = exclude_config.get("labels", [])
                if any(label in item_markers for label in excluded_labels):
                    should_exclude = True

            # Exclude by test name (supports exact match, prefix match, or parametrized ID match)
            excluded_tests = exclude_config.get("tests", [])
            for excluded_test in excluded_tests:
                # Support multiple match patterns:
                # 1. Exact match: item.name == "K8sNodeCountCheck"
                # 2. Prefix match: item.name.startswith("K8sNodeCountCheck[")
                # 3. Parametrized ID match: "test_validation[K8sNodeCountCheck]" contains "[K8sNodeCountCheck]"
                if (
                    item.name == excluded_test
                    or item.name.startswith(excluded_test + "[")
                    or f"[{excluded_test}]" in item.name
                ):
                    logger.debug(f"Excluding test {item.nodeid} due to test name exclusion: {excluded_test}")
                    should_exclude = True
                    break

            # Exclude by file name
            excluded_files = exclude_config.get("files", [])
            if excluded_files:
                item_file = (
                    item.fspath.basename if hasattr(item.fspath, "basename") else str(item.fspath).split("/")[-1]
                )
                logger.debug(f"Checking file exclusion: {item_file} against {excluded_files}")
                if item_file in excluded_files:
                    logger.debug(f"Excluding test {item.nodeid} due to file exclusion: {item_file}")
                    should_exclude = True

            if should_exclude:
                deselected.append(item)

        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if item not in deselected]

    except Exception as e:
        # If config loading fails, continue without exclusions
        logger.warning(f"Failed to load exclusion config: {e}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Automatically add markers based on test location and handle exclusions.

    This hook:
    1. Removes dummy tests used to avoid fixture errors when parametrization list is empty
    2. Adds markers automatically based on directory structure
    3. Excludes tests based on cluster config exclude rules

    Args:
        config: Pytest config object
        items: List of collected test items
    """
    # 1. Remove dummy tests (NO_REFRAME_TESTS, NO_VALIDATIONS)
    # These are created when dynamic test generators find no tests to run,
    # to avoid "fixture not found" errors. We just want to silence them now.
    dummy_ids = {"NO_REFRAME_TESTS", "NO_VALIDATIONS"}
    items[:] = [
        item
        for item in items
        # Use getattr because 'callspec' is added dynamically by parametrization
        if not (getattr(item, "callspec", None) and getattr(item, "callspec").id in dummy_ids)
    ]

    # 2. add automatic markers
    _add_automatic_markers(items)

    # 3. handle exclusions from cluster config
    _handle_test_exclusions(config, items)


def pytest_itemcollected(item: pytest.Item) -> None:
    """Customize test display to show only function name with params.

    Transforms verbose output like:
        isvtest/src/isvtest/tests/test_validations.py::test_validation[BmCudaVersion]
    Into:
        test_validation[BmCudaVersion]

    Args:
        item: The collected test item
    """
    # Override nodeid to show just the test name (with parameters if any)
    item._nodeid = item.name


@pytest.fixture(scope="session", autouse=True)
def setup_env_vars(request: pytest.FixtureRequest) -> None:
    """Set up environment variables from cluster config.

    This fixture runs automatically and sets environment variables
    defined in the cluster config before any tests run.
    """
    config_file_arg = request.config.getoption("--config", default=None)
    inventory_arg = request.config.getoption("--inventory", default=None)

    if config_file_arg:
        try:
            loader = ConfigLoader()
            cluster_config = loader.load_cluster_config(
                config_file=config_file_arg,
                inventory_path=inventory_arg,
            )

            # Set environment variables from config (only if not already set)
            env_vars = cluster_config.get("env_vars", {})
            for key, value in env_vars.items():
                if key not in os.environ:
                    os.environ[key] = str(value)
                    logger.info(f"Setting env var from config: {key}={value}")
                else:
                    logger.info(f"Using existing env var: {key}={os.environ[key]} (config value: {value})")
        except Exception as e:
            # If config loading fails, continue without setting env vars
            logger.warning(f"Failed to set environment variables from config: {e}")


@pytest.fixture(scope="session")
def cluster_name(request: pytest.FixtureRequest) -> str:
    """Get cluster name from config or environment."""
    # Try environment variable
    cluster = os.environ.get("ISV_CLUSTER_NAME")
    if cluster:
        return cluster

    # Try config file
    config_file = request.config.getoption("--config")
    inventory_path = request.config.getoption("--inventory")
    if config_file:
        try:
            loader = ConfigLoader()
            config = loader.load_cluster_config(
                config_file=config_file,
                inventory_path=inventory_path,
            )
            return config.get("cluster_name", "custom-config")
        except Exception:
            return "custom-config"

    pytest.skip("No cluster configuration provided. Use --config or ISV_CLUSTER_NAME env var")


@pytest.fixture(scope="session")
def config_file(request: pytest.FixtureRequest) -> str | None:
    """Get config file path from CLI."""
    return request.config.getoption("--config")


@pytest.fixture(scope="session")
def inventory_path(request: pytest.FixtureRequest) -> str | None:
    """Get inventory file path from CLI."""
    return request.config.getoption("--inventory")


@pytest.fixture(scope="session")
def cluster_config(config_file: str | None, inventory_path: str | None) -> dict[str, Any]:
    """Load cluster configuration, merged with inventory if provided.

    Returns:
        Cluster configuration dictionary
    """
    if not config_file:
        pytest.skip("No cluster configuration provided. Use --config option.")

    loader = ConfigLoader()
    try:
        config = loader.load_cluster_config(
            config_file=config_file,
            inventory_path=inventory_path,
        )
        return config
    except FileNotFoundError as e:
        pytest.skip(f"Cluster configuration not found: {e}")


@pytest.fixture(scope="session")
def platform(request: pytest.FixtureRequest, cluster_config: dict[str, Any]) -> str:
    """Determine platform (bare_metal, kubernetes, slurm).

    Returns:
        Platform identifier string
    """
    # CLI override takes precedence
    cli_platform = request.config.getoption("--platform")
    if cli_platform:
        return cli_platform

    # Check cluster config
    config_platform = cluster_config.get("platform")
    if config_platform and config_platform != "auto":
        return config_platform

    # Auto-detect
    loader = ConfigLoader()
    return loader.detect_platform()


@pytest.fixture(scope="session")
def step_outputs(request: pytest.FixtureRequest, cluster_config: dict[str, Any]) -> dict[str, Any]:
    """Get step outputs from orchestration.

    Step outputs can come from:
    1. --step-outputs CLI option (JSON file path)
    2. inventory.steps in the cluster config (set by run_validations_via_pytest)

    Returns:
        Dictionary of step names to their output dicts
    """
    import json

    # Try --step-outputs CLI option first
    step_outputs_path = request.config.getoption("--step-outputs", default=None)
    if step_outputs_path:
        try:
            with open(step_outputs_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to load step outputs from {step_outputs_path}: {e}")

    # Fall back to inventory.steps in config
    return cluster_config.get("inventory", {}).get("steps", {})


@pytest.fixture(scope="session")
def all_validations(cluster_config: dict[str, Any]) -> dict[str, Any]:
    """Get all validations configuration for current cluster.

    Returns validations from ALL categories, allowing any validation to be used
    regardless of test location.

    Returns:
        Dictionary of validation names to configurations
    """
    loader = ConfigLoader()

    # Get all validations from all categories
    all_validations = loader.get_all_validations(cluster_config, categories=None)

    return all_validations
