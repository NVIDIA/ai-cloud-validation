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
from isvtest.catalog import build_catalog, build_label_file_map, get_catalog_version
from isvtest.release_manifest import load_released_test_filter, load_released_tests
from rich.console import Console
from rich.table import Table

from isvctl.cli import setup_logging
from isvctl.cli.common import get_output_dir, print_error, print_progress
from isvctl.config.label_discovery import list_providers, summarize_provider_labels

logger = logging.getLogger(__name__)
CONFIGS_ROOT = Path(__file__).resolve().parents[3] / "configs"

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
    table.add_column("Test IDs", style="magenta", max_width=32)
    table.add_column("Labels (Platforms)", style="dim", max_width=40)
    table.add_column("Description")

    for entry in sorted(catalog_entries, key=lambda e: e["name"]):
        labels = ", ".join(entry.get("labels") or [])
        platforms = ", ".join(entry.get("platforms") or [])
        if labels and platforms:
            labels_platforms = f"{labels} ({platforms})"
        else:
            labels_platforms = labels or platforms
        table.add_row(
            entry["name"],
            ", ".join(entry.get("test_ids") or []) or "-",
            labels_platforms or "-",
            entry.get("description") or "-",
        )

    console.print(table)


@app.command("labels")
def labels_cmd(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the labels as JSON instead of a table"),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Limit labels to runnable checks in one provider's configs"),
    ] = None,
    show_files: Annotated[
        bool,
        typer.Option("--files", help="Also show the config file(s) declaring each label"),
    ] = False,
) -> None:
    """List every label in the catalog with the number of tests carrying it.

    Released tests only by default. Set ``ISVTEST_INCLUDE_UNRELEASED=1`` to
    include unreleased validations (matches the gate used at run time). Pass
    ``--files`` to also list the config file(s) that declare each label.

    Examples:
        isvctl catalog labels
        isvctl catalog labels --provider aws
        isvctl catalog labels --files
        isvctl catalog labels --json
        ISVTEST_INCLUDE_UNRELEASED=1 isvctl catalog labels
    """
    if provider:
        known_providers = list_providers(CONFIGS_ROOT)
        if provider not in known_providers:
            print_error(f"Unknown provider {provider!r}. Available providers: {', '.join(known_providers)}")
            raise typer.Exit(code=1)

        released_tests = load_released_test_filter()
        summaries = summarize_provider_labels(provider, configs_root=CONFIGS_ROOT, released_tests=released_tests)

        def files_for_provider(config_paths: tuple[Path, ...]) -> list[str]:
            """Return provider config paths relative to the configs root."""
            return [path.relative_to(CONFIGS_ROOT).as_posix() for path in config_paths]

        if json_output:
            labels = [
                {
                    "label": summary.label,
                    "tests": len(summary.checks),
                    "configs": len(summary.config_paths),
                    **({"files": files_for_provider(summary.config_paths)} if show_files else {}),
                }
                for summary in summaries
            ]
            typer.echo(json.dumps({"provider": provider, "labels": labels}, indent=2))
            return

        table = Table(
            title=f"Provider Catalog Labels: {provider} ({len(summaries)} labels)",
            title_justify="left",
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        table.add_column("Label", style="green", no_wrap=True)
        table.add_column("Tests", style="cyan", justify="right")
        table.add_column("Configs", style="cyan", justify="right")
        if show_files:
            table.add_column("Files", style="dim")

        for summary in summaries:
            row = [summary.label, str(len(summary.checks)), str(len(summary.config_paths))]
            if show_files:
                row.append("\n".join(files_for_provider(summary.config_paths)) or "-")
            table.add_row(*row)

        console.print(table)
        return

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

    print_progress("Building test catalog...")
    catalog_entries = build_catalog()
    catalog_version = get_catalog_version()
    print_progress(f"  {len(catalog_entries)} tests (version: {catalog_version})")

    output_dir = get_output_dir()
    catalog_path = output_dir / "test_catalog.json"
    catalog_path.write_text(json.dumps({"isvTestVersion": catalog_version, "entries": catalog_entries}, indent=2))
    print_progress(f"  Saved to: {catalog_path}")

    if no_upload:
        print_progress("Skipping upload (--no-upload)")
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
    ):
        print_progress(typer.style("[OK]", fg=typer.colors.GREEN) + " Catalog push complete")
    else:
        print_error("Catalog upload failed")
        raise typer.Exit(1)
