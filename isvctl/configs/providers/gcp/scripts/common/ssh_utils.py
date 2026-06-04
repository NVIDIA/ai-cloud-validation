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

"""SSH helpers for GCP VM stubs.

Defensive option set per the sister-stub consistency rule: every SSH
call site in the GCP target must
use the same flag set, including ``IdentitiesOnly=yes`` +
``PasswordAuthentication=no`` so operator environments with multiple
loaded agent keys don't exhaust ``MaxAuthTries`` before the ``-i`` key
gets tried.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time

# Canonical SSH options. Keep the set identical across every stub that
# runs SSH from the orchestrator host (sister-stub consistency rule).
# `IdentitiesOnly=yes` + `IdentityAgent=none` together ensure the explicit
# `-i <key_file>` argument is the ONLY credential SSH considers, even when
# the operator has an ssh-agent running with multiple identities. Without
# the agent disable, agent-offered keys are tried first and exhaust
# MaxAuthTries before the `-i` key is reached, producing spurious auth
# failures. Mirrors the AWS provider's ssh_utils canonical options.
_SSH_OPTS: tuple[str, ...] = (
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "IdentityAgent=none",
    "-o",
    "PasswordAuthentication=no",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
)


def _ssh_argv(host: str, user: str, key_file: str, remote_cmd: str) -> list[str]:
    """Build a canonical ``ssh`` argv: shared ``_SSH_OPTS`` + ``-i <key>`` + ``<user>@<host>`` + ``<remote_cmd>``.

    Used by ``_try_ssh`` / ``get_uptime_via_ssh`` / cloud-init waiters so
    every subprocess SSH call in this module uses identical option
    semantics (canonical-options consistency rule).
    """
    return ["ssh", *_SSH_OPTS, "-i", key_file, f"{user}@{host}", remote_cmd]


def _try_ssh(host: str, user: str, key_file: str, remote_cmd: str = "exit 0") -> bool:
    """Single SSH probe. Returns True on rc=0; False on rc!=0 / OSError / timeout."""
    try:
        result = subprocess.run(
            _ssh_argv(host, user, key_file, remote_cmd),
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def ssh_run(
    host: str,
    user: str,
    key_file: str,
    command: str,
    *,
    timeout: int = 30,
    connect_timeout: int = 10,
) -> tuple[int, str, str]:
    """Run a single command over SSH. Returns ``(exit_code, stdout, stderr)``.

    Sister to the AWS provider's ``common.ssh_utils.ssh_run`` — same return
    contract so guest probes can be reused verbatim. Errors map to fixed
    sentinel exit codes (124 for timeout, 255 for OSError) instead of
    raising, mirroring the AWS helper.
    """
    opts = list(_SSH_OPTS)
    # Override the canonical ConnectTimeout with the caller-supplied value.
    try:
        idx = opts.index("ConnectTimeout=5")
        opts[idx] = f"ConnectTimeout={connect_timeout}"
    except ValueError:
        opts.extend(["-o", f"ConnectTimeout={connect_timeout}"])
    try:
        proc = subprocess.run(
            ["ssh", *opts, "-i", key_file, f"{user}@{host}", "--", command],
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
    max_attempts: int = 30,
    interval: int = 10,
) -> bool:
    """Poll until SSH accepts a connection. Mirrors AWS provider's wait_for_ssh.

    Single-success gate — NOT sufficient as a post-lifecycle stability
    gate (see ``wait_for_ssh_stable``).
    """
    for attempt in range(1, max_attempts + 1):
        if _try_ssh(host, user, key_file):
            print(f"  SSH ready after attempt {attempt}", file=sys.stderr)
            return True
        print(f"  waiting for SSH... (attempt {attempt}/{max_attempts})", file=sys.stderr)
        time.sleep(interval)
    return False


def wait_for_ssh_drop(
    host: str,
    user: str,
    key_file: str,
    max_attempts: int = 18,
    interval: int = 5,
) -> bool:
    """Poll for SSH connection FAILURE.

    Used after ``instances.reset`` to confirm the pre-reboot sshd has
    dropped before the post-reboot stability gate. On async soft-reboot
    APIs, wait for SSH to DROP before waiting for it to stabilize.
    Default budget ~90s.
    """
    for attempt in range(1, max_attempts + 1):
        if not _try_ssh(host, user, key_file):
            print(f"  SSH dropped after attempt {attempt}", file=sys.stderr)
            return True
        print(f"  waiting for SSH to drop... (attempt {attempt}/{max_attempts})", file=sys.stderr)
        time.sleep(interval)
    return False


def wait_for_ssh_stable(
    host: str,
    user: str,
    key_file: str,
    consecutive: int = 3,
    interval: int = 10,
    max_attempts: int = 36,
) -> bool:
    """Block until SSH responds ``consecutive`` times in a row.

    Compute Engine acks lifecycle calls (start, reset) before the guest
    agent finishes; sshd may transiently restart and rewrite
    authorized_keys mid-replay. Downstream validators racing the replay
    flake without this gate.
    """
    if consecutive < 1:
        consecutive = 1

    streak = 0
    for attempt in range(1, max_attempts + 1):
        if _try_ssh(host, user, key_file):
            streak += 1
            print(f"  SSH probe {streak}/{consecutive} ok (attempt {attempt})", file=sys.stderr)
            if streak >= consecutive:
                return True
            time.sleep(interval)
            continue
        if streak > 0:
            print(f"  SSH probe {streak + 1} failed; resetting streak", file=sys.stderr)
        streak = 0
        time.sleep(interval)
    return False


def wait_for_cloud_init(
    host: str,
    user: str,
    key_file: str,
    timeout_seconds: int = 600,
    transport_backoff: int = 10,
) -> bool:
    """Run ``cloud-init status --wait`` over SSH.

    Wait-command exit codes: 0 and warnings both mean done. rc 0 (clean)
    AND rc 2 (recoverable warnings) BOTH count as terminal completion.
    Only rc 1 (fatal) is failure.

    The shell wrapper captures the upstream rc into the stdout marker
    ``CLOUDINIT_RC=N`` so we never consult ``r.returncode`` from the
    ``|| echo ...`` chain (which would always be 0 because the echo
    succeeded — shell-based wait helpers that capture non-zero exit
    codes via ``|| echo`` must parse the echoed RC before consulting
    ``r.returncode``).

    Distinguish transport-level failure (sshd refused / dropped
    connection — no ``CLOUDINIT_RC`` marker ever lands) from semantic
    failure (cloud-init returned rc=1). Transport failures retry within
    the deadline (with a short backoff between probes); semantic
    failures terminate immediately so the operator sees the cause.
    """
    remote_script = "sudo cloud-init status --wait && echo CLOUDINIT_RC=0 || echo CLOUDINIT_RC=$?"
    deadline = time.monotonic() + timeout_seconds
    last_transport_rc: int | None = None
    while True:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            print(
                f"  cloud-init wait deadline exhausted (last transport rc={last_transport_rc})",
                file=sys.stderr,
            )
            return False
        try:
            result = subprocess.run(
                ["ssh", *_SSH_OPTS, "-i", key_file, f"{user}@{host}", remote_script],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            print(f"  cloud-init wait timed out after {timeout_seconds}s", file=sys.stderr)
            return False
        except OSError as e:
            print(f"  cloud-init wait failed (OSError): {e}", file=sys.stderr)
            return False

        rc_marker: int | None = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("CLOUDINIT_RC="):
                try:
                    rc_marker = int(line.split("=", 1)[1])
                except ValueError:
                    rc_marker = None

        if rc_marker is None:
            # SSH never reached the echo — transport-level failure
            # (connect drop, auth refusal, command-not-found). Retry
            # within the deadline rather than terminate.
            last_transport_rc = result.returncode
            print(
                f"  cloud-init wait: no marker (ssh rc={result.returncode}); transport retry in {transport_backoff}s",
                file=sys.stderr,
            )
            time.sleep(transport_backoff)
            continue

        if rc_marker in (0, 2):
            print(f"  cloud-init wait completed (rc={rc_marker})", file=sys.stderr)
            return True
        # Semantic failure (rc=1 or other non-zero) — terminate; the
        # guest reached the echo, so it has a definite answer.
        print(f"  cloud-init wait reported fatal rc={rc_marker}", file=sys.stderr)
        return False


def get_uptime_via_ssh(host: str, user: str, key_file: str) -> float | None:
    """Return ``/proc/uptime`` seconds via SSH, or None on failure.

    Used for reboot affirmation (post-reset uptime should be substantially
    less than pre-reset uptime — see ``reboot_instance.py``).
    """
    try:
        result = subprocess.run(
            _ssh_argv(host, user, key_file, "cat /proc/uptime | cut -d' ' -f1"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


def quote(s: str) -> str:
    """Shell-quote a string for inline composition in SSH commands."""
    return shlex.quote(s)
