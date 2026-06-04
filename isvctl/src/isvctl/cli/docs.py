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

"""CLI command for accessing documentation."""

import inspect
from collections import defaultdict
from pathlib import Path
from textwrap import dedent
from typing import Any

import typer
import yaml
from isvtest.catalog import build_label_map
from isvtest.core.discovery import discover_all_tests
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from isvctl.cli.common import err_console, print_error, print_progress

app = typer.Typer(help="Access AI Cloud Validation Suite documentation")

console = Console()


def _find_local_docs() -> Path | None:
    """Find local docs directory.

    Checks in order:
    1. ./docs/ (current working directory - installed via install.sh)
    2. Relative to this package (development mode)
    """
    # Check current working directory (install.sh extracts here)
    cwd_docs = Path.cwd() / "docs"
    if cwd_docs.is_dir() and (cwd_docs / "README.md").exists():
        return cwd_docs

    # Check relative to package (development mode)
    package_dir = Path(__file__).parent.parent.parent.parent.parent.parent
    dev_docs = package_dir / "docs"
    if dev_docs.is_dir() and (dev_docs / "README.md").exists():
        return dev_docs

    return None


# Topic mapping
TOPICS = {
    "getting-started": "getting-started.md",
    "configuration": "guides/configuration.md",
    "remote-deployment": "guides/remote-deployment.md",
    "workloads": "guides/workloads.md",
    "local-development": "guides/local-development.md",
    "contributing": "contributing.md",
    "isvctl": "packages/isvctl.md",
    "isvtest": "packages/isvtest.md",
    "isvreporter": "packages/isvreporter.md",
}


@app.callback(invoke_without_command=True)
def docs(
    ctx: typer.Context,
    topic: str | None = typer.Option(
        None,
        "--topic",
        "-t",
        help=f"Documentation topic: {', '.join(TOPICS)}",
    ),
    list_topics: bool = typer.Option(False, "--list", "-l", help="List available documentation topics"),
    path_only: bool = typer.Option(False, "--path", "-p", help="Show file path instead of content"),
) -> None:
    """View documentation in the terminal.

    Examples:
        isvctl docs                              # Show docs index
        isvctl docs -t getting-started           # Show getting started guide
        isvctl docs --list                       # List available topics
        isvctl docs tests                        # List all validation tests
        isvctl docs tests -m kubernetes          # Only kubernetes tests
    """
    if ctx.invoked_subcommand is not None:
        return

    if list_topics:
        typer.echo("Available documentation topics:\n")
        for name, filepath in TOPICS.items():
            typer.echo(f"  {name:20} {filepath}")
        typer.echo(f"\n  {'tests':20} (subcommand) List validation tests by category")
        typer.echo("\nUsage: isvctl docs -t <topic>  or  isvctl docs tests [OPTIONS]")
        local_docs = _find_local_docs()
        if local_docs:
            typer.echo(f"\nDocs location: {local_docs}")
        return

    local_docs = _find_local_docs()

    if not local_docs:
        print_error("Documentation not found.")
        print_progress("Run from the install directory or clone the repository.")
        raise typer.Exit(1)

    if topic:
        if topic not in TOPICS:
            print_error(f"Unknown topic: {topic}")
            print_progress(f"Available: {', '.join(TOPICS)}")
            raise typer.Exit(1)
        doc_file = local_docs / TOPICS[topic]
        if not doc_file.exists():
            print_error(f"Topic file not found: {doc_file}")
            raise typer.Exit(1)
    else:
        doc_file = local_docs / "README.md"

    if path_only:
        typer.echo(str(doc_file))
        return

    content = doc_file.read_text()
    md = Markdown(content)
    console.print(md)


@app.command()
def tests(
    label: list[str] = typer.Option(
        None,
        "--label",
        "-l",
        "--marker",
        "-m",
        help="Filter by label (repeatable, e.g. -l kubernetes -l gpu; --marker is legacy)",
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config",
        "-f",
        help="Show test instances from a config file (counts aliases like CheckName-variant)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    flat: bool = typer.Option(False, "--flat", help="Flat list without grouping by label"),
    info: str | None = typer.Option(
        None,
        "--info",
        "-i",
        help="Show detailed info for a specific test (e.g. -i GpuStressCheck)",
    ),
) -> None:
    """List all available validation tests grouped by category.

    Examples:
        isvctl docs tests                          # All tests by category
        isvctl docs tests -l kubernetes            # Only kubernetes tests
        isvctl docs tests -f isvctl/configs/suites/k8s.yaml  # Tests from config file
        isvctl docs tests --flat                   # Flat alphabetical list
        isvctl docs tests -i GpuStressCheck     # Detailed info for a test
    """
    all_classes = list(discover_all_tests())

    if not all_classes:
        err_console.print("[yellow]No validation tests discovered.[/yellow]")
        raise typer.Exit(1)

    _warn_duplicates(all_classes)

    # Labels live on the YAML wiring, so source them from the same map the
    # catalog uses (check/variant name -> labels across all configs).
    label_map = build_label_map()

    if info:
        _print_test_info(all_classes, info, label_map)
        return

    if config_file:
        _print_config_instances(all_classes, config_file, label, label_map)
    elif flat:
        _print_flat(all_classes, label, label_map)
    else:
        _print_grouped(all_classes, label, label_map)


def _labels_for(label_map: dict[str, set[str]], *names: str) -> list[str]:
    """Return sorted wiring labels for the first of ``names`` present in the map."""
    for name in names:
        if name in label_map:
            return sorted(label_map[name])
    return []


def _warn_duplicates(classes: list[type]) -> None:
    """Warn if duplicate test class names are found."""
    seen: dict[str, int] = {}
    for cls in classes:
        seen[cls.__name__] = seen.get(cls.__name__, 0) + 1
    dupes = [name for name, count in seen.items() if count > 1]
    if dupes:
        err_console.print(f"[yellow]Warning: Duplicate test class names found: {', '.join(dupes)}[/yellow]")


def _print_test_info(classes: list[type], name: str, label_map: dict[str, set[str]]) -> None:
    """Print detailed info for a single test class."""
    by_name = {cls.__name__: cls for cls in classes}
    cls = by_name.get(name)

    if cls is None:
        err_console.print(f"[red]Test not found:[/red] {name}")
        close = [c.__name__ for c in classes if name in c.__name__]
        if close:
            err_console.print(f"[dim]Did you mean: {', '.join(close)}?[/dim]")
        raise typer.Exit(1)

    source_file = Path(inspect.getfile(cls))
    _, start_line = inspect.getsourcelines(cls)
    try:
        source_display = f"{source_file.relative_to(Path.cwd())}:{start_line}"
    except ValueError:
        source_display = f"{source_file}:{start_line}"

    console.print()
    console.print(
        Panel(
            f"[bold green]{cls.__name__}[/bold green]",
            subtitle=f"[dim]{cls.description}[/dim]" if cls.description else None,
        )
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()
    labels = _labels_for(label_map, cls.__name__)
    table.add_row("Labels", ", ".join(labels) if labels else "(none)")
    table.add_row("Timeout", f"{cls.timeout}s")
    table.add_row("Source", source_display)
    console.print(table)

    docstring = inspect.getdoc(cls)
    if docstring:
        console.print()
        console.print(Panel(dedent(docstring), title="[bold]Documentation[/bold]", title_align="left"))
    else:
        console.print("\n[dim]No docstring available.[/dim]")

    console.print()


def _print_grouped(classes: list[type], label_filter: list[str] | None, label_map: dict[str, set[str]]) -> None:
    """Print tests grouped by label category."""
    by_label: dict[str, list[type]] = defaultdict(list)

    for cls in classes:
        labels = _labels_for(label_map, cls.__name__) or ["uncategorized"]
        for item in labels:
            by_label[item].append(cls)

    if label_filter:
        by_label = {k: v for k, v in by_label.items() if k in label_filter}

    if not by_label:
        err_console.print("[yellow]No tests found for the given labels.[/yellow]")
        raise typer.Exit(1)

    total = len({cls.__name__ for group in by_label.values() for cls in group})
    console.print(f"\n[bold]Validation Tests[/bold] ({total} unique across {len(by_label)} categories)\n")

    for label_name in sorted(by_label):
        table = Table(
            title=f"[bold cyan]{label_name}[/bold cyan] ({len(by_label[label_name])})",
            title_justify="left",
            show_header=True,
            header_style="bold",
            padding=(0, 1),
            show_lines=False,
        )
        table.add_column("Test", style="green", no_wrap=True)
        table.add_column("Description")
        table.add_column("Labels", style="dim")

        for cls in sorted(by_label[label_name], key=lambda c: c.__name__):
            labels = _labels_for(label_map, cls.__name__)
            table.add_row(
                cls.__name__,
                cls.description or "-",
                ", ".join(labels) if labels else "-",
            )

        console.print(table)
        console.print()


def _extract_config_instances(config_path: Path) -> dict[str, list[str]]:
    """Extract validation instance names from a config file, grouped by category.

    Handles both config formats:
    - Group defaults: {checks: [{Name: {...}}, ...]}
    - List format: [{Name: {...}}, ...]

    Returns:
        Dict mapping category name to list of instance names (e.g. "CheckName-variant").
    """
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    validations: dict[str, Any] = (raw.get("tests") or {}).get("validations", {})
    result: dict[str, list[str]] = {}

    for category, category_config in validations.items():
        names: list[str] = []

        if isinstance(category_config, dict) and "checks" in category_config:
            checks = category_config["checks"]
        elif isinstance(category_config, list):
            checks = category_config
        else:
            continue

        for check in checks:
            if isinstance(check, dict):
                for name in check:
                    names.append(name)
            elif isinstance(check, str):
                names.append(check)

        if names:
            result[category] = names

    return result


def _resolve_class(instance_name: str, class_map: dict[str, type]) -> type | None:
    """Resolve an instance name (possibly with suffix) to a class.

    Uses the same suffix matching logic as the test runner:
    exact match first, then longest class name prefix with ``-`` separator.
    """
    if instance_name in class_map:
        return class_map[instance_name]

    possible = [name for name in class_map if instance_name.startswith(name)]
    if possible:
        longest = max(possible, key=len)
        if instance_name.startswith(f"{longest}-"):
            return class_map[longest]

    return None


def _print_config_instances(
    classes: list[type], config_path: Path, label_filter: list[str] | None, label_map: dict[str, set[str]]
) -> None:
    """Print test instances as defined in a config file, grouped by config category."""
    class_map = {cls.__name__: cls for cls in classes}
    categories = _extract_config_instances(config_path)

    if not categories:
        err_console.print("[yellow]No validations found in config file.[/yellow]")
        raise typer.Exit(1)

    if label_filter:
        filtered: dict[str, list[str]] = {}
        for cat, names in categories.items():
            matched = [
                n
                for n in names
                if (cls := _resolve_class(n, class_map))
                and any(label in _labels_for(label_map, n, cls.__name__) for label in label_filter)
            ]
            if matched:
                filtered[cat] = matched
        categories = filtered

    if not categories:
        err_console.print("[yellow]No test instances match the given labels.[/yellow]")
        raise typer.Exit(1)

    total = sum(len(names) for names in categories.values())
    console.print(
        f"\n[bold]Config: {config_path.name}[/bold] ({total} test instances across {len(categories)} categories)\n"
    )

    for category in categories:
        names = categories[category]
        table = Table(
            title=f"[bold cyan]{category}[/bold cyan] ({len(names)})",
            title_justify="left",
            show_header=True,
            header_style="bold",
            padding=(0, 1),
            show_lines=False,
        )
        table.add_column("Test", no_wrap=True)
        table.add_column("Description")
        table.add_column("Labels", style="dim")

        for name in names:
            cls = _resolve_class(name, class_map)
            if cls:
                labels = _labels_for(label_map, name, cls.__name__)
                label = f"[green]{name}[/green]"
                if cls.__name__ != name:
                    label += f" [dim]({cls.__name__})[/dim]"
                table.add_row(
                    label,
                    cls.description or "-",
                    ", ".join(labels) if labels else "-",
                )
            else:
                table.add_row(f"[green]{name}[/green]", "[red]not found[/red]", "-")

        console.print(table)
        console.print()


def _print_flat(classes: list[type], label_filter: list[str] | None, label_map: dict[str, set[str]]) -> None:
    """Print a flat alphabetical list of tests."""
    if label_filter:
        classes = [
            cls for cls in classes if any(label in _labels_for(label_map, cls.__name__) for label in label_filter)
        ]

    if not classes:
        err_console.print("[yellow]No tests found for the given labels.[/yellow]")
        raise typer.Exit(1)

    table = Table(
        title=f"[bold]All Validation Tests[/bold] ({len(classes)})",
        title_justify="left",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Test", style="green", no_wrap=True)
    table.add_column("Description")
    table.add_column("Labels", style="dim")

    for cls in sorted(classes, key=lambda c: c.__name__):
        labels = _labels_for(label_map, cls.__name__)
        table.add_row(
            cls.__name__,
            cls.description or "-",
            ", ".join(labels) if labels else "-",
        )

    console.print()
    console.print(table)
    console.print()
