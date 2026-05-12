#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Per-host status log sampler for AWS bare-metal.

SSHes to the BM host and samples journalctl + dmesg to confirm at least
one Linux status log is producing fresh entries within the configured
recency window. Output is consumed by isvtest.validations.bm_host_status.BmHostStatusLog.

Usage:
    python host_status_log.py --instance-id i-xxx --region us-west-2 \\
        --key-file /tmp/key.pem --public-ip 54.x.x.x [--max-age-minutes 5]
"""

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.errors import handle_aws_errors
from common.ssh_utils import ssh_run, wait_for_ssh

JOURNALCTL_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{4})")
DMESG_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:,\d+)?(?:[+\-]\d{2}:?\d{2}|Z)?)")


def _positive_int(value: str) -> int:
    """Parse a strictly positive integer argument."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _build_result(entry_count: int, latest_ts: str, max_age_minutes: int) -> dict[str, Any]:
    """Build a per-source status log result.

    Args:
        entry_count: Number of recent log entries found.
        latest_ts: Timestamp string from the most recent matching entry.
        max_age_minutes: Recency window used for the sample.

    Returns:
        A result payload with pass/fail state, message, entry count, and
        latest timestamp.
    """
    passed = entry_count >= 1
    msg = (
        f"{entry_count} entries in last {max_age_minutes}min, latest {latest_ts}"
        if passed
        else f"no entries in last {max_age_minutes}min"
    )
    return {
        "passed": passed,
        "message": msg,
        "entry_count": entry_count,
        "latest_timestamp": latest_ts,
    }


def _ssh_error_result(source: str, exit_code: int, stderr: str) -> dict[str, Any]:
    """Build a failed per-source result for an SSH command error.

    Args:
        source: Log source name, such as ``journalctl`` or ``dmesg``.
        exit_code: SSH command exit code.
        stderr: SSH command standard error text.

    Returns:
        A failed result payload matching the successful result shape.
    """
    return {
        "passed": False,
        "message": f"{source} exited {exit_code}: {stderr.strip()[:200] or 'no stderr'}",
        "entry_count": 0,
        "latest_timestamp": "",
    }


def _sample_journalctl(host: str, user: str, key_file: str, max_age_minutes: int) -> dict[str, Any]:
    """Sample journalctl for entries within the last `max_age_minutes`."""
    cmd = f"journalctl --since '{max_age_minutes} minutes ago' --no-pager -o short-iso 2>/dev/null | tail -n 500"
    exit_code, stdout, stderr = ssh_run(host, user, key_file, cmd)
    if exit_code != 0:
        return _ssh_error_result("journalctl", exit_code, stderr)

    last_match = None
    entry_count = 0
    for line in stdout.splitlines():
        if m := JOURNALCTL_ISO_TS.match(line):
            entry_count += 1
            last_match = m
    latest_ts = last_match.group(1) if last_match else ""
    return _build_result(entry_count, latest_ts, max_age_minutes)


def _parse_dmesg_timestamp(line: str) -> tuple[datetime, str] | None:
    """Parse the dmesg ISO timestamp prefix; returns (datetime, raw_string).

    util-linux's `dmesg --time-format=iso` emits e.g. `2024-01-15T10:30:45,123456+0000`.
    Python 3.12+ `fromisoformat` accepts that shape (comma-fractional, `Z`, and
    `+HHMM` offsets) natively, so no normalization is needed.
    """
    match = DMESG_ISO_TS.match(line)
    if not match:
        return None
    raw = match.group(1)
    try:
        return datetime.fromisoformat(raw), raw
    except ValueError:
        return None


def _sample_dmesg(host: str, user: str, key_file: str, max_age_minutes: int) -> dict[str, Any]:
    """Sample dmesg for entries within the last `max_age_minutes`.

    Filters in-process so we don't depend on dmesg --since (stricter timestamp
    formats). Falls back to sudo when unprivileged dmesg is restricted.
    """
    cmd = "dmesg --time-format=iso 2>/dev/null | tail -n 1000"
    exit_code, stdout, stderr = ssh_run(host, user, key_file, cmd)
    if exit_code != 0 or not stdout.strip():
        cmd_fallback = "sudo -n dmesg --time-format=iso 2>/dev/null | tail -n 1000"
        exit_code, stdout, stderr = ssh_run(host, user, key_file, cmd_fallback)
    if exit_code != 0:
        return _ssh_error_result("dmesg", exit_code, stderr)

    cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
    entry_count = 0
    latest_ts = ""
    for line in stdout.splitlines():
        parsed = _parse_dmesg_timestamp(line)
        if parsed is None:
            continue
        ts, raw = parsed
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            entry_count += 1
            latest_ts = raw

    return _build_result(entry_count, latest_ts, max_age_minutes)


@handle_aws_errors
def main() -> int:
    """Sample host status logs and emit the validation JSON payload.

    Returns:
        Process exit code, where 0 means at least one status log source has
        fresh entries and 1 means the check failed.
    """
    parser = argparse.ArgumentParser(description="Sample per-host status log on AWS bare metal")
    parser.add_argument("--instance-id", required=True, help="EC2 instance ID")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--public-ip", required=True, help="Public IP of the host")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument(
        "--max-age-minutes",
        type=_positive_int,
        default=5,
        help="Maximum age of the most recent log entry, in minutes",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "test_name": "host_status_log",
        "tests": {},
    }

    if not wait_for_ssh(args.public_ip, args.ssh_user, args.key_file, max_attempts=20, interval=10):
        result["error"] = f"SSH did not become ready on {args.public_ip}"
        print(json.dumps(result, indent=2))
        return 1

    journalctl_result = _sample_journalctl(args.public_ip, args.ssh_user, args.key_file, args.max_age_minutes)
    dmesg_result = _sample_dmesg(args.public_ip, args.ssh_user, args.key_file, args.max_age_minutes)

    result["tests"] = {
        "journalctl_recent": journalctl_result,
        "dmesg_recent": dmesg_result,
    }
    result["success"] = journalctl_result["passed"] or dmesg_result["passed"]

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
