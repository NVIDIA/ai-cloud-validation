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
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from isvreporter.version import build_is_release
from isvtest.catalog import build_catalog, build_label_file_map, catalog_digest, catalog_document, get_catalog_version
from rich.console import Console
from rich.table import Table

from isvctl.cli import setup_logging
from isvctl.cli.common import get_output_dir, print_error, print_progress

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
) -> None:
    """List the tests that would be uploaded by `isvctl catalog push`.

    A checkout's complete catalog is its contract. Tags freeze the complete set
    they contain, while main remains a developer workflow that runs everything.

    Examples:
        isvctl catalog list
        isvctl catalog list --json
    """
    catalog_entries = build_catalog()
    catalog_version = get_catalog_version()

    if json_output:
        typer.echo(json.dumps(catalog_document(catalog_entries, catalog_version), indent=2))
        return

    table = Table(
        title=f"Test Catalog ({len(catalog_entries)} tests, version {catalog_version})",
        title_justify="left",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Test", style="green", no_wrap=True)
    table.add_column("Test IDs", style="magenta", max_width=32)
    table.add_column("Suite / Capability", style="dim", max_width=40)
    table.add_column("Description")

    for entry in sorted(catalog_entries, key=lambda e: e["name"]):
        suite = entry.get("suite") or "-"
        # Platform-suite checks carry capability (always-on for that platform) and
        # have no requires axis. Plain-suite checks use requires; empty means core.
        if entry.get("capability"):
            suite_requirement = suite
        else:
            requirement = ", ".join(entry.get("requires") or []) or "core"
            suite_requirement = f"{suite} / {requirement}"
        table.add_row(
            entry["name"],
            ", ".join(entry.get("test_ids") or []) or "-",
            suite_requirement,
            entry.get("description") or "-",
        )

    console.print(table)


@app.command("labels")
def labels_cmd(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the labels as JSON instead of a table"),
    ] = False,
    show_files: Annotated[
        bool,
        typer.Option("--files", help="Also show the config file(s) declaring each label"),
    ] = False,
) -> None:
    """List every label in the catalog with the number of tests carrying it.

    Pass ``--files`` to also list the config file(s) that declare each label.

    Examples:
        isvctl catalog labels
        isvctl catalog labels --files
        isvctl catalog labels --json
    """
    counts = Counter(label for entry in build_catalog() for label in (entry.get("labels") or []))
    sorted_counts = sorted(counts.items())
    label_files = build_label_file_map() if show_files else {}

    def files_for(label: str) -> list[str]:
        """Return the sorted config files declaring ``label`` (empty without --files)."""
        return sorted(label_files.get(label, set()))

    if json_output:
        labels = [
            {"label": label, "tests": count, **({"files": files_for(label)} if show_files else {})}
            for label, count in sorted_counts
        ]
        typer.echo(json.dumps({"labels": labels}, indent=2))
        return

    table = Table(
        title=f"Catalog Labels ({len(counts)} labels)",
        title_justify="left",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Label", style="green", no_wrap=True)
    table.add_column("Tests", style="cyan", justify="right")
    if show_files:
        table.add_column("Files", style="dim")

    for label, count in sorted_counts:
        row = [label, str(count)]
        if show_files:
            row.append("\n".join(files_for(label)) or "-")
        table.add_row(*row)

    console.print(table)


@app.command("push")
def push(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose logging"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "--no-upload",
            help="Build and save locally without uploading",
        ),
    ] = False,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="Publish an existing catalog artifact instead of generating one",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
) -> None:
    """Build the test catalog and upload it to ISV Lab Service.

    Discovers all validation tests, saves the catalog to
    _output/test_catalog.json, and uploads it to the backend.
    Repeating an identical artifact succeeds; changing its digest or source
    reference under an existing version is rejected.

    Examples:
        isvctl catalog push
        isvctl catalog push --dry-run
    """
    setup_logging(verbose)

    if file:
        print_progress(f"Reading test catalog: {file}")
        try:
            document = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print_error(f"Cannot read catalog artifact: {exc}")
            raise typer.Exit(1)
        catalog_path = file
    else:
        print_progress("Building test catalog...")
        catalog_entries = build_catalog()
        catalog_version = get_catalog_version()
        document = catalog_document(catalog_entries, catalog_version)
        output_dir = get_output_dir()
        catalog_path = output_dir / "test_catalog.json"
        catalog_path.write_text(json.dumps(document, indent=2))
        print_progress(f"  Saved to: {catalog_path}")

    try:
        catalog_entries = document["entries"]
        catalog_version = document["isvTestVersion"]
        recorded_digest = document["catalogDigest"]
        reference = document["isvTestBuildRef"]
        schema_version = document["schemaVersion"]
        capabilities = document["capabilities"]
        suites = document["suites"]
        if not isinstance(catalog_entries, list):
            raise TypeError("entries must be a list")
        if not isinstance(catalog_version, str) or not catalog_version:
            raise ValueError("isvTestVersion is required")
        if not isinstance(recorded_digest, str):
            raise TypeError("catalogDigest must be a string")
        if not isinstance(schema_version, int) or schema_version < 1:
            raise ValueError("schemaVersion must be a positive integer")
        if not isinstance(capabilities, list) or not isinstance(suites, list):
            raise TypeError("capabilities and suites must be lists")
        if recorded_digest != catalog_digest(document):
            raise ValueError("catalogDigest does not match the artifact contents")
        if not isinstance(reference, str) or not reference:
            raise ValueError("isvTestBuildRef is required")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        print_error(f"Invalid catalog artifact: {exc}")
        raise typer.Exit(1)

    print_progress(f"  {len(catalog_entries)} tests (version: {catalog_version}, digest: {recorded_digest})")
    if build_is_release(catalog_version, reference) is not True:
        print_error(
            "Catalog publication requires a clean, zero-distance build reference "
            f"whose tag matches version {catalog_version}."
        )
        raise typer.Exit(1)

    if dry_run:
        print_progress("Dry run: saved catalog locally (upload skipped)")
        return

    from isvctl.reporting import check_upload_credentials, get_environment_config

    can_upload, client_id, client_secret = check_upload_credentials()
    if not can_upload or not client_id or not client_secret:
        print_error("ISV_CLIENT_ID and/or ISV_CLIENT_SECRET not set")
        raise typer.Exit(1)

    endpoint, ssa_issuer = get_environment_config()
    if not endpoint or not ssa_issuer:
        print_error("ISV_SERVICE_ENDPOINT and/or ISV_SSA_ISSUER not set")
        raise typer.Exit(1)

    from isvreporter.auth import get_jwt_token
    from isvreporter.client import upload_test_catalog

    jwt_token = get_jwt_token(ssa_issuer, client_id, client_secret)
    if upload_test_catalog(
        endpoint=endpoint,
        jwt_token=jwt_token,
        isv_test_version=catalog_version,
        entries=catalog_entries,
        schema_version=schema_version,
        capabilities=capabilities,
        suites=suites,
        catalog_digest=recorded_digest,
        isv_test_build_ref=reference,
    ):
        print_progress(typer.style("[OK]", fg=typer.colors.GREEN) + " Catalog push complete")
    else:
        print_error("Catalog upload failed")
        raise typer.Exit(1)
