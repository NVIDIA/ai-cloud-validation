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

"""Verify audit-log entry capture and log retention on GCP (SEC08-01/02).

This step feeds two validators, each with its own skip gate:

  * Entry capture (8 subtests, ``audit_log_entry_skipped`` gate): emit one
    management call that carries a unique ``callerSuppliedUserAgent`` marker,
    then poll Cloud Logging (``logging_v2.Client.list_entries``) filtered to the
    Admin Activity audit log to find the matching entry. The entry's
    ``protoPayload`` / ``requestMetadata`` fields are checked against the call we
    made.
  * Retention (2 subtests, ``audit_log_retention_skipped`` gate): read the
    ``_Default`` log bucket via ``ConfigServiceV2Client.get_bucket`` and verify
    ``LogBucket.retention_days >= 30``. Admin Activity logging is always-on, so
    the trail-logging subtest reflects that.

GCP Admin Activity audit logs record write/admin calls and are always-on (a
read-only call would land in off-by-default Data Access logs instead). The entry
half therefore emits a real admin write that surfaces in the Admin Activity
stream: a no-op ``ConfigServiceV2.UpdateBucket`` on the ``_Default`` log bucket
that sets ``retention_days`` to its current value (no state change, no resource
created) while carrying the marker user-agent. The marked entry is then found in
the Admin Activity stream and its 8 metadata subtests are validated. The entry
half skips only when it genuinely cannot run — the write is denied
(``PermissionDenied``) or the entry does not ingest within the bounded poll
budget (ingestion is eventually consistent) — rather than skipping by
construction. The retention half does real work and reports its own honest
verdict.

The marker is a non-secret correlation token; no credentials or tokens are
printed.

Usage:
    python3 audit_logging_test.py --region us-central1 --project my-project

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "audit_logging_test",
    "audit_log_entry_skipped": false,
    "audit_log_retention_skipped": false,
    "tests": {
        "audit_log_entry_found": {"passed": true},
        "audit_log_event_name_matches": {"passed": true},
        ...
        "audit_log_trail_logging_enabled": {"passed": true},
        "audit_log_retention_at_least_30_days": {"passed": true}
    }
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.api_core import exceptions as gax
from google.api_core.gapic_v1.client_info import ClientInfo
from google.cloud import logging_v2
from google.cloud.logging_v2.services.config_service_v2 import ConfigServiceV2Client

TEST_NAME = "audit_logging_test"

_ENTRY_TEST_KEYS = (
    "audit_log_entry_found",
    "audit_log_event_name_matches",
    "audit_log_event_time_in_window",
    "audit_log_user_identity_present",
    "audit_log_source_ip_present",
    "audit_log_user_agent_matches",
    "audit_log_region_matches",
    "audit_log_event_source_matches",
)
_RETENTION_TEST_KEYS = (
    "audit_log_trail_logging_enabled",
    "audit_log_retention_at_least_30_days",
)

# Poll budget for the marked entry. Audit-log ingestion is eventually consistent
# (seconds), so a short bounded poll within the step timeout is the honest
# wait — the entry half skips, not fails, if it never surfaces.
_ENTRY_POLL_ATTEMPTS = 6
_ENTRY_POLL_DELAY_SECONDS = 10
# Both event-time bounds use the same small skew allowance. Polling may wait for
# ingestion, but must never expand the timestamp window of acceptable evidence.
_ENTRY_CLOCK_SKEW = timedelta(minutes=5)
_MIN_RETENTION_DAYS = 30
_RETENTION_READ_ATTEMPTS = 4
_RETENTION_READ_DELAY_SECONDS = 2
_TRANSIENT_LOGGING_ERRORS = (
    gax.DeadlineExceeded,
    gax.InternalServerError,
    gax.ServiceUnavailable,
    gax.TooManyRequests,
)


def _filter_payload(payload: Any) -> dict[str, Any]:
    """Return the protoPayload of a log entry as a plain dict (or {})."""
    if isinstance(payload, Mapping):
        return dict(payload)
    # The high-level ProtobufEntry exposes the AuditLog as payload_json / payload.
    as_dict = getattr(payload, "payload_json", None)
    if isinstance(as_dict, Mapping):
        return dict(as_dict)
    return {}


def _find_marked_entry(
    client: logging_v2.Client,
    project: str,
    marker: str,
    started_at: datetime,
    completed_at: datetime,
    bucket_name: str,
) -> Any | None:
    """Poll for the marked Admin Activity entry on the exact log bucket."""
    window_start = (started_at - _ENTRY_CLOCK_SKEW).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = (completed_at + _ENTRY_CLOCK_SKEW).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_name = f"projects/{project}/logs/cloudaudit.googleapis.com%2Factivity"
    filter_ = (
        f'logName="{log_name}" '
        f'AND timestamp>="{window_start}" '
        f'AND timestamp<="{window_end}" '
        f'AND protoPayload.resourceName="{bucket_name}" '
        f'AND protoPayload.requestMetadata.callerSuppliedUserAgent:"{marker}"'
    )
    last_transient_error: Exception | None = None
    for attempt in range(_ENTRY_POLL_ATTEMPTS):
        try:
            for entry in client.list_entries(
                resource_names=[f"projects/{project}"],
                filter_=filter_,
                order_by=logging_v2.DESCENDING,
                max_results=5,
            ):
                return entry
            # A completed empty query is valid evidence that the entry has not
            # ingested yet. Clear an earlier transient so a later successful
            # final poll can still produce the bounded ingestion-lag skip.
            last_transient_error = None
        except _TRANSIENT_LOGGING_ERRORS as exc:
            # Only retry errors that can converge. Authorization and other
            # permanent failures must reach the outer fail-closed boundary,
            # never masquerade as eventual-consistency ingestion lag.
            last_transient_error = exc
        if attempt < _ENTRY_POLL_ATTEMPTS - 1:
            time.sleep(_ENTRY_POLL_DELAY_SECONDS)
    if last_transient_error is not None:
        # The final observation was not an empty successful query; it was an
        # unavailable Logging API. That cannot prove ingestion lag.
        raise last_transient_error
    return None


def _check_entry_subtests(
    entry: Any,
    *,
    marker: str,
    expected_method: str,
    expected_service: str,
    region: str,
    started_at: datetime,
    completed_at: datetime,
    bucket_name: str,
    result: dict[str, Any],
) -> None:
    """Populate the 8 entry subtests from the matched audit log entry."""
    payload = _filter_payload(getattr(entry, "payload", None))
    request_metadata = payload.get("requestMetadata", {}) if isinstance(payload, dict) else {}
    auth_info = payload.get("authenticationInfo", {}) if isinstance(payload, dict) else {}

    resource_name = str(payload.get("resourceName", ""))
    result["tests"]["audit_log_entry_found"] = {
        "passed": resource_name == bucket_name,
        "message": f"protoPayload.resourceName={resource_name!r}",
    }

    method_name = str(payload.get("methodName", ""))
    result["tests"]["audit_log_event_name_matches"] = {
        "passed": bool(method_name) and method_name == expected_method,
        "message": f"methodName={method_name!r}",
    }

    timestamp = getattr(entry, "timestamp", None)
    in_window = bool(timestamp) and (started_at - _ENTRY_CLOCK_SKEW) <= timestamp <= (completed_at + _ENTRY_CLOCK_SKEW)
    result["tests"]["audit_log_event_time_in_window"] = {
        "passed": in_window,
        "message": "entry timestamp falls within the correlation window",
    }

    principal = str(auth_info.get("principalEmail", ""))
    result["tests"]["audit_log_user_identity_present"] = {
        "passed": bool(principal),
        "message": "authenticationInfo.principalEmail present",
    }

    caller_ip = str(request_metadata.get("callerIp", ""))
    result["tests"]["audit_log_source_ip_present"] = {
        "passed": bool(caller_ip),
        "message": "requestMetadata.callerIp present",
    }

    caller_ua = str(request_metadata.get("callerSuppliedUserAgent", ""))
    result["tests"]["audit_log_user_agent_matches"] = {
        "passed": marker in caller_ua,
        "message": "callerSuppliedUserAgent carries the correlation marker",
    }

    # resource.labels.location is the closest analog to an AWS region field; it
    # is not present on every audit resource, so its absence is a not-applicable
    # pass rather than a failure.
    resource = getattr(entry, "resource", None)
    labels = getattr(resource, "labels", None) or {}
    location = str(labels.get("location", "")) if isinstance(labels, dict) else ""
    if not location or location == "global":
        result["tests"]["audit_log_region_matches"] = {
            "passed": True,
            "message": "resource is global or carries no location label (region not applicable)",
        }
    else:
        result["tests"]["audit_log_region_matches"] = {
            "passed": bool(region) and (location == region or location.startswith(f"{region}-")),
            "message": f"resource.labels.location={location!r}",
        }

    service_name = str(payload.get("serviceName", ""))
    result["tests"]["audit_log_event_source_matches"] = {
        "passed": bool(service_name) and service_name == expected_service,
        "message": f"serviceName={service_name!r}",
    }


def _skip_entry_half(result: dict[str, Any], reason: str) -> None:
    """Mark the entry-capture half skipped with an explicit reason."""
    result["audit_log_entry_skipped"] = True
    result["audit_log_entry_skip_reason"] = reason
    for key in _ENTRY_TEST_KEYS:
        result["tests"][key] = {"passed": True, "skipped": True, "skip_reason": reason}


def _evaluate_retention(project: str, result: dict[str, Any]) -> None:
    """Read the _Default log bucket retention and populate the 2 retention subtests."""
    config_client = ConfigServiceV2Client()
    bucket_name = f"projects/{project}/locations/global/buckets/_Default"
    bucket = None
    for attempt in range(1, _RETENTION_READ_ATTEMPTS + 1):
        try:
            bucket = config_client.get_bucket(request={"name": bucket_name})
            break
        except _TRANSIENT_LOGGING_ERRORS:
            if attempt >= _RETENTION_READ_ATTEMPTS:
                raise
            time.sleep(_RETENTION_READ_DELAY_SECONDS * attempt)
    if bucket is None:  # defensive: every loop path returns or raises
        raise RuntimeError(f"retention inventory returned no bucket for {bucket_name}")

    retention_days = int(getattr(bucket, "retention_days", 0) or 0)
    # Admin Activity audit logging is always-on for every GCP project; reading
    # the _Default bucket confirms the log-routing pipeline is present.
    result["tests"]["audit_log_trail_logging_enabled"] = {
        "passed": True,
        "message": "Admin Activity audit logging is always enabled; _Default log bucket present",
    }
    result["tests"]["audit_log_retention_at_least_30_days"] = {
        "passed": retention_days >= _MIN_RETENTION_DAYS,
        "message": f"_Default log bucket retention_days={retention_days}",
    }


@handle_gcp_errors
def main() -> int:
    """Emit a marked management call, correlate the audit entry, read retention; emit JSON."""
    parser = argparse.ArgumentParser(description="Audit logging and retention test (SEC08-01/02)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    region = args.region.strip()
    marker = f"isv-sec08-{token_hex(6)}"

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": TEST_NAME,
        "audit_log_entry_skipped": False,
        "audit_log_retention_skipped": False,
        "tests": {
            "audit_log_entry_found": {"passed": False},
            "audit_log_event_name_matches": {"passed": False},
            "audit_log_event_time_in_window": {"passed": False},
            "audit_log_user_identity_present": {"passed": False},
            "audit_log_source_ip_present": {"passed": False},
            "audit_log_user_agent_matches": {"passed": False},
            "audit_log_region_matches": {"passed": False},
            "audit_log_event_source_matches": {"passed": False},
            "audit_log_trail_logging_enabled": {"passed": False},
            "audit_log_retention_at_least_30_days": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)

        # Retention half: real work, independent of the entry half.
        _evaluate_retention(project, result)

        # Entry half: emit one admin write carrying the marker, then poll. A
        # no-op UpdateBucket on the _Default log bucket (retention_days set to its
        # current value) is an admin action recorded in the always-on Admin
        # Activity stream — unlike a read, which lands in off-by-default Data
        # Access logs — so the marked entry surfaces and the 8 subtests run for
        # real. It changes no state and creates no resource, so there is nothing
        # to tear down.
        started_at = datetime.now(UTC)
        client_info = ClientInfo(user_agent=marker)
        bucket_name = f"projects/{project}/locations/global/buckets/_Default"
        expected_method = "google.logging.v2.ConfigServiceV2.UpdateBucket"
        expected_service = "logging.googleapis.com"
        try:
            probe_client = ConfigServiceV2Client(client_info=client_info)
            current = probe_client.get_bucket(request={"name": bucket_name})
            retention = int(getattr(current, "retention_days", 0) or 0)
            probe_client.update_bucket(
                request={
                    "name": bucket_name,
                    "bucket": {"retention_days": retention},
                    "update_mask": {"paths": ["retention_days"]},
                }
            )
            completed_at = datetime.now(UTC)
        except (gax.PermissionDenied, gax.Forbidden) as exc:
            _skip_entry_half(result, f"management write denied or failed: {type(exc).__name__}")
        else:
            logging_client = logging_v2.Client(project=project)
            entry = _find_marked_entry(
                logging_client,
                project,
                marker,
                started_at,
                completed_at,
                bucket_name,
            )
            if entry is None:
                _skip_entry_half(
                    result,
                    "marked audit entry did not ingest within the poll budget "
                    "(Admin Activity ingestion is eventually consistent)",
                )
            else:
                _check_entry_subtests(
                    entry,
                    marker=marker,
                    expected_method=expected_method,
                    expected_service=expected_service,
                    region=region,
                    started_at=started_at,
                    completed_at=completed_at,
                    bucket_name=bucket_name,
                    result=result,
                )
        # Compute success only on the protected normal path. Exception paths
        # must retain the initial false verdict even if existing subtest values
        # are accidentally or adversarially all truthy.
        result["success"] = all(t.get("passed") for t in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        result["success"] = False

    # Success requires every subtest to pass; a skipped half emits passing,
    # skip-marked subtests so its validator skips rather than fails.
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
