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

"""Tests for the SSH client module."""

from unittest.mock import MagicMock, patch

from isvctl.remote.ssh import SSHClient, SSHResult


class TestSSHClient:
    """Tests for SSHClient class."""

    def test_init_basic(self) -> None:
        """Test basic initialization."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        assert ssh.host == "192.168.1.100"
        assert ssh.user == "nvidia"
        assert ssh.port == 22
        assert ssh.jumphost is None
        assert ssh.timeout == 30
        assert ssh.quiet is True

    def test_init_with_all_options(self) -> None:
        """Test initialization with all options."""
        ssh = SSHClient(
            host="192.168.1.100",
            user="ubuntu",
            port=2222,
            jumphost="bastion:2260",
            timeout=60,
            quiet=False,
        )
        assert ssh.host == "192.168.1.100"
        assert ssh.user == "ubuntu"
        assert ssh.port == 2222
        assert ssh.jumphost == "bastion:2260"
        assert ssh.timeout == 60
        assert ssh.quiet is False

    def test_build_ssh_options_basic(self) -> None:
        """Test SSH options for basic connection."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        opts = ssh._build_ssh_options()

        assert "-q" in opts
        assert "StrictHostKeyChecking=accept-new" in " ".join(opts)
        assert "BatchMode=yes" in " ".join(opts)
        assert "-p" not in opts  # Default port, no -p flag

    def test_build_ssh_options_not_quiet(self) -> None:
        """Test SSH options can leave diagnostics enabled."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia", quiet=False)
        opts = ssh._build_ssh_options()

        assert "-q" not in opts
        assert "StrictHostKeyChecking=accept-new" in " ".join(opts)
        assert "BatchMode=yes" in " ".join(opts)

    def test_build_ssh_options_quiet_override(self) -> None:
        """Test per-command quiet override."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        opts = ssh._build_ssh_options(quiet=False)

        assert "-q" not in opts

    def test_build_ssh_options_custom_port(self) -> None:
        """Test SSH options with custom port."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia", port=2222)
        opts = ssh._build_ssh_options()

        assert "-p" in opts
        idx = opts.index("-p")
        assert opts[idx + 1] == "2222"

    def test_build_ssh_options_with_jumphost(self) -> None:
        """Test SSH options with jumphost."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia", jumphost="bastion:2260")
        opts = ssh._build_ssh_options()

        assert "-J" in opts
        idx = opts.index("-J")
        assert opts[idx + 1] == "bastion:2260"

    def test_build_target(self) -> None:
        """Test target string building."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        assert ssh._build_target() == "nvidia@192.168.1.100"

    @patch("subprocess.run")
    def test_execute_success(self, mock_run: MagicMock) -> None:
        """Test successful command execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="hostname123\n",
            stderr="",
        )

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        result = ssh.execute("hostname")

        assert result.success is True
        assert result.exit_code == 0
        assert "hostname123" in result.stdout
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_execute_failure(self, mock_run: MagicMock) -> None:
        """Test failed command execution."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="command not found",
        )

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        result = ssh.execute("nonexistent-command")

        assert result.success is False
        assert result.exit_code == 1
        assert "command not found" in result.stderr

    @patch("subprocess.run")
    def test_execute_with_env(self, mock_run: MagicMock) -> None:
        """Test command execution with environment variables."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="value\n",
            stderr="",
        )

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        result = ssh.execute("echo $MY_VAR", env={"MY_VAR": "value"})

        assert result.success is True
        # Verify the script passed via stdin includes env vars
        call_kwargs = mock_run.call_args[1]
        script = call_kwargs.get("input", "")
        assert "MY_VAR=" in script
        assert "value" in script

    @patch("subprocess.run")
    def test_check_command_exists_true(self, mock_run: MagicMock) -> None:
        """Test check_command_exists returns True when command exists."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        assert ssh.check_command_exists("uv") is True

    @patch("subprocess.run")
    def test_check_command_exists_false(self, mock_run: MagicMock) -> None:
        """Test check_command_exists returns False when command doesn't exist."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        assert ssh.check_command_exists("nonexistent") is False

    @patch("subprocess.run")
    def test_ensure_directory(self, mock_run: MagicMock) -> None:
        """Test ensure_directory creates directory."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        result = ssh.ensure_directory("/home/nvidia/test")
        assert result.success is True

    @patch("subprocess.run")
    def test_test_connection(self, mock_run: MagicMock) -> None:
        """Test test_connection method."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        result = ssh.test_connection()
        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert "-q" not in cmd

    @patch("subprocess.run")
    def test_is_connection_error(self, mock_run: MagicMock) -> None:
        """Test is_connection_error detects SSH connection failures."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia")

        # Exit code 255 indicates SSH connection error
        from isvctl.remote.ssh import SSHResult

        conn_error = SSHResult(success=False, exit_code=255, stderr="Connection refused")
        assert ssh.is_connection_error(conn_error) is True

        # Other exit codes are command failures, not connection errors
        cmd_error = SSHResult(success=False, exit_code=1, stderr="Command failed")
        assert ssh.is_connection_error(cmd_error) is False

    def test_repr(self) -> None:
        """Test string representation."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        assert "nvidia@192.168.1.100:22" in repr(ssh)

    def test_repr_with_jumphost(self) -> None:
        """Test string representation with jumphost."""
        ssh = SSHClient(host="192.168.1.100", user="nvidia", jumphost="bastion:2260")
        repr_str = repr(ssh)
        assert "nvidia@192.168.1.100:22" in repr_str
        assert "bastion:2260" in repr_str

    @patch("subprocess.run")
    def test_execute_file_not_found(self, mock_run: MagicMock) -> None:
        """Test handling when ssh command is not found."""
        mock_run.side_effect = FileNotFoundError("ssh not found")

        ssh = SSHClient(host="192.168.1.100", user="nvidia")
        result = ssh.execute("hostname")

        assert result.success is False
        assert result.exit_code == -1
        assert "not found" in result.stderr.lower()


class TestSSHResult:
    """Tests for SSHResult dataclass."""

    def test_result_success(self) -> None:
        """Test successful result."""
        result = SSHResult(success=True, exit_code=0, stdout="output", stderr="")
        assert result.success is True
        assert result.exit_code == 0
        assert result.stdout == "output"

    def test_result_failure(self) -> None:
        """Test failed result."""
        result = SSHResult(success=False, exit_code=1, stdout="", stderr="error")
        assert result.success is False
        assert result.exit_code == 1
        assert result.stderr == "error"
