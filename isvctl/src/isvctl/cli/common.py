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

"""Shared constants and helpers for CLI subcommands."""

import logging
from pathlib import Path

import typer
from rich.console import Console

from isvctl.config.user import apply_user_env, load_user_env

logger = logging.getLogger(__name__)

OUTPUT_DIR_NAME = "_output"

# Rich console for DIAGNOSTIC output (warnings/errors rendered with rich markup).
# Primary data tables/markdown keep using a plain Console() at the call site.
err_console = Console(stderr=True)


def print_error(message: str) -> None:
    """Write a standardized error message to stderr."""
    typer.echo(typer.style("Error:", fg=typer.colors.RED) + f" {message}", err=True)


def print_warning(message: str) -> None:
    """Write a standardized warning message to stderr."""
    typer.echo(typer.style("Warning:", fg=typer.colors.YELLOW) + f" {message}", err=True)


def print_progress(message: str) -> None:
    """Write a progress/status line to stderr (no prefix)."""
    typer.echo(message, err=True)


def print_step(message: str) -> None:
    """Write a progress step with the '==>' marker to stderr."""
    typer.echo(typer.style("==>", fg=typer.colors.GREEN) + f" {message}", err=True)


def apply_user_config(no_user_config: bool) -> None:
    """Apply persisted user config (config.yml / secrets.yml) to the environment.

    No-op when ``--no-user-config`` is set. Loaded values never override env
    vars already exported in the process (precedence: process env > files).
    Exits with a clear error if the user config is malformed, before any work
    begins.
    """
    if no_user_config:
        return
    try:
        applied = apply_user_env(load_user_env())
    except (ValueError, OSError) as e:
        print_error(f"Failed to load user config: {e}")
        raise typer.Exit(code=1) from e
    if applied:
        logger.debug("Applied user config env vars: %s", ", ".join(applied))


def get_output_dir(root: Path | None = None) -> Path:
    """Return the output directory, creating it if needed.

    Args:
        root: Base directory. Defaults to cwd when None.

    Returns:
        Path to the output directory (already created on disk).
    """
    base = root or Path.cwd()
    output_dir = base / OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir
