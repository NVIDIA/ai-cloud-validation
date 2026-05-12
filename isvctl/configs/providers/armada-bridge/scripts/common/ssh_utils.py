#!/usr/bin/env python3
"""SSH polling helpers for Armada Bridge provider scripts."""
from __future__ import annotations


def wait_for_ssh(
    host: str,
    key_file: str,
    username: str = "ubuntu",
    timeout: int = 300,
) -> None:
    """Poll SSH until host accepts a connection or timeout exceeded.

    Args:
        host: IP or hostname to poll.
        key_file: Path to SSH private key file.
        username: SSH username (default: ubuntu).
        timeout: Max seconds to wait.

    Bridge implementation note:
        Use paramiko (same as aws/common/ssh_utils.py). Poll with short
        sleep intervals. Raise TimeoutError if not reachable within timeout.
    """
    raise NotImplementedError(
        "wait_for_ssh() not yet implemented. "
        "Use paramiko to attempt SSH connection in a retry loop."
    )
