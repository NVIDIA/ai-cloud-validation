#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Per-host status log sampler for bare-metal - TEMPLATE.

Samples journalctl and dmesg on the BM host and emits per-source status
so the BmHostStatusLog validation can assert that at least one status
log is producing fresh entries within the configured recency window.

Required JSON output fields:
  {
    "success": true,
    "platform": "bm",
    "test_name": "host_status_log",
    "tests": {
      "journalctl_recent": {
        "passed": true,
        "message": "<count> entries in last <N>min, latest <timestamp>",
        "entry_count": <int>,
        "latest_timestamp": "<iso8601>"
      },
      "dmesg_recent": {
        "passed": true,
        "message": "...",
        "entry_count": <int>,
        "latest_timestamp": "<iso8601>"
      }
    }
  }

Usage:
    python host_status_log.py --instance-id <id> --region <region> \\
        --key-file <path> --public-ip <ip> [--max-age-minutes 5]

Reference implementation: ../../aws/bare_metal/host_status_log.py
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _positive_int(value: str) -> int:
    """Parse a strictly positive integer argument."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> int:
    """Emit template per-host status log results.

    Returns:
        Process exit code, where 0 means the demo or implementation reported
        fresh status log entries and 1 means it did not.
    """
    parser = argparse.ArgumentParser(description="Per-host status log sampler (template)")
    parser.add_argument("--instance-id", required=True, help="Instance ID")
    parser.add_argument("--region", required=True, help="Cloud region")
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

    # TODO: Replace with your platform's per-host status log sampling.
    # Real implementations should SSH to the host, run journalctl and dmesg
    # filtered by --max-age-minutes, and populate the `tests` block.
    if DEMO_MODE:
        result["tests"] = {
            "journalctl_recent": {
                "passed": True,
                "message": f"42 entries in last {args.max_age_minutes}min, latest 2026-05-12T09:14:03",
                "entry_count": 42,
                "latest_timestamp": "2026-05-12T09:14:03",
            },
            "dmesg_recent": {
                "passed": True,
                "message": f"7 entries in last {args.max_age_minutes}min, latest 2026-05-12T09:13:58",
                "entry_count": 7,
                "latest_timestamp": "2026-05-12T09:13:58",
            },
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's per-host log fetch logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
