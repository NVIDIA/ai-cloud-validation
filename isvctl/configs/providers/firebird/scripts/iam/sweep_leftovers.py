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

"""Delete service accounts and projects left behind by earlier, killed runs.

A step killed mid-way (orchestrator timeout, Ctrl-C) never prints the IDs it
created, so teardown cannot delete them. This sweep, the first setup step of
the control-plane, iam, and security configs, finds them by name instead:

  service accounts  ``isv-cp-``, ``isv-iam-``, ``isv-sec-sa-``, ``isv-sec-lp-``,
                    ``isv-sec-audit-`` followed by the 6-hex suffix the scripts add
  projects          ``isv-lp-`` + 6 hex (the least-privilege test's project B)

Only exact matches older than ``--min-age-hours`` (``FIREBIRD_SWEEP_MIN_AGE_HOURS``,
default 6) are deleted, so a concurrent run's resources are never touched; a
resource without a creation time is left alone, as is the run's own project.
Anything else is never deleted. When a list call is refused the sweep skips that
kind, and a failed delete is reported without failing setup.

Usage:
    python sweep_leftovers.py [--min-age-hours 6]

Output JSON:
{
    "success": true,
    "platform": "iam",
    "min_age_hours": 6.0,
    "resources_deleted": ["service_account:service-account.x (isv-cp-a1b2c3)"],
    "resources_failed": [],
    "message": "Deleted 1 leftover resource(s)"
}
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

from common import service_accounts
from common.firebird_client import FirebirdClient, log, parse_timestamp

SA_NAME = service_accounts.SA_NAME
PROJECT_NAME = service_accounts.PROJECT_NAME
DEFAULT_MIN_AGE_HOURS = 6.0


def stale(
    items: list[dict[str, Any]], name_key: str, pattern: re.Pattern[str], cutoff: datetime
) -> list[dict[str, Any]]:
    """Return the items whose name matches ``pattern`` and that were created before ``cutoff``."""
    matches = []
    for item in items:
        created = parse_timestamp(item.get("createdAt"))
        if pattern.match(str(item.get(name_key, ""))) and created is not None and created < cutoff:
            matches.append(item)
    return matches


def main() -> int:
    """List, filter, and delete leftovers; always succeed so setup continues.

    Returns:
        0
    """
    parser = argparse.ArgumentParser(description="Delete Firebird isv-* leftovers from killed runs")
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=float(os.environ.get("FIREBIRD_SWEEP_MIN_AGE_HOURS", "").strip() or DEFAULT_MIN_AGE_HOURS),
        help="Only delete resources older than this (default 6, FIREBIRD_SWEEP_MIN_AGE_HOURS)",
    )
    args = parser.parse_args()

    deleted: list[str] = []
    failed: list[str] = []
    notes: list[str] = []
    result: dict[str, Any] = {
        "success": True,
        "platform": "iam",
        "min_age_hours": args.min_age_hours,
        "resources_deleted": deleted,
        "resources_failed": failed,
    }
    listed = 0
    try:
        client = FirebirdClient()
        cutoff = datetime.now(UTC) - timedelta(hours=args.min_age_hours)
        targets: list[tuple[str, str, str]] = []  # (kind, id, name); accounts before projects
        try:
            accounts = stale(
                client.paginate(service_accounts.SA_PATH, "serviceAccounts"), "displayName", SA_NAME, cutoff
            )
            targets += [("service_account", a["id"], a["displayName"]) for a in accounts]
            listed += 1
        except Exception as e:
            notes.append(f"service accounts not listed: {e}")
        try:
            projects = stale(client.paginate("/projects", "projects"), "name", PROJECT_NAME, cutoff)
            targets += [("project", p["id"], p["name"]) for p in projects if p.get("id") != client.project_id]
            listed += 1
        except Exception as e:
            notes.append(f"projects not listed: {e}")

        for kind, resource_id, name in targets:
            label = f"{kind}:{resource_id} ({name})"
            try:
                if kind == "service_account":
                    service_accounts.delete(client, resource_id)
                else:
                    service_accounts.delete_project(client, resource_id)
                deleted.append(label)
                log(f"  swept {label}")
            except Exception as e:
                failed.append(f"{label}: {e}")
                log(f"  could not sweep {label}: {e}")
    except Exception as e:
        notes.append(str(e))

    if not listed:
        result.update(skipped=True, skip_reason="Leftover sweep skipped: " + "; ".join(notes))
    elif notes:
        result["notes"] = notes
    result["message"] = f"Deleted {len(deleted)} leftover resource(s)" + (f", {len(failed)} failed" if failed else "")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
