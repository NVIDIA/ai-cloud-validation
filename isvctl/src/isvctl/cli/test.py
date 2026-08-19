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
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TextIO

import typer
import yaml
from isvtest.catalog import build_catalog, catalog_document, get_catalog_version
from isvtest.core.resolution import parse_validations, requirements_satisfied
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
from isvctl.config.schema import RunConfig
from isvctl.config.suite_resolution import (
    CONFIGS_ROOT,
    SuiteResolutionError,
    parse_capability,
    resolve_suite_name,
    select_suite,
)
from isvctl.orchestrator.loop import Orchestrator, Phase, _has_explicit_pytest_selection
from isvctl.reporting import check_upload_credentials, create_test_run, get_environment_config, update_test_run

logger = logging.getLogger(__name__)
CORE_REQUIREMENT_CONTEXT = "core"


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


def _junitxml_for_discovered_config(junitxml: Path, match: ProviderConfigMatch, total: int) -> Path:
    """Return a non-overlapping JUnit path for a discovered config run."""
    if total <= 1:
        return junitxml
    return junitxml.with_name(f"{junitxml.stem}-{match.config_path.stem}{junitxml.suffix}")


def _resolve_capability_context(config: RunConfig, capability: str | None, suite_label: str) -> str | None:
    """Return the requirement context a run should execute under.

    One rule for every entry path: a plain suite with no ``--capability`` runs
    its core checks. "Unfiltered" corresponds to no real ISV situation - nobody
    runs on vm and kubernetes at once - so a plain suite always has a context.
    Platform suites run under their declared capability and reject an explicit
    capability context. This still matters when a provider config imports a
    plain suite whose checks carry ``requires:`` metadata.
    """
    if config.tests and config.tests.capability:
        if capability is not None:
            raise SuiteResolutionError(
                f"--capability cannot be used with platform suite {suite_label!r}; "
                f"it already runs under capability {config.tests.capability!r}."
            )
        return config.tests.capability

    if capability is None:
        print_progress(f"No capability selected; running {suite_label!r} core checks.")
        return CORE_REQUIREMENT_CONTEXT

    entries = parse_validations(config.tests.validations if config.tests else {})
    if not any(capability in entry.requires for entry in entries):
        print_warning(f"No check in {suite_label!r} requires {capability}; running core checks only.")
    return capability


def _reported_capability(config: RunConfig, capability_context: str | None) -> str | None:
    """Return the capability to record on the uploaded test run.

    Two client-side spellings have to be translated before they leave the process.
    ``CORE_REQUIREMENT_CONTEXT`` is this module's word for "no capability", which
    the service records as NULL. A platform suite reports its declaration rather
    than the resolved filter context so the run cannot be mislabeled.
    """
    if config.tests and config.tests.capability:
        return config.tests.capability
    if capability_context == CORE_REQUIREMENT_CONTEXT:
        return None
    return capability_context


def _validation_result_detail(validation: dict[str, Any], reason: str | None = None) -> str:
    """Return concise success text while preserving skip and failure diagnostics."""
    message = str(validation.get("message", ""))
    summary = validation.get("subtest_summary")
    is_success = validation.get("passed", False) and not validation.get("skipped")
    if validation.get("state") != "error" and is_success and isinstance(summary, dict):
        passed = int(summary.get("passed", 0) or 0)
        failed = int(summary.get("failed", 0) or 0)
        skipped = int(summary.get("skipped", 0) or 0)
        total = int(summary.get("total", passed + failed + skipped) or 0)
        if total > 0:
            if passed == total:
                return f"{total} subtests passed"
            return f"{total} subtests: {passed} passed, {failed} failed, {skipped} skipped"
    return f"{reason}: {message}" if reason and message else str(reason or message)


def _human_readable_dry_run(
    config: RunConfig,
    capability: str | None,
    include_labels: list[str] | None = None,
    exclude_labels: list[str] | None = None,
    extra_pytest_args: list[str] | None = None,
) -> str:
    """Render the validation requirement plan without executing lifecycle steps.

    The config's own ``exclude`` block follows orchestrator precedence: test-name
    exclusions always apply, while label exclusions yield to CLI include labels
    or explicit pytest selection.
    """
    declared = config.tests.capability if config.tests and config.tests.capability else None
    suite_type = f"platform ({declared})" if declared else "plain"
    context = "not filtered" if capability is None else capability
    selected_labels = set(include_labels or [])
    config_exclude = (config.tests.exclude if config.tests and config.tests.exclude else None) or {}
    rejected_labels = set(exclude_labels or [])
    if not selected_labels and not _has_explicit_pytest_selection(extra_pytest_args):
        rejected_labels.update(config_exclude.get("labels") or [])
    rejected_tests = set(config_exclude.get("tests") or [])
    validations = config.tests.validations if config.tests else {}
    entries = parse_validations(validations)

    lines = [
        "Dry-run plan",
        f"  Suite type: {suite_type}",
        f"  Capability: {context}",
        f"  Checks: {len(entries)}",
    ]
    if selected_labels:
        lines.append(f"  Labels: {', '.join(sorted(selected_labels))} (all required)")
    if rejected_labels:
        lines.append(f"  Excluded labels: {', '.join(sorted(rejected_labels))}")
    if rejected_tests:
        lines.append(f"  Excluded tests: {', '.join(sorted(rejected_tests))}")

    for entry in entries:
        # Name exclusion first, mirroring resolve_entries' precedence.
        if entry.name in rejected_tests:
            lines.append(f"  [SKIP] {entry.name}: excluded by name")
        elif capability is not None and not requirements_satisfied(entry.requires, capability):
            requirement = ", ".join(entry.requires)
            lines.append(f"  [SKIP] {entry.name}: requires {requirement} (context: {capability})")
        elif missing_labels := sorted(selected_labels.difference(entry.labels)):
            lines.append(f"  [SKIP] {entry.name}: does not match all selected labels: {', '.join(missing_labels)}")
        elif matched_labels := sorted(rejected_labels.intersection(entry.labels)):
            lines.append(f"  [SKIP] {entry.name}: excluded by label: {', '.join(matched_labels)}")
        elif entry.requires:
            lines.append(f"  [RUN]  {entry.name}: requires {', '.join(entry.requires)}")
        else:
            lines.append(f"  [RUN]  {entry.name}")
    return "\n".join(lines)


@app.command(
    "run",
    # Allow pytest args after `--`, but reject unknown options before it so
    # stale flags like `--platform` fail loudly instead of being forwarded.
    context_settings={"allow_extra_args": True, "ignore_unknown_options": False},
)
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
            help="Use provider-backed --suite selection or provider-scoped --label discovery.",
        ),
    ] = None,
    suite: Annotated[
        str | None,
        typer.Option(
            "--suite",
            help="Run a canonical suite directly, or its provider-backed config with --provider.",
        ),
    ] = None,
    capability: Annotated[
        str | None,
        typer.Option(
            "--capability",
            help=(
                "Capability context used to filter check requirements (one of the platform suites). "
                "Omit it and a plain suite runs its core checks -- the same rule for --suite, -f and --label."
            ),
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
    exclude_labels: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-label",
            help="Exclude validations carrying this label (can be repeated).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Validate configuration and show what would be executed without running",
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
) -> None:
    """Run the full test lifecycle: setup cluster, run tests, teardown.

    Merges multiple YAML configuration files and executes the test pipeline.
    The setup command output is validated and used as inventory for tests.

    Use -- to pass additional arguments to pytest/isvtest.

    Examples:
        isvctl test run --suite storage --capability kubernetes --phase test
        isvctl test run --provider aws --suite k8s
        isvctl test run --provider aws --suite storage --label min_req
        isvctl test run --provider aws --label network
        isvctl test run -f lab.yaml -f commands.yaml -f suites/k8s.yaml
        isvctl test run -f config.yaml --set context.node_count=8
        isvctl test run -f config.yaml --phase setup
        isvctl test run -f config.yaml --label gpu --label slow
        isvctl test run -f config.yaml -- -v -s -k "test_name"
    """
    setup_logging(verbose)
    apply_user_config(no_user_config)

    # --color governs this command's own output, not just the pytest args built
    # from it below. click otherwise auto-detects a terminal, and under `deploy`
    # stdout is a pipe: the results summary would arrive unstyled in the log
    # while pytest, told explicitly, keeps its colors.
    if color in ("yes", "no"):
        ctx.color = color == "yes"

    try:
        capability_context = parse_capability(capability, CONFIGS_ROOT)
    except SuiteResolutionError as exc:
        print_error(str(exc))
        raise typer.Exit(code=1)

    suite_label: str | None = None

    if suite:
        try:
            selected_suite, selection_message = select_suite(suite, config_files, provider, configs_root=CONFIGS_ROOT)
        except SuiteResolutionError as exc:
            print_error(str(exc))
            raise typer.Exit(code=1)
        print_progress(selection_message)
        suite_label = selected_suite.name
        config_files = [selected_suite.config_path]
        provider = None

    if provider:
        if config_files:
            print_error("--provider discovery cannot be combined with --config/-f.")
            raise typer.Exit(code=1)
        if not labels:
            print_error("--provider requires either --suite NAME or at least one --label/-l.")
            raise typer.Exit(code=1)

        known_providers = list_providers(CONFIGS_ROOT)
        if provider not in known_providers:
            print_error(f"Unknown provider {provider!r}. Available providers: {', '.join(known_providers)}")
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
            typer.echo(json.dumps(_provider_discovery_plan(provider, labels, matches), indent=2))
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
                suite=None,
                capability=capability,
                set_values=set_values,
                phase=phase,
                labels=labels,
                exclude_labels=exclude_labels,
                dry_run=False,
                working_dir=working_dir,
                verbose=verbose,
                no_user_config=no_user_config,
                junitxml=_junitxml_for_discovered_config(junitxml, match, len(matches)),
                color=color,
                no_upload=no_upload,
                lab_id=lab_id,
                tags=tags,
                isv_software_version=isv_software_version,
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

    # Resolve the requirement context here rather than per entry path, so the
    # same config behaves identically whether it was reached via --suite, -f,
    # or --label discovery. Platform suites use their declared capability, which
    # lets imported plain-suite checks filter on their `requires:` metadata.
    suite_name = suite_label or resolve_suite_name(config_files, CONFIGS_ROOT)
    try:
        capability_context = _resolve_capability_context(
            config,
            capability_context,
            suite_name or config_files[0].stem,
        )
    except SuiteResolutionError as exc:
        print_error(str(exc))
        raise typer.Exit(code=1)

    if dry_run:
        typer.echo(_human_readable_dry_run(config, capability_context, labels, exclude_labels, extra_pytest_args))
        if extra_pytest_args:
            print_progress(f"\n--- Extra pytest args ---\n{extra_pytest_args}")
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
        print_progress("Creating test run in ISV Lab Service...")
        platform = (
            config.tests.capability
            if config.tests and config.tests.capability
            else next(iter(config.commands), "unknown")
        )
        test_run_id = create_test_run(
            lab_id=lab_id,
            platform=platform,
            tags=tags or ["validation-test", "isvctl"],
            start_time=start_time,
            isv_software_version=isv_software_version,
            suite=suite_name,
            capability=_reported_capability(config, capability_context),
        )
        if not test_run_id:
            print_warning("Failed to create test run, continuing without upload")
            upload_results = False

    # Run orchestration with log file capture (tee to _output/pytest-output.log)
    orchestrator = Orchestrator(config, working_dir=effective_working_dir)
    output_dir = get_output_dir()
    log_file_path = output_dir / "pytest-output.log"

    # Build test catalog early so it runs inside the TeeWriter context
    # (avoids logging errors from stale stream references after the log file closes)
    test_catalog_document: dict[str, Any] | None = None

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
                capability=capability_context,
                verbose=verbose,
                junitxml=str(junitxml),
            )
            if upload_results:
                try:
                    catalog_entries = build_catalog()
                    catalog_version = get_catalog_version()
                    test_catalog_document = catalog_document(catalog_entries, catalog_version)
                    print_progress(f"Built test catalog: {len(catalog_entries)} tests (version: {catalog_version})")
                    catalog_path = output_dir / "test_catalog.json"
                    catalog_path.write_text(json.dumps(test_catalog_document, indent=2))
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
            catalog_document=test_catalog_document,
        ):
            print_progress(typer.style("[OK]", fg=typer.colors.GREEN) + " Test results uploaded successfully")
        else:
            print_warning("Failed to upload test results")

    # Display results
    typer.echo("\n" + "=" * 60)
    typer.echo("ORCHESTRATION RESULTS")
    typer.echo("=" * 60)

    show_skipped_tests = bool(config.tests and config.tests.settings.get("show_skipped_tests", False))
    for phase_result in result.phases:
        phase_details = phase_result.details or {}
        displayed_validations = phase_details.get("validations", [])
        if not show_skipped_tests:
            displayed_validations = [
                validation for validation in displayed_validations if not validation.get("skipped")
            ]
            if phase_details.get("validations") and not displayed_validations and not phase_details.get("steps"):
                continue
        if phase_result.message.startswith("SKIPPED:"):
            status = typer.style("[SKIP]", fg=typer.colors.YELLOW)
        elif phase_result.success:
            status = typer.style("[PASS]", fg=typer.colors.GREEN)
        else:
            status = typer.style("[FAIL]", fg=typer.colors.RED)
        phase_name = (phase_result.name or phase_result.phase.value).upper().ljust(24)
        typer.echo(f"{status} {phase_name}: {phase_result.message}")

        # Display step details (schema validation, errors)
        if "steps" in phase_details:
            for step in phase_details["steps"]:
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
        if displayed_validations:
            for vr in displayed_validations:
                vr_name = vr.get("name", "unknown")
                # Handle case where name might be a dict (extract class name)
                if isinstance(vr_name, dict):
                    vr_name = next(iter(vr_name.keys()), "unknown")
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
                detail = _validation_result_detail(vr, reason)
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
        declared = run_config.tests.capability if run_config.tests else None
        typer.echo(f"\nCapability: {declared or 'not specified (plain suite)'}")
        if run_config.commands:
            typer.echo(f"Commands defined: {list(run_config.commands.keys())}")
        if run_config.context:
            typer.echo(f"Context variables: {list(run_config.context.keys())}")
    except Exception as e:
        print_error(f"Validation failed: {e}")
        raise typer.Exit(code=1)
