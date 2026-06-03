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

"""Unit tests for SSH/local command helpers in isvtest.core.ssh."""

import subprocess
from unittest.mock import patch

import pytest

from isvtest.core.ssh import (
    LocalExecutor,
    is_local_execution,
    open_host_session,
    run_ssh_command,
)


class TestIsLocalExecution:
    """Tests for the local-execution config flag."""

    def test_enabled_when_local_true(self) -> None:
        assert is_local_execution({"local": True}) is True

    def test_disabled_by_default(self) -> None:
        assert is_local_execution({}) is False

    def test_disabled_when_falsey(self) -> None:
        assert is_local_execution({"local": False}) is False


class TestOpenHostSession:
    """open_host_session returns a LocalExecutor only in local mode."""

    def test_returns_local_executor_in_local_mode(self) -> None:
        session = open_host_session({}, {"local": True})
        assert isinstance(session, LocalExecutor)

    def test_uses_ssh_client_when_not_local(self) -> None:
        ssh_cfg = {"ssh_host": "10.0.0.1", "ssh_user": "ubuntu", "ssh_key_path": "/tmp/k.pem"}
        with patch("isvtest.core.ssh.get_ssh_client") as mock_client:
            open_host_session(ssh_cfg, {})
        mock_client.assert_called_once_with("10.0.0.1", "ubuntu", "/tmp/k.pem", timeout=30)


class TestRunSshCommandLocal:
    """run_ssh_command executes locally when given a LocalExecutor."""

    def test_local_stdout_and_exit_code(self) -> None:
        exit_code, stdout, _ = run_ssh_command(LocalExecutor(), "printf hello")
        assert exit_code == 0
        assert stdout == "hello"

    def test_local_nonzero_exit_code(self) -> None:
        exit_code, _, _ = run_ssh_command(LocalExecutor(), "exit 3")
        assert exit_code == 3

    def test_local_timeout_raises(self) -> None:
        with patch("isvtest.core.ssh.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 1)):
            with pytest.raises(TimeoutError):
                run_ssh_command(LocalExecutor(), "sleep 100", timeout=1)


def test_local_executor_close_is_noop() -> None:
    assert LocalExecutor().close() is None
