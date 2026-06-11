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

"""Command executor for ISV stub scripts.

This module handles running ISV-provided lifecycle commands (stubs) and
capturing/validating their JSON output.
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from isvctl.config.schema import CommandConfig, CommandOutput
from isvctl.orchestrator.context import _create_jinja_env
from isvctl.redaction import mask_sensitive_args

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result of executing an ISV stub command.

    Attributes:
        success: Whether the command succeeded (exit code 0)
        exit_code: Process exit code
        stdout: Standard output (raw string)
        stderr: Standard error output
        output: Parsed and validated CommandOutput (for create commands)
        error: Error message if command or parsing failed
    """

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    output: CommandOutput | None = None
    error: str | None = None


class CommandExecutor:
    """Executes ISV stub commands and validates their output.

    The executor runs shell commands, captures stdout/stderr, and for
    create commands, parses and validates the JSON output against the
    CommandOutput schema.
    """

    def __init__(self, working_dir: str | Path | None = None) -> None:
        """Initialize command executor.

        Args:
            working_dir: Default working directory for commands
        """
        self.working_dir = Path(working_dir) if working_dir else Path.cwd()

    def execute(
        self,
        config: CommandConfig,
        context: dict[str, Any] | None = None,
        validate_output: bool = False,
    ) -> CommandResult:
        """Execute an ISV stub command.

        Args:
            config: Command configuration
            context: Context dict for rendering Jinja2 templates in args
            validate_output: If True, parse stdout as JSON and validate
                           against CommandOutput schema

        Returns:
            CommandResult with execution results

        Note:
            The command itself is NOT rendered with Jinja2 - only the args are.
            This is intentional to prevent command injection.
        """
        if config.skip:
            return CommandResult(
                success=True,
                exit_code=0,
                stdout="",
                stderr="",
                error="Command skipped",
            )

        if not config.command:
            return CommandResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr="",
                error="No command specified",
            )

        # Render args with context
        rendered_args = self._render_args(config.args, context or {})

        # Build full command
        cmd_parts = [config.command] + rendered_args

        # Determine working directory
        cwd = Path(config.working_dir) if config.working_dir else self.working_dir

        # Build environment
        env = None
        if config.env:
            import os

            env = os.environ.copy()
            env.update(config.env)

        logger.info(f"Executing: {mask_sensitive_args(cmd_parts)}")
        logger.debug(f"Working directory: {cwd}")

        try:
            result = subprocess.run(
                cmd_parts,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=config.timeout,
            )

            cmd_result = CommandResult(
                success=result.returncode == 0,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

            if result.returncode != 0:
                cmd_result.error = f"Command exited with code {result.returncode}"
                if result.stderr:
                    cmd_result.error += f": {result.stderr.strip()}"

            # Validate JSON output if requested
            if validate_output and cmd_result.success:
                cmd_result = self._validate_output(cmd_result)

            return cmd_result

        except subprocess.TimeoutExpired as e:
            return CommandResult(
                success=False,
                exit_code=-1,
                stdout=e.stdout if isinstance(e.stdout, str) else "",
                stderr=e.stderr if isinstance(e.stderr, str) else "",
                error=f"Command timed out after {config.timeout} seconds",
            )
        except FileNotFoundError:
            return CommandResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=f"Command not found: {config.command}",
            )
        except Exception as e:
            return CommandResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="",
                error=f"Command execution failed: {e}",
            )

    def _render_args(self, args: list[str], context: dict[str, Any]) -> list[str]:
        """Render Jinja2 templates in command arguments.

        Args:
            args: List of argument strings (may contain {{ }} templates)
            context: Context dictionary for template rendering

        Returns:
            List of rendered argument strings
        """
        env = _create_jinja_env()
        rendered = []

        for arg in args:
            if "{{" in arg and "}}" in arg:
                try:
                    template = env.from_string(arg)
                    rendered.append(template.render(**context))
                except Exception as e:
                    logger.warning(f"Failed to render arg '{arg}': {e}")
                    rendered.append(arg)
            else:
                rendered.append(arg)

        return rendered

    def _validate_output(self, result: CommandResult) -> CommandResult:
        """Parse and validate command stdout as JSON.

        Args:
            result: CommandResult with stdout to validate

        Returns:
            Updated CommandResult with parsed output or error
        """
        stdout = result.stdout.strip()
        if not stdout:
            result.error = "Command produced no output (expected JSON)"
            result.success = False
            return result

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            result.error = f"Invalid JSON output: {e}"
            result.success = False
            return result

        try:
            result.output = CommandOutput.model_validate(data)
        except ValidationError as e:
            result.error = f"Output validation failed: {e}"
            result.success = False

        return result
