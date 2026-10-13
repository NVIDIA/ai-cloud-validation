#!/usr/bin/env python3
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

"""Shared paramiko SSH helpers for the cross-provider NIM scripts.

Both ``deploy_nim.py`` and ``teardown_nim.py`` connect to a remote host and run
docker commands over SSH; this module hosts the single timeout-aware
``ssh_connect``/``run_cmd`` implementation so the two scripts cannot drift.
"""

import paramiko

SSH_CONNECT_TIMEOUT = 30


def ssh_connect(host: str, user: str, key_file: str) -> paramiko.SSHClient:
    """Create and return an SSH client connected to a remote host.

    Args:
        host: Remote host IP address or hostname.
        user: SSH username.
        key_file: Path to the SSH private key file.

    Returns:
        Connected paramiko.SSHClient instance.

    Raises:
        TimeoutError: If the connection attempt exceeds SSH_CONNECT_TIMEOUT seconds.
        paramiko.SSHException: If Paramiko fails to establish the SSH connection.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            key_filename=key_file,
            timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
    except TimeoutError as e:
        raise TimeoutError(f"SSH connection timed out after {SSH_CONNECT_TIMEOUT}s") from e
    return client


def run_cmd(
    ssh: paramiko.SSHClient,
    command: str,
    timeout: int = 120,
    operation: str | None = None,
) -> tuple[int, str, str]:
    """Execute a command over SSH and return its exit status and output.

    Args:
        ssh: Active Paramiko SSH client.
        command: Shell command to execute on the remote host.
        timeout: Command execution timeout in seconds.
        operation: Optional operation label for timeout diagnostics.

    Returns:
        Tuple of exit_code, stdout, and stderr, with output decoded as strings.

    Raises:
        TimeoutError: If command execution exceeds the configured timeout.
        paramiko.SSHException: If Paramiko fails while executing the command.
    """
    try:
        _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()
    except TimeoutError as e:
        label = operation or command
        raise TimeoutError(f"{label} timed out after {timeout}s") from e
