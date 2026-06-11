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

"""Main CLI entry point for isvctl."""

from typing import Annotated

import typer
from isvreporter.main import app as report_app
from isvreporter.version import get_version

from isvctl.cli import catalog, clean, deploy, docs, doctor, provider, test

app = typer.Typer(
    name="isvctl",
    help="ISV Lab controller for cluster lifecycle orchestration",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"isvctl {get_version('isvctl')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Show version and exit.", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """ISV Lab controller for cluster lifecycle orchestration."""


# Register subcommands
app.add_typer(catalog.app, name="catalog")
app.add_typer(clean.app, name="clean")
app.add_typer(deploy.app, name="deploy")
app.add_typer(docs.app, name="docs")
app.add_typer(doctor.app, name="doctor")
app.add_typer(provider.app, name="provider")
app.add_typer(test.app, name="test")
app.add_typer(report_app, name="report")


if __name__ == "__main__":
    app()
