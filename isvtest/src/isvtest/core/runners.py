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

"""Runners for executing commands and ReFrame tests.

Security Note:
    These runners execute shell commands. Commands should only come from trusted
    sources (validation test code, config files authored by administrators).
    Do not pass untrusted user input directly to these runners.
"""

import logging
import shlex
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================================
# Command Execution Runners (for validations/workloads)
# ============================================================================


@dataclass
class CommandResult:
    """Result of a command execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


class Runner(ABC):
    """Abstract base class for execution runners."""

    @abstractmethod
    def run(self, cmd: str | list[str], timeout: int = 60) -> CommandResult:
        """Run a command and return the result.

        Args:
            cmd: Command to execute. Can be a string or list of arguments.
            timeout: Maximum execution time in seconds.

        Returns:
            CommandResult with exit code, stdout, stderr, and timing info.
        """
        pass


class LocalRunner(Runner):
    """Executes commands on the local machine.

    Security Note:
        When cmd is a string, shell=True is used to support shell features
        (pipes, redirections, etc.). Only pass trusted commands from validation
        test code or admin-authored config files.

        For safer execution without shell interpretation, pass cmd as a list.
    """

    def run(self, cmd: str | list[str], timeout: int = 60) -> CommandResult:
        """Run a command locally.

        Args:
            cmd: Command to execute. Can be a string (uses shell) or list (no shell).
            timeout: Maximum execution time in seconds.

        Returns:
            CommandResult with exit code, stdout, stderr, and timing info.
        """
        start_time = time.time()
        logger.debug(f"LocalRunner executing: {cmd}")

        # Determine if we need shell interpretation
        use_shell = isinstance(cmd, str)

        try:
            process = subprocess.run(
                cmd,
                shell=use_shell,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.time() - start_time
            return CommandResult(
                exit_code=process.returncode,
                stdout=process.stdout.strip(),
                stderr=process.stderr.strip(),
                duration=duration,
            )
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_time
            # Preserve any partial output for debugging hung commands
            stdout = (getattr(e, "stdout", "") or "").strip() if hasattr(e, "stdout") else ""
            stderr = (getattr(e, "stderr", "") or "").strip() if hasattr(e, "stderr") else ""
            stderr = stderr or f"Command timed out after {timeout}s"
            return CommandResult(
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                duration=duration,
                timed_out=True,
            )
        except Exception as e:
            duration = time.time() - start_time
            return CommandResult(exit_code=-1, stdout="", stderr=str(e), duration=duration)


class KubernetesRunner(Runner):
    """Executes commands inside a Kubernetes Pod.

    Security Note:
        Commands are passed to kubectl exec. The namespace, pod_name, and
        container are quoted to prevent shell injection.
    """

    def __init__(self, namespace: str = "default", pod_name: str | None = None, container: str | None = None):
        self.namespace = namespace
        self.pod_name = pod_name
        self.container = container
        self._local_runner = LocalRunner()

    def run(self, cmd: str, timeout: int = 60) -> CommandResult:
        """Execute a command inside the Kubernetes pod.

        Args:
            cmd: Command to execute inside the pod.
            timeout: Maximum execution time in seconds.

        Returns:
            CommandResult with exit code, stdout, stderr, and timing info.
        """
        if not self.pod_name:
            return CommandResult(exit_code=-1, stdout="", stderr="No pod specified for KubernetesRunner", duration=0.0)

        # Import here to avoid circular dependency
        from isvtest.core.k8s import get_kubectl_command

        # Build kubectl command as a list for safer execution
        kubectl_cmd = get_kubectl_command()
        kubectl_cmd.extend(
            [
                "exec",
                "-n",
                self.namespace,
                self.pod_name,
            ]
        )
        if self.container:
            kubectl_cmd.extend(["-c", self.container])

        # Use -- to separate kubectl args from command
        # The command is passed to sh -c for shell interpretation inside the container
        kubectl_cmd.extend(["--", "sh", "-c", cmd])

        logger.debug(f"KubernetesRunner executing: {shlex.join(kubectl_cmd)}")
        return self._local_runner.run(kubectl_cmd, timeout)


# ============================================================================
# ReFrame Test Runners (for ReFrame-based tests)
# ============================================================================

# Compute default test path relative to this module's location
_DEFAULT_TEST_PATH = str(Path(__file__).parent.parent / "workloads")


def run_reframe_tests(
    test_path: str = _DEFAULT_TEST_PATH,
    tags: list[str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """Run ReFrame tests from specified path.

    Args:
        test_path: Path to ReFrame tests directory.
        tags: Optional list of tags to filter tests.
        timeout: Command timeout in seconds (default: 300).

    Returns:
        Dictionary with success status and return code.
        On timeout, returns success=False with returncode=-1 and error message.
    """
    # Find reframe executable (should be in same venv as current Python)
    reframe_cmd = shutil.which("reframe")
    if not reframe_cmd:
        # Try in the same directory as the Python executable
        python_dir = Path(sys.executable).parent
        reframe_cmd = str(python_dir / "reframe")
        if not Path(reframe_cmd).exists():
            return {
                "success": False,
                "returncode": -1,
                "error": "reframe command not found in PATH or venv",
            }

    # -R forces recursive search (needed because directories have __init__.py)
    cmd = [reframe_cmd, "-c", test_path, "-R", "-r"]
    if tags:
        # Add multiple --tag arguments for AND logic
        for tag in tags:
            cmd.extend(["--tag", tag])

    # Run without capturing output so user can see ReFrame's progress
    try:
        result = subprocess.run(cmd, timeout=timeout, check=False)
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "returncode": -1,
            "error": f"ReFrame test execution timed out after {timeout} seconds",
        }


def run_workload_tests(tags: list[str] | None = None, timeout: int = 600) -> dict[str, Any]:
    """Run workload tests (ReFrame-based) with extended timeout.

    Args:
        tags: Optional list of tags to filter workload tests (e.g., ['nccl']).
        timeout: Command timeout in seconds (default: 600).

    Returns:
        Dictionary with success status and return code.
    """
    workload_path = str(Path(__file__).parent.parent / "workloads")
    return run_reframe_tests(
        test_path=workload_path,
        tags=tags,
        timeout=timeout,
    )
