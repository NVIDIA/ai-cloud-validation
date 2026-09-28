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

"""Per-host status log sampler for Firebird bare metal.

Samples journalctl and dmesg on the BM over SSH and reports, per source,
whether fresh entries were written within --max-age-minutes.

Usage:
    python host_status_log.py --key-file /tmp/key --public-ip 10.x.x.x [--max-age-minutes 5]

Output JSON:
{
    "success": true,
    "platform": "bm",
    "test_name": "host_status_log",
    "tests": {
        "journalctl_recent": {"passed": true, "message": "...", "entry_count": 12, "latest_timestamp": "..."},
        "dmesg_recent": {"passed": false, "message": "...", "entry_count": 0, "latest_timestamp": ""}
    }
}
"""

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.ssh_utils import ssh_run, wait_for_ssh

JOURNALCTL_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{4})")
DMESG_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:,\d+)?(?:[+\-]\d{2}:?\d{2}|Z)?)")
# Worst case of the sampling after SSH is up: journalctl, dmesg, and dmesg's sudo retry (30 s each).
SAMPLING_SECONDS = 3 * 30


def _source_result(entry_count: int, latest_ts: str, max_age_minutes: int) -> dict[str, Any]:
    """Build a per-source result from the number of recent entries found."""
    passed = entry_count >= 1
    return {
        "passed": passed,
        "message": (
            f"{entry_count} entries in last {max_age_minutes}min, latest {latest_ts}"
            if passed
            else f"no entries in last {max_age_minutes}min"
        ),
        "entry_count": entry_count,
        "latest_timestamp": latest_ts,
    }


def _ssh_error(source: str, exit_code: int, stderr: str) -> dict[str, Any]:
    """Build a failed per-source result for an SSH command error."""
    return {
        "passed": False,
        "message": f"{source} exited {exit_code}: {stderr.strip()[:200] or 'no stderr'}",
        "entry_count": 0,
        "latest_timestamp": "",
    }


def _sample_journalctl(host: str, user: str, key_file: str, max_age_minutes: int) -> dict[str, Any]:
    """Count journalctl entries written within the last ``max_age_minutes``."""
    cmd = f"journalctl --since '{max_age_minutes} minutes ago' --no-pager -o short-iso 2>/dev/null | tail -n 500"
    exit_code, stdout, stderr = ssh_run(host, user, key_file, cmd)
    if exit_code != 0:
        return _ssh_error("journalctl", exit_code, stderr)
    stamps = [m.group(1) for line in stdout.splitlines() if (m := JOURNALCTL_ISO_TS.match(line))]
    return _source_result(len(stamps), stamps[-1] if stamps else "", max_age_minutes)


def _sample_dmesg(host: str, user: str, key_file: str, max_age_minutes: int) -> dict[str, Any]:
    """Count dmesg entries within the last ``max_age_minutes`` (falls back to sudo)."""
    cmd = "dmesg --time-format=iso 2>/dev/null | tail -n 1000"
    exit_code, stdout, stderr = ssh_run(host, user, key_file, cmd)
    if exit_code != 0 or not stdout.strip():
        exit_code, stdout, stderr = ssh_run(host, user, key_file, f"sudo -n {cmd}")
    if exit_code != 0:
        return _ssh_error("dmesg", exit_code, stderr)

    cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
    entry_count = 0
    latest_ts = ""
    for line in stdout.splitlines():
        match = DMESG_ISO_TS.match(line)
        if not match:
            continue
        try:
            ts = datetime.fromisoformat(match.group(1))
        except ValueError:
            continue
        if (ts if ts.tzinfo else ts.replace(tzinfo=UTC)) >= cutoff:
            entry_count += 1
            latest_ts = match.group(1)
    return _source_result(entry_count, latest_ts, max_age_minutes)


def main() -> int:
    """Sample host status logs and emit the validation JSON payload.

    Returns:
        0 when at least one source has fresh entries, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Sample per-host status log on Firebird bare metal")
    parser.add_argument("--instance-id", help="BM ID (informational)")
    parser.add_argument("--key-file", required=True, help="Path to SSH private key")
    parser.add_argument("--public-ip", required=True, help="BM IP address")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--max-age-minutes", type=int, default=5, help="Recency window in minutes")
    parser.add_argument(
        "--timeout", type=int, default=270, help="Overall timeout in seconds (keep below the step timeout)"
    )
    args = parser.parse_args()
    # The SSH wait leaves room for the sampling, so the JSON is printed before --timeout.
    ssh_deadline = time.monotonic() + args.timeout - SAMPLING_SECONDS

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "test_name": "host_status_log",
        "tests": {},
    }

    if not wait_for_ssh(args.public_ip, args.ssh_user, args.key_file, ssh_deadline, interval=10):
        result["error"] = f"SSH did not become ready on {args.public_ip}"
        print(json.dumps(result, indent=2))
        return 1

    journalctl = _sample_journalctl(args.public_ip, args.ssh_user, args.key_file, args.max_age_minutes)
    dmesg = _sample_dmesg(args.public_ip, args.ssh_user, args.key_file, args.max_age_minutes)
    result["tests"] = {"journalctl_recent": journalctl, "dmesg_recent": dmesg}
    result["success"] = journalctl["passed"] or dmesg["passed"]

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
