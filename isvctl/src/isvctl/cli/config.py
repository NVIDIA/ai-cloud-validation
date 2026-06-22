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

"""Interactive ``isvctl configure`` command.

Persists the env vars an ``isvctl test run`` needs so users do not re-export
them in every shell. Non-secret values are written to ``config.yml`` and
secrets to a ``0600`` ``secrets.yml``. The variable catalog and provider
grouping come from ``isvctl.config.env_catalog`` (shared with ``doctor``).
"""

from typing import Annotated

import typer

from isvctl.cli.common import print_error, print_step
from isvctl.config.env_catalog import EnvVar, env_to_section_key, vars_for_provider
from isvctl.config.user import (
    get_config_path,
    get_secrets_path,
    load_user_env,
    write_user_config,
)
from isvctl.redaction import is_secret_env_var

app = typer.Typer(
    name="configure",
    help="Persist env vars for `isvctl test run` (interactive).",
    invoke_without_command=True,
    no_args_is_help=False,
)

_SECRET_PLACEHOLDER = "(set)"


def _resolve_vars(provider: str | None) -> list[EnvVar]:
    """Resolve persistable catalog vars for a provider, exiting on a bad name.

    Per-run flags (the "Flags" group) are excluded — they are not persisted.
    """
    try:
        return [var for var in vars_for_provider(provider) if var.persistable]
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(code=2) from exc


def _load_current() -> dict[str, str]:
    """Load persisted config, exiting cleanly on a malformed file.

    ``configure`` is the command users reach for to *fix* a broken file, so a
    bad config.yml/secrets.yml must surface as a clear error, not a traceback.
    """
    try:
        return load_user_env()
    except (ValueError, OSError) as exc:
        print_error(f"Failed to read user config: {exc}")
        raise typer.Exit(code=1) from exc


@app.callback(invoke_without_command=True)
def configure(
    ctx: typer.Context,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Only prompt for this provider's vars (e.g. 'nico', 'aws').",
        ),
    ] = None,
) -> None:
    """Interactively set isvctl configuration.

    Walks every variable isvctl knows about (or just one provider's, with
    ``--provider``), pre-filling current values. Press Enter to keep a value;
    secrets are entered hidden. Non-secrets are saved to config.yml, secrets to
    a 0600 secrets.yml.

    Examples:
        isvctl configure
        isvctl configure --provider nico
        isvctl configure show
        isvctl configure path
    """
    if ctx.invoked_subcommand is not None:
        return

    variables = _resolve_vars(provider)
    current = _load_current()

    typer.echo("Configuring isvctl. Press Enter to keep the current value.\n")

    answers: dict[str, str] = {}
    last_group: str | None = None
    for var in variables:
        if var.group != last_group:
            typer.echo(typer.style(var.group, bold=True))
            last_group = var.group

        secret = is_secret_env_var(var.name)
        existing = current.get(var.name)
        prompt_text = f"  {var.name} ({var.hint})"

        if secret:
            if existing:
                prompt_text += f" [{_SECRET_PLACEHOLDER}]"
            value = typer.prompt(prompt_text, default="", hide_input=True, show_default=False)
        else:
            value = typer.prompt(prompt_text, default=existing or "", show_default=bool(existing))

        if value:
            answers[var.name] = value

    if not answers:
        typer.echo("\nNothing to save.")
        return

    try:
        result = write_user_config(answers, existing=current)
    except (ValueError, OSError) as exc:
        print_error(f"Failed to write user config: {exc}")
        raise typer.Exit(code=1) from exc

    config_count = sum(1 for name in answers if not is_secret_env_var(name))
    secret_count = len(answers) - config_count
    typer.echo("")
    if config_count:
        print_step(f"Wrote {config_count} var(s) to {result.config_path}")
    if secret_count:
        print_step(f"Wrote {secret_count} secret(s) to {result.secrets_path} (mode 0600)")

    verify = "isvctl doctor" + (f" --provider {provider}" if provider else "")
    typer.echo(f"\nVerify with: {verify}")


@app.command("show")
def show(
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Only show this provider's vars."),
    ] = None,
) -> None:
    """Show persisted configuration (secret values are never printed)."""
    variables = _resolve_vars(provider)
    current = _load_current()
    configured = [var for var in variables if var.name in current]

    typer.echo(f"config.yml:  {get_config_path()}")
    typer.echo(f"secrets.yml: {get_secrets_path()}")

    if not configured:
        typer.echo("\nNo configuration found. Run `isvctl configure`.")
        return

    typer.echo("")
    pairs = [(env_to_section_key(var.name), var) for var in configured]
    width = max(len(key) for (_, key), _ in pairs)
    last_section: str | None = None
    for (section, key), var in pairs:
        if section != last_section:
            typer.echo(typer.style(f"{section}:", bold=True))
            last_section = section
        display = _SECRET_PLACEHOLDER if is_secret_env_var(var.name) else current[var.name]
        typer.echo(f"  {key.ljust(width)} = {display}")


@app.command("path")
def path() -> None:
    """Print the config and secrets file paths."""
    typer.echo(str(get_config_path()))
    typer.echo(str(get_secrets_path()))
