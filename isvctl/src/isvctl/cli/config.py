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
from isvctl.config.env_catalog import (
    ENV_VARS,
    SECTIONS,
    EnvVar,
    env_to_section_key,
    section_key_to_env,
    vars_for_provider,
)
from isvctl.config.user import (
    clear_user_config,
    get_config_path,
    get_secrets_path,
    load_user_env,
    unset_user_config,
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
_NON_PERSISTABLE_NAMES = frozenset(var.name for var in ENV_VARS if not var.persistable)
_SECTION_NAMES = frozenset(section.name for section in SECTIONS)
_SECTION_ENV_NAMES = {
    section.name: tuple(
        var.name for var in ENV_VARS if var.persistable and env_to_section_key(var.name)[0] == section.name
    )
    for section in SECTIONS
}


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


def _resolve_config_key(key: str) -> str:
    """Resolve an env var name or ``section.key`` alias to a persistable env name."""
    if "." in key:
        section, section_key = key.split(".", 1)
        env_name = section_key_to_env(section, section_key)
        if env_name is not None:
            return env_name
    else:
        if key in _NON_PERSISTABLE_NAMES:
            raise ValueError(
                f"'{key}' is a per-run flag and is not persisted; pass it on the command line or export it"
            )
        try:
            env_to_section_key(key)
        except KeyError:
            pass
        else:
            return key
    raise ValueError(f"unknown config key '{key}' (use an env var name or section.key)")


def _resolve_config_key_or_exit(key: str) -> str:
    """Resolve a CLI key, exiting with a usage error on bad input."""
    try:
        return _resolve_config_key(key)
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(code=2) from exc


def _parse_assignment(raw: str) -> tuple[str, str]:
    """Parse one ``key=value`` token into an env var name and value."""
    key, value = raw.split("=", 1)
    return _resolve_config_key_or_exit(key), value


def _parse_set_args(args: list[str]) -> tuple[dict[str, str], str | None]:
    """Parse ``configure set`` args into explicit values and optional prompt key."""
    if not args:
        print_error("provide a key or key=value assignment")
        raise typer.Exit(code=2)

    if len(args) == 1:
        if "=" in args[0]:
            return dict([_parse_assignment(args[0])]), None
        return {}, _resolve_config_key_or_exit(args[0])

    if len(args) == 2:
        if "=" not in args[0]:
            return {_resolve_config_key_or_exit(args[0]): args[1]}, None
        if "=" not in args[1]:
            print_error("cannot combine key=value with a separate value")
            raise typer.Exit(code=2)
        return dict(_parse_assignment(arg) for arg in args), None

    if all("=" in arg for arg in args):
        return dict(_parse_assignment(arg) for arg in args), None

    print_error("multiple values must use key=value assignments")
    raise typer.Exit(code=2)


def _unset_section(section: str) -> None:
    """Remove all persisted values for one config section after confirmation."""
    current = _load_current()
    env_names = tuple(name for name in _SECTION_ENV_NAMES[section] if name in current)
    if not env_names:
        typer.echo(f"No saved values for {section}.")
        return

    typer.echo(f"{section}:")
    for env_name in env_names:
        _, section_key = env_to_section_key(env_name)
        suffix = " (secret)" if is_secret_env_var(env_name) else ""
        typer.echo(f"  {section_key}{suffix}")

    confirmed = typer.confirm(f"Remove {len(env_names)} saved value(s) from section '{section}'?", default=False)
    if not confirmed:
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    try:
        unset_user_config(env_names, existing=current)
    except (ValueError, OSError) as exc:
        print_error(f"Failed to update user config: {exc}")
        raise typer.Exit(code=1) from exc

    print_step(f"Unset {len(env_names)} var(s) from {section}.")


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
        isvctl configure set NICO_API_BASE https://nico.example.com
        isvctl configure unset nico.api_base
        isvctl configure unset nico
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


@app.command("set")
def set_value(
    args: Annotated[
        list[str],
        typer.Argument(
            help=("Env var name or section.key, optionally as key=value. Use key=value for multiple values."),
        ),
    ],
) -> None:
    """Set one or more persisted configuration values."""
    values, prompt_env_name = _parse_set_args(args)
    current = _load_current()

    if prompt_env_name is not None:
        section, section_key = env_to_section_key(prompt_env_name)
        prompt = f"{prompt_env_name} ({section}.{section_key})"
        values[prompt_env_name] = typer.prompt(
            prompt,
            hide_input=is_secret_env_var(prompt_env_name),
            show_default=False,
        )

    try:
        result = write_user_config(values, existing=current)
    except (ValueError, OSError) as exc:
        print_error(f"Failed to write user config: {exc}")
        raise typer.Exit(code=1) from exc

    config_count = sum(1 for name in values if not is_secret_env_var(name))
    secret_count = len(values) - config_count
    if config_count:
        print_step(f"Set {config_count} var(s) in {result.config_path}")
    if secret_count:
        print_step(f"Set {secret_count} secret(s) in {result.secrets_path} (mode 0600)")


@app.command("unset")
def unset_value(
    key: Annotated[
        str | None,
        typer.Argument(help="Env var name, section.key, or section name to remove."),
    ] = None,
    all_values: Annotated[
        bool,
        typer.Option("--all", help="Remove all saved isvctl configuration after confirmation."),
    ] = False,
) -> None:
    """Remove one persisted value, one section, or all values with ``--all``."""
    if all_values and key is not None:
        print_error("pass either a key or --all, not both")
        raise typer.Exit(code=2)
    if not all_values and key is None:
        print_error("provide a key or pass --all")
        raise typer.Exit(code=2)

    if all_values:
        config_path = get_config_path()
        secrets_path = get_secrets_path()
        if not config_path.exists() and not secrets_path.exists():
            typer.echo("No configuration found.")
            return
        typer.echo(f"config.yml:  {config_path}")
        typer.echo(f"secrets.yml: {secrets_path}")
        confirmed = typer.confirm("Remove all saved isvctl configuration?", default=False)
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=1)
        try:
            clear_user_config()
        except OSError as exc:
            print_error(f"Failed to remove user config: {exc}")
            raise typer.Exit(code=1) from exc
        print_step("Removed all saved isvctl configuration.")
        return

    if key in _SECTION_NAMES:
        _unset_section(key)
        return

    env_name = _resolve_config_key_or_exit(key)
    current = _load_current()
    if env_name not in current:
        typer.echo(f"{env_name} is not saved.")
        return

    try:
        result = unset_user_config([env_name], existing=current)
    except (ValueError, OSError) as exc:
        print_error(f"Failed to update user config: {exc}")
        raise typer.Exit(code=1) from exc

    target = result.secrets_path if is_secret_env_var(env_name) else result.config_path
    print_step(f"Unset {env_name} from {target}")


@app.command("path")
def path() -> None:
    """Print the config and secrets file paths."""
    typer.echo(str(get_config_path()))
    typer.echo(str(get_secrets_path()))
