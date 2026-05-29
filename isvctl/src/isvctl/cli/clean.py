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

"""Clean subcommand for isvctl.

Handles ISV Lab clean-up operations: firmware validation, flashing,
network reset, BCM validation, OS reimage, and OS configuration.
"""

import logging
from typing import Annotated

import typer

from isvctl.cleaner.operations import OPERATIONS
from isvctl.cleaner.runner import OperationRunner
from isvctl.cli import setup_logging
from isvctl.cli.common import print_error, print_progress

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="clean",
    help="ISV Lab clean-up operations",
    no_args_is_help=True,
)


def _validate_operations(operations: list[str]) -> list[str]:
    """Validate and expand operation names.

    Args:
        operations: List of operation names (may include 'all')

    Returns:
        Expanded list of valid operation names

    Raises:
        typer.BadParameter: If an unknown operation is specified or list is empty
    """
    if not operations:
        raise typer.BadParameter("At least one operation must be provided")

    if "all" in operations:
        return list(OPERATIONS.keys())

    # Validate all operations exist
    for op in operations:
        if op not in OPERATIONS:
            valid_ops = ", ".join(["all"] + list(OPERATIONS.keys()))
            raise typer.BadParameter(f"Unknown operation: '{op}'. Valid operations: {valid_ops}")
    return operations


@app.command("run")
def run(
    operations: Annotated[
        list[str],
        typer.Argument(
            help="Operations to execute. Use 'all' for all operations, or specify one or more: "
            + ", ".join(OPERATIONS.keys()),
        ),
    ],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show detailed output for each operation",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show operations that would be executed without running them",
        ),
    ] = False,
    continue_on_error: Annotated[
        bool,
        typer.Option(
            "--continue-on-error",
            help="Continue executing operations even if one fails",
        ),
    ] = False,
) -> None:
    """Run ISV Lab clean-up operations.

    Execute one or more clean-up operations sequentially. Use 'all' to run
    all available operations in order.

    Examples:
        isvctl clean run all
        isvctl clean run firmware-validation firmware-flashing
        isvctl clean run all --dry-run
        isvctl clean run network-reset -v
    """
    setup_logging(verbose)

    # Validate and expand operations
    try:
        operations_to_run = _validate_operations(operations)
    except typer.BadParameter as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    # Create runner instance
    runner = OperationRunner(
        verbose=verbose,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
    )

    # Execute operations
    print_progress("Starting NVIDIA ISV Lab clean-up operations...")
    results = runner.run_operations(operations_to_run)

    # Report results
    typer.echo("\n" + "=" * 70)
    typer.echo("EXECUTION SUMMARY")
    typer.echo("=" * 70)

    for result in results:
        if result["success"]:
            status = typer.style("[PASS]", fg=typer.colors.GREEN)
        else:
            status = typer.style("[FAIL]", fg=typer.colors.RED)
        typer.echo(f"{status} {result['operation']}")
        if not result["success"] and result.get("error"):
            typer.echo(f"  Error: {result['error']}", err=True)

    # Calculate final exit code
    failed_count = sum(1 for r in results if not r["success"])
    if failed_count > 0:
        typer.echo(f"\n{failed_count} operation(s) failed", err=True)
        raise typer.Exit(code=1)
    else:
        typer.echo("\nAll operations completed successfully")


@app.command("list")
def list_operations() -> None:
    """List all available clean-up operations."""
    typer.echo("Available clean-up operations:\n")
    for name, (_, description) in OPERATIONS.items():
        typer.echo(f"  {name}")
        typer.echo(f"    {description}\n")
