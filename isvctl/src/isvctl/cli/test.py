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
from isvtest.catalog import build_catalog, get_catalog_version
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
    PlannedRun,
    PlatformResolutionError,
    plan_platform_run,
    resolve_module_configs,
)
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.loop import Orchestrator, Phase
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


def _planned_run_plan(provider: str, selection: dict[str, Any], runs: list[PlannedRun]) -> dict[str, Any]:
    """Return a JSON-serializable plan for a platform/module selection dry-run."""
    return {
        "provider": provider,
        **selection,
        "runs": [
            {
                "config": str(run.config_path),
                "role": run.role,
                "platform": run.platform,
                "exclude_labels": list(run.exclude_labels),
            }
            for run in runs
        ],
    }


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

    Each config's platform-scoped excludes are unioned with any user
    ``--exclude-label`` values. A combined pass/fail summary is printed and the
    process exits 1 if any config failed. ``forward_kwargs`` are the remaining
    ``run()`` options, passed through unchanged.
    """
    total = len(runs)
    outcomes: list[tuple[Path, bool]] = []
    for planned in runs:
        combined_excludes = list(dict.fromkeys([*planned.exclude_labels, *(user_exclude_labels or [])]))
        print_progress(f"\n--- Running {planned.config_path} ({planned.role}) ---")
        try:
            run(
                ctx,
                config_files=[planned.config_path],
                provider=None,
                labels=labels,
                exclude_labels=combined_excludes or None,
                dry_run=False,
                junitxml=_junitxml_for_config(junitxml, planned.config_path, total),
                **forward_kwargs,
            )
            outcomes.append((planned.config_path, True))
        except typer.Exit as exc:
            outcomes.append((planned.config_path, exc.exit_code == 0))

    typer.echo("\n" + "=" * 60)
    typer.echo("PLATFORM/MODULE RUN SUMMARY")
    typer.echo("=" * 60)
    for config_path, ok in outcomes:
        status = typer.style("[PASS]", fg=typer.colors.GREEN) if ok else typer.style("[FAIL]", fg=typer.colors.RED)
        typer.echo(f"{status} {config_path}")
    if not all(ok for _, ok in outcomes):
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
            help="Run a whole platform column (its config + every module config) for --provider.",
        ),
    ] = None,
    modules: Annotated[
        list[str] | None,
        typer.Option(
            "--module",
            help="Run a single module suite for --provider (can be repeated).",
        ),
    ] = None,
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
        isvctl test run -f lab.yaml -f commands.yaml -f suites/k8s.yaml
        isvctl test run -f config.yaml --set context.node_count=8
        isvctl test run -f config.yaml --phase setup
        isvctl test run -f config.yaml --label gpu --label slow
        isvctl test run -f config.yaml -- -v -s -k "test_name"
    """
    setup_logging(verbose)
    apply_user_config(no_user_config)

    if platform and modules:
        print_error("--platform and --module are mutually exclusive.")
        raise typer.Exit(code=1)
    if (platform or modules) and config_files:
        print_error("--platform/--module cannot be combined with --config/-f.")
        raise typer.Exit(code=1)
    if (platform or modules) and not provider:
        print_error("--platform/--module require --provider.")
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
                if platform:
                    runs = plan_platform_run(provider, platform, configs_root=CONFIGS_ROOT)
                    selection: dict[str, Any] = {"platform": platform}
                else:
                    runs = resolve_module_configs(provider, modules or [], configs_root=CONFIGS_ROOT)
                    selection = {"modules": modules}
            except PlatformResolutionError as exc:
                print_error(str(exc))
                raise typer.Exit(code=1)

            if dry_run:
                typer.echo(json.dumps(_planned_run_plan(provider, selection, runs), indent=2))
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
        # Keep stdout as pure JSON so it can be consumed with `json.loads`;
        # decorative headers and extra args go to stderr.
        print_progress("\n--- Dry Run: Configuration ---")
        redacted_config = redact_dict(config.model_dump(mode="json"))
        typer.echo(json.dumps(redacted_config, indent=2))
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
        platform = config.tests.platform if config.tests and config.tests.platform else "kubernetes"
        test_run_id = create_test_run(
            lab_id=lab_id,
            platform=platform,
            tags=tags or ["validation-test", "isvctl"],
            start_time=start_time,
            isv_software_version=isv_software_version,
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
                verbose=verbose,
                junitxml=str(junitxml),
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
