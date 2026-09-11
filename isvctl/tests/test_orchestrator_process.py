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

"""Tests for orchestration subprocess lifecycle handling."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from isvctl.orchestrator.process import run_command_process


def test_run_command_process_captures_output(tmp_path: Path) -> None:
    """Successful commands return captured text output."""
    completed = run_command_process(
        [sys.executable, "-c", "print('ready')"],
        cwd=tmp_path,
        env=None,
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout == "ready\n"
    assert completed.stderr == ""


def test_run_command_process_accepts_no_timeout(tmp_path: Path) -> None:
    """A null step timeout waits for a command that owns its deadline."""
    completed = run_command_process(
        [sys.executable, "-c", "print('tool-owned-timeout')"],
        cwd=tmp_path,
        env=None,
        timeout=None,
    )

    assert completed.returncode == 0
    assert completed.stdout == "tool-owned-timeout\n"
    assert completed.stderr == ""


@pytest.mark.skipif(os.name != "posix", reason="process-group behavior is POSIX-specific")
def test_timeout_terminates_descendant_process(tmp_path: Path) -> None:
    """A timed-out wrapper must not leave its provider CLI child running."""
    child_pid_path = tmp_path / "child.pid"
    wrapper = """
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(str(child.pid))
print("wrapper-ready", flush=True)
time.sleep(60)
"""
    child_pid: int | None = None

    try:
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            run_command_process(
                [sys.executable, "-c", wrapper, str(child_pid_path)],
                cwd=tmp_path,
                env=None,
                timeout=0.5,
            )

        assert "wrapper-ready" in (exc_info.value.stdout or "")
        child_pid = int(child_pid_path.read_text())

        deadline = time.monotonic() + 2
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_exists(child_pid)
    finally:
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def _process_exists(pid: int) -> bool:
    """Return whether a process currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
