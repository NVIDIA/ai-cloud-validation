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

"""CLI entry point for isvreporter - ISV Lab test results reporting tool."""

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from isvreporter import get_endpoint, get_ssa_issuer
from isvreporter.auth import get_jwt_token
from isvreporter.client import (
    calculate_duration,
    create_test_run,
    load_test_run_id,
    report_test_results,
    update_test_run,
    upload_test_catalog,
)
from isvreporter.platform import get_axes_from_config, is_valid_platform, normalize_platform
from isvreporter.version import get_version

app = typer.Typer(
    name="report",
    help="Report ISV Lab test results to the ISV Lab Service API",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"isvreporter {get_version('isvreporter')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Show version and exit.", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Report ISV Lab test results to the ISV Lab Service API."""


def _get_credentials() -> tuple[str, str, str, str]:
    """Get ISV Lab Service credentials from environment.

    Returns:
        Tuple of (endpoint, ssa_issuer, client_id, client_secret)

    Raises:
        typer.Exit: If required credentials are missing
    """
    endpoint = get_endpoint()
    ssa_issuer = get_ssa_issuer()
    client_id = os.environ.get("ISV_CLIENT_ID")
    client_secret = os.environ.get("ISV_CLIENT_SECRET")

    if not client_id or not client_secret:
        typer.echo("ERROR: Missing required environment variables:", err=True)
        typer.echo("  ISV_CLIENT_ID", err=True)
        typer.echo("  ISV_CLIENT_SECRET", err=True)
        raise typer.Exit(code=1)

    return endpoint, ssa_issuer, client_id, client_secret


@app.command("create")
def create(
    lab_id: Annotated[
        int,
        typer.Option("--lab-id", "-l", help="Lab ID"),
    ],
    tags: Annotated[
        list[str],
        typer.Option("--tag", "-t", help="Tags for the test run (can be repeated)"),
    ],
    executed_by: Annotated[
        str,
        typer.Option("--executed-by", "-e", help="Who/what executed the test run"),
    ],
    ci_reference: Annotated[
        str,
        typer.Option("--ci-reference", "-c", help="CI job URL or reference"),
    ],
    start_time: Annotated[
        str,
        typer.Option("--start-time", "-s", help="Test run start time (ISO 8601 format)"),
    ],
    platform: Annotated[
        str | None,
        typer.Option(
            "--platform",
            "-p",
            help="Capability the run targets (kubernetes, slurm, bare_metal, vm). Auto-detected from --config if not provided.",
        ),
    ] = None,
    module: Annotated[
        str | None,
        typer.Option(
            "--module",
            "-m",
            help="Module the run exercises (iam, network, ...). Auto-detected from --config if not provided.",
        ),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-f",
            help="Path to isvctl config YAML file (auto-detects capability/module from 'tests.platform'/'tests.module')",
            exists=True,
        ),
    ] = None,
    isv_software_version: Annotated[
        str | None,
        typer.Option(
            "--isv-software-version",
            help="ISV software stack version (opaque string provided by ISV)",
        ),
    ] = None,
    isv_test_version: Annotated[
        str | None,
        typer.Option(
            "--isv-test-version",
            help="ISV test tool version (e.g., '1.12.3')",
        ),
    ] = None,
) -> None:
    """Create a new test run in ISV Lab Service.

    Examples:
        isvctl report create --lab-id 3 --tag validation --executed-by gitlab-ci \\
            --ci-reference "$CI_JOB_URL" --start-time "$CI_JOB_STARTED_AT"
    """
    endpoint, ssa_issuer, client_id, client_secret = _get_credentials()

    # Determine the (capability, module) axis pair:
    # explicit args > config file > default capability (kubernetes).
    capability: str | None
    detected_module: str | None
    if platform and is_valid_platform(platform):
        capability = normalize_platform(platform)
        detected_module = module.strip().upper().replace("-", "_") if module else None
    elif module:
        capability = None
        detected_module = module.strip().upper().replace("-", "_")
    elif config:
        capability, detected_module = get_axes_from_config(config)
        if capability is None and detected_module is None:
            capability = "KUBERNETES"
    else:
        capability, detected_module = "KUBERNETES", None

    axes = ", ".join(
        part
        for part in (
            f"capability={capability}" if capability else None,
            f"module={detected_module}" if detected_module else None,
        )
        if part
    )
    typer.echo(f"Using {axes or 'no axis metadata'}")

    # Get JWT token and create test run
    jwt_token = get_jwt_token(ssa_issuer, client_id, client_secret)
    create_test_run(
        endpoint=endpoint,
        lab_id=lab_id,
        jwt_token=jwt_token,
        test_target_type=capability,
        test_module=detected_module,
        tags=list(tags),
        executed_by=executed_by,
        ci_reference=ci_reference,
        start_time=start_time,
        isv_software_version=isv_software_version,
        isv_test_version=isv_test_version,
    )


@app.command("update")
def update(
    lab_id: Annotated[
        int,
        typer.Option("--lab-id", "-l", help="Lab ID"),
    ],
    status: Annotated[
        str,
        typer.Option("--status", "-s", help="Test run status (SUCCESS, FAILED, etc.)"),
    ],
    test_run_id: Annotated[
        str | None,
        typer.Option(
            "--test-run-id",
            "-r",
            help="Test run ID (reads from _output/testrun_id.txt if not provided)",
        ),
    ] = None,
    duration_seconds: Annotated[
        int | None,
        typer.Option("--duration-seconds", "-d", help="Test duration in seconds"),
    ] = None,
    complete_time: Annotated[
        str | None,
        typer.Option("--complete-time", help="Test completion time (ISO 8601 format, defaults to now)"),
    ] = None,
    calculate_duration_from: Annotated[
        str | None,
        typer.Option("--calculate-duration-from", help="Calculate duration from this start time (ISO 8601 format)"),
    ] = None,
    log_file: Annotated[
        Path | None,
        typer.Option("--log-file", help="Path to log file to include in the update"),
    ] = None,
    junit_xml: Annotated[
        Path | None,
        typer.Option("--junit-xml", help="Path to JUnit XML file to upload test results"),
    ] = None,
    isv_software_version: Annotated[
        str | None,
        typer.Option(
            "--isv-software-version",
            help="ISV software stack version (opaque string provided by ISV)",
        ),
    ] = None,
    isv_test_version: Annotated[
        str | None,
        typer.Option(
            "--isv-test-version",
            help="ISV test tool version (e.g., '1.12.3')",
        ),
    ] = None,
    test_catalog: Annotated[
        Path | None,
        typer.Option(
            "--test-catalog",
            help="Path to test catalog JSON file to upload for coverage tracking",
        ),
    ] = None,
) -> None:
    """Update an existing test run with completion status.

    Examples:
        isvctl report update --lab-id 3 --status SUCCESS \\
            --calculate-duration-from "$CI_JOB_STARTED_AT"

        isvctl report update --lab-id 3 --status FAILED \\
            --log-file pytest-output.log --junit-xml junit-validation.xml
    """
    endpoint, ssa_issuer, client_id, client_secret = _get_credentials()

    # Get test run ID
    run_id = test_run_id or load_test_run_id()
    if run_id is None:
        typer.echo(
            "ERROR: No test run ID available. Provide --test-run-id or ensure the test run was created successfully.",
            err=True,
        )
        raise typer.Exit(1)

    # Calculate duration if needed
    final_duration = duration_seconds
    if calculate_duration_from:
        final_duration = calculate_duration(calculate_duration_from)

    # Read log file if provided
    log_output = None
    if log_file:
        try:
            log_output = log_file.read_text()
            typer.echo(f"Read log file: {log_file} ({len(log_output)} characters)")
        except FileNotFoundError:
            typer.echo(f"Warning: Log file not found: {log_file}", err=True)
        except Exception as e:
            typer.echo(f"Warning: Failed to read log file: {e}", err=True)

    # Get JWT token
    jwt_token = get_jwt_token(ssa_issuer, client_id, client_secret)

    # Upload JUnit XML test results first (if provided)
    if junit_xml:
        try:
            typer.echo(f"Reading JUnit XML: {junit_xml}")
            junit_content = junit_xml.read_text()

            report_test_results(
                endpoint=endpoint,
                lab_id=lab_id,
                test_run_id=run_id,
                jwt_token=jwt_token,
                junit_xml=junit_content,
            )
        except FileNotFoundError:
            typer.echo(f"Warning: JUnit XML file not found: {junit_xml}", err=True)
        except Exception as e:
            typer.echo(f"Warning: Failed to upload JUnit XML: {e}", err=True)

    # Upload test catalog if provided (for coverage tracking)
    if test_catalog:
        try:
            typer.echo(f"Reading test catalog: {test_catalog}")
            catalog_data = json.loads(test_catalog.read_text())
            catalog_version = catalog_data.get("isvTestVersion", isv_test_version or "unknown")
            catalog_entries = catalog_data.get("entries", [])

            upload_test_catalog(
                endpoint=endpoint,
                jwt_token=jwt_token,
                isv_test_version=catalog_version,
                entries=catalog_entries,
            )
        except FileNotFoundError:
            typer.echo(f"Warning: Test catalog file not found: {test_catalog}", err=True)
        except Exception as e:
            typer.echo(f"Warning: Failed to upload test catalog: {e}", err=True)

    # Update test run with status and log
    update_test_run(
        endpoint=endpoint,
        lab_id=lab_id,
        test_run_id=run_id,
        jwt_token=jwt_token,
        status=status,
        duration_seconds=final_duration,
        complete_time=complete_time,
        log_output=log_output,
        isv_software_version=isv_software_version,
        isv_test_version=isv_test_version,
    )


if __name__ == "__main__":
    app()
