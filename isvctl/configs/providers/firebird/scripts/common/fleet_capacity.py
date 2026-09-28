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

"""Read the tenant's NCP fleet-capacity report (``GET /reports/ncp/fleet-capacity``).

The report is optional. When the route answers 404 or 501 the checks emit a
structured skip. Rows are the calling tenant's own nodes (every row's
``cspAccount`` is the caller's tenant), so every report built on it is
tenant-scoped, not site-wide.
"""

from typing import Any

from common.firebird_client import FirebirdApiError, FirebirdClient, is_not_registered

FLEET_CAPACITY_PATH = "/reports/ncp/fleet-capacity"
NOT_ENABLED_REASON = "The NCP fleet-capacity report is not enabled on this Firebird API"


def fleet_capacity_rows(client: FirebirdClient) -> list[dict[str, Any]] | None:
    """Return the report rows, or None when the report is not served here."""
    try:
        report = client.request("GET", FLEET_CAPACITY_PATH)
    except FirebirdApiError as e:
        if is_not_registered(e):
            return None
        raise
    return [row for row in report.get("rows") or [] if isinstance(row, dict)]


def mark_skipped(result: dict[str, Any]) -> dict[str, Any]:
    """Turn ``result`` into a structured skip for a deployment without the report."""
    result.update({"success": True, "skipped": True, "skip_reason": NOT_ENABLED_REASON})
    return result
