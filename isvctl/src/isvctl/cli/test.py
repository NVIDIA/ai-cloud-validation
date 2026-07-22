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

"""Test subcommand for isvctl.

Handles the test lifecycle: setup cluster, run tests, teardown.
"""

import json
import logging
import sys
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TextIO

import typer
import yaml
from isvtest.catalog import build_catalog, get_catalog_version
from isvtest.core.resolution import State, parse_validations, resolve_entries
from isvtest.release_manifest import load_released_test_filter

from isvctl.cli import setup_logging
from isvctl.cli.common import (
    OUTPUT_DIR_NAME,
    apply_user_config,
    get_output_dir,
    print_error,
    print_progress,
    print_warning,
)
from isvctl.config.label_discovery import (
    ProviderConfigMatch,
    available_labels,
    discover_provider_label_configs,
    list_providers,
)
from isvctl.config.merger import merge_yaml_files
from isvctl.config.platform_resolution import (
    OmittedRun,
    PlannedRun,
    PlatformResolutionError,
    column_is_modules_only,
    plan_platform_run,
    resolve_module_configs,
    synthetic_platform_columns,
)
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.loop import Orchestrator, Phase, _apply_step_validation_gates, _has_explicit_pytest_selection
from isvctl.redaction import redact_dict
from isvctl.reporting import check_upload_credentials, create_test_run, get_environment_config, update_test_run

logger = logging.getLogger(__name__)
CONFIGS_ROOT = Path(__file__).resolve().parents[3] / "configs"


class TeeWriter:
    """Writes to multiple streams simultaneously (like Unix `tee`)."""

    def __init__(self, terminal: TextIO, file: TextIO) -> None:
        self._terminal = terminal
        self._file = file

    def write(self, s: str) -> int:
        self._terminal.write(s)
        self._file.write(s)
        return len(s)

    def writelines(self, lines: list[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._terminal.flush()
        self._file.flush()

    def isatty(self) -> bool:
        return self._terminal.isatty()


app = typer.Typer(
    name="test",
    help="Run validation tests with cluster lifecycle management",
    no_args_is_help=True,
)


def _provider_discovery_plan(provider: str, labels: list[str], matches: list[ProviderConfigMatch]) -> dict[str, Any]:
    """Return a JSON-serializable provider label discovery plan."""
    return {
        "provider": provider,
        "labels": labels,
        "configs": [
            {
                "config": str(match.config_path),
                "matched_checks": [
                    {
                        "category": check.category,
                        "name": check.name,
                        "labels": list(check.labels),
                    }
                    for check in match.matched_checks
                ],
            }
            for match in matches
        ],
    }


def _junitxml_for_config(junitxml: Path, config_path: Path, total: int) -> Path:
    """Return a non-overlapping JUnit path for one planned config run."""
    if total <= 1:
        return junitxml
    return junitxml.with_name(f"{junitxml.stem}-{config_path.stem}{junitxml.suffix}")


def _planned_run_plan(
    provider: str,
    selection: dict[str, Any],
    runs: list[PlannedRun],
    omitted: list[OmittedRun] | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable plan for a platform/module selection dry-run."""
    plan: dict[str, Any] = {
        "provider": provider,
        **selection,
        "runs": [
            {
                "config": str(run.config_path),
                "role": run.role,
                "platform": run.platform,
                "column_platform": run.column_platform,
                # Session module runs execute inside the platform run's
                # setup/teardown bracket, via their commands[<module>@<column>]
                # lifecycle, with the platform outputs at {{ session.* }}.
                "session": run.session,
                "commands_key": run.commands_key,
                "upload": {
                    "capability": run.column_platform,
                    "module": run.platform if run.role == "module" else None,
                },
            }
            for run in runs
        ],
    }
    if omitted is not None:
        plan["omitted"] = [
            {"config": str(entry.config_path), "module": entry.module, "reason": entry.reason} for entry in omitted
        ]
    return plan


_TEXT_WIDTH = 100


def _wrap_field(prefix: str, value: str, width: int = _TEXT_WIDTH) -> list[str]:
    """Wrap ``value`` at ``width``, hanging-indenting continuation lines to align under it.

    Individual tokens (comma-separated names) are never split mid-word.
    """
    wrapper = textwrap.TextWrapper(
        width=width,
        initial_indent=prefix,
        subsequent_indent=" " * len(prefix),
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapper.wrap(value) or [prefix.rstrip()]


@contextmanager
def _suppress_dry_run_resolution_warnings():
    """Silence expected Jinja ``default()`` warnings while planning without step output."""
    resolution_logger = logging.getLogger("isvtest.core.resolution")
    previous_level = resolution_logger.level
    resolution_logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        resolution_logger.setLevel(previous_level)


def _render_provider_discovery_text(provider: str, labels: list[str], matches: list[ProviderConfigMatch]) -> str:
    """Render a provider label discovery dry-run plan as human-readable text."""
    total_checks = sum(len(match.matched_checks) for match in matches)
    lines = [
        "Dry run: provider label discovery (nothing will be executed)",
        f"  Provider: {provider}",
        f"  Labels:   {', '.join(labels)}",
        f"  Tests:    {total_checks} validation(s)",
        f"  Matched configs ({len(matches)}):",
    ]
    for match in matches:
        lines.append(f"    {match.config_path}")
        for check in match.matched_checks:
            lines.extend(
                _wrap_field(f"      - [{check.category}] {check.name} (labels: ", f"{', '.join(check.labels)})")
            )
    return "\n".join(lines)


@dataclass(frozen=True)
class ValidationPlan:
    """Validation names that would run in a dry-run, plus any that would be skipped."""

    ready_by_category: dict[str, list[str]]
    skipped: list[tuple[str, str]]  # (check name, reason phrase)


def _planned_validations_by_category(
    config: RunConfig,
    *,
    include_labels: list[str] | None,
    exclude_labels: list[str] | None,
    requested_phase: Phase,
    extra_pytest_args: list[str],
    column_platform: str | None = None,
    commands_key: str | None = None,
) -> ValidationPlan:
    """Return validations that would run, plus any that would be skipped.

    ``commands_key`` overrides the ``commands[...]`` lookup for a session run
    (``<module>@<column>``), so step-bound checks resolve against the session
    lifecycle's steps instead of the module's own.
    """
    if not config.tests or not config.tests.validations:
        return ValidationPlan(ready_by_category={}, skipped=[])

    platform = commands_key or config.tests.platform
    released_tests = load_released_test_filter()

    step_phases: dict[str, str] = {}
    config_phases: list[str] = []
    if platform and platform in config.commands:
        platform_cmds = config.commands[platform]
        config_phases = list(platform_cmds.phases)
        gated_steps = _apply_step_validation_gates(platform_cmds.steps, released_tests)
        for step in gated_steps:
            if step.skip:
                continue
            step_phases[step.name] = (step.phase or "setup").lower()

    exclude_tests: list[str] = []
    config_exclude_labels: list[str] = []
    if config.tests.exclude:
        exclude_tests = list(config.tests.exclude.get("tests", []))
        config_exclude_labels = list(config.tests.exclude.get("labels", []))

    skip_config_label_exclusions = bool(include_labels) or _has_explicit_pytest_selection(extra_pytest_args)
    resolution_exclude_labels = list(
        dict.fromkeys([*([] if skip_config_label_exclusions else config_exclude_labels), *(exclude_labels or [])])
    )

    if requested_phase == Phase.ALL:
        requested_phase_names = set(config_phases) if config_phases else {"setup", "test", "teardown"}
    else:
        requested_phase_names = {requested_phase.value}

    # Dry-run assumes configured steps will produce output so step-bound checks count.
    step_outputs = {name: {} for name in step_phases}
    render_context = {"steps": step_outputs, **(config.context or {})}

    validation_entries = parse_validations(config.tests.validations)
    with _suppress_dry_run_resolution_warnings():
        resolved = resolve_entries(
            validation_entries,
            step_outputs=step_outputs,
            step_phases=step_phases,
            requested_phases=requested_phase_names,
            include_labels=set(include_labels or []),
            exclude_labels=set(resolution_exclude_labels),
            exclude_tests=set(exclude_tests),
            released_tests=released_tests,
            render_context=render_context,
            column_platform=column_platform,
            synthetic_columns=synthetic_platform_columns(CONFIGS_ROOT),
        )

    ready_by_category: dict[str, list[str]] = {}
    skipped: list[tuple[str, str]] = []
    for item in resolved:
        if item.is_ready:
            ready_by_category.setdefault(item.entry.category, []).append(item.entry.name)
        elif item.state == State.SKIPPED:
            reason = item.skip_reason.value.replace("_", " ") if item.skip_reason else "skipped"
            skipped.append((item.entry.name, reason))
    return ValidationPlan(ready_by_category=ready_by_category, skipped=skipped)


def _short_config_path(config_path: Path) -> str:
    """Return a config path relative to the configs root when possible."""
    try:
        return str(config_path.relative_to(CONFIGS_ROOT))
    except ValueError:
        return str(config_path)


def _validation_plan_for_run(
    run: PlannedRun,
    *,
    include_labels: list[str] | None,
    user_exclude_labels: list[str] | None,
    requested_phase: Phase,
    extra_pytest_args: list[str],
) -> tuple[RunConfig, ValidationPlan]:
    """Load one planned config and resolve which validations would run."""
    merged_config = merge_yaml_files([str(run.config_path)])
    config = RunConfig.model_validate(merged_config)
    plan = _planned_validations_by_category(
        config,
        include_labels=include_labels,
        exclude_labels=user_exclude_labels,
        requested_phase=requested_phase,
        extra_pytest_args=extra_pytest_args,
        column_platform=run.column_platform,
        commands_key=run.commands_key,
    )
    return config, plan


def _format_validation_count(plan: ValidationPlan) -> str:
    """Format a ready/skipped validation count for display."""
    ready = sum(len(names) for names in plan.ready_by_category.values())
    if plan.skipped:
        return f"{ready} validation(s) ({len(plan.skipped)} skipped)"
    return f"{ready} validation(s)"


def _render_config_body_lines(
    config: RunConfig,
    planned: ValidationPlan,
    *,
    indent: str = "  ",
    commands_key: str | None = None,
) -> list[str]:
    """Render the per-config dry-run body: summary, steps, validations, and skipped checks.

    ``commands_key`` selects a session run's ``<module>@<column>`` lifecycle
    block for the steps display instead of the config's own.
    """
    platform = config.tests.platform if config.tests else None
    module = config.tests.module if config.tests else None
    total_tests = sum(len(names) for names in planned.ready_by_category.values())
    detail_indent = indent + "  "
    lines = [f"{indent}Platform: {platform or 'not specified'}"]
    if module:
        lines.append(f"{indent}Module:   {module}")
    if planned.skipped:
        lines.append(f"{indent}Tests:    {total_tests} validation(s) ({len(planned.skipped)} skipped)")
    else:
        lines.append(f"{indent}Tests:    {total_tests} validation(s)")

    commands_lookup = commands_key or platform
    platform_cmds = config.commands.get(commands_lookup) if commands_lookup else None
    if platform_cmds:
        lines.append(f"{indent}Phases:   {', '.join(platform_cmds.phases)}")
        steps_by_phase: dict[str, list[str]] = {}
        for step in platform_cmds.steps:
            label = f"{step.name}: {step.command}" + (" (skipped)" if step.skip else "")
            steps_by_phase.setdefault(step.phase, []).append(label)
        if steps_by_phase:
            lines.append(f"{indent}Steps:")
            for phase in platform_cmds.phases:
                for label in steps_by_phase.get(phase, []):
                    lines.append(f"{detail_indent}[{phase}] {label}")

    validations = config.tests.validations if config.tests else {}
    if planned.ready_by_category:
        lines.append(f"{indent}Validations ({total_tests}):")
        for category in validations:
            names = planned.ready_by_category.get(category, [])
            if names:
                lines.extend(_wrap_field(f"{detail_indent}{category} ({len(names)}): ", ", ".join(names)))

    if planned.skipped:
        lines.append(f"{indent}Skipped ({len(planned.skipped)}):")
        skipped_by_reason: dict[str, list[str]] = {}
        for name, reason in planned.skipped:
            skipped_by_reason.setdefault(reason, []).append(name)
        for reason, names in skipped_by_reason.items():
            lines.extend(_wrap_field(f"{detail_indent}{reason}: ", ", ".join(names)))
    return lines


def _render_planned_run_text(
    provider: str,
    selection: dict[str, Any],
    runs: list[PlannedRun],
    *,
    include_labels: list[str] | None = None,
    exclude_labels: list[str] | None = None,
    requested_phase: Phase = Phase.ALL,
    extra_pytest_args: list[str] | None = None,
    omitted: list[OmittedRun] | None = None,
) -> str:
    """Render a platform/module selection dry-run plan as human-readable text."""
    pytest_args = extra_pytest_args or []
    has_platform = "platform" in selection
    has_modules = "modules" in selection
    if has_platform and has_modules:
        title = "Dry run: module selection under platform column (nothing will be executed)"
    elif has_platform:
        title = "Dry run: platform column (nothing will be executed)"
    else:
        title = "Dry run: module selection (nothing will be executed)"
    header_lines = []
    if has_platform:
        header_lines.append(f"  Platform: {selection['platform']}")
    if has_modules:
        header_lines.append(f"  Modules:  {', '.join(selection.get('modules') or [])}")

    run_summaries = [
        (
            run,
            *_validation_plan_for_run(
                run,
                include_labels=include_labels,
                user_exclude_labels=exclude_labels,
                requested_phase=requested_phase,
                extra_pytest_args=pytest_args,
            ),
        )
        for run in runs
    ]
    total_ready = sum(sum(len(names) for names in plan.ready_by_category.values()) for _, _, plan in run_summaries)
    total_skipped = sum(len(plan.skipped) for _, _, plan in run_summaries)

    lines = [title, f"  Provider: {provider}", *header_lines]
    if total_skipped:
        lines.append(f"  Tests:    {total_ready} validation(s) ({total_skipped} skipped) across {len(runs)} config(s)")
    else:
        lines.append(f"  Tests:    {total_ready} validation(s) across {len(runs)} config(s)")
    if any(run.session for run in runs):
        lines.append("  Order:    platform setup + tests → session module run(s) → platform teardown (always last)")

    for run, config, plan in run_summaries:
        label = run.platform
        if run.session:
            label = f"{run.platform} session under {run.column_platform}, commands[{run.commands_key}]"
        lines.append("")
        lines.append(f"  --- {_short_config_path(run.config_path)} [{label}] — {_format_validation_count(plan)} ---")
        lines.extend(_render_config_body_lines(config, plan, indent="    ", commands_key=run.commands_key))

    if omitted:
        lines.append("")
        for entry in omitted:
            lines.append(f"  omitted: {entry.module} ({entry.reason})")

    if pytest_args:
        lines.append(f"  Extra pytest args: {' '.join(pytest_args)}")
    return "\n".join(lines)


def _render_config_text(
    config: RunConfig,
    extra_pytest_args: list[str],
    *,
    include_labels: list[str] | None = None,
    exclude_labels: list[str] | None = None,
    requested_phase: Phase = Phase.ALL,
) -> str:
    """Render a merged config dry-run as a human-readable summary of what would run."""
    planned = _planned_validations_by_category(
        config,
        include_labels=include_labels,
        exclude_labels=exclude_labels,
        requested_phase=requested_phase,
        extra_pytest_args=extra_pytest_args,
    )
    lines = ["Dry run: configuration (nothing will be executed)"]
    lines.extend(_render_config_body_lines(config, planned, indent="  "))

    if extra_pytest_args:
        lines.append(f"  Extra pytest args: {' '.join(extra_pytest_args)}")
    return "\n".join(lines)


def _run_descriptor(planned: PlannedRun) -> str:
    """Operator-facing label for one planned run."""
    if planned.session:
        return f"{planned.platform} session under {planned.column_platform}"
    return planned.role


def _execute_planned_runs(
    ctx: typer.Context,
    runs: list[PlannedRun],
    *,
    labels: list[str] | None,
    user_exclude_labels: list[str] | None,
    junitxml: Path,
    forward_kwargs: dict[str, Any],
) -> None:
    """Run each planned config as its own orchestration, continuing past failures.

    Standalone runs execute sequentially as before. Session module runs
    execute INSIDE the platform run's lifecycle — after its pre-teardown
    phases, before its teardown (which always runs last) — each with the
    platform's step outputs mounted at ``{{ session.* }}``. If the platform
    setup fails, session runs are reported as skipped rather than executed
    against a broken environment.

    Each run carries its column platform so checks declaring a ``platforms:``
    restriction are filtered against it; user ``--exclude-label`` values pass
    through unchanged. A combined summary is printed and the process exits 1
    if any config failed. ``forward_kwargs`` are the remaining ``run()``
    options.
    """
    total = len(runs)
    outcomes: list[tuple[Path, str]] = []  # (config, "PASS" | "FAIL" | "SKIP")

    extra_pytest_args = list(ctx.args)
    color = forward_kwargs.get("color")
    if color:
        extra_pytest_args.append(f"--color={color}")

    common: dict[str, Any] = {
        "set_values": forward_kwargs.get("set_values"),
        "extra_pytest_args": extra_pytest_args,
        "labels": labels,
        "exclude_labels": user_exclude_labels,
        "dry_run": False,
        "json_output": False,
        "phase": forward_kwargs.get("phase", Phase.ALL),
        "working_dir": forward_kwargs.get("working_dir"),
        "verbose": forward_kwargs.get("verbose", False),
        "no_upload": forward_kwargs.get("no_upload", False),
        "lab_id": forward_kwargs.get("lab_id"),
        "tags": forward_kwargs.get("tags"),
        "isv_software_version": forward_kwargs.get("isv_software_version"),
    }

    def _execute_one(
        planned: PlannedRun,
        *,
        session_data: dict[str, Any] | None = None,
        pre_teardown_hook: Any = None,
    ) -> None:
        print_progress(f"\n--- Running {planned.config_path} ({_run_descriptor(planned)}) ---")
        try:
            _run_single_config(
                config_files=[planned.config_path],
                junitxml=_junitxml_for_config(junitxml, planned.config_path, total),
                column_platform=planned.column_platform,
                session_data=session_data,
                commands_key=planned.commands_key,
                pre_teardown_hook=pre_teardown_hook,
                **common,
            )
            outcomes.append((planned.config_path, "PASS"))
        except typer.Exit as exc:
            outcomes.append((planned.config_path, "PASS" if exc.exit_code == 0 else "FAIL"))

    platform_run = next((r for r in runs if r.role == "platform"), None)
    session_runs = [r for r in runs if r.role == "module" and r.session]
    standalone_runs = [r for r in runs if r.role == "module" and not r.session]

    if platform_run is not None:
        session_body = None
        if session_runs:

            def session_body(session_steps: dict[str, Any], platform_ok: bool) -> None:
                if not platform_ok:
                    for planned in session_runs:
                        print_warning(f"Skipping {planned.config_path} — platform setup failed, no session available")
                        outcomes.append((planned.config_path, "SKIP"))
                    return
                for planned in session_runs:
                    _execute_one(planned, session_data=session_steps)

        _execute_one(platform_run, pre_teardown_hook=session_body)
    elif session_runs:
        # Defensive: a session run cannot execute without its platform bracket.
        for planned in session_runs:
            print_warning(f"Skipping {planned.config_path} — session runs require the platform bracket")
            outcomes.append((planned.config_path, "SKIP"))

    for planned in standalone_runs:
        _execute_one(planned)

    typer.echo("\n" + "=" * 60)
    typer.echo("PLATFORM/MODULE RUN SUMMARY")
    typer.echo("=" * 60)
    styles = {
        "PASS": typer.style("[PASS]", fg=typer.colors.GREEN),
        "FAIL": typer.style("[FAIL]", fg=typer.colors.RED),
        "SKIP": typer.style("[SKIP]", fg=typer.colors.YELLOW),
    }
    for config_path, status in outcomes:
        typer.echo(f"{styles[status]} {config_path}")
    if any(status == "FAIL" for _, status in outcomes):
        raise typer.Exit(code=1)


@app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    config_files: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            "-f",
            help="YAML configuration file(s) to merge. Later files override earlier ones.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Provider name for label discovery when no --config/-f files are supplied.",
        ),
    ] = None,
    set_values: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Set values on the command line (e.g., --set context.node_count=8)",
        ),
    ] = None,
    phase: Annotated[
        Phase,
        typer.Option(
            "--phase",
            "-p",
            help="Run only a specific phase of the test lifecycle",
        ),
    ] = Phase.ALL,
    labels: Annotated[
        list[str] | None,
        typer.Option(
            "--label",
            "-l",
            help="Label to filter validations (can be repeated; all selected labels must match)",
        ),
    ] = None,
    platform: Annotated[
        str | None,
        typer.Option(
            "--platform",
            help=(
                "Run a whole platform column (its config + every compatible module config) "
                "for --provider; with --module, run only those modules under the column."
            ),
        ),
    ] = None,
    modules: Annotated[
        list[str] | None,
        typer.Option(
            "--module",
            help=(
                "Run a single module suite for --provider (can be repeated). "
                "With --platform, the module runs under that column."
            ),
        ),
    ] = None,
    no_modules: Annotated[
        bool,
        typer.Option(
            "--no-modules",
            help="With --platform, run only the platform config (skip the module fan-out).",
        ),
    ] = False,
    exclude_labels: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-label",
            help="Label to exclude validations (can be repeated; any match excludes).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Validate configuration and show what would be executed without running",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="With --dry-run, emit the plan as JSON instead of human-readable text.",
        ),
    ] = False,
    working_dir: Annotated[
        Path | None,
        typer.Option(
            "--working-dir",
            "-C",
            help="Working directory for command execution",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging",
        ),
    ] = False,
    no_user_config: Annotated[
        bool,
        typer.Option(
            "--no-user-config",
            help="Do not apply persisted user config (config.yml / secrets.yml).",
        ),
    ] = False,
    junitxml: Annotated[
        Path,
        typer.Option(
            "--junitxml",
            help="Path to write JUnit XML test report",
        ),
    ] = Path(OUTPUT_DIR_NAME) / "junit-validation.xml",
    color: Annotated[
        str | None,
        typer.Option(
            "--color",
            help="Color output: yes, no, auto",
        ),
    ] = None,
    # ISV Lab Service result upload options
    no_upload: Annotated[
        bool,
        typer.Option(
            "--no-upload",
            help="Disable uploading results to ISV Lab Service",
        ),
    ] = False,
    lab_id: Annotated[
        int | None,
        typer.Option(
            "--lab-id",
            help="ISV Lab ID for result upload (required if uploading)",
        ),
    ] = None,
    tags: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            "-t",
            help="Tags for the test run (can be repeated)",
        ),
    ] = None,
    isv_software_version: Annotated[
        str | None,
        typer.Option(
            "--isv-software-version",
            help="ISV software stack version (opaque string provided by ISV, e.g., 'nemo-2.1.0-rc3')",
        ),
    ] = None,
    column_platform: Annotated[
        str | None,
        typer.Option(
            "--column-platform",
            hidden=True,
            help=(
                "Internal: the platform column a module config runs under "
                "(set by --platform fan-out so upload reports the capability "
                "and per-check platforms: restrictions are applied)."
            ),
        ),
    ] = None,
) -> None:
    """Run the full test lifecycle: setup cluster, run tests, teardown.

    Merges multiple YAML configuration files and executes the test pipeline.
    The setup command output is validated and used as inventory for tests.

    Use -- to pass additional arguments to pytest/isvtest.

    Examples:
        isvctl test run -f lab.yaml -f commands.yaml -f suites/k8s.yaml
        isvctl test run -f config.yaml --set context.node_count=8
        isvctl test run -f config.yaml --phase setup
        isvctl test run -f config.yaml --label gpu --label slow
        isvctl test run -f config.yaml -- -v -s -k "test_name"
    """
    setup_logging(verbose)
    apply_user_config(no_user_config)

    if (platform or modules) and config_files:
        print_error("--platform/--module cannot be combined with --config/-f.")
        raise typer.Exit(code=1)
    if (platform or modules) and not provider:
        print_error("--platform/--module require --provider.")
        raise typer.Exit(code=1)
    if no_modules and not platform:
        print_error("--no-modules requires --platform.")
        raise typer.Exit(code=1)
    if no_modules and modules:
        print_error("--no-modules cannot be combined with --module.")
        raise typer.Exit(code=1)

    if provider:
        if config_files:
            print_error("--provider discovery cannot be combined with --config/-f.")
            raise typer.Exit(code=1)

        known_providers = list_providers(CONFIGS_ROOT)
        if provider not in known_providers:
            print_error(f"Unknown provider {provider!r}. Available providers: {', '.join(known_providers)}")
            raise typer.Exit(code=1)

        # run() options forwarded unchanged into each per-config recursive run.
        forward_kwargs: dict[str, Any] = {
            "set_values": set_values,
            "phase": phase,
            "working_dir": working_dir,
            "verbose": verbose,
            "no_user_config": no_user_config,
            "color": color,
            "no_upload": no_upload,
            "lab_id": lab_id,
            "tags": tags,
            "isv_software_version": isv_software_version,
        }

        if platform or modules:
            try:
                omitted: list[OmittedRun] | None = None
                if platform and modules:
                    if column_is_modules_only(platform, configs_root=CONFIGS_ROOT):
                        # Intersect under a modules-only column (e.g.
                        # foundational): the requested modules run standalone
                        # with column filtering — there is no platform config.
                        runs = resolve_module_configs(
                            provider, modules or [], configs_root=CONFIGS_ROOT, column_platform=platform
                        )
                    else:
                        # Intersect under a real column: the requested modules
                        # are session runs, so the plan is the column plan
                        # filtered to them — platform bracket included (its
                        # setup provides the session, its teardown runs last).
                        column_plan = plan_platform_run(provider, platform, configs_root=CONFIGS_ROOT)
                        requested = set(modules or [])
                        planned_modules = {r.platform for r in column_plan.runs if r.role == "module"}
                        omitted_modules = {o.module for o in column_plan.omitted}
                        unknown = requested - planned_modules - omitted_modules
                        if unknown:
                            print_error(f"Unknown module(s) for provider {provider!r}: {', '.join(sorted(unknown))}.")
                            raise typer.Exit(code=1)
                        runs = [r for r in column_plan.runs if r.role == "platform" or r.platform in requested]
                        omitted = [o for o in column_plan.omitted if o.module in requested]
                        if not any(r.role == "module" for r in runs):
                            reasons = "; ".join(f"{o.module}: {o.reason}" for o in omitted)
                            print_error(
                                f"No requested module can run under column '{platform}' ({reasons}). "
                                f"Nothing to execute."
                            )
                            raise typer.Exit(code=1)
                    selection: dict[str, Any] = {"platform": platform, "modules": modules}
                elif platform:
                    column_plan = plan_platform_run(provider, platform, configs_root=CONFIGS_ROOT)
                    runs, omitted = column_plan.runs, column_plan.omitted
                    if no_modules:
                        runs = [run for run in runs if run.role == "platform"]
                        omitted = None
                    selection = {"platform": platform}
                else:
                    runs = resolve_module_configs(provider, modules or [], configs_root=CONFIGS_ROOT)
                    selection = {"modules": modules}
            except PlatformResolutionError as exc:
                print_error(str(exc))
                raise typer.Exit(code=1)

            if dry_run:
                planned_pytest_args = list(ctx.args)
                if color:
                    planned_pytest_args.append(f"--color={color}")
                if json_output:
                    typer.echo(json.dumps(_planned_run_plan(provider, selection, runs, omitted), indent=2))
                else:
                    typer.echo(
                        _render_planned_run_text(
                            provider,
                            selection,
                            runs,
                            include_labels=labels,
                            exclude_labels=exclude_labels,
                            requested_phase=phase,
                            extra_pytest_args=planned_pytest_args,
                            omitted=omitted,
                        )
                    )
                return

            _execute_planned_runs(
                ctx,
                runs,
                labels=labels,
                user_exclude_labels=exclude_labels,
                junitxml=junitxml,
                forward_kwargs=forward_kwargs,
            )
            return

        if not labels:
            print_error("--provider requires --label, --platform, or --module.")
            raise typer.Exit(code=1)

        matches = discover_provider_label_configs(
            provider, labels, configs_root=CONFIGS_ROOT, released_tests=load_released_test_filter()
        )
        if not matches:
            known_labels = available_labels(provider, configs_root=CONFIGS_ROOT)
            print_error(
                f"No {provider!r} provider configs match labels: {', '.join(labels)}. "
                f"Available labels for {provider!r}: {', '.join(sorted(known_labels))}"
            )
            raise typer.Exit(code=1)

        if dry_run:
            if json_output:
                typer.echo(json.dumps(_provider_discovery_plan(provider, labels, matches), indent=2))
            else:
                typer.echo(_render_provider_discovery_text(provider, labels, matches))
            return

        print_progress(
            f"Discovered {len(matches)} {provider!r} provider config(s) matching labels: {', '.join(labels)}"
        )
        for match in matches:
            print_progress(f"\n--- Running {match.config_path} ---")
            run(
                ctx,
                config_files=[match.config_path],
                provider=None,
                labels=labels,
                exclude_labels=exclude_labels,
                dry_run=False,
                junitxml=_junitxml_for_config(junitxml, match.config_path, len(matches)),
                **forward_kwargs,
            )
        return

    # Validate at least one config file is provided
    if not config_files:
        if labels:
            print_error("--label requires either --provider (for label discovery) or --config/-f.")
        else:
            print_error("At least one --config/-f config file is required.")
        raise typer.Exit(code=1)

    # Collect extra pytest args from context (after --)
    extra_pytest_args = list(ctx.args)
    if color:
        extra_pytest_args.extend([f"--color={color}"])

    _run_single_config(
        config_files=config_files,
        extra_pytest_args=extra_pytest_args,
        set_values=set_values,
        labels=labels,
        exclude_labels=exclude_labels,
        dry_run=dry_run,
        json_output=json_output,
        phase=phase,
        working_dir=working_dir,
        verbose=verbose,
        junitxml=junitxml,
        no_upload=no_upload,
        lab_id=lab_id,
        tags=tags,
        isv_software_version=isv_software_version,
        column_platform=column_platform,
    )


def _run_single_config(
    *,
    config_files: list[Path],
    extra_pytest_args: list[str],
    set_values: list[str] | None,
    labels: list[str] | None,
    exclude_labels: list[str] | None,
    dry_run: bool,
    json_output: bool,
    phase: Phase,
    working_dir: Path | None,
    verbose: bool,
    junitxml: Path,
    no_upload: bool,
    lab_id: int | None,
    tags: list[str] | None,
    isv_software_version: str | None,
    column_platform: str | None,
    session_data: dict[str, Any] | None = None,
    commands_key: str | None = None,
    pre_teardown_hook: Any = None,
) -> None:
    """Merge, validate, and execute one config as a full orchestration.

    The execution core of ``test run``: config merge, dry-run rendering, ISV
    Lab Service run creation, orchestration, result upload, and the results
    display. Raises ``typer.Exit(1)`` on failure, exactly like the CLI command
    it was extracted from (callers in the fan-out catch it per run).

    ``session_data``/``commands_key`` configure a module session run under a
    platform column; ``pre_teardown_hook`` makes this run a platform-session
    bracket whose body executes before this run's teardown (see
    ``Orchestrator.run``).
    """
    # Load and merge YAML files (resolving import: directives)
    try:
        merged_config = merge_yaml_files([str(p) for p in config_files], set_values or [])
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        raise typer.Exit(code=1)

    # Count imports by parsing each file's top-level keys
    import_count = 0
    for p in config_files:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "import" in data:
                import_count += 1
        except Exception:
            pass
    parts = []
    if len(config_files) > 1:
        parts.append(f"{len(config_files)} files")
    if import_count:
        parts.append(f"{import_count} import{'s' if import_count > 1 else ''}")
    if parts:
        print_progress(f"Loaded configuration ({', '.join(parts)}).")

    # Validate against schema
    print_progress("Validating configuration...")
    try:
        config = RunConfig.model_validate(merged_config)
    except Exception as e:
        print_error(f"Configuration validation failed: {e}")
        raise typer.Exit(code=1)

    if dry_run:
        if json_output:
            # Keep stdout as pure JSON so it can be consumed with `json.loads`;
            # decorative headers and extra args go to stderr.
            print_progress("\n--- Dry Run: Configuration ---")
            redacted_config = redact_dict(config.model_dump(mode="json"))
            typer.echo(json.dumps(redacted_config, indent=2))
            if extra_pytest_args:
                print_progress(f"\n--- Extra pytest args ---\n{extra_pytest_args}")
        else:
            typer.echo(
                _render_config_text(
                    config,
                    extra_pytest_args,
                    include_labels=labels,
                    exclude_labels=exclude_labels,
                    requested_phase=phase,
                )
            )
        return

    # Determine which phases to run
    if phase == Phase.ALL:
        phases = [Phase.SETUP, Phase.TEST, Phase.TEARDOWN]
    else:
        phases = [phase]

    print_progress(f"\nRunning phases: {[p.value for p in phases]}")

    # Default working directory to first config file's parent (for relative paths in config)
    effective_working_dir = working_dir or config_files[0].parent
    logger.debug(f"Working directory: {effective_working_dir}")

    # Check if we should upload results to ISV Lab Service
    upload_results = not no_upload
    test_run_id: str | None = None
    start_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if upload_results:
        can_upload, _, _ = check_upload_credentials()
        if not can_upload:
            print_warning("ISV_CLIENT_ID and/or ISV_CLIENT_SECRET not set")
            print_warning("Test results will not be uploaded to ISV Lab Service")
            upload_results = False
        elif not lab_id:
            print_warning("--lab-id not specified, skipping result upload")
            upload_results = False
        else:
            endpoint, ssa_issuer = get_environment_config()
            if not endpoint or not ssa_issuer:
                missing = []
                if not endpoint:
                    missing.append("ISV_SERVICE_ENDPOINT")
                if not ssa_issuer:
                    missing.append("ISV_SSA_ISSUER")
                print_warning(f"{', '.join(missing)} not set, skipping result upload")
                upload_results = False

    # Create test run before running tests
    if upload_results and lab_id:
        # A run reports both axes: the capability (platform column) and the
        # module. A module suite's derived tests.platform is its module name,
        # never a capability - its capability is the column it ran under
        # (--column-platform from --platform fan-out; None standalone).
        module_axis = config.tests.module if config.tests else None
        if module_axis:
            capability = column_platform
        else:
            capability = config.tests.platform if config.tests and config.tests.platform else "kubernetes"
        axes = ", ".join(
            part
            for part in (
                f"capability={capability}" if capability else None,
                f"module={module_axis}" if module_axis else None,
            )
            if part
        )
        print_progress(f"Creating test run in ISV Lab Service ({axes})...")
        test_run_id = create_test_run(
            lab_id=lab_id,
            platform=capability,
            module=module_axis,
            tags=tags or ["validation-test", "isvctl"],
            start_time=start_time,
            isv_software_version=isv_software_version,
        )
        if not test_run_id:
            print_warning("Failed to create test run, continuing without upload")
            upload_results = False

    # Run orchestration with log file capture (tee to _output/pytest-output.log).
    # A session module run executes while its platform run's log file is still
    # open, so it gets its own file instead of clobbering the platform's.
    orchestrator = Orchestrator(config, working_dir=effective_working_dir)
    output_dir = get_output_dir()
    if session_data is None:
        log_file_path = output_dir / "pytest-output.log"
    else:
        log_file_path = output_dir / f"pytest-output-{config_files[0].stem}.log"

    # Build test catalog early so it runs inside the TeeWriter context
    # (avoids logging errors from stale stream references after the log file closes)
    catalog_entries: list[dict] | None = None
    catalog_version: str | None = None

    # Always capture output to log file while still displaying (like `tee`)
    with open(log_file_path, "w") as log_file:
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout = TeeWriter(terminal=original_stdout, file=log_file)  # type: ignore[assignment]
        sys.stderr = TeeWriter(terminal=original_stderr, file=log_file)  # type: ignore[assignment]
        try:
            result = orchestrator.run(
                phases=phases,
                extra_pytest_args=extra_pytest_args,
                include_labels=labels,
                exclude_labels=exclude_labels,
                column_platform=column_platform,
                verbose=verbose,
                junitxml=str(junitxml),
                session_data=session_data,
                commands_key=commands_key,
                pre_teardown_hook=pre_teardown_hook,
                synthetic_columns=synthetic_platform_columns(CONFIGS_ROOT),
            )
            if upload_results:
                try:
                    catalog_entries = build_catalog()
                    catalog_version = get_catalog_version()
                    print_progress(f"Built test catalog: {len(catalog_entries)} tests (version: {catalog_version})")
                    catalog_path = output_dir / "test_catalog.json"
                    catalog_path.write_text(
                        json.dumps({"isvTestVersion": catalog_version, "entries": catalog_entries}, indent=2)
                    )
                    print_progress(f"  Saved test catalog to: {catalog_path}")
                except Exception as e:
                    logger.warning("Failed to build test catalog: %s", e)
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr

    # Update test run after tests complete
    if upload_results and test_run_id and lab_id:
        print_progress("Uploading test results to ISV Lab Service...")
        # Prefer the requested --junitxml (provider discovery gives each config
        # its own report name), then fall back to _output, working dir, or cwd.
        junit_path = junitxml
        if not junit_path.exists():
            junit_path = output_dir / "junit-validation.xml"
        if not junit_path.exists():
            junit_path = effective_working_dir / "junit-validation.xml"
        if not junit_path.exists():
            junit_path = Path("junit-validation.xml")

        if update_test_run(
            lab_id=lab_id,
            test_run_id=test_run_id,
            success=result.success,
            start_time=start_time,
            junit_xml=junit_path if junit_path.exists() else None,
            log_file=log_file_path if log_file_path.exists() else None,
            isv_software_version=isv_software_version,
            catalog_entries=catalog_entries,
            catalog_version=catalog_version,
        ):
            print_progress(typer.style("[OK]", fg=typer.colors.GREEN) + " Test results uploaded successfully")
        else:
            print_warning("Failed to upload test results")

    # Display results
    typer.echo("\n" + "=" * 60)
    typer.echo("ORCHESTRATION RESULTS")
    typer.echo("=" * 60)

    for phase_result in result.phases:
        if phase_result.message.startswith("SKIPPED:"):
            status = typer.style("[SKIP]", fg=typer.colors.YELLOW)
        elif phase_result.success:
            status = typer.style("[PASS]", fg=typer.colors.GREEN)
        else:
            status = typer.style("[FAIL]", fg=typer.colors.RED)
        phase_name = phase_result.phase.value.upper().ljust(8)
        typer.echo(f"{status} {phase_name}: {phase_result.message}")

        # Display step details (schema validation, errors)
        if phase_result.details and "steps" in phase_result.details:
            for step in phase_result.details["steps"]:
                step_name = step.get("name", "unknown")
                step_success = step.get("success", False)
                schema_valid = step.get("schema_valid", True)
                schema_errors = step.get("schema_errors", [])
                schema_name = step.get("schema_name")

                # Show schema validation result (only failures by default, all with -v)
                if schema_name and schema_name != "generic":
                    if not schema_valid:
                        # Always show schema failures
                        schema_status = typer.style("FAILED", fg=typer.colors.RED)
                        typer.echo(f"  [{step_name}] Schema({schema_name}): {schema_status}")
                        for err in schema_errors:
                            typer.echo(f"    - {err}")
                    elif verbose:
                        # Only show schema success with -v flag
                        schema_status = typer.style("PASSED", fg=typer.colors.GREEN)
                        typer.echo(f"  [{step_name}] Schema({schema_name}): {schema_status}")

                # Show error if step failed
                if not step_success:
                    error = step.get("error", "Unknown error")
                    typer.echo(f"  [{step_name}] " + typer.style(f"ERROR: {error}", fg=typer.colors.RED))
                    # Show output if available (helpful for debugging)
                    output = step.get("output")
                    if output and verbose:
                        typer.echo(f"    Output: {json.dumps(output, indent=2)[:500]}")

        # Display centralized validation results
        if phase_result.details and "validations" in phase_result.details:
            validations = phase_result.details["validations"]
            if validations:
                for vr in validations:
                    vr_name = vr.get("name", "unknown")
                    # Handle case where name might be a dict (extract class name)
                    if isinstance(vr_name, dict):
                        vr_name = next(iter(vr_name.keys()), "unknown")
                    vr_message = vr.get("message", "")
                    vr_category = vr.get("category", "")
                    category_prefix = f"[{vr_category}] " if vr_category else ""
                    if vr.get("state") == "error":
                        vr_status = typer.style("ERROR", fg=typer.colors.RED)
                        reason = vr.get("error_reason")
                    elif vr.get("skipped"):
                        vr_status = typer.style("SKIPPED", fg=typer.colors.YELLOW)
                        reason = vr.get("skip_reason")
                    elif vr.get("passed", False):
                        vr_status = typer.style("PASSED", fg=typer.colors.GREEN)
                        reason = None
                    else:
                        vr_status = typer.style("FAILED", fg=typer.colors.RED)
                        reason = None
                    detail = f"{reason}: {vr_message}" if reason and vr_message else (reason or vr_message)
                    typer.echo(f"  {category_prefix}{vr_name}: {vr_status} - {detail}")

    typer.echo("-" * 60)
    if result.success:
        status = typer.style("[PASS]", fg=typer.colors.GREEN)
        typer.echo(f"{status} All phases completed successfully")
    else:
        status = typer.style("[FAIL]", fg=typer.colors.RED)
        typer.echo(f"{status} Orchestration failed")
        raise typer.Exit(code=1)


@app.command("validate")
def validate(
    config_files: Annotated[
        list[Path],
        typer.Option(
            "--config",
            "-f",
            help="YAML configuration file(s) to merge and validate.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    set_values: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Set values on the command line",
        ),
    ] = None,
    no_user_config: Annotated[
        bool,
        typer.Option(
            "--no-user-config",
            help="Do not apply persisted user config (config.yml / secrets.yml).",
        ),
    ] = False,
) -> None:
    """Validate merged configuration without running.

    Useful for checking configuration syntax and schema compliance
    before executing a test run.
    """
    apply_user_config(no_user_config)

    # Validate at least one config file is provided
    if not config_files:
        print_error("At least one --config/-f config file is required.")
        raise typer.Exit(code=1)

    print_progress(f"Validating {len(config_files)} configuration file(s)...")
    try:
        merged_config = merge_yaml_files([str(p) for p in config_files], set_values or [])
    except Exception as e:
        print_error(f"Failed to merge configuration files: {e}")
        raise typer.Exit(code=1)

    try:
        run_config = RunConfig.model_validate(merged_config)
        ok_status = typer.style("[OK]", fg=typer.colors.GREEN)
        typer.echo(f"{ok_status} Configuration is valid")
        typer.echo(f"\nPlatform: {run_config.tests.platform if run_config.tests else 'not specified'}")
        if run_config.commands:
            typer.echo(f"Commands defined: {list(run_config.commands.keys())}")
        if run_config.context:
            typer.echo(f"Context variables: {list(run_config.context.keys())}")
    except Exception as e:
        print_error(f"Validation failed: {e}")
        raise typer.Exit(code=1)
