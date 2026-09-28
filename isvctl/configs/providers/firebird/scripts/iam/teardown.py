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

"""Delete the service accounts and projects a run created.

Serves control-plane ``delete_access_key``, iam ``teardown``, and security
``teardown``. Service accounts go first (``DELETE /service-accounts/{id}``,
which also drops their role bindings), then projects (``DELETE /projects/{id}``).
Each resource is read first and deleted only if it carries a name this
provider creates (``isv-cp-``, ``isv-iam-``, ``isv-sec-*-`` accounts and
``isv-lp-`` projects, each with the 6-hex run suffix); the run's own project is
never deleted. A refused ID is reported as failed and nothing is deleted for it.
Idempotent: a resource that is already gone (404) counts as deleted. Every ID is
attempted even when an earlier delete fails.

Usage:
    python teardown.py --service-account-ids service-account.a,service-account.b [--project-ids project.x]
    python teardown.py --service-account-ids service-account.a --skip-destroy

Output JSON:
{
    "success": true,
    "platform": "iam",
    "resources_deleted": ["service_account:service-account.a", "project:project.x"],
    "resources_failed": [],
    "message": "Deleted 2 resource(s)"
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import service_accounts
from common.firebird_client import FirebirdClient, log


def _ids(value: str) -> list[str]:
    """Split a comma-separated ID list, dropping blanks and duplicates (order kept)."""
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def main() -> int:
    """Delete the listed service accounts, then the listed projects.

    Returns:
        0 when everything is gone, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Delete Firebird service accounts and projects")
    parser.add_argument("--service-account-ids", default="", help="Comma-separated service-account IDs")
    parser.add_argument("--project-ids", default="", help="Comma-separated project IDs")
    parser.add_argument("--skip-destroy", action="store_true", help="Keep the resources")
    args = parser.parse_args()

    sa_ids, project_ids = _ids(args.service_account_ids), _ids(args.project_ids)
    result: dict[str, Any] = {"success": False, "platform": "iam", "resources_deleted": [], "resources_failed": []}

    if args.skip_destroy:
        kept = ", ".join(sa_ids + project_ids) or "nothing"
        result.update(success=True, skipped=True, skip_reason=f"Teardown skipped; kept {kept}")
        print(json.dumps(result, indent=2))
        return 0

    targets = [("service_account", sa_id) for sa_id in sa_ids] + [("project", pid) for pid in project_ids]
    try:
        client = FirebirdClient() if targets else None
        for kind, resource_id in targets:
            try:
                if kind == "service_account":
                    service_accounts.delete(client, resource_id)
                else:
                    service_accounts.delete_project(client, resource_id)
                result["resources_deleted"].append(f"{kind}:{resource_id}")
            except Exception as e:
                result["resources_failed"].append(f"{kind}:{resource_id}: {e}")
                log(f"ERROR deleting {kind} {resource_id}: {e}")
        result["success"] = not result["resources_failed"]
        result["message"] = f"Deleted {len(result['resources_deleted'])} resource(s)"
        if result["resources_failed"]:
            result["error"] = "; ".join(result["resources_failed"])
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
