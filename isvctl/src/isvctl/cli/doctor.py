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

"""Doctor subcommand for isvctl.

Pre-flight diagnostics: verify external tools, environment variables, and
config integrity before a full ``isvctl test run``.
"""

import logging
from pathlib import Path
from typing import Annotated

import typer
from isvreporter.version import get_version

from isvctl.cli import setup_logging
from isvctl.doctor.checks import check_configs, check_env, check_tools
from isvctl.doctor.report import render_json, render_rich
from isvctl.doctor.result import CategoryReport, Status, worst

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="doctor",
    help="Pre-flight diagnostics for tools, environment, and configs",
    invoke_without_command=True,
    no_args_is_help=False,
)

# Categories the user can pass to --check. Keep in sync with the dispatch
# table inside `doctor()`.
_CATEGORY_NAMES: tuple[str, ...] = ("tools", "env", "config")


def _parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated CLI value into a clean list."""
    if not value:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


@app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    config_files: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            "-f",
            help="Validate this YAML config (repeatable; merged like `isvctl test run`).",
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
            help="Comma-separated provider names (e.g. 'aws') to require their tools "
            "and run provider-specific readiness checks (e.g. the AWS credential chain).",
        ),
    ] = None,
    check: Annotated[
        str | None,
        typer.Option(
            "--check",
            help=f"Comma-separated subset of categories to run ({', '.join(_CATEGORY_NAMES)}).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as JSON to stdout."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detail lines (paths, versions) per check."),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Treat warnings as failures for exit-code purposes.",
        ),
    ] = False,
) -> None:
    """Diagnose the local environment for `isvctl test run`.

    Examples:
        isvctl doctor
        isvctl doctor -f path/to/your-config.yaml
        isvctl doctor --provider aws -v
        isvctl doctor --check env --json
    """
    # When invoked with a real subcommand, defer to it. (There are none today
    # but the callback shape leaves room for future grouping like `doctor
    # show <category>`.)
    if ctx.invoked_subcommand is not None:
        return

    setup_logging(verbose)

    providers = _parse_csv(provider)
    requested = _parse_csv(check) or list(_CATEGORY_NAMES)
    unknown = [c for c in requested if c not in _CATEGORY_NAMES]
    if unknown:
        typer.echo(
            f"Error: unknown --check value(s): {', '.join(unknown)}. Valid: {', '.join(_CATEGORY_NAMES)}",
            err=True,
        )
        raise typer.Exit(code=2)

    reports: list[CategoryReport] = []
    if "tools" in requested:
        reports.append(check_tools(providers))
    if "env" in requested:
        reports.append(check_env(providers))
    if "config" in requested:
        reports.append(check_configs(config_files or None, providers))

    version = get_version("isvctl")

    if json_output:
        typer.echo(render_json(reports, isvctl_version=version, verbose=verbose))
    else:
        render_rich(reports, isvctl_version=version, verbose=verbose)

    overall = worst([r.worst_status for r in reports])
    if overall == Status.FAIL or (strict and overall == Status.WARN):
        raise typer.Exit(code=1)
