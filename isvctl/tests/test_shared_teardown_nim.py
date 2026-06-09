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

"""Tests for the shared NIM teardown script."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_teardown_nim() -> ModuleType:
    """Load the shared teardown_nim.py script as a module."""
    script_path = Path(__file__).resolve().parents[1] / "configs" / "providers" / "shared" / "teardown_nim.py"
    spec = importlib.util.spec_from_file_location("shared_teardown_nim", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_container_remove_uses_teardown_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NIM container removal should not inherit the short generic SSH timeout."""
    module = _load_teardown_nim()
    calls: list[tuple[str, int]] = []

    class FakeSSH:
        """Minimal SSH object accepted by teardown_nim.main."""

        def close(self) -> None:
            """Close fake SSH client."""

    def fake_run_cmd(
        _ssh: FakeSSH,
        command: str,
        timeout: int = 60,
        operation: str | None = None,
    ) -> tuple[int, str, str]:
        _ = operation
        calls.append((command, timeout))
        return 0, "isv-nim\n", ""

    monkeypatch.setattr(module, "ssh_connect", lambda *_args: FakeSSH())
    monkeypatch.setattr(module, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        sys,
        "argv",
        ["teardown_nim.py", "--host", "192.0.2.10", "--key-file", "/tmp/key.pem"],
    )

    assert module.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["success"] is True
    remove_calls = [(command, timeout) for command, timeout in calls if command.startswith("docker rm -f")]
    assert remove_calls == [("docker rm -f isv-nim", 240)]


def test_timeout_error_names_failed_operation() -> None:
    """A remote command timeout should say which operation timed out."""
    module = _load_teardown_nim()

    class TimeoutSSH:
        """SSH stub that times out while starting a remote command."""

        def exec_command(self, _command: str, timeout: int | None = None) -> tuple[Any, Any, Any]:
            raise TimeoutError("timed out")

    with pytest.raises(TimeoutError, match="docker rm timed out after 240s"):
        module.run_cmd(TimeoutSSH(), "docker rm -f isv-nim", timeout=240, operation="docker rm")


def test_skip_returns_success_without_ssh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Teardown should be a no-op when the NIM deploy step was skipped."""
    module = _load_teardown_nim()

    def fail_connect(*_args: str) -> None:
        raise AssertionError("ssh_connect should not be called")

    monkeypatch.setattr(module, "ssh_connect", fail_connect)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "teardown_nim.py",
            "--host",
            "192.0.2.10",
            "--key-file",
            "/tmp/key.pem",
            "--skip",
            "--skip-reason",
            "NIM deployment skipped",
        ],
    )

    assert module.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["success"] is True
    assert output["skipped"] is True
    assert output["skip_reason"] == "NIM deployment skipped"
    assert output["message"] == "NIM deployment skipped"
