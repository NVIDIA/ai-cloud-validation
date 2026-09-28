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

"""Shared SSH utilities for Firebird bare-metal scripts.

Firebird BMs have no SSH-key API: the public key is injected through cloud-init
user-data at provision time (``generate_key_pair`` or ``public_key``, then
``user_data_b64``). The BM's ``ipAddress`` sits on the tenant subnet, so these
helpers must run from a host that can route to that subnet.
"""

import base64
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SSH_OPTIONS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "BatchMode=yes",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "IdentityAgent=none",
    "-o",
    "LogLevel=ERROR",
]


# Worst-case cost of one SSH readiness attempt: ``ssh_run`` kills ssh after this many seconds.
SSH_ATTEMPT_TIMEOUT = 20


def _ssh_keygen(path: Path) -> None:
    """Write a new passphrase-less ed25519 key pair to ``path`` and ``path``.pub."""
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", path.stem, "-f", str(path)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def generate_key_pair(name: str) -> str:
    """Generate a key pair for this run in a new private directory; return the private key path.

    The directory is a fresh ``tempfile.mkdtemp`` (mode 0700, unique per call), so
    no other local user can plant or read the key, and a run never reuses or later
    deletes the key of another run (for example a BM kept with ``BM_SKIP_TEARDOWN``).
    """
    path = Path(tempfile.mkdtemp(prefix=f"{name}-")) / f"{name}-key"
    _ssh_keygen(path)
    return str(path)


def public_key(key_file: str) -> str:
    """Return the public key of an existing, explicitly supplied private key."""
    path = Path(key_file)
    if not path.is_file():
        raise RuntimeError(f"SSH key file {key_file} does not exist")
    public_file = Path(f"{key_file}.pub")
    if public_file.exists():
        return public_file.read_text().strip()
    derived = subprocess.run(["ssh-keygen", "-y", "-f", str(path)], check=True, capture_output=True, text=True)
    return derived.stdout.strip()


def user_data_b64(public_key: str) -> str:
    """Return base64 cloud-config that authorizes ``public_key`` for the image's default user."""
    cloud_config = f"#cloud-config\nssh_authorized_keys:\n  - {public_key}\n"
    return base64.b64encode(cloud_config.encode()).decode()


def ssh_run(
    host: str,
    user: str,
    key_file: str,
    command: str,
    *,
    timeout: int = 30,
    connect_timeout: int = 10,
    input_text: str | None = None,
) -> tuple[int, str, str]:
    """Run a single command over SSH. Returns (exit_code, stdout, stderr).

    ``input_text`` is written to the remote command's stdin. It is how a secret
    reaches the host: it never appears in the ssh command line (``ps``), and the
    remote side reads it from stdin rather than from its own argv.
    """
    try:
        proc = subprocess.run(
            [
                "ssh",
                *SSH_OPTIONS,
                "-o",
                f"ConnectTimeout={connect_timeout}",
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
            input=input_text,
            # Without input, give ssh an empty stdin rather than the step's own.
            stdin=None if input_text is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as err:
        return 124, "", f"TimeoutExpired: {err}"
    except OSError as err:
        return 255, "", f"OSError: {err}"
    return proc.returncode, proc.stdout, proc.stderr


def wait_for_ssh(host: str, user: str, key_file: str, deadline: float, interval: int = 15) -> bool:
    """Wait for SSH to become available on the host, finishing before ``deadline``.

    An attempt is started only when it (``SSH_ATTEMPT_TIMEOUT``), plus the sleep
    before it, still ends by ``deadline``, so the caller always gets control back
    in time to print its result before the step timeout.

    Args:
        host: BM IP address
        user: SSH username
        key_file: Path to SSH private key
        deadline: ``time.monotonic()`` value the wait must end by
        interval: Seconds between attempts

    Returns:
        True if SSH is ready, False if the deadline leaves no room for another attempt
    """
    attempt = 0
    while time.monotonic() + SSH_ATTEMPT_TIMEOUT <= deadline:
        attempt += 1
        if ssh_run(host, user, key_file, "exit 0", timeout=SSH_ATTEMPT_TIMEOUT, connect_timeout=5)[0] == 0:
            print(f"  SSH ready after attempt {attempt}", file=sys.stderr)
            return True
        if time.monotonic() + interval + SSH_ATTEMPT_TIMEOUT > deadline:
            break
        print(f"  Waiting for SSH... (attempt {attempt})", file=sys.stderr)
        time.sleep(interval)
    print(f"  SSH not ready after {attempt} attempt(s); stopping before the deadline", file=sys.stderr)
    return False


CLOUD_INIT_PENDING = ("running", "not started")


def wait_for_cloud_init(host: str, user: str, key_file: str, deadline: float, interval: int = 10) -> str:
    """Wait until cloud-init on the host leaves its running state, finishing before ``deadline``.

    SSH answers as soon as sshd starts, which is before cloud-init's final
    modules finish, so a host that answers SSH may still be configuring itself.
    Polls ``cloud-init status`` and returns its status (``done``, ``error``,
    ``degraded done``, ``disabled``, ...), ``not_found`` when cloud-init is not
    installed, ``unknown`` for output it cannot read, or ``timeout`` when the
    deadline leaves no room for another attempt. SSH failures are retried.
    """
    attempt = 0
    while time.monotonic() + SSH_ATTEMPT_TIMEOUT <= deadline:
        attempt += 1
        code, stdout, _ = ssh_run(
            host,
            user,
            key_file,
            "cloud-init status 2>/dev/null || echo not_found",
            timeout=SSH_ATTEMPT_TIMEOUT,
            connect_timeout=5,
        )
        if code == 0:
            text = stdout.strip()
            if "not_found" in text:
                return "not_found"
            status = next(
                (line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("status:")), ""
            )
            if not status:
                return "unknown"
            if status not in CLOUD_INIT_PENDING:
                print(f"  cloud-init {status} after attempt {attempt}", file=sys.stderr)
                return status
        if time.monotonic() + interval + SSH_ATTEMPT_TIMEOUT > deadline:
            break
        print(f"  Waiting for cloud-init... (attempt {attempt})", file=sys.stderr)
        time.sleep(interval)
    print(f"  cloud-init still running after {attempt} attempt(s); stopping before the deadline", file=sys.stderr)
    return "timeout"


def get_uptime(host: str, user: str, key_file: str) -> float | None:
    """Return host uptime in seconds from /proc/uptime, or None if unreadable."""
    exit_code, stdout, _ = ssh_run(host, user, key_file, "cat /proc/uptime")
    if exit_code != 0 or not stdout.strip():
        return None
    try:
        return float(stdout.split()[0])
    except ValueError:
        return None
