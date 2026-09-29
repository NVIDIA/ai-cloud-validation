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

"""Provider scaffold and registry commands."""

import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from isvctl import __version__
from isvctl.cli.common import print_error, print_progress, print_warning
from isvctl.config.env_catalog import DEMO_MODE_ENV
from isvctl.config.provider_registry import (
    ProviderRegistryError,
    RegistryEntry,
    external_checkout,
    fetched_commit,
    is_fetched_checkout,
    load_registry,
    mark_fetched,
    run_git,
)
from isvctl.config.suite_resolution import CONFIGS_ROOT, EXTERNAL_PROVIDERS_DIRNAME

app = typer.Typer(
    name="provider",
    help="Manage provider scaffolds and the registry of externally maintained providers",
    no_args_is_help=True,
)

console = Console()

PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
TEMPLATE_PROVIDER_NAME = "my-isv"
TEMPLATE_PROVIDER_TOKEN_RE = re.compile(r"(?<!\w)" + re.escape(TEMPLATE_PROVIDER_NAME) + r"(?!\w)")
IGNORE_NAMES = ("__pycache__", ".pytest_cache")
SCAFFOLD_META_FILE = ".scaffold-meta"
REGISTRY_ENTRY_FILE = "registry-entry.yaml"
# The header every file in this repository carries; registry entries are copied here.
SPDX_HEADER = """\
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
"""
STATUS_COMMENT = """\
# status is one of:
#   qualified     pinned to `commit` and validated against `tested_with`
#   experimental  may give only a `ref` instead of `commit`; results are not reproducible
#   deprecated    no longer validated; hidden from `isvctl provider list` unless --all
#   demo          scripts only return dummy results; runs in demo mode and is never uploaded
"""
RELATIVE_PATH_RE = re.compile(r"\.\./[^\s\"',]+\.(?:yaml|yml|py|sh)")


def _validate_provider_name(provider_name: str) -> str:
    """Validate a provider scaffold name."""
    if not PROVIDER_NAME_RE.fullmatch(provider_name):
        raise typer.BadParameter("Provider name must contain only lowercase letters, numbers, '_' and '-'.")
    return provider_name


def _find_template_dir() -> Path:
    """Find the source provider scaffold directory."""
    repo_root = Path(__file__).resolve().parents[4]
    template_dir = repo_root / "isvctl" / "configs" / "providers" / TEMPLATE_PROVIDER_NAME
    if not template_dir.is_dir():
        raise FileNotFoundError(f"Provider template not found at {template_dir}")
    return template_dir.resolve()


def _resolve_target_path(provider_name: str, output_dir: Path | None, template_dir: Path) -> Path:
    """Resolve the scaffold destination path: providers-external/<name>/ unless --output-dir is given."""
    if output_dir is not None:
        return output_dir.expanduser().resolve()
    if (template_dir.parent / provider_name).exists():
        raise ValueError(
            f"'{provider_name}' is already an in-tree provider in providers/; "
            "pick another name so --provider stays unambiguous."
        )
    return external_checkout(provider_name, CONFIGS_ROOT).resolve()


def _display_path(path: Path) -> str:
    """Format a path for CLI output."""
    try:
        return str(path.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _rewrite_text_files(target_dir: Path, provider_name: str) -> None:
    """Rewrite UTF-8 text files from the template provider name to the requested name.

    Matches `my-isv` only at token boundaries so compound forms like
    `my-isv-vm-validation` and `my-isv.gpu.1x` are rewritten while embedded
    occurrences (e.g. `my-isvfoo`, `xxxmy-isv`) are left alone.
    """
    for path in target_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = TEMPLATE_PROVIDER_TOKEN_RE.sub(provider_name, content)
        if updated != content:
            path.write_text(updated, encoding="utf-8")


def _relative_posix_path(path: Path, start: Path) -> str:
    """Return a POSIX relative path for generated YAML references."""
    try:
        relative = Path(os.path.relpath(path, start=start))
    except ValueError:
        return path.as_posix()
    return relative.as_posix()


def _built_in_reference(path: Path, start: Path, target_dir: Path, template_dir: Path) -> str:
    """Return a reference to a validation-suite-owned file.

    In-tree provider scaffolds keep config-file-relative references. Out-of-tree
    scaffolds are supported from the validation checkout root, so use paths
    relative to that root instead of generating long ``../../Users/...`` paths.
    """
    providers_dir = template_dir.parent.resolve()
    if target_dir.resolve().is_relative_to(providers_dir):
        return _relative_posix_path(path, start=start)

    repo_root = template_dir.parents[3]
    return _relative_posix_path(path, start=repo_root)


def _rewrite_config_paths(target_dir: Path, template_dir: Path) -> None:
    """Rewrite generated YAML references that depend on provider-tree placement."""
    config_dir = target_dir / "config"
    if not config_dir.is_dir():
        return

    suites_dir = template_dir.parents[1] / "suites"
    shared_dir = template_dir.parent / "shared"

    for path in config_dir.glob("*.yaml"):
        content = path.read_text(encoding="utf-8")
        updated = re.sub(
            r"\.\./\.\./\.\./suites/([A-Za-z0-9_.-]+\.yaml)",
            lambda match: _built_in_reference(
                suites_dir / match.group(1),
                start=path.parent,
                target_dir=target_dir,
                template_dir=template_dir,
            ),
            content,
        )
        updated = re.sub(
            r"\.\./\.\./shared/([A-Za-z0-9_./-]+\.py)",
            lambda match: _built_in_reference(
                shared_dir / match.group(1),
                start=path.parent,
                target_dir=target_dir,
                template_dir=template_dir,
            ),
            updated,
        )
        if updated != content:
            path.write_text(updated, encoding="utf-8")
        _assert_relative_paths_resolve(path, updated)


def _assert_relative_paths_resolve(yaml_path: Path, content: str) -> None:
    """Fail loudly if any `../`-rooted reference in the rewritten YAML doesn't exist.

    Catches silent breakage when a future template introduces a relative-path
    pattern the rewriter doesn't know about, leaving a dangling reference in
    the generated scaffold.
    """
    for match in RELATIVE_PATH_RE.finditer(content):
        reference = match.group(0)
        candidate = (yaml_path.parent / reference).resolve()
        if not candidate.exists():
            raise ValueError(
                f"Generated scaffold {_display_path(yaml_path)} references {reference} "
                f"which does not resolve (expected at {candidate}).",
            )


def _assert_target_outside_template(target_dir: Path, template_dir: Path) -> None:
    """Refuse scaffold targets that would mutate the source template tree."""
    if target_dir.resolve().is_relative_to(template_dir.resolve()):
        raise ValueError("Target path points inside the provider template.")


def _looks_like_scaffold(path: Path) -> bool:
    """Return True if path was produced by this command (has the sentinel meta file)."""
    return path.is_dir() and (path / SCAFFOLD_META_FILE).is_file()


def _assert_safe_to_overwrite(target_dir: Path, template_dir: Path) -> None:
    """Refuse to overwrite paths that don't look like a scaffold this command produced."""
    if template_dir.resolve().is_relative_to(target_dir.resolve()):
        raise ValueError(f"Refusing to overwrite {_display_path(target_dir)}: would delete the provider template.")
    if not target_dir.is_dir():
        raise ValueError(f"Refusing to overwrite {_display_path(target_dir)}: not a directory.")
    if (target_dir / ".git").exists():
        raise ValueError(
            f"Refusing to overwrite {_display_path(target_dir)}: it is a git repository, "
            "and overwriting would delete its history."
        )
    if not _looks_like_scaffold(target_dir):
        raise ValueError(
            f"Refusing to overwrite {_display_path(target_dir)}: "
            f"missing scaffold marker ({SCAFFOLD_META_FILE}). "
            f"Remove the directory manually if you want to replace it.",
        )


def _copy_scaffold(template_dir: Path, target_dir: Path, provider_name: str) -> None:
    """Copy the scaffold template into the target directory."""
    if target_dir.exists():
        _assert_safe_to_overwrite(target_dir, template_dir)
        shutil.rmtree(target_dir)

    shutil.copytree(
        template_dir,
        target_dir,
        copy_function=shutil.copy2,
        ignore=shutil.ignore_patterns(*IGNORE_NAMES),
    )
    _rewrite_text_files(target_dir, provider_name)
    _rewrite_config_paths(target_dir, template_dir)
    (target_dir / SCAFFOLD_META_FILE).write_text(f"provider_name={provider_name}\n", encoding="utf-8")
    if not _is_in_tree(target_dir, template_dir):
        (target_dir / REGISTRY_ENTRY_FILE).write_text(_registry_entry_stub(target_dir, provider_name), encoding="utf-8")


def _is_in_tree(target_dir: Path, template_dir: Path) -> bool:
    """Return True for a scaffold inside this repository's providers/ directory."""
    return target_dir.resolve().is_relative_to(template_dir.parent.resolve())


def _registry_entry_stub(target_dir: Path, provider_name: str) -> str:
    """Return a registry entry for the scaffold, prefilled where possible.

    The ``<...>`` placeholders fail validation, so the stub cannot be registered
    until they are replaced, just as the scaffold scripts fail until implemented.
    """
    suites_dir = CONFIGS_ROOT / "suites"
    suites = sorted(path.stem for path in (target_dir / "config").glob("*.yaml") if (suites_dir / path.name).is_file())
    return f"""{SPDX_HEADER}
# Registry entry for this provider. Once it is validated, copy this file to
# isvctl/configs/providers-registry/{provider_name}.yaml in ai-cloud-validation and open a
# pull request (see docs/guides/provider-registry.md). Replace every <...>
# placeholder first: they fail validation on purpose.

schema_version: 1
name: {provider_name}  # must match the file name
vendor: "<legal entity that maintains this provider>"
description: "<one sentence, shown by `isvctl provider list`>"
repo_url: "<https clone URL of this repository>"
commit: "<validated commit, from git rev-parse HEAD>"
tested_with: "{__version__}"  # ai-cloud-validation release you validated against, without a leading v
suites: [{", ".join(suites)}]  # keep only the suites you implement
maintainers:
  - github: "<GitHub handle, added to CODEOWNERS for this entry>"
    email: "<optional contact email>"
documentation_url: "<https URL explaining the setup and how to reproduce your results>"
{STATUS_COMMENT}status: qualified
"""


def _print_next_steps(target_dir: Path, action: str, provider_name: str, in_tree: bool) -> None:
    """Print scaffold creation output and next commands."""
    display_target = _display_path(target_dir)
    launch_script = shlex.quote(f"{display_target}/scripts/vm/launch_instance.py")
    typer.echo(f"{action} provider scaffold: {display_target}")
    typer.echo()
    typer.echo("Preview without cloud:")
    if target_dir == external_checkout(provider_name, CONFIGS_ROOT).resolve():
        typer.echo(f"  {DEMO_MODE_ENV}=1 uv run isvctl test run --provider {provider_name} --suite vm")
    else:
        demo_config = shlex.quote(f"{display_target}/config/vm.yaml")
        typer.echo(f"  {DEMO_MODE_ENV}=1 uv run isvctl test run -f {demo_config}")
    typer.echo()
    typer.echo("Start implementing:")
    typer.echo(f"  {launch_script}")
    if not in_tree:
        typer.echo()
        typer.echo("When it is validated, fill in and submit its registry entry:")
        typer.echo(f"  {shlex.quote(f'{display_target}/{REGISTRY_ENTRY_FILE}')}")


@app.command("scaffold")
def scaffold(
    provider_name: Annotated[
        str,
        typer.Argument(
            help="Provider name to scaffold. Use lowercase letters, numbers, '_' or '-'.",
        ),
    ],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help="Destination directory. Defaults to isvctl/configs/providers-external/<provider-name> "
            "(git-ignored), where `isvctl test run --provider <provider-name>` finds it.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would be created without writing files.",
        ),
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Replace the target directory if it already exists.",
        ),
    ] = False,
) -> None:
    """Create a ready-to-edit provider scaffold from the my-isv template."""
    try:
        provider_name = _validate_provider_name(provider_name)
        template_dir = _find_template_dir()
        target_dir = _resolve_target_path(provider_name, output_dir, template_dir)

        _assert_target_outside_template(target_dir, template_dir)

        if dry_run:
            if target_dir.exists():
                if overwrite:
                    _assert_safe_to_overwrite(target_dir, template_dir)
                else:
                    print_progress(f"Note: target exists; --overwrite would be required: {_display_path(target_dir)}")
            _print_next_steps(target_dir, "Would create", provider_name, _is_in_tree(target_dir, template_dir))
            return

        if target_dir.exists() and not overwrite:
            raise FileExistsError(f"Target already exists: {_display_path(target_dir)}")

        _copy_scaffold(template_dir, target_dir, provider_name)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc

    _print_next_steps(target_dir, "Created", provider_name, _is_in_tree(target_dir, template_dir))


def _load_registry_or_exit() -> list[RegistryEntry]:
    """Load the provider registry, exiting with the loader's problems if it is invalid."""
    try:
        return load_registry(CONFIGS_ROOT)
    except ProviderRegistryError as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc


@app.command("list")
def list_cmd(
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Include deprecated providers."),
    ] = False,
) -> None:
    """List externally maintained providers from the provider registry.

    Examples:
        isvctl provider list
        isvctl provider list --all
    """
    entries = _load_registry_or_exit()
    if not show_all:
        entries = [entry for entry in entries if entry.status != "deprecated"]
    if not entries:
        print_progress("No providers registered.")
        return

    table = Table(
        title=f"Provider Registry ({len(entries)} providers)",
        title_justify="left",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Name", style="green", no_wrap=True)
    table.add_column("Vendor")
    table.add_column("Status", no_wrap=True)
    table.add_column("Tested on", no_wrap=True)
    table.add_column("Commit", style="magenta", no_wrap=True)
    table.add_column("Fetched", no_wrap=True)

    for entry in entries:
        commit = entry.commit[:12] if entry.commit else f"{entry.ref} (unpinned)"
        table.add_row(entry.name, entry.vendor, entry.status, entry.tested_with, commit, _fetch_state(entry))

    console.print(table)


def _fetch_state(entry: RegistryEntry) -> str:
    """Describe the checkout of ``entry``: ``no``, ``yes``, ``stale (<commit>)``, or ``local`` (not from fetch)."""
    head = fetched_commit(entry.name, CONFIGS_ROOT)
    if head is None:
        return "local" if external_checkout(entry.name, CONFIGS_ROOT).is_dir() else "no"
    if entry.commit is None or head == entry.commit:
        return "yes"
    return f"stale ({head[:12]})"


def _fetch_revision(repo_url: str, revision: str, checkout_dir: Path) -> str:
    """Check out one revision of ``repo_url`` into the empty ``checkout_dir``; return its commit SHA.

    ``--`` keeps a registry-supplied URL or ref that starts with ``-`` from
    being read as a git option. ``--template=`` keeps the user's template hooks
    (for example ``post-checkout``) out of the fetched checkout.
    """
    run_git("init", "-q", "--template=", cwd=checkout_dir)
    run_git("fetch", "-q", "--depth", "1", "--", repo_url, revision, cwd=checkout_dir)
    run_git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=checkout_dir)
    return run_git("rev-parse", "HEAD", cwd=checkout_dir)


def _print_fetch_next_steps(entry: RegistryEntry, checkout_dir: Path) -> None:
    """Print the declared suites the fetched provider ships a config for, and how to run one."""
    suites = [suite for suite in entry.suites if (checkout_dir / "config" / f"{suite}.yaml").is_file()]
    if not suites:
        typer.echo(f"No config/<suite>.yaml found for suites {', '.join(entry.suites)}.")
        typer.echo(f"See {_display_path(checkout_dir)} for the provider's configs.")
        return
    typer.echo(f"Suites: {', '.join(suites)}")
    if entry.status == "demo":
        typer.echo("Status 'demo': it runs in demo mode (dummy results, never uploaded) with no setup.")
    else:
        typer.echo(f"Setup and prerequisites (credentials, environment): {entry.documentation_url}")
    typer.echo("Then run one with:")
    typer.echo(f"  uv run isvctl test run --provider {entry.name} --suite {suites[0]}")


@app.command("fetch")
def fetch_cmd(
    name: Annotated[
        str,
        typer.Argument(help="Registry name of the provider, as shown by `isvctl provider list`."),
    ],
) -> None:
    """Fetch a registered provider at its pinned commit.

    The provider is checked out into isvctl/configs/providers-external/<name>/
    (git-ignored), replacing any previous checkout, and can then be run with
    `isvctl test run --provider <name>`.

    Examples:
        isvctl provider fetch acme
        isvctl test run --provider acme --suite vm
    """
    entry = next((entry for entry in _load_registry_or_exit() if entry.name == name), None)
    if entry is None:
        print_error(f"Unknown provider '{name}'. Run 'isvctl provider list --all' to see registered providers.")
        raise typer.Exit(code=1)
    if entry.status == "deprecated":
        print_warning(f"'{name}' is deprecated and no longer validated against current releases.")
    if entry.commit is None:
        print_warning(
            f"'{name}' is experimental and pinned only to ref '{entry.ref}'; its results are not reproducible."
        )

    target = external_checkout(entry.name, CONFIGS_ROOT)
    if target.exists() and not is_fetched_checkout(target):
        print_error(
            f"{_display_path(target)} was not created by `isvctl provider fetch` (a scaffold you are "
            "developing?). Move or delete it yourself before fetching."
        )
        raise typer.Exit(code=1)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Check out into a sibling staging directory and rename it into place, so a
    # failed fetch never leaves a partial checkout, or loses the previous one.
    staging = Path(tempfile.mkdtemp(prefix=f".fetch-{entry.name}-", dir=target.parent))
    try:
        head = _fetch_revision(entry.repo_url, entry.commit or entry.ref, staging)
        if entry.commit is not None and head != entry.commit:
            raise RuntimeError(f"checked out {head}, but the registry pins {entry.commit}")
    except RuntimeError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print_error(f"Could not fetch '{name}' from {entry.repo_url}: {exc}")
        raise typer.Exit(code=1) from exc
    mark_fetched(staging)

    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)

    typer.echo(f"Fetched {name} at {head[:12]} into {_display_path(target)}")
    _print_fetch_next_steps(entry, target)


@app.command("remove")
def remove_cmd(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Fetched providers to remove."),
    ] = None,
    remove_all: Annotated[
        bool,
        typer.Option("--all", help="Remove every fetched provider."),
    ] = False,
) -> None:
    """Remove fetched providers from isvctl/configs/providers-external/.

    Only checkouts made by `isvctl provider fetch` are deleted, including any
    local edits to them. Scaffolds you are developing there, the registry, and
    in-tree providers are never touched. Fetch again to restore one.

    Examples:
        isvctl provider remove acme
        isvctl provider remove --all
    """
    if remove_all == bool(names):
        print_error("Give one or more provider names, or --all.")
        raise typer.Exit(code=1)

    external_dir = CONFIGS_ROOT / EXTERNAL_PROVIDERS_DIRNAME
    if remove_all:
        entries = sorted(external_dir.iterdir()) if external_dir.is_dir() else []
        fetched = [path for path in entries if is_fetched_checkout(path)]
        # Staging directories left by an interrupted fetch go too.
        for path in [*fetched, *(path for path in entries if path.name.startswith(".fetch-"))]:
            shutil.rmtree(path)
        typer.echo(
            f"Removed {len(fetched)} fetched provider(s)"
            + (f": {', '.join(path.name for path in fetched)}" if fetched else ".")
        )
        kept = [path.name for path in entries if path.is_dir() and path not in fetched and path.name[0] != "."]
        if kept:
            typer.echo(f"Kept (not created by fetch): {', '.join(kept)}")
        return

    # The name pattern keeps a name such as '../providers' from reaching outside providers-external/.
    missing = [name for name in names if not PROVIDER_NAME_RE.fullmatch(name) or not (external_dir / name).is_dir()]
    local = [name for name in names if name not in missing and not is_fetched_checkout(external_dir / name)]
    if missing or local:
        if missing:
            print_error(f"Not fetched: {', '.join(missing)}. Run 'isvctl provider list' to see fetched providers.")
        if local:
            print_error(f"Not created by fetch, delete it yourself if you mean to: {', '.join(local)}")
        raise typer.Exit(code=1)
    for name in names:
        shutil.rmtree(external_dir / name)
        typer.echo(f"Removed {name} from {_display_path(external_dir / name)}")
