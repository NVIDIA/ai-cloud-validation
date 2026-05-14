#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Shared SSH utilities for stub scripts.

Provides wait_for_ssh() used by bare-metal and VM stubs that need to
poll for SSH readiness after instance state changes (start, reboot,
power-cycle, reinstall), and ssh_run() for one-shot commands.
"""

import subprocess
import sys
import time


def ssh_run(
    host: str,
    user: str,
    key_file: str,
    command: str,
    *,
    timeout: int = 30,
    connect_timeout: int = 10,
) -> tuple[int, str, str]:
    """Run a single command over SSH. Returns (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                f"ConnectTimeout={connect_timeout}",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-i",
                key_file,
                f"{user}@{host}",
                "--",
                command,
            ],
            capture_output=True,
            timeout=timeout,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        return 124, "", f"TimeoutExpired: {err}"
    except OSError as err:
        return 255, "", f"OSError: {err}"
    return proc.returncode, proc.stdout, proc.stderr


def wait_for_ssh(
    host: str,
    user: str,
    key_file: str,
    max_attempts: int = 60,
    interval: int = 15,
) -> bool:
    """Wait for SSH to become available on the host.

    Args:
        host: Public IP or hostname
        user: SSH username
        key_file: Path to SSH private key
        max_attempts: Maximum number of connection attempts
        interval: Seconds between attempts

    Returns:
        True if SSH is ready, False if timed out
    """
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "IdentityAgent=none",
                    "-i",
                    key_file,
                    f"{user}@{host}",
                    "exit 0",
                ],
                capture_output=True,
                timeout=15,
            )
            if result.returncode == 0:
                print(f"  SSH ready after attempt {attempt}", file=sys.stderr)
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass

        print(f"  Waiting for SSH... (attempt {attempt}/{max_attempts})", file=sys.stderr)
        time.sleep(interval)

    return False
