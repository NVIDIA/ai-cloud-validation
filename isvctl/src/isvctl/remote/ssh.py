# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""SSH command execution using native ssh subprocess.

This module uses the native `ssh` command rather than paramiko to ensure
compatibility with SSH agents, certificates, and complex authentication setups.
"""

import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import IO

logger = logging.getLogger(__name__)


@dataclass
class SSHResult:
    """Result of an SSH command execution.

    Attributes:
        success: Whether the command succeeded (exit code 0)
        exit_code: Process exit code
        stdout: Standard output (if captured)
        stderr: Standard error output (if captured)
    """

    success: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class SSHClient:
    """SSH client using native ssh command.

    Uses subprocess to invoke the system's ssh command, which ensures
    compatibility with SSH agents, certificates, and ProxyJump for jumphosts.

    Example:
        >>> ssh = SSHClient(host="192.168.1.100", user="nvidia")
        >>> result = ssh.execute("hostname")
        >>> print(result.stdout)
    """

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        jumphost: str | None = None,
        timeout: int = 30,
        quiet: bool = True,
    ) -> None:
        """Initialize SSH client.

        Args:
            host: Target host to connect to
            user: Username for SSH connection
            port: SSH port (default: 22)
            jumphost: Optional jump host in format "host" or "host:port"
            timeout: Connection timeout in seconds
            quiet: Suppress non-fatal SSH diagnostics for routine commands
        """
        self.host = host
        self.user = user
        self.port = port
        self.jumphost = jumphost
        self.timeout = timeout
        self.quiet = quiet

    def _build_ssh_options(self, *, quiet: bool | None = None) -> list[str]:
        """Build common SSH options.

        Returns:
            List of SSH command-line options
        """
        effective_quiet = self.quiet if quiet is None else quiet
        opts = [
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={self.timeout}",
            "-o",
            "BatchMode=yes",  # Disable password prompts
        ]

        if effective_quiet:
            opts.insert(0, "-q")

        if self.port != 22:
            opts.extend(["-p", str(self.port)])

        if self.jumphost:
            opts.extend(["-J", self.jumphost])

        return opts

    def _build_target(self) -> str:
        """Build SSH target string.

        Returns:
            SSH target in format user@host
        """
        return f"{self.user}@{self.host}"

    def execute(
        self,
        command: str,
        stream: bool = False,
        env: dict[str, str] | None = None,
        quiet: bool | None = None,
    ) -> SSHResult:
        """Execute a command on the remote host.

        Args:
            command: Shell command to execute (can be a multi-line script)
            stream: If True, stream output to stdout/stderr in real-time
            env: Optional environment variables to pass to the remote command
            quiet: Override whether SSH suppresses non-fatal diagnostics

        Returns:
            SSHResult with execution details
        """
        cmd = ["ssh", "-T"] + self._build_ssh_options(quiet=quiet) + [self._build_target()]

        # Build the remote command with optional env vars
        remote_cmd = command
        if env:
            env_prefix = " ".join(f'{k}="{v}"' for k, v in env.items())
            remote_cmd = f"{env_prefix} {command}"

        # Use bash -l -s to run script from stdin (like heredoc)
        # This avoids command-line escaping issues with complex scripts
        # Note: -l (login shell) is needed to get PATH from profile (e.g., ~/.local/bin)
        cmd.append("bash -l -s")

        logger.debug(f"Executing SSH command: {' '.join(cmd)}")

        try:
            if stream:
                return self._execute_streaming(cmd, script=remote_cmd)
            else:
                return self._execute_captured(cmd, script=remote_cmd)
        except FileNotFoundError:
            logger.error("ssh command not found in PATH")
            return SSHResult(
                success=False,
                exit_code=-1,
                stderr="ssh command not found in PATH",
            )
        except Exception as e:
            logger.error(f"SSH execution failed: {e}")
            return SSHResult(
                success=False,
                exit_code=-1,
                stderr=str(e),
            )

    def _execute_captured(self, cmd: list[str], script: str) -> SSHResult:
        """Execute command and capture output.

        Args:
            cmd: Full SSH command to execute
            script: Script to pass via stdin

        Returns:
            SSHResult with captured stdout/stderr
        """
        result = subprocess.run(
            cmd,
            input=script,
            capture_output=True,
            text=True,
        )

        return SSHResult(
            success=result.returncode == 0,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _execute_streaming(
        self,
        cmd: list[str],
        script: str,
        stdout_stream: IO[str] | None = None,
        stderr_stream: IO[str] | None = None,
    ) -> SSHResult:
        """Execute command with streaming output.

        Streams stdout/stderr line-by-line in real-time using threads to read
        both streams concurrently, avoiding deadlocks. Output is both streamed
        to the provided streams and collected in the returned SSHResult.

        Args:
            cmd: Full SSH command to execute
            script: Script to pass via stdin
            stdout_stream: Stream to write stdout to (default: sys.stdout)
            stderr_stream: Stream to write stderr to (default: sys.stderr)

        Returns:
            SSHResult with collected stdout/stderr content
        """
        stdout_target = sys.stdout if stdout_stream is None else stdout_stream
        stderr_target = sys.stderr if stderr_stream is None else stderr_stream

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def read_stream(
            stream: IO[str] | None,
            target: IO[str],
            collected: list[str],
        ) -> None:
            """Read from stream line-by-line and write to target in real-time."""
            if stream is None:
                return
            try:
                for line in stream:
                    target.write(line)
                    target.flush()
                    collected.append(line)
            except Exception as e:
                logger.debug(f"Error reading stream: {e}")

        # Start threads to read stdout and stderr concurrently
        stdout_thread = threading.Thread(
            target=read_stream,
            args=(process.stdout, stdout_target, stdout_lines),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_stream,
            args=(process.stderr, stderr_target, stderr_lines),
            daemon=True,
        )

        stdout_thread.start()
        stderr_thread.start()

        # Write script to stdin and close it so remote process knows input is complete
        if process.stdin:
            try:
                process.stdin.write(script)
                process.stdin.flush()
            except BrokenPipeError:
                logger.debug("Stdin pipe broken (process may have exited early)")
            finally:
                process.stdin.close()

        # Wait for threads to finish reading all output
        stdout_thread.join()
        stderr_thread.join()

        # Wait for process to complete and get exit code
        process.wait()

        return SSHResult(
            success=process.returncode == 0,
            exit_code=process.returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )

    def check_command_exists(self, command: str) -> bool:
        """Check if a command exists on the remote host.

        Args:
            command: Command name to check

        Returns:
            True if command exists, False otherwise
        """
        result = self.execute(f"command -v {command} > /dev/null 2>&1")
        return result.success

    def ensure_directory(self, path: str) -> SSHResult:
        """Ensure a directory exists on the remote host.

        Args:
            path: Directory path to create

        Returns:
            SSHResult with execution details
        """
        return self.execute(f"mkdir -p {path}")

    def test_connection(self) -> SSHResult:
        """Test SSH connectivity to the remote host.

        Returns:
            SSHResult with connection test details
        """
        return self.execute("echo ok", quiet=False)

    def is_connection_error(self, result: SSHResult) -> bool:
        """Check if an SSHResult indicates a connection failure.

        SSH exit code 255 typically indicates connection errors (auth failure,
        network issues, jumphost problems, etc.) rather than remote command failures.

        Args:
            result: SSHResult to check

        Returns:
            True if this appears to be a connection error
        """
        return result.exit_code == 255

    def __repr__(self) -> str:
        """Return string representation."""
        jumphost_info = f" via {self.jumphost}" if self.jumphost else ""
        return f"SSHClient({self.user}@{self.host}:{self.port}{jumphost_info})"
