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

"""SCP file transfer using native scp subprocess.

This module uses the native `scp` command rather than paramiko to ensure
compatibility with SSH agents, certificates, and complex authentication setups.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class SCPTransferError(Exception):
    """Exception raised when SCP transfer fails."""

    pass


class SCPTransfer:
    """SCP file transfer using native scp command.

    Uses subprocess to invoke the system's scp command, which ensures
    compatibility with SSH agents, certificates, and ProxyJump for jumphosts.

    Example:
        >>> scp = SCPTransfer(host="192.168.1.100", user="nvidia")
        >>> scp.upload(Path("archive.tar.gz"), "/home/nvidia/")
        >>> scp.download("/home/nvidia/results.xml", Path("./results.xml"))
    """

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        jumphost: str | None = None,
        timeout: int = 30,
    ) -> None:
        """Initialize SCP transfer.

        Args:
            host: Target host for transfers
            user: Username for SSH connection
            port: SSH port (default: 22)
            jumphost: Optional jump host in format "host" or "host:port"
            timeout: Connection timeout in seconds
        """
        self.host = host
        self.user = user
        self.port = port
        self.jumphost = jumphost
        self.timeout = timeout

    def _build_scp_options(self) -> list[str]:
        """Build common SCP options.

        Returns:
            List of SCP command-line options
        """
        opts = [
            "-q",  # Quiet mode
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={self.timeout}",
            "-o",
            "BatchMode=yes",  # Disable password prompts
        ]

        if self.port != 22:
            opts.extend(["-P", str(self.port)])

        if self.jumphost:
            opts.extend(["-J", self.jumphost])

        return opts

    def _build_remote_path(self, path: str) -> str:
        """Build remote path string.

        Args:
            path: Remote path

        Returns:
            Full remote path in format user@host:path
        """
        return f"{self.user}@{self.host}:{path}"

    def upload(self, local_path: Path, remote_path: str) -> None:
        """Upload a file to the remote host.

        Args:
            local_path: Local file path to upload
            remote_path: Remote destination path

        Raises:
            SCPTransferError: If upload fails
            FileNotFoundError: If local file doesn't exist
        """
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        cmd = ["scp"] + self._build_scp_options() + [str(local_path), self._build_remote_path(remote_path)]

        logger.debug(f"SCP upload: {local_path} -> {self._build_remote_path(remote_path)}")
        logger.debug(f"SCP command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or f"scp exited with code {result.returncode}"
                raise SCPTransferError(f"Upload failed: {error_msg}")

            logger.info(f"Uploaded {local_path} to {self.host}:{remote_path}")

        except FileNotFoundError:
            raise SCPTransferError("scp command not found in PATH")

    def download(self, remote_path: str, local_path: Path) -> None:
        """Download a file from the remote host.

        Args:
            remote_path: Remote file path to download
            local_path: Local destination path

        Raises:
            SCPTransferError: If download fails
        """
        # Ensure parent directory exists
        local_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = ["scp"] + self._build_scp_options() + [self._build_remote_path(remote_path), str(local_path)]

        logger.debug(f"SCP download: {self._build_remote_path(remote_path)} -> {local_path}")
        logger.debug(f"SCP command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or f"scp exited with code {result.returncode}"
                raise SCPTransferError(f"Download failed: {error_msg}")

            logger.info(f"Downloaded {self.host}:{remote_path} to {local_path}")

        except FileNotFoundError:
            raise SCPTransferError("scp command not found in PATH")

    def download_optional(self, remote_path: str, local_path: Path) -> bool:
        """Download a file if it exists on the remote host.

        Args:
            remote_path: Remote file path to download
            local_path: Local destination path

        Returns:
            True if download succeeded, False if file doesn't exist or failed
        """
        try:
            self.download(remote_path, local_path)
            return True
        except SCPTransferError as e:
            logger.debug(f"Optional download failed: {e}")
            return False

    def __repr__(self) -> str:
        """Return string representation."""
        jumphost_info = f" via {self.jumphost}" if self.jumphost else ""
        return f"SCPTransfer({self.user}@{self.host}:{self.port}{jumphost_info})"
