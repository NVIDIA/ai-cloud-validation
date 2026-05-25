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

"""Main CLI entry point for nv-isv-test.

Note: For cluster lifecycle management, use isvctl instead:
    isvctl test run -f isvctl/configs/suites/k8s.yaml
"""

import copy
import json
import os
import tempfile
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import pytest
import typer
from isvreporter.version import get_version

from isvtest.config.constants import RESOLVED_ENTRIES_FLAG
from isvtest.config.loader import ConfigLoader
from isvtest.core import runners as reframe_runner
from isvtest.core.logger import setup_logger
from isvtest.core.resolution import ErrorReason, ResolvedEntry, SkipReason, State
from isvtest.tests.test_validations import (
    clear_validation_results,
    get_validation_results,
)

logger = setup_logger()


def run_validations_via_pytest(
    entries: list[ResolvedEntry],
    extra_pytest_args: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
    verbose: bool = False,
    junitxml: str | None = None,
    suite_name: str | None = None,
) -> tuple[int, list[ResolvedEntry]]:
    """Run ready validation entries via pytest.

    Resolution decisions happen before this function. This bridge only executes
    ready entries and maps pytest results back to the ResolvedEntry channel.

    Args:
        entries: Ready resolved entries to execute.
        extra_pytest_args: Additional pytest arguments (-k, -m, -v, etc.).
        settings: Test settings dict (e.g., show_skipped_tests).
        inventory: Inventory passed to validations.
        verbose: Enable verbose output.
        junitxml: Path to write JUnit XML report.
        suite_name: Name for the JUnit XML test suite (defaults to pytest's "pytest").

    Returns:
        Tuple of (exit_code, validation_results).
        exit_code: 0 if all validations passed, non-zero otherwise.
        validation_results: Updated resolved entries with terminal states.
    """
    execution_entries = _entries_with_pytest_names(entries)
    transformed_validations = _resolved_entries_to_pytest_validations(execution_entries)

    if not transformed_validations:
        logger.info("No ready validations to run")
        return 0, []

    effective_inventory = _build_inventory(execution_entries, inventory)

    temp_config: dict[str, Any] = {
        RESOLVED_ENTRIES_FLAG: True,
        "validations": {"phase_validations": transformed_validations},
        "inventory": effective_inventory,
    }
    if settings:
        temp_config["settings"] = settings

    # JSON (valid YAML) avoids YAML quote-escaping that breaks Jinja2 templates
    # (single quotes become ''nvidia'', backslashes are added).
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        json.dump(temp_config, f, indent=2)
        temp_config_path = f.name

    try:
        tests_dir = Path(__file__).parent / "tests"

        pytest_args = [
            str(tests_dir / "test_validations.py"),
            f"--rootdir={tests_dir}",
            "-o",
            "cache_dir=.pytest_cache",
            "--tb=short",
            "--config",
            temp_config_path,
        ]

        if verbose:
            pytest_args.insert(1, "--verbose")

        if junitxml:
            pytest_args.extend(["--junitxml", junitxml])

        if suite_name:
            pytest_args.extend(["-o", f"junit_suite_name={suite_name}"])

        # exclude_markers from YAML are read directly by conftest.py; passing them
        # as -m args would flip conftest's "explicit marker selection" branch.
        if extra_pytest_args:
            pytest_args.extend(extra_pytest_args)

        clear_validation_results()

        logger.info(f"Running validations via pytest: {' '.join(pytest_args)}")
        exit_code = pytest.main(pytest_args)

        raw_results = get_validation_results()

        if not raw_results and extra_pytest_args:
            k_filters = [
                extra_pytest_args[i + 1]
                for i, a in enumerate(extra_pytest_args)
                if a == "-k" and i + 1 < len(extra_pytest_args)
            ]
            if k_filters:
                logger.warning(
                    f"No tests matched -k '{k_filters[0]}' - check spelling or run without -k to see available tests"
                )

        return exit_code, _apply_pytest_results(execution_entries, raw_results)

    finally:
        Path(temp_config_path).unlink(missing_ok=True)


def _resolved_entries_to_pytest_validations(entries: list[ResolvedEntry]) -> list[dict[str, dict[str, Any]]]:
    """Return pytest config validations for already-named resolved entries."""
    return [{entry.entry.name: copy.deepcopy(entry.rendered_params or {})} for entry in entries]


def _entries_with_pytest_names(entries: list[ResolvedEntry]) -> list[ResolvedEntry]:
    """Return ready entries with duplicate names qualified for pytest config storage."""
    ready_entries = [entry for entry in entries if entry.is_ready]
    name_counts: dict[str, int] = {}
    for entry in ready_entries:
        name_counts[entry.entry.name] = name_counts.get(entry.entry.name, 0) + 1

    pair_counts: dict[tuple[str, str], int] = {}
    named_entries: list[ResolvedEntry] = []
    for entry in ready_entries:
        class_name = entry.entry.name
        category = entry.entry.category
        if name_counts[class_name] == 1:
            key = class_name
        else:
            pair = (class_name, category)
            pair_counts[pair] = pair_counts.get(pair, 0) + 1
            suffix = category if pair_counts[pair] == 1 else f"{category}-{pair_counts[pair]}"
            key = f"{class_name}-{suffix}"
        named_entries.append(ResolvedEntry(entry=replace(entry.entry, name=key), rendered_params=entry.rendered_params))
    return named_entries


def _build_inventory(entries: list[ResolvedEntry], inventory: dict[str, Any] | None) -> dict[str, Any]:
    """Build pytest inventory from supplied inventory and ready entry step outputs."""
    result = copy.deepcopy(inventory or {})
    steps = result.setdefault("steps", {})
    if not isinstance(steps, dict):
        steps = {}
        result["steps"] = steps

    for entry in entries:
        params = entry.rendered_params or {}
        step_output = params.get("step_output")
        if entry.entry.step and isinstance(step_output, dict):
            steps[entry.entry.step] = copy.deepcopy(step_output)
            for key, value in step_output.items():
                if isinstance(value, dict):
                    existing = result.setdefault(key, {})
                    if isinstance(existing, dict):
                        existing.update(copy.deepcopy(value))
                else:
                    result[key] = copy.deepcopy(value)
    return result


def _apply_pytest_results(entries: list[ResolvedEntry], results: list[dict[str, Any]]) -> list[ResolvedEntry]:
    """Apply captured pytest results to ready execution entries."""
    by_name = {result.get("name"): result for result in results}
    updated: list[ResolvedEntry] = []
    for entry in entries:
        result = by_name.get(entry.entry.name)
        if result is None:
            updated.append(
                ResolvedEntry(
                    entry=entry.entry,
                    rendered_params=entry.rendered_params,
                    state=State.SKIPPED,
                    skip_reason=SkipReason.EXCLUDED,
                    message="excluded by pytest -k/-m filter",
                )
            )
            continue
        updated.append(_result_to_resolved_entry(entry, result))
    return updated


def _result_to_resolved_entry(entry: ResolvedEntry, result: dict[str, Any]) -> ResolvedEntry:
    """Convert a captured pytest validation result to a terminal resolved entry."""
    message = str(result.get("message", ""))
    duration = float(result.get("duration", 0.0) or 0.0)
    if result.get("skipped"):
        return ResolvedEntry(
            entry=entry.entry,
            rendered_params=entry.rendered_params,
            state=State.SKIPPED,
            skip_reason=SkipReason.RUNTIME_SKIP,
            message=message,
            duration_seconds=duration,
        )
    if result.get("passed", False):
        return ResolvedEntry(
            entry=entry.entry,
            rendered_params=entry.rendered_params,
            state=State.PASSED,
            message=message,
            duration_seconds=duration,
        )
    if result.get("error_reason") == ErrorReason.RUNTIME_EXCEPTION.value:
        return ResolvedEntry(
            entry=entry.entry,
            rendered_params=entry.rendered_params,
            state=State.ERROR,
            error_reason=ErrorReason.RUNTIME_EXCEPTION,
            message=message,
            duration_seconds=duration,
        )
    return ResolvedEntry(
        entry=entry.entry,
        rendered_params=entry.rendered_params,
        state=State.FAILED,
        message=message,
        duration_seconds=duration,
    )


class Platform(StrEnum):
    """Supported platforms for validation."""

    ALL = "all"
    BARE_METAL = "bare_metal"
    KUBERNETES = "kubernetes"
    K8S = "kubernetes"  # Alias for kubernetes
    SLURM = "slurm"
    COMMON = "common"


def run_pytest_tests(
    platform: str | None = None,
    config_file: str | None = None,
    inventory_path: str | None = None,
    labels: list[str] | None = None,
    markers: list[str] | None = None,
    verbose: bool = False,
    extra_pytest_args: list[str] | None = None,
) -> int:
    """Run pytest-based tests.

    Args:
        platform: Platform to validate (bare_metal, kubernetes, slurm, common, or all)
        config_file: Direct path to cluster configuration file
        inventory_path: Path to cluster inventory file (JSON or YAML)
        labels: Public labels to filter tests (e.g., ['gpu', 'network']). All must match.
        markers: Legacy pytest markers to filter tests.
        verbose: Show verbose output
        extra_pytest_args: Additional arguments to pass to pytest

    Returns:
        Exit code (0 = success, non-zero = failure)
    """
    # Load config and apply env_vars
    config = None
    if config_file:
        try:
            config = ConfigLoader().load_cluster_config(
                config_file=config_file,
                inventory_path=inventory_path,
            )
        except FileNotFoundError as e:
            error_msg = str(e)
            if "Inventory file not found" in error_msg:
                logger.error(f"Inventory error: {error_msg}")
            else:
                logger.error(f"Configuration file '{config_file}' not found: {error_msg}")
            return 1

    # Apply env_vars from config (only if not already set in environment)
    if config:
        env_vars = config.get("env_vars", {}) or {}
        for key, value in env_vars.items():
            if key not in os.environ:
                logger.info(f"Setting {key}={value} from config")
                os.environ[key] = str(value)
            else:
                logger.debug(f"Using existing {key}={os.environ[key]} (overrides config)")

    # Get the tests directory relative to this module
    tests_dir = Path(__file__).parent / "tests"

    pytest_args = [
        str(tests_dir),
        f"--rootdir={tests_dir}",  # Clean path display (avoid ../../../)
        "-o",
        "cache_dir=.pytest_cache",  # Writable cache in cwd (not installed package)
        "--tb=short",
        "--junitxml=junit-validation.xml",
    ]

    # Only add verbosity flag if explicitly requested
    if verbose:
        pytest_args.insert(1, "--verbose")

    # Add config file
    if config_file:
        pytest_args.extend(["--config", config_file])

    # Add inventory path if specified
    if inventory_path:
        pytest_args.extend(["--inventory", inventory_path])

    marker_terms: list[str] = []

    # Add platform marker if specified
    if platform and platform != "all":
        normalized_platform = "bare_metal" if platform == "common" else platform
        if normalized_platform in ["bare_metal", "kubernetes", "slurm"]:
            marker_terms.append(normalized_platform)

    marker_terms.extend([*(labels or []), *(markers or [])])
    if marker_terms:
        pytest_args.extend(["-m", " and ".join(marker_terms)])

    # Add any extra pytest arguments
    if extra_pytest_args:
        pytest_args.extend(extra_pytest_args)

    logger.info(f"Running tests: {' '.join(pytest_args)}")
    return pytest.main(pytest_args)


app = typer.Typer(
    name="isvtest",
    help="NVIDIA ISV Lab validation tests",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("test", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def test_cmd(
    ctx: typer.Context,
    platform: Annotated[
        Platform,
        typer.Option(
            "--platform",
            "-p",
            help="Platform to test",
        ),
    ] = Platform.ALL,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-f",
            help="Path to test configuration file (YAML)",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    labels: Annotated[
        list[str] | None,
        typer.Option(
            "--label",
            "-l",
            help="Label to filter tests (can be repeated; all selected labels must match)",
        ),
    ] = None,
    markers: Annotated[
        list[str] | None,
        typer.Option(
            "--markers",
            "-m",
            help="Legacy alias for --label; pytest marker compatibility",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show detailed output for each test",
        ),
    ] = False,
) -> None:
    """Run validation tests.

    Examples:
        isvtest test --config /path/to/tests.yaml

        isvtest test --platform bare_metal --config tests.yaml

        isvtest test --config tests.yaml --label gpu --label network

        isvtest test --config tests.yaml -k test_gpu --maxfail=1
    """
    # Extra args after -- are passed to pytest
    extra_args = list(ctx.args)

    exit_code = run_pytest_tests(
        platform=platform.value,
        config_file=str(config) if config else None,
        labels=labels,
        markers=markers,
        verbose=verbose,
        extra_pytest_args=extra_args,
    )
    raise typer.Exit(code=exit_code)


@app.command("workload")
def workload(
    tags: Annotated[
        list[str] | None,
        typer.Option(
            "--tags",
            "-t",
            help="Tags to filter workload tests (can be repeated)",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show detailed output",
        ),
    ] = False,
) -> None:
    """Run ReFrame workload tests.

    Examples:
        isvtest workload --tags gpu --tags cuda

        isvtest workload --tags nccl
    """
    if verbose:
        logger.info("Running workload tests with verbose output")

    results = reframe_runner.run_reframe_tests(tags=tags)
    raise typer.Exit(code=0 if results["success"] else 1)


def _version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"isvtest {get_version('isvtest')}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Show version and exit.", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """NVIDIA ISV Lab validation tests.

    For full cluster lifecycle management, use isvctl:
        isvctl test run -f isvctl/configs/suites/k8s.yaml
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(test_cmd)


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
