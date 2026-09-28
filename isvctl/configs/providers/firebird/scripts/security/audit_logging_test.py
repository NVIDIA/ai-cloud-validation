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

"""Audit-log entry and retention via the Firebird audit API (SEC08-01, SEC08-02).

Entry (SEC08-01): creates a service account - a known management call - and
polls ``GET /audit?targetKind=IAM&targetId=<account>`` for its CREATE event.
The event must carry the action, a timestamp inside the call's window, the
actor (``actorId``), and the emitting service (``source``). Three more
subtests check the event's ``sourceIp``, ``userAgent`` (against this client's
own ``USER_AGENT``), and ``region`` (against the run's project) when the API
reports them; a Firebird audit API that predates those fields omits the key
entirely, and that subtest is then reported failed as not supported
(``supported: false``) exactly as before.

Retention (SEC08-02): audit logging is live when the last day holds an event
(read after the call above), and 30-day retention is shown by an event in the
year ending 30 days ago - the serial-console check's observed-retention method:
the API does not expose the configured retention policy, so the evidence is the
age of a retained event.

The audit API is registered only when audit is enabled on the deployment; a
404/501 on the first read is a structured skip, taken before anything is
created. The account is recorded as soon as it exists and deleted by the
security teardown step.

Usage:
    python audit_logging_test.py [--audit-timeout 180] [--retention-days 30]

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "audit_logging_test",
    "audit_event_id": "audit.xxx",
    "retention_days_observed": 45,
    "created_service_account_ids": ["service-account.xxx"],
    "tests": {"audit_log_entry_found": {"passed": true}, ...,
              "audit_log_source_ip_present": {"passed": true, "message": "sourceIp '10.0.0.5'"},
              "audit_log_user_agent_matches": {"passed": true, "message": "..."},
              "audit_log_region_matches": {"passed": false, "supported": false, "error": "not supported: ..."},
              "audit_log_trail_logging_enabled": {"passed": true},
              "audit_log_retention_at_least_30_days": {"passed": true}}
}
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import service_accounts
from common.firebird_client import (
    USER_AGENT,
    FirebirdApiError,
    FirebirdClient,
    is_not_registered,
    log,
    parse_timestamp,
    rfc3339,
)

ENTRY_KEYS = (
    "audit_log_entry_found",
    "audit_log_event_name_matches",
    "audit_log_event_time_in_window",
    "audit_log_user_identity_present",
    "audit_log_event_source_matches",
)
# Subtest key -> (the event's JSON field, the message when an older API omits that field).
UNSUPPORTED = {
    "audit_log_source_ip_present": ("sourceIp", "Firebird audit events carry no source IP"),
    "audit_log_user_agent_matches": ("userAgent", "Firebird audit events carry no user agent"),
    "audit_log_region_matches": ("region", "Firebird audit events carry no region"),
}
PROVENANCE_KEYS = tuple(UNSUPPORTED)
RETENTION_KEYS = ("audit_log_trail_logging_enabled", "audit_log_retention_at_least_30_days")
# Clock skew allowed between this host and the API when checking the event time.
SKEW = timedelta(seconds=120)


def outcome(passed: bool, detail: str) -> dict[str, Any]:
    """Return a subtest result carrying ``detail`` as its message or error."""
    return {"passed": True, "message": detail} if passed else {"passed": False, "error": detail}


def events(client: FirebirdClient, **params: Any) -> list[dict[str, Any]]:
    """Return one page of audit events matching ``params``."""
    return client.request("GET", "/audit", params={"pageSize": 100, **params}).get("items") or []


def find_create_event(client: FirebirdClient, sa_id: str, timeout: int) -> dict[str, Any] | None:
    """Poll for the CREATE audit event of service account ``sa_id``."""
    deadline = time.monotonic() + timeout
    while True:
        found = [e for e in events(client, targetKind="IAM", targetId=sa_id) if e.get("operationAction") == "CREATE"]
        if found:
            # The completed event is the one that names the account (pending precedes the ID).
            return next((e for e in found if e.get("state") == "COMPLETED"), found[0])
        if time.monotonic() > deadline:
            return None
        time.sleep(10)


def entry_tests(event: dict[str, Any] | None, sa_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Grade the service account's CREATE event against the SEC08-01 fields Firebird records."""
    if event is None:
        missing = f"no CREATE audit event for {sa_id}"
        return {key: outcome(False, missing) for key in ENTRY_KEYS}
    ts = parse_timestamp(event.get("ts"))
    in_window = ts is not None and start - SKEW <= ts <= end + SKEW
    return {
        "audit_log_entry_found": outcome(True, f"event {event.get('id')} targets {sa_id}"),
        "audit_log_event_name_matches": outcome(
            event.get("operationAction") == "CREATE" and event.get("targetKind") == "IAM",
            f"{event.get('targetKind')} {event.get('operationAction')}",
        ),
        "audit_log_event_time_in_window": outcome(in_window, f"ts {event.get('ts')}, call at {rfc3339(start)}"),
        "audit_log_user_identity_present": outcome(bool(event.get("actorId")), f"actor {event.get('actorId')}"),
        "audit_log_event_source_matches": outcome(bool(event.get("source")), f"source {event.get('source')!r}"),
    }


def provenance_tests(client: FirebirdClient, event: dict[str, Any]) -> dict[str, Any]:
    """Grade the event's source IP, user agent, and region when the API reports them.

    A key absent from ``event`` means this Firebird API predates the SEC08
    provenance fields; that subtest reports ``supported: false`` as before,
    decided independently per key.
    """
    tests: dict[str, Any] = {}
    for key, (field, reason) in UNSUPPORTED.items():
        if field not in event:
            tests[key] = {"passed": False, "supported": False, "error": f"not supported: {reason}"}

    if "sourceIp" in event:
        source_ip = event.get("sourceIp") or ""
        tests["audit_log_source_ip_present"] = outcome(bool(source_ip), f"sourceIp {source_ip!r}")

    if "userAgent" in event:
        actual_agent = event.get("userAgent") or ""
        tests["audit_log_user_agent_matches"] = outcome(
            actual_agent == USER_AGENT, f"expected user agent {USER_AGENT!r}, got {actual_agent!r}"
        )

    if "region" in event:
        actual_region = event.get("region") or ""
        if not actual_region:
            tests["audit_log_region_matches"] = outcome(False, "event region is empty")
        else:
            project_region = client.get_project().get("region") or ""
            if project_region:
                tests["audit_log_region_matches"] = outcome(
                    actual_region == project_region, f"expected region {project_region!r}, got {actual_region!r}"
                )
            else:
                tests["audit_log_region_matches"] = outcome(
                    True, f"event region {actual_region!r} (project carries no region; non-empty only)"
                )
    return tests


def main() -> int:
    """Emit a management call, grade its audit event, and probe retention.

    Returns:
        0 when every supported subtest passed or audit is disabled, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird audit-log entry and retention test")
    parser.add_argument("--audit-timeout", type=int, default=180, help="Seconds to wait for the audit event")
    parser.add_argument("--retention-days", type=int, default=30, help="Retention to prove")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    created: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "audit_logging_test",
        "created_service_account_ids": created,
        "tests": tests,
    }
    try:
        client = FirebirdClient()
        now = datetime.now(UTC)
        retention_end = now - timedelta(days=args.retention_days)
        try:
            retained = events(
                client, fromTs=rfc3339(retention_end - timedelta(days=365)), toTs=rfc3339(retention_end), pageSize=1
            )
        except FirebirdApiError as e:
            if not is_not_registered(e):
                raise
            result.update(success=True, skipped=True, skip_reason="The audit API is not enabled on this Firebird API")
            print(json.dumps(result, indent=2))
            return 0
        oldest = parse_timestamp(retained[0].get("ts")) if retained else None
        if oldest:
            result["retention_days_observed"] = (now - oldest).days
        tests["audit_log_retention_at_least_30_days"] = outcome(
            oldest is not None,
            f"event from {retained[0].get('ts')} retained (observed; no retention policy is exposed)"
            if oldest
            else f"no audit event older than {args.retention_days} days",
        )

        start = datetime.now(UTC)
        account = service_accounts.create(client, "isv-sec-audit")
        created.append(account.id)
        event = find_create_event(client, account.id, args.audit_timeout)
        result["audit_event_id"] = (event or {}).get("id", "")
        tests.update(entry_tests(event, account.id, start, datetime.now(UTC)))
        tests.update(provenance_tests(client, event or {}))

        # After the call above, so a live trail always has at least that event.
        recent = events(client, fromTs=rfc3339(now - timedelta(days=1)), pageSize=1)
        tests["audit_log_trail_logging_enabled"] = outcome(bool(recent), f"{len(recent)} event(s) in the last day")
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    for key in (*ENTRY_KEYS, *RETENTION_KEYS, *PROVENANCE_KEYS):
        tests.setdefault(key, outcome(False, result.get("error", "not run")))
    # The step succeeds when everything Firebird can record was shown; a
    # provenance subtest the API does not yet report (``supported: false``)
    # does not count against success, but one it does report must pass.
    result["success"] = "error" not in result and all(
        t["passed"] for t in tests.values() if t.get("supported") is not False
    )
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
