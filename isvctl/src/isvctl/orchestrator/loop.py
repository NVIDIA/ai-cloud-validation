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

"""Main orchestration loop for isvctl.

This module implements the test lifecycle using step-based execution:
1. Execute steps grouped by phase (defined in config's `phases` list)
2. Run validations after each phase
"""

import logging
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from isvtest.core.resolution import (
    ErrorReason,
    ResolvedEntry,
    SkipReason,
    State,
    ValidationEntry,
    get_entry_phase,
    parse_validations,
    resolve_class_key,
    resolve_entries,
)
from isvtest.main import run_validations_via_pytest
from isvtest.release_manifest import INCLUDE_UNRELEASED_ENV, load_released_test_filter

from isvctl.config.schema import RunConfig
from isvctl.orchestrator.commands import CommandExecutor
from isvctl.orchestrator.context import Context
from isvctl.orchestrator.step_executor import StepExecutor, StepResults
from isvctl.redaction import redact_dict, redact_junit_xml_tree

logger = logging.getLogger(__name__)


class Phase(StrEnum):
    """Test lifecycle phases."""

    ALL = "all"
    SETUP = "setup"
    TEST = "test"
    TEARDOWN = "teardown"


@dataclass
class PhaseResult:
    """Result of a single phase execution.

    Attributes:
        phase: Which phase was executed
        success: Whether the phase succeeded
        message: Human-readable status message
        details: Additional details (command output, test results, etc.)
    """

    phase: Phase
    success: bool
    message: str
    details: dict[str, Any] | None = None


@dataclass
class OrchestratorResult:
    """Result of the full orchestration run.

    Attributes:
        success: Whether all phases succeeded
        phases: Results for each phase that was executed
        inventory: Final inventory data (step outputs)
    """

    success: bool
    phases: list[PhaseResult]
    inventory: dict[str, Any] | None = None
    validations: list[ResolvedEntry] = field(default_factory=list)


def _fold_suite(target: ET.Element, source: ET.Element) -> None:
    """Append source's testcases into target and add their counter contributions.

    Used when multiple per-phase XML files contribute to the same logical
    testsuite (pytest's run + the orchestrator's pre-resolved / deselected
    stubs for the same phase). Both producers always emit the counter and
    time attributes, so we parse them strictly: a missing/malformed value
    indicates a bug we want to surface, not paper over.
    """
    for case in source.findall("testcase"):
        target.append(case)
    for attr in ("tests", "failures", "errors", "skipped"):
        target.set(attr, str(int(target.get(attr, "0")) + int(source.get(attr, "0"))))
    target.set("time", f"{float(target.get('time', '0')) + float(source.get('time', '0')):.3f}")


def _sort_suite_testcases(suite: ET.Element, name_order: list[str]) -> None:
    """Reorder a suite's <testcase> children to match a canonical name order.

    Pre-resolved stub testcases get appended before pytest's ones during
    merge, which makes the dashboard show every skipped check at the top of
    the suite. Sorting here puts each testcase back where its name appears
    in the YAML config, interleaving skips with the rest.

    Subtests carry the parent name before ``::``; they sort with their
    parent and keep their relative order via Python's stable sort.
    """
    order_index: dict[str, int] = {}
    for i, name in enumerate(name_order):
        order_index.setdefault(name, i)
    fallback = len(name_order)

    def key(case: ET.Element) -> int:
        case_name = case.get("name") or ""
        return order_index.get(case_name.split("::", 1)[0], fallback)

    # ElementTree has no in-place sort; remove + re-append in sorted order
    # leaves each case at the end of the suite in turn, so after we touch all
    # of them they're laid out in sorted order. Stable sort keeps subtests in
    # pytest's emitted order beside their parent.
    for case in sorted(suite.findall("testcase"), key=key):
        suite.remove(case)
        suite.append(case)


def _merge_junit_xmls(
    phase_files: list[Path],
    output_path: Path,
    name_order: list[str] | None = None,
) -> None:
    """Merge multiple per-phase JUnit XML files into a single file.

    Combines all <testsuite> elements from each phase file into a single
    <testsuites> root element. Suites with the same ``name`` are folded into
    a single testsuite (testcases concatenated, counters summed) so a phase's
    pytest output and its pre-resolved/deselected stubs render as one suite
    on dashboards instead of separate cards.

    Args:
        phase_files: List of per-phase JUnit XML file paths
        output_path: Final output path for the merged XML
        name_order: Optional list of validation names defining the canonical
            order. When provided, each merged suite's testcases are sorted to
            match it (typically YAML config order from ``parse_validations``).
    """
    root = ET.Element("testsuites")
    root.set("name", "isvctl validation tests")
    suites_by_name: dict[str, ET.Element] = {}

    for phase_file in phase_files:
        if not phase_file.exists():
            continue
        try:
            tree = ET.parse(phase_file)
        except ET.ParseError:
            logger.warning(f"Failed to parse JUnit XML: {phase_file}")
            continue
        phase_root = tree.getroot()
        if phase_root.tag == "testsuites":
            suites = [s for s in phase_root if s.tag == "testsuite"]
        elif phase_root.tag == "testsuite":
            suites = [phase_root]
        else:
            continue
        for suite in suites:
            name = suite.get("name") or ""
            existing = suites_by_name.get(name)
            if existing is None:
                suites_by_name[name] = suite
                root.append(suite)
            else:
                _fold_suite(existing, suite)

    if name_order:
        for suite in suites_by_name.values():
            _sort_suite_testcases(suite, name_order)

    redact_junit_xml_tree(root)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")
    tree.write(str(output_path), encoding="utf-8", xml_declaration=True)
    logger.info(f"JUnit XML report written to: {output_path}")


def _entries_missing_from_junit(entries: list[ResolvedEntry], junit_path: Path | None) -> list[ResolvedEntry]:
    """Return executed entries whose names aren't present in a pytest JUnit XML.

    Entries deselected by ``-k``/``-m`` are returned by pytest with a terminal
    state but never appear in pytest's XML. This helper finds them so the
    orchestrator can emit a stub testcase for each, preserving the "every
    config-declared validation reaches the report" invariant.

    A non-existent file is the normal path for phases that ran no pytest
    validations (e.g., setup/teardown); silently return the entries as-is.
    A file that exists but fails to parse is a real problem worth logging.
    """
    if junit_path is None or not junit_path.exists():
        return list(entries)
    try:
        tree = ET.parse(junit_path)
    except ET.ParseError:
        logger.warning(f"Failed to parse JUnit XML: {junit_path}; treating all executed entries as missing")
        return list(entries)
    captured = {case.get("name") for case in tree.iter("testcase") if case.get("name")}
    return [entry for entry in entries if entry.entry.name not in captured]


def _write_terminal_junit_xml(entries: list[ResolvedEntry], output_path: Path, suite_name: str) -> None:
    """Write JUnit XML for terminal entries that did not run through pytest.

    Attribute layout mirrors pytest's: ``type`` carries the typed reason (enum
    value), ``message`` carries the human-readable text. The element body is
    left empty for skipped entries (no stack trace exists) so dashboards do
    not render a misleading "Stack Trace: <same as message>" panel; error
    entries keep the body so any captured diagnostic survives.
    """
    suite = ET.Element("testsuite")
    suite.set("name", suite_name)
    suite.set("tests", str(len(entries)))
    suite.set("failures", "0")
    suite.set("errors", str(sum(1 for entry in entries if entry.state == State.ERROR)))
    suite.set("skipped", str(sum(1 for entry in entries if entry.state == State.SKIPPED)))
    suite.set("time", f"{sum(entry.duration_seconds for entry in entries):.3f}")

    for entry in entries:
        case = ET.SubElement(suite, "testcase")
        case.set("name", entry.entry.name)
        case.set("time", f"{entry.duration_seconds:.3f}")
        if entry.state == State.SKIPPED:
            reason = entry.skip_reason or SkipReason.EXCLUDED
            skipped = ET.SubElement(case, "skipped")
            skipped.set("type", reason.value)
            skipped.set("message", entry.message or reason.value)
        elif entry.state == State.ERROR:
            reason = entry.error_reason or ErrorReason.RUNTIME_EXCEPTION
            error = ET.SubElement(case, "error")
            error.set("type", reason.value)
            error.set("message", entry.message or reason.value)
            if entry.message:
                error.text = entry.message

    tree = ET.ElementTree(suite)
    ET.indent(tree, space="    ")
    tree.write(str(output_path), encoding="utf-8", xml_declaration=True)


def _resolved_entry_to_result_dict(entry: ResolvedEntry) -> dict[str, Any]:
    """Convert a resolved entry to the CLI/result detail dictionary."""
    skipped = entry.state == State.SKIPPED
    passed = entry.state in {State.PASSED, State.SKIPPED}
    return {
        "name": entry.entry.name,
        "passed": passed,
        "skipped": skipped,
        "message": entry.message,
        "category": entry.entry.category,
        "labels": list(entry.entry.labels),
        "state": entry.state.value if entry.state else None,
        "skip_reason": entry.skip_reason.value if entry.skip_reason else None,
        "error_reason": entry.error_reason.value if entry.error_reason else None,
    }


def _resolved_entry_success(entry: ResolvedEntry) -> bool:
    """Return whether a resolved validation outcome should keep the phase successful."""
    return entry.state in {State.PASSED, State.SKIPPED}


def _phase_enum_for_name(phase_name: str) -> Phase:
    """Map a configured phase name to the display enum."""
    if phase_name == "setup":
        return Phase.SETUP
    if phase_name == "teardown":
        return Phase.TEARDOWN
    return Phase.TEST


def _requested_config_phases(config_phases: list[str], requested_phases: list[Phase]) -> list[str]:
    """Return configured phase names selected by the requested phase filter."""
    if Phase.ALL in requested_phases:
        return config_phases

    requested_phase_names = {phase.value for phase in requested_phases}
    return [phase for phase in config_phases if phase in requested_phase_names]


def _has_explicit_pytest_selection(extra_pytest_args: list[str] | None) -> bool:
    """Return whether pytest args explicitly select tests or markers."""
    if not extra_pytest_args:
        return False
    return any(
        arg == "-k" or arg.startswith("-k=") or arg == "-m" or arg.startswith("-m=") for arg in extra_pytest_args
    )


def _apply_step_validation_gates(steps: list[Any], released_tests: set[str] | None) -> list[Any]:
    """Mark steps skipped when their required validations are unavailable."""
    if released_tests is None:
        return steps

    gated_steps: list[Any] = []
    for step in steps:
        required_validations = getattr(step, "requires_available_validations", [])
        unavailable = [
            validation for validation in required_validations if resolve_class_key(validation, released_tests) is None
        ]
        if not unavailable:
            gated_steps.append(step)
            continue
        skipped_step = step.model_copy(update={"skip": True})
        logger.info(
            "Skipping step '%s' because required validation(s) are unavailable: %s",
            skipped_step.name,
            ", ".join(unavailable),
        )
        gated_steps.append(skipped_step)
    return gated_steps


class Orchestrator:
    """Orchestrates the full test lifecycle using step-based execution.

    The orchestrator:
    1. Executes steps grouped by phase (in order defined by config's `phases` list)
    2. Runs validations after each phase
    3. Handles failure and teardown logic
    """

    def __init__(
        self,
        config: RunConfig,
        working_dir: str | Path | None = None,
    ) -> None:
        """Initialize orchestrator.

        Args:
            config: Merged test run configuration
            working_dir: Working directory for command execution
        """
        self.config = config
        self.context = Context(config)
        self.executor = CommandExecutor(working_dir=working_dir)
        self.step_executor = StepExecutor(working_dir=working_dir)
        self._results: list[PhaseResult] = []
        self._extra_pytest_args: list[str] | None = None
        self._include_labels: list[str] = []
        self._verbose: bool = False
        self._junitxml: str | None = None

    def run(
        self,
        phases: list[Phase] | None = None,
        teardown_on_failure: bool = True,
        extra_pytest_args: list[str] | None = None,
        include_labels: list[str] | None = None,
        verbose: bool = False,
        junitxml: str | None = None,
    ) -> OrchestratorResult:
        """Run the test lifecycle.

        Args:
            phases: Specific phases to run (default: all)
            teardown_on_failure: Run teardown even if earlier phases fail
            extra_pytest_args: Pytest arguments for validations (-k, -m, -v, etc.)
                - `-k AccessKey`: Run only validations matching "AccessKey"
                - `-m kubernetes`: Run only validations whose labels include "kubernetes"
                  (labels are mirrored as pytest marks)
            include_labels: Labels that selected validations must all contain.
            verbose: Enable verbose output for validations
            junitxml: Path to write JUnit XML report for validations

        Returns:
            OrchestratorResult with phase results
        """
        if phases is None:
            phases = [Phase.SETUP, Phase.TEST, Phase.TEARDOWN]

        self._results = []
        self._extra_pytest_args = extra_pytest_args
        self._include_labels = include_labels or []
        self._verbose = verbose
        self._junitxml = junitxml

        platform = self._detect_platform()
        if not platform:
            return OrchestratorResult(
                success=False,
                phases=[
                    PhaseResult(
                        phase=Phase.SETUP,
                        success=False,
                        message="Cannot determine platform from configuration",
                    )
                ],
            )

        logger.info(f"Starting orchestration for platform: {platform}")

        return self._run_steps_mode(platform, phases, teardown_on_failure)

    def _run_steps_mode(
        self,
        platform: str,
        requested_phases: list[Phase],
        teardown_on_failure: bool,
    ) -> OrchestratorResult:
        """Run orchestration using the steps mode with phase-based validations.

        Phases are defined in the config's `phases` list and executed in that order.
        For each phase:
        1. Execute all steps with that phase
        2. Run all validations matching that phase (inferred from step or default to test)

        Args:
            platform: Target platform
            requested_phases: CLI-requested phases (for filtering)
            teardown_on_failure: Whether to run teardown on failure

        Returns:
            OrchestratorResult with step outcomes
        """
        logger.info(f"Running in steps mode for platform: {platform}")

        config_phases = self.config.get_phases(platform)
        if self.config.commands[platform].skip:
            skipped_phases = _requested_config_phases(config_phases, requested_phases)
            return OrchestratorResult(
                success=True,
                phases=[
                    PhaseResult(
                        phase=_phase_enum_for_name(phase_name),
                        success=True,
                        message=f"SKIPPED: platform '{platform}' is skipped by configuration",
                    )
                    for phase_name in skipped_phases
                ],
                inventory={},
            )

        steps = self.config.get_steps(platform)
        if not steps:
            return OrchestratorResult(
                success=False,
                phases=[
                    PhaseResult(
                        phase=Phase.SETUP,
                        success=False,
                        message=f"No steps defined for platform: {platform}",
                    )
                ],
            )

        released_tests = load_released_test_filter()
        if released_tests is None:
            logger.info(f"Including unreleased validations because {INCLUDE_UNRELEASED_ENV} is enabled")

        steps = _apply_step_validation_gates(steps, released_tests)

        logger.info(f"Configured phases: {config_phases}")

        for step in steps:
            step_phase = (step.phase or "setup").lower()
            if step_phase not in config_phases:
                return OrchestratorResult(
                    success=False,
                    phases=[
                        PhaseResult(
                            phase=Phase.SETUP,
                            success=False,
                            message=f"Step '{step.name}' has phase '{step_phase}' not in phases list: {config_phases}",
                        )
                    ],
                )

        steps_by_phase: dict[str, list] = {phase: [] for phase in config_phases}
        for step in steps:
            step_phase = (step.phase or "setup").lower()
            steps_by_phase[step_phase].append(step)

        # Register step phases upfront so validation phase inference works
        # even before a step has executed. Skipped steps are excluded so
        # their validations are also skipped automatically.
        for step in steps:
            if step.skip:
                continue
            step_phase = (step.phase or "setup").lower()
            self.context.set_step_phase(step.name, step_phase)

        all_validations = {}
        if self.config.tests and self.config.tests.validations:
            all_validations = self.config.tests.validations
        validation_entries = parse_validations(all_validations)
        resolved_validations_by_index: dict[int, ResolvedEntry] = {}

        exclude_labels: list[str] = []
        exclude_tests: list[str] = []
        if self.config.tests and self.config.tests.exclude:
            exclude_labels = self.config.tests.exclude.get("labels", [])
            exclude_tests = self.config.tests.exclude.get("tests", [])
        skip_config_label_exclusions = bool(self._include_labels) or _has_explicit_pytest_selection(
            self._extra_pytest_args
        )
        resolution_exclude_labels = [] if skip_config_label_exclusions else exclude_labels

        phase_results: list[PhaseResult] = []
        overall_success = True
        setup_steps_ran = False

        requested_phase_names = {p.value for p in requested_phases}

        # Per-phase JUnit XML files merge at the end so later phases don't
        # overwrite earlier ones.
        junit_tmpdir: str | None = None
        phase_junit_files: list[Path] = []

        try:
            if self._junitxml:
                junit_tmpdir = tempfile.mkdtemp(prefix="junit-phases-")

            for phase_name in config_phases:
                if phase_name not in requested_phase_names and Phase.ALL not in requested_phases:
                    continue
                phase_steps = steps_by_phase.get(phase_name, [])
                phase_enum = _phase_enum_for_name(phase_name)

                is_teardown = phase_name == "teardown"
                skip_reason: str | None = None

                if not overall_success and not is_teardown:
                    skip_reason = "previous phase failed"

                # Teardown gating depends on whether setup was part of this run:
                # - If setup was requested alongside teardown (full lifecycle), skip
                #   teardown only when setup steps never actually executed.
                # - If teardown was requested alone (e.g., `--phase teardown`), run
                #   it unconditionally - the user is explicitly cleaning up resources
                #   from a previous run.
                if is_teardown:
                    setup_was_requested = "setup" in requested_phase_names or Phase.ALL in requested_phases
                    if setup_was_requested and not setup_steps_ran:
                        skip_reason = "setup steps did not run"
                    elif not overall_success and not teardown_on_failure:
                        skip_reason = "teardown_on_failure is disabled"

                if skip_reason:
                    logger.info(f"Skipping {phase_name}: {skip_reason}")
                    phase_results.append(
                        PhaseResult(
                            phase=phase_enum,
                            success=True,
                            message=f"SKIPPED: {skip_reason}",
                        )
                    )
                    continue

                if phase_steps:
                    step_results = self.step_executor.execute_steps(phase_steps, self.context, best_effort=is_teardown)
                else:
                    step_results = StepResults()

                # ``step_results.steps`` includes placeholder records for skip:true
                # steps; require at least one step that wasn't skipped before letting
                # teardown run (otherwise a fully-skipped setup would falsely "satisfy"
                # the teardown gate even though no resources were created).
                if phase_name == "setup" and any(not step.skip for step in phase_steps):
                    setup_steps_ran = True

                phase_junitxml: str | None = None
                if junit_tmpdir:
                    phase_junitxml = str(Path(junit_tmpdir) / f"junit-{phase_name}.xml")

                step_phases_snapshot = self.context.get_all_step_phases()
                phase_entry_indexes = [
                    index
                    for index, entry in enumerate(validation_entries)
                    if index not in resolved_validations_by_index
                    and get_entry_phase(entry, step_phases_snapshot) == phase_name
                ]
                phase_entries = [validation_entries[index] for index in phase_entry_indexes]
                resolved_phase_entries = self._resolve_validation_entries(
                    phase_entries,
                    requested_phase_names if Phase.ALL not in requested_phases else set(config_phases),
                    set(self._include_labels),
                    set(resolution_exclude_labels),
                    set(exclude_tests),
                    released_tests,
                )
                ready_entries = [entry for entry in resolved_phase_entries if entry.is_ready]
                terminal_before_pytest = [entry for entry in resolved_phase_entries if not entry.is_ready]

                # Write a per-phase stub for entries pre-resolved to SKIPPED/ERROR.
                # Suite name matches pytest's so the merger collapses them into one suite.
                if terminal_before_pytest and junit_tmpdir:
                    terminal_junit = Path(junit_tmpdir) / f"junit-{phase_name}-resolved.xml"
                    _write_terminal_junit_xml(terminal_before_pytest, terminal_junit, f"{platform}/{phase_name}")
                    phase_junit_files.append(terminal_junit)

                test_settings = self.config.tests.settings if self.config.tests else {}
                if ready_entries:
                    _exit_code, executed_entries = run_validations_via_pytest(
                        entries=ready_entries,
                        settings=test_settings,
                        inventory=self.context.get_accumulated_context(),
                        extra_pytest_args=self._extra_pytest_args,
                        verbose=self._verbose,
                        junitxml=phase_junitxml,
                        suite_name=f"{platform}/{phase_name}",
                    )
                else:
                    executed_entries = []

                phase_junit_path = Path(phase_junitxml) if phase_junitxml else None
                if phase_junit_path and phase_junit_path.exists():
                    phase_junit_files.append(phase_junit_path)

                # Entries deselected by pytest -k / -m are terminalized in memory but
                # not written to pytest's XML. Stub them so they reach the merged report.
                missing_executed = _entries_missing_from_junit(executed_entries, phase_junit_path)
                if missing_executed and junit_tmpdir:
                    deselected_junit = Path(junit_tmpdir) / f"junit-{phase_name}-deselected.xml"
                    _write_terminal_junit_xml(missing_executed, deselected_junit, f"{platform}/{phase_name}")
                    phase_junit_files.append(deselected_junit)

                executed_iter = iter(executed_entries)
                terminal_phase_entries = [
                    next(executed_iter) if resolved_entry.is_ready else resolved_entry
                    for resolved_entry in resolved_phase_entries
                ]
                for index, resolved_entry in zip(phase_entry_indexes, terminal_phase_entries, strict=True):
                    resolved_validations_by_index[index] = resolved_entry

                phase_validations = [_resolved_entry_to_result_dict(entry) for entry in terminal_phase_entries]

                if phase_steps or phase_validations:
                    phase_results.append(
                        self._create_phase_result(phase_enum, step_results, phase_validations, phase_name)
                    )

                phase_success = step_results.success and all(v.get("passed", False) for v in phase_validations)
                if not phase_success:
                    overall_success = False

            remaining_entries = [
                (index, entry)
                for index, entry in enumerate(validation_entries)
                if index not in resolved_validations_by_index
            ]
            if remaining_entries:
                terminal_remaining = self._resolve_remaining_validation_entries(
                    remaining_entries,
                    requested_phase_names if Phase.ALL not in requested_phases else set(config_phases),
                    set(self._include_labels),
                    set(resolution_exclude_labels),
                    set(exclude_tests),
                    released_tests,
                    config_phases,
                )
                for index, resolved_entry in terminal_remaining:
                    resolved_validations_by_index[index] = resolved_entry
                if junit_tmpdir:
                    step_phases_for_unexecuted = self.context.get_all_step_phases()
                    unexecuted_by_phase: dict[str, list[ResolvedEntry]] = {}
                    for _, resolved_entry in terminal_remaining:
                        entry_phase = get_entry_phase(resolved_entry.entry, step_phases_for_unexecuted)
                        unexecuted_by_phase.setdefault(entry_phase, []).append(resolved_entry)
                    for entry_phase, entries_for_phase in unexecuted_by_phase.items():
                        terminal_junit = Path(junit_tmpdir) / f"junit-{entry_phase}-unexecuted.xml"
                        _write_terminal_junit_xml(entries_for_phase, terminal_junit, f"{platform}/{entry_phase}")
                        phase_junit_files.append(terminal_junit)
                self._append_resolution_only_phase_results(phase_results, terminal_remaining)
                if not all(_resolved_entry_success(resolved_entry) for _, resolved_entry in terminal_remaining):
                    overall_success = False

            if self._junitxml and phase_junit_files:
                _merge_junit_xmls(
                    phase_junit_files,
                    Path(self._junitxml),
                    name_order=[entry.name for entry in validation_entries],
                )

        finally:
            if junit_tmpdir:
                shutil.rmtree(junit_tmpdir, ignore_errors=True)

        return OrchestratorResult(
            success=overall_success,
            phases=phase_results,
            inventory=self.context.get_accumulated_context().get("steps", {}),
            validations=[resolved_validations_by_index[index] for index in sorted(resolved_validations_by_index)],
        )

    def _create_phase_result(
        self,
        phase: Phase,
        step_results: StepResults,
        validation_results: list[dict],
        phase_name: str | None = None,
    ) -> PhaseResult:
        """Create a PhaseResult from step execution and validation results.

        Args:
            phase: The phase enum for display
            step_results: Results from step execution
            validation_results: Results from phase validations
            phase_name: Custom phase name (for non-standard phases)

        Returns:
            PhaseResult for the phase
        """
        display_name = phase_name or phase.value

        # Build step messages
        step_messages = []
        for step in step_results.steps:
            if step.error == "Step skipped":
                status = "skipped"
            elif step.success:
                status = "passed"
            else:
                status = "failed"
            step_messages.append(f"{step.name}: {status}")

        # Check if any validations failed
        validation_failures = [v for v in validation_results if not v.get("passed", False)]
        all_validations_passed = len(validation_failures) == 0

        # Overall phase success
        phase_success = step_results.success and all_validations_passed

        # Build message
        if step_messages:
            message = "; ".join(step_messages)
        else:
            message = f"{display_name} phase completed"

        return PhaseResult(
            phase=phase,
            success=phase_success,
            message=message,
            details={
                "steps": [
                    {
                        "name": s.name,
                        "success": s.success,
                        "error": s.error,
                        "output": redact_dict(s.output),
                        "schema_name": s.schema_name,
                        "schema_valid": s.schema_valid,
                        "schema_errors": s.schema_errors,
                    }
                    for s in step_results.steps
                ],
                "validations": validation_results,
            },
        )

    def _resolve_validation_entries(
        self,
        entries: list[ValidationEntry],
        requested_phase_names: set[str],
        include_labels: set[str],
        exclude_labels: set[str],
        exclude_tests: set[str],
        released_tests: set[str] | None,
    ) -> list[ResolvedEntry]:
        """Resolve validation entries against the current orchestration context."""
        step_outputs = self.context.get_accumulated_context().get("steps", {})
        return resolve_entries(
            entries,
            step_outputs=step_outputs,
            step_phases=self.context.get_all_step_phases(),
            requested_phases=requested_phase_names,
            include_labels=include_labels,
            exclude_labels=exclude_labels,
            exclude_tests=exclude_tests,
            released_tests=released_tests,
            render_context=self.context.get_accumulated_context(),
        )

    def _resolve_remaining_validation_entries(
        self,
        entries: list[tuple[int, ValidationEntry]],
        requested_phase_names: set[str],
        include_labels: set[str],
        exclude_labels: set[str],
        exclude_tests: set[str],
        released_tests: set[str] | None,
        config_phases: list[str],
    ) -> list[tuple[int, ResolvedEntry]]:
        """Resolve entries left after the phase loop and terminalize ready entries."""
        resolved_entries = self._resolve_validation_entries(
            [entry for _, entry in entries],
            requested_phase_names,
            include_labels,
            exclude_labels,
            exclude_tests,
            released_tests,
        )
        step_phases = self.context.get_all_step_phases()
        terminal_entries: list[tuple[int, ResolvedEntry]] = []
        for (index, entry), resolved in zip(entries, resolved_entries, strict=True):
            if resolved.is_ready:
                phase_name = get_entry_phase(entry, step_phases)
                if phase_name in config_phases:
                    message = f"phase '{phase_name}' did not run"
                else:
                    message = f"phase '{phase_name}' is not configured for this run"
                resolved = ResolvedEntry(
                    entry=entry,
                    rendered_params=resolved.rendered_params,
                    state=State.SKIPPED,
                    skip_reason=SkipReason.PHASE_NOT_REQUESTED,
                    message=message,
                )
            terminal_entries.append((index, resolved))
        return terminal_entries

    def _append_resolution_only_phase_results(
        self,
        phase_results: list[PhaseResult],
        entries: list[tuple[int, ResolvedEntry]],
    ) -> None:
        """Append phase results that only contain unexecuted validation outcomes."""
        step_phases = self.context.get_all_step_phases()
        by_phase: dict[str, list[ResolvedEntry]] = {}
        for _, entry in entries:
            phase_name = get_entry_phase(entry.entry, step_phases)
            by_phase.setdefault(phase_name, []).append(entry)

        for phase_name, resolved_entries in by_phase.items():
            phase_results.append(
                PhaseResult(
                    phase=_phase_enum_for_name(phase_name),
                    success=all(_resolved_entry_success(entry) for entry in resolved_entries),
                    message=f"{phase_name} phase validations resolved without execution",
                    details={
                        "steps": [],
                        "validations": [_resolved_entry_to_result_dict(entry) for entry in resolved_entries],
                    },
                )
            )

    def _detect_platform(self) -> str | None:
        """Detect platform from configuration.

        Checks multiple locations for platform:
        1. tests.platform (isvctl schema)
        2. Root-level platform (legacy isvtest schema)

        Returns:
            Platform string (e.g., 'kubernetes', 'slurm', 'bare_metal') or None
        """
        platform = None

        # Check isvctl schema location first
        if self.config.tests and self.config.tests.platform:
            platform = self.config.tests.platform
        # Fall back to root-level platform (legacy isvtest configs)
        elif hasattr(self.config, "model_extra") and self.config.model_extra:
            platform = self.config.model_extra.get("platform")

        if platform:
            # Normalize 'k8s' to 'kubernetes'
            if platform == "k8s":
                return "kubernetes"
            return platform
        return None
