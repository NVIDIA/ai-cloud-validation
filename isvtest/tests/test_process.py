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

"""Tests for shared subprocess lifecycle handling."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from isvtest.core.process import run_command_process


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


@pytest.mark.skipif(os.name != "posix", reason="process-group behavior is POSIX-specific")
def test_keyboard_interrupt_stops_descendant_process(tmp_path: Path) -> None:
    """Ctrl-C must not leave a step's provider CLI child running."""
    child_pid_path = tmp_path / "child.pid"
    wrapper = """
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
    child_pid: int | None = None

    def _interrupt(signum: int, frame: object) -> None:
        """Turn the alarm into the Ctrl-C the orchestrator would receive."""
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGALRM, _interrupt)
    try:
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        with pytest.raises(KeyboardInterrupt):
            run_command_process(
                [sys.executable, "-c", wrapper, str(child_pid_path)],
                cwd=tmp_path,
                env=None,
                timeout=None,
            )

        child_pid = int(child_pid_path.read_text())
        deadline = time.monotonic() + 2
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_exists(child_pid)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def _process_exists(pid: int) -> bool:
    """Return whether a process is still running.

    A killed orphan stays a zombie when PID 1 does not reap children, as in
    CI containers whose PID 1 is ``tail -f /dev/null``; that counts as gone.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if not Path("/proc").is_dir():
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


@pytest.mark.skipif(os.name != "posix", reason="process-group behavior is POSIX-specific")
def test_timeout_does_not_wait_on_pipes_held_outside_the_group(tmp_path: Path) -> None:
    """A descendant in its own session keeps the pipes open; the timeout must still return."""
    wrapper = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"], start_new_session=True)
time.sleep(60)
"""
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_command_process([sys.executable, "-c", wrapper], cwd=tmp_path, env=None, timeout=1)

    assert time.monotonic() - started < 10


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="emulating macOS needs /proc to spot zombies")
def test_timeout_signals_group_after_wrapper_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrapper that already exited must not turn the timeout into a signalling error.

    macOS refuses to signal a group whose only member is an unreaped zombie
    with EPERM; emulate that so the escalation path is covered on Linux too.
    """
    real_killpg = os.killpg

    def macos_killpg(pgid: int, sig: int) -> None:
        # Still listed in /proc but not running: the leader is a zombie.
        if Path(f"/proc/{pgid}").is_dir() and not _process_exists(pgid):
            raise PermissionError(1, "Operation not permitted")
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", macos_killpg)

    test_timeout_does_not_wait_on_pipes_held_outside_the_group(tmp_path)
