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

"""SSH connection and utility helpers.

Provides shared functions for SSH-based validations:
- SSH client creation via paramiko
- Remote command execution
- SSH configuration extraction from config/inventory
- CPU range parsing utilities

These validations are platform-agnostic and work on ANY host with SSH access:
AWS, GCP, Azure, bare metal, etc.

Requires paramiko: pip install paramiko
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import paramiko

log = logging.getLogger(__name__)


def get_ssh_client(
    host: str,
    user: str,
    key_path: str,
    timeout: int = 30,
) -> paramiko.SSHClient:
    """Create SSH client connection using paramiko.

    Args:
        host: Hostname or IP address to connect to
        user: SSH username
        key_path: Path to SSH private key file
        timeout: Connection timeout in seconds

    Returns:
        Connected paramiko SSHClient instance
    """
    import paramiko

    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh_client.connect(
        hostname=host,
        username=user,
        key_filename=key_path,
        timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return ssh_client


class LocalExecutor:
    """Session sentinel that runs commands on the local host.

    Used by host validations that already execute on the target machine
    (e.g. via ``isvctl deploy run``), so they run shell commands directly
    instead of opening a second SSH connection back into the host (which
    would otherwise need its own private key).

    It duck-types the subset of ``paramiko.SSHClient`` that host validations
    rely on: it is accepted by :func:`run_ssh_command` and supports ``close()``.
    """

    def close(self) -> None:
        """No-op for interface parity with ``paramiko.SSHClient``."""
        return None


def is_local_execution(config: dict[str, Any]) -> bool:
    """Whether to run commands locally (``local: true``) instead of over SSH.

    Use when the validation already runs on the target host.
    """
    return bool(config.get("local"))


def open_host_session(
    ssh_cfg: dict[str, Any],
    config: dict[str, Any],
    timeout: int = 30,
) -> paramiko.SSHClient | LocalExecutor:
    """Open a command session to the host under test.

    Returns a connected paramiko SSH client, or a :class:`LocalExecutor` when
    local execution is enabled (config ``local: true``). Both are accepted by
    :func:`run_ssh_command`.
    """
    if is_local_execution(config):
        return LocalExecutor()
    return get_ssh_client(
        ssh_cfg["ssh_host"],
        ssh_cfg["ssh_user"],
        ssh_cfg["ssh_key_path"],
        timeout=timeout,
    )


def _run_local_command(command: str, timeout: int) -> tuple[int, str, str]:
    """Run a shell command on the local host, mirroring run_ssh_command's contract.

    Raises:
        TimeoutError: If the command does not complete within timeout.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("timed out") from exc
    return proc.returncode, proc.stdout, proc.stderr


def run_ssh_command(
    ssh: paramiko.SSHClient | LocalExecutor,
    command: str,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run command via SSH (or locally) and return exit_code, stdout, stderr.

    When ``ssh`` is a :class:`LocalExecutor`, the command runs on the local
    host via a subprocess. Otherwise it runs over the paramiko SSH channel.

    Uses a threading event to enforce a wall-clock timeout, since
    paramiko's channel timeout only applies to socket operations and
    does not bound recv_exit_status(). Drains stdout/stderr before
    waiting for exit status to avoid deadlocks when output exceeds
    the channel window size.

    Args:
        ssh: Connected SSH client or LocalExecutor
        command: Command to execute
        timeout: Wall-clock timeout in seconds (default: 120)

    Returns:
        Tuple of (exit_code, stdout, stderr)

    Raises:
        TimeoutError: If the command does not complete within timeout
    """
    if isinstance(ssh, LocalExecutor):
        return _run_local_command(command, timeout)

    _, stdout, _stderr = ssh.exec_command(command)
    channel = stdout.channel

    stdout_data: list[bytes] = []
    stderr_data: list[bytes] = []

    def _drain() -> None:
        while not channel.exit_status_ready():
            if channel.recv_ready():
                stdout_data.append(channel.recv(65536))
            elif channel.recv_stderr_ready():
                stderr_data.append(channel.recv_stderr(65536))
            else:
                time.sleep(0.1)
        while channel.recv_ready():
            stdout_data.append(channel.recv(65536))
        while channel.recv_stderr_ready():
            stderr_data.append(channel.recv_stderr(65536))

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        channel.close()
        raise TimeoutError("timed out")

    exit_code = channel.recv_exit_status()
    return (
        exit_code,
        b"".join(stdout_data).decode(),
        b"".join(stderr_data).decode(),
    )


def get_ssh_config(config: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    """Extract SSH configuration from config and inventory.

    Supports multiple sources:
    - Direct config values (host, key_file, user)
    - Step output references (step_output.public_ip, etc.)
    - Inventory structures (ssh.*, vm.*)

    Args:
        config: Test configuration dictionary
        inventory: Inventory data dictionary

    Returns:
        Dictionary with ssh_host, ssh_user, ssh_key_path, and optional metadata
    """
    # Check step_output first (from Jinja2 references)
    step_output = config.get("step_output", {})

    # Try different inventory structures
    ssh_inv = inventory.get("ssh", {})
    vmaas_inv = inventory.get("vmaas", {})

    # Determine host (check multiple sources)
    host = (
        config.get("host")
        or config.get("ssh_host")
        or step_output.get("public_ip")
        or step_output.get("private_ip")
        or step_output.get("host")
        or ssh_inv.get("host")
        or ssh_inv.get("public_ip")
        or vmaas_inv.get("public_ip")
        or vmaas_inv.get("private_ip")
    )

    # Determine user
    user = (
        config.get("user")
        or config.get("ssh_user")
        or step_output.get("ssh_user")
        or ssh_inv.get("user")
        or vmaas_inv.get("ssh_user")
        or "ubuntu"
    )

    # Determine key path
    key_path = (
        config.get("key_file")
        or config.get("key_path")
        or config.get("ssh_key_path")
        or step_output.get("key_file")
        or step_output.get("key_path")
        or step_output.get("ssh_key_path")
        or ssh_inv.get("key_path")
        or vmaas_inv.get("ssh_key_path")
    )

    log.debug(
        "SSH config resolved: host=%s, user=%s, key=%s (sources: step_output.public_ip=%s, config.host=%s)",
        host,
        user,
        key_path,
        step_output.get("public_ip"),
        config.get("host"),
    )

    return {
        "ssh_host": host,
        "ssh_user": user,
        "ssh_key_path": key_path,
        # Optional metadata
        "gpu_count": config.get("expected_gpus") or vmaas_inv.get("gpu_count") or ssh_inv.get("gpu_count") or 0,
        "gpu_name": vmaas_inv.get("gpu_name") or ssh_inv.get("gpu_name"),
        "instance_type": vmaas_inv.get("instance_type") or ssh_inv.get("instance_type"),
        "ami_id": vmaas_inv.get("ami_id") or ssh_inv.get("ami_id"),
    }


def get_failed_subtests(results: list[dict[str, Any]]) -> list[str]:
    """Return names of failed (non-skipped) subtests.

    Args:
        results: List of subtest result dicts from BaseValidation._subtest_results

    Returns:
        List of failed subtest names
    """
    return [r["name"] for r in results if not r["passed"] and not r.get("skipped", False)]


def parse_cpu_range_count(cpu_range: str) -> int:
    """Parse a CPU range string like '0-3,5,7-9' and return total CPU count.

    Args:
        cpu_range: Comma-separated ranges (e.g., "0-3", "0-3,5,7-9")

    Returns:
        Total number of CPUs in the range
    """
    total = 0
    for part in cpu_range.split(","):
        part = part.strip()
        if "-" in part:
            bounds = part.split("-")
            try:
                total += int(bounds[1]) - int(bounds[0]) + 1
            except (ValueError, IndexError):
                pass
        elif part.isdigit():
            total += 1
    return total
