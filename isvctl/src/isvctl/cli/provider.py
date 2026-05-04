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

"""Provider scaffold commands."""

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="provider",
    help="Manage provider scaffolds",
    no_args_is_help=True,
)

PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
TEMPLATE_PROVIDER_NAME = "my-isv"
TEMPLATE_PROVIDER_TOKEN_RE = re.compile(r"(?<!\w)" + re.escape(TEMPLATE_PROVIDER_NAME) + r"(?!\w)")
IGNORE_NAMES = ("__pycache__", ".pytest_cache")
SCAFFOLD_META_FILE = ".scaffold-meta"
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
    """Resolve the scaffold destination path."""
    if output_dir is None:
        return (template_dir.parent / provider_name).resolve()
    return output_dir.expanduser().resolve()


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


def _print_next_steps(target_dir: Path, action: str) -> None:
    """Print scaffold creation output and next commands."""
    display_target = _display_path(target_dir)
    demo_config = shlex.quote(f"{display_target}/config/vm.yaml")
    launch_script = shlex.quote(f"{display_target}/scripts/vm/launch_instance.py")
    typer.echo(f"{action} provider scaffold: {display_target}")
    typer.echo()
    typer.echo("Preview without cloud:")
    typer.echo(f"  ISVCTL_DEMO_MODE=1 uv run isvctl test run -f {demo_config}")
    typer.echo()
    typer.echo("Start implementing:")
    typer.echo(f"  {launch_script}")


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
            help="Destination directory. Defaults to isvctl/configs/providers/<provider-name>.",
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
                    typer.echo(f"Note: target exists; --overwrite would be required: {_display_path(target_dir)}")
            _print_next_steps(target_dir, "Would create")
            return

        if target_dir.exists() and not overwrite:
            raise FileExistsError(f"Target already exists: {_display_path(target_dir)}")

        _copy_scaffold(template_dir, target_dir, provider_name)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _print_next_steps(target_dir, "Created")
