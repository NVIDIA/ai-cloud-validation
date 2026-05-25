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

"""Catalog subcommand for isvctl.

Manage the test catalog: build, save, and upload to ISV Lab Service.
"""

import json
import logging
from typing import Annotated

import typer
from isvtest.catalog import build_catalog, get_catalog_version
from isvtest.release_manifest import load_released_tests
from rich.console import Console
from rich.table import Table

from isvctl.cli import setup_logging
from isvctl.cli.common import get_output_dir

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="catalog",
    help="Manage the test catalog for coverage tracking",
    no_args_is_help=True,
)

console = Console()


@app.command("list")
def list_cmd(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the catalog as JSON instead of a table"),
    ] = False,
    unreleased: Annotated[
        bool,
        typer.Option("--unreleased", help="Show only tests not present in the release manifest"),
    ] = False,
) -> None:
    """List the tests that would be uploaded by `isvctl catalog push`.

    Released tests only by default. Set ``ISVTEST_INCLUDE_UNRELEASED=1`` to
    include unreleased validations (matches the gate used at run time and by
    `catalog push`), or use ``--unreleased`` to list only unreleased tests.

    Examples:
        isvctl catalog list
        isvctl catalog list --json
        isvctl catalog list --unreleased
        ISVTEST_INCLUDE_UNRELEASED=1 isvctl catalog list
    """
    if unreleased:
        released_tests = load_released_tests()
        catalog_entries = [entry for entry in build_catalog(released_only=False) if entry["name"] not in released_tests]
    else:
        catalog_entries = build_catalog()
    catalog_version = get_catalog_version()

    if json_output:
        typer.echo(json.dumps({"isvTestVersion": catalog_version, "entries": catalog_entries}, indent=2))
        return

    table = Table(
        title=f"Test Catalog ({len(catalog_entries)} tests, version {catalog_version})",
        title_justify="left",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Test", style="green", no_wrap=True)
    table.add_column("Platforms", style="cyan")
    table.add_column("Labels", style="dim")
    table.add_column("Description")

    for entry in sorted(catalog_entries, key=lambda e: e["name"]):
        table.add_row(
            entry["name"],
            ", ".join(entry.get("platforms") or []) or "-",
            ", ".join(entry.get("labels") or entry.get("markers") or []) or "-",
            entry.get("description") or "-",
        )

    console.print(table)


@app.command("push")
def push(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
    no_upload: Annotated[
        bool,
        typer.Option("--no-upload", help="Build and save locally without uploading"),
    ] = False,
) -> None:
    """Build the test catalog and upload it to ISV Lab Service.

    Discovers all validation tests, saves the catalog to
    _output/test_catalog.json, and uploads it to the backend.
    If the catalog for this version already exists, the upload
    is skipped.

    Examples:
        isvctl catalog push
        isvctl catalog push --no-upload
    """
    setup_logging(verbose)

    typer.echo("Building test catalog...")
    catalog_entries = build_catalog()
    catalog_version = get_catalog_version()
    typer.echo(f"  {len(catalog_entries)} tests (version: {catalog_version})")

    output_dir = get_output_dir()
    catalog_path = output_dir / "test_catalog.json"
    catalog_path.write_text(json.dumps({"isvTestVersion": catalog_version, "entries": catalog_entries}, indent=2))
    typer.echo(f"  Saved to: {catalog_path}")

    if no_upload:
        typer.echo("Skipping upload (--no-upload)")
        return

    from isvctl.reporting import check_upload_credentials, get_environment_config

    can_upload, client_id, client_secret = check_upload_credentials()
    if not can_upload or not client_id or not client_secret:
        typer.echo(
            typer.style("Error:", fg=typer.colors.RED) + " ISV_CLIENT_ID and/or ISV_CLIENT_SECRET not set",
            err=True,
        )
        raise typer.Exit(1)

    endpoint, ssa_issuer = get_environment_config()
    if not endpoint or not ssa_issuer:
        typer.echo(
            typer.style("Error:", fg=typer.colors.RED) + " ISV_SERVICE_ENDPOINT and/or ISV_SSA_ISSUER not set",
            err=True,
        )
        raise typer.Exit(1)

    from isvreporter.auth import get_jwt_token
    from isvreporter.client import upload_test_catalog

    jwt_token = get_jwt_token(ssa_issuer, client_id, client_secret)
    if upload_test_catalog(
        endpoint=endpoint,
        jwt_token=jwt_token,
        isv_test_version=catalog_version,
        entries=catalog_entries,
    ):
        typer.echo(typer.style("[OK]", fg=typer.colors.GREEN) + " Catalog push complete")
    else:
        typer.echo(typer.style("[FAIL]", fg=typer.colors.RED) + " Catalog upload failed")
        raise typer.Exit(1)
