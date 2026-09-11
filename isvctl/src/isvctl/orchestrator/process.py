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

"""Subprocess execution shared by orchestration command models."""

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

_TERMINATION_GRACE_SECONDS = 2.0


def run_command_process(
    args: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str] | None,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Run an orchestration command and terminate its process group on timeout.

    Orchestration steps commonly invoke wrappers which then start a provider
    CLI.  Killing only the wrapper can leave that CLI running after the step is
    reported as timed out.  On POSIX, every command therefore starts in a new
    session and timeout handling signals the whole process group.  Other
    platforms fall back to terminating the direct child process.

    Args:
        args: Command and arguments to execute without a shell.
        cwd: Working directory for the command.
        env: Complete process environment, or ``None`` to inherit it.
        timeout: Maximum execution time in seconds, or ``None`` for no limit.

    Returns:
        The completed process with captured text stdout and stderr.

    Raises:
        subprocess.TimeoutExpired: The command exceeded ``timeout``. Captured
            stdout and stderr are attached after the process tree is stopped.
        OSError: The command could not be started.
    """
    command = list(args)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            stdout, stderr = process.communicate()
        else:
            # The direct child may exit while a descendant that closed the
            # inherited pipes remains alive. Ensure the process group is gone.
            _kill_process_tree(process)

        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Request graceful termination of a process group or direct process."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Force termination of a process group or direct process."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
