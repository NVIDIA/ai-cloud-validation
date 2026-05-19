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

"""Tests for the SCP transfer module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from isvctl.remote.transfer import SCPTransfer, SCPTransferError


class TestSCPTransfer:
    """Tests for SCPTransfer class."""

    def test_init_basic(self) -> None:
        """Test basic initialization."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        assert scp.host == "192.168.1.100"
        assert scp.user == "nvidia"
        assert scp.port == 22
        assert scp.jumphost is None
        assert scp.timeout == 30

    def test_init_with_all_options(self) -> None:
        """Test initialization with all options."""
        scp = SCPTransfer(
            host="192.168.1.100",
            user="ubuntu",
            port=2222,
            jumphost="bastion:2260",
            timeout=60,
        )
        assert scp.host == "192.168.1.100"
        assert scp.user == "ubuntu"
        assert scp.port == 2222
        assert scp.jumphost == "bastion:2260"
        assert scp.timeout == 60

    def test_build_scp_options_basic(self) -> None:
        """Test SCP options for basic connection."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        opts = scp._build_scp_options()

        assert "-q" in opts
        assert "StrictHostKeyChecking=accept-new" in " ".join(opts)
        assert "BatchMode=yes" in " ".join(opts)
        assert "-P" not in opts  # Default port, no -P flag

    def test_build_scp_options_custom_port(self) -> None:
        """Test SCP options with custom port."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia", port=2222)
        opts = scp._build_scp_options()

        assert "-P" in opts  # SCP uses -P for port (not -p like ssh)
        idx = opts.index("-P")
        assert opts[idx + 1] == "2222"

    def test_build_scp_options_with_jumphost(self) -> None:
        """Test SCP options with jumphost."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia", jumphost="bastion:2260")
        opts = scp._build_scp_options()

        assert "-J" in opts
        idx = opts.index("-J")
        assert opts[idx + 1] == "bastion:2260"

    def test_build_remote_path(self) -> None:
        """Test remote path building."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        assert scp._build_remote_path("/home/nvidia/test") == "nvidia@192.168.1.100:/home/nvidia/test"

    @patch("subprocess.run")
    def test_upload_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test successful file upload."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        scp.upload(test_file, "/home/nvidia/test.txt")

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "scp" == call_args[0]
        assert str(test_file) in call_args
        assert "nvidia@192.168.1.100:/home/nvidia/test.txt" in call_args

    @patch("subprocess.run")
    def test_upload_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test failed file upload."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Permission denied",
        )

        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        scp = SCPTransfer(host="192.168.1.100", user="nvidia")

        with pytest.raises(SCPTransferError) as exc_info:
            scp.upload(test_file, "/home/nvidia/test.txt")

        assert "Permission denied" in str(exc_info.value)

    def test_upload_file_not_found(self, tmp_path: Path) -> None:
        """Test upload when local file doesn't exist."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia")

        with pytest.raises(FileNotFoundError):
            scp.upload(tmp_path / "nonexistent.txt", "/home/nvidia/test.txt")

    @patch("subprocess.run")
    def test_download_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test successful file download."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        local_path = tmp_path / "downloaded.txt"
        scp.download("/home/nvidia/test.txt", local_path)

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "scp" == call_args[0]
        assert "nvidia@192.168.1.100:/home/nvidia/test.txt" in call_args
        assert str(local_path) in call_args

    @patch("subprocess.run")
    def test_download_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test failed file download."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="No such file",
        )

        scp = SCPTransfer(host="192.168.1.100", user="nvidia")

        with pytest.raises(SCPTransferError) as exc_info:
            scp.download("/home/nvidia/test.txt", tmp_path / "downloaded.txt")

        assert "No such file" in str(exc_info.value)

    @patch("subprocess.run")
    def test_download_optional_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test optional download when file exists."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        result = scp.download_optional("/home/nvidia/test.txt", tmp_path / "downloaded.txt")

        assert result is True

    @patch("subprocess.run")
    def test_download_optional_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test optional download when file doesn't exist."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="No such file",
        )

        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        result = scp.download_optional("/home/nvidia/test.txt", tmp_path / "downloaded.txt")

        assert result is False

    def test_repr(self) -> None:
        """Test string representation."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        assert "nvidia@192.168.1.100:22" in repr(scp)

    def test_repr_with_jumphost(self) -> None:
        """Test string representation with jumphost."""
        scp = SCPTransfer(host="192.168.1.100", user="nvidia", jumphost="bastion:2260")
        repr_str = repr(scp)
        assert "nvidia@192.168.1.100:22" in repr_str
        assert "bastion:2260" in repr_str

    @patch("subprocess.run")
    def test_upload_scp_not_found(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Test handling when scp command is not found."""
        mock_run.side_effect = FileNotFoundError("scp not found")

        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        scp = SCPTransfer(host="192.168.1.100", user="nvidia")

        with pytest.raises(SCPTransferError) as exc_info:
            scp.upload(test_file, "/home/nvidia/test.txt")

        assert "not found" in str(exc_info.value).lower()
