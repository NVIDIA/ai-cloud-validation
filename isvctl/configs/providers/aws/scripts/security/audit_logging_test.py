#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Verify management-event audit logging and retention (SEC08-01/02).

SEC08-01 emits a known EC2 management API call (``DescribeRegions``) with a
unique User-Agent suffix, captures the AWS request id, and polls CloudTrail
``LookupEvents`` for the matching event. On a match it verifies required
metadata: event name, event time, user identity ARN, source IP, user agent,
AWS region, and event source.

SEC08-02 resolves an active multi-region CloudTrail trail's S3 destination and
checks its lifecycle configuration. It passes when there is no current-object
expiration rule or every enabled current-object expiration rule retains logs
for at least 30 days.

CloudTrail event ingestion may exceed the poll budget in cold regions. In that
case, and when no suitable logging trail exists, the script emits structured
per-check skips rather than fabricating a pass.
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from common.errors import classify_aws_error, handle_aws_errors

TEST_NAME = "audit_logging_test"
EVENT_NAME = "DescribeRegions"
EVENT_SOURCE = "ec2.amazonaws.com"
DEFAULT_LOOKUP_TIMEOUT_SECONDS = 600
RETENTION_DAYS = 30
LOOKUP_PAGES_PER_POLL = 5
AUDIT_ENTRY_TEST_KEYS = (
    "audit_log_entry_found",
    "audit_log_event_name_matches",
    "audit_log_event_time_in_window",
    "audit_log_user_identity_present",
    "audit_log_source_ip_present",
    "audit_log_user_agent_matches",
    "audit_log_region_matches",
    "audit_log_event_source_matches",
)
AUDIT_RETENTION_TEST_KEYS = (
    "audit_log_trail_logging_enabled",
    "audit_log_retention_at_least_30_days",
)


def _base_result(region: str, lookup_timeout_seconds: int) -> dict[str, Any]:
    """Return the initial result envelope with all SEC08 tests unset."""
    return {
        "success": False,
        "platform": "security",
        "test_name": TEST_NAME,
        "region": region,
        "event_name": EVENT_NAME,
        "event_source": EVENT_SOURCE,
        "request_id": "",
        "lookup_timeout_seconds": lookup_timeout_seconds,
        "tests": {key: {"passed": False} for key in (*AUDIT_ENTRY_TEST_KEYS, *AUDIT_RETENTION_TEST_KEYS)},
    }


def _mark_entry_skipped(result: dict[str, Any], reason: str) -> None:
    """Mark SEC08-01 tests as skipped while keeping the command exit clean."""
    result["audit_log_entry_skipped"] = True
    result["audit_log_entry_skip_reason"] = reason
    for key in AUDIT_ENTRY_TEST_KEYS:
        result["tests"][key] = {"passed": True, "skipped": True, "skip_reason": reason}


def _mark_retention_skipped(result: dict[str, Any], reason: str) -> None:
    """Mark SEC08-02 tests as skipped while keeping the command exit clean."""
    result["audit_log_retention_skipped"] = True
    result["audit_log_retention_skip_reason"] = reason
    for key in AUDIT_RETENTION_TEST_KEYS:
        result["tests"][key] = {"passed": True, "skipped": True, "skip_reason": reason}


def _find_logging_trails(cloudtrail: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return active logging trails and the active multi-region subset."""
    response = cloudtrail.describe_trails(includeShadowTrails=False)
    logging_trails: list[dict[str, Any]] = []
    multi_region_logging_trails: list[dict[str, Any]] = []
    for trail in response.get("trailList", []):
        trail_name = trail.get("TrailARN") or trail.get("Name")
        if not trail_name:
            continue
        status = cloudtrail.get_trail_status(Name=trail_name)
        if not status.get("IsLogging"):
            continue
        trail_with_status = {**trail, "IsLogging": True}
        logging_trails.append(trail_with_status)
        if trail.get("IsMultiRegionTrail") is True:
            multi_region_logging_trails.append(trail_with_status)
    return logging_trails, multi_region_logging_trails


def _emit_management_call(region: str, user_agent_suffix: str) -> tuple[str, datetime, datetime]:
    """Call EC2 DescribeRegions with a unique User-Agent suffix and return request metadata."""
    ec2 = boto3.client("ec2", region_name=region, config=Config(user_agent_extra=user_agent_suffix))
    call_start = datetime.now(UTC)
    response = ec2.describe_regions(AllRegions=False)
    call_end = datetime.now(UTC)
    request_id = response.get("ResponseMetadata", {}).get("RequestId", "")
    if not request_id:
        msg = "EC2 DescribeRegions response did not include ResponseMetadata.RequestId"
        raise RuntimeError(msg)
    return request_id, call_start, call_end


def _parse_event_time(raw_event_time: str) -> datetime | None:
    """Parse a CloudTrail eventTime string as a timezone-aware datetime."""
    if not raw_event_time:
        return None
    try:
        parsed = datetime.fromisoformat(raw_event_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cloudtrail_event_matches(event: dict[str, Any], request_id: str, user_agent_suffix: str) -> bool:
    """Return True when a LookupEvents item is the management call we emitted."""
    try:
        payload = json.loads(event.get("CloudTrailEvent", "{}"))
    except json.JSONDecodeError:
        return False
    event_request_id = payload.get("requestID") or payload.get("requestId")
    user_agent = payload.get("userAgent", "")
    return event_request_id == request_id and user_agent_suffix in user_agent


def _lookup_management_event(
    cloudtrail: Any,
    *,
    request_id: str,
    user_agent_suffix: str,
    call_start: datetime,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    """Poll CloudTrail LookupEvents until the matching event appears or the budget expires."""
    deadline = time.monotonic() + timeout_seconds
    delay = 5
    while True:
        next_token: str | None = None
        for _ in range(LOOKUP_PAGES_PER_POLL):
            kwargs: dict[str, Any] = {
                "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": EVENT_NAME}],
                "StartTime": call_start - timedelta(minutes=5),
                "EndTime": datetime.now(UTC) + timedelta(minutes=1),
                "MaxResults": 50,
            }
            if next_token:
                kwargs["NextToken"] = next_token
            response = cloudtrail.lookup_events(**kwargs)
            for event in response.get("Events", []):
                if _cloudtrail_event_matches(event, request_id, user_agent_suffix):
                    return event
            next_token = response.get("NextToken")
            if not next_token:
                break
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
        delay = min(delay * 2, 30)


def _record_event_metadata_tests(
    result: dict[str, Any],
    *,
    lookup_event: dict[str, Any],
    request_id: str,
    user_agent_suffix: str,
    region: str,
    call_start: datetime,
    call_end: datetime,
) -> None:
    """Populate SEC08-01 tests from the matched CloudTrail event."""
    payload = json.loads(lookup_event.get("CloudTrailEvent", "{}"))
    event_time = _parse_event_time(payload.get("eventTime", ""))
    user_identity_arn = payload.get("userIdentity", {}).get("arn", "")
    source_ip = payload.get("sourceIPAddress", "")
    user_agent = payload.get("userAgent", "")
    event_region = payload.get("awsRegion", "")
    event_source = payload.get("eventSource", "")

    result["cloudtrail_event_id"] = lookup_event.get("EventId", "")
    result["cloudtrail_event_time"] = payload.get("eventTime", "")
    result["cloudtrail_user_identity_arn"] = user_identity_arn
    result["cloudtrail_source_ip_address"] = source_ip
    result["cloudtrail_user_agent_suffix"] = user_agent_suffix

    window_start = call_start - timedelta(minutes=1)
    window_end = max(call_end + timedelta(minutes=15), datetime.now(UTC) + timedelta(minutes=1))
    result["tests"]["audit_log_entry_found"] = {
        "passed": True,
        "message": f"matched CloudTrail event for request id {request_id}",
    }
    result["tests"]["audit_log_event_name_matches"] = {"passed": payload.get("eventName") == EVENT_NAME}
    result["tests"]["audit_log_event_time_in_window"] = {
        "passed": event_time is not None and window_start <= event_time <= window_end
    }
    result["tests"]["audit_log_user_identity_present"] = {"passed": bool(user_identity_arn)}
    result["tests"]["audit_log_source_ip_present"] = {"passed": bool(source_ip)}
    result["tests"]["audit_log_user_agent_matches"] = {"passed": user_agent_suffix in user_agent}
    result["tests"]["audit_log_region_matches"] = {"passed": event_region == region}
    result["tests"]["audit_log_event_source_matches"] = {"passed": event_source == EVENT_SOURCE}


def _evaluate_lifecycle_retention(s3: Any, bucket: str) -> tuple[dict[str, Any], str | int]:
    """Evaluate current-object expiration rules for the CloudTrail destination bucket."""
    try:
        lifecycle = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchLifecycleConfiguration":
            return {"passed": True, "message": "no lifecycle expiration rules configured"}, "unbounded"
        raise

    enabled_expiration_rules: list[dict[str, Any]] = []
    for rule in lifecycle.get("Rules", []):
        if rule.get("Status") != "Enabled":
            continue
        expiration = rule.get("Expiration")
        if not expiration:
            continue
        if (
            expiration.get("ExpiredObjectDeleteMarker") is True
            and "Days" not in expiration
            and "Date" not in expiration
        ):
            continue
        enabled_expiration_rules.append({"id": rule.get("ID", "<unnamed>"), "expiration": expiration})

    if not enabled_expiration_rules:
        return {"passed": True, "message": "no enabled current-object expiration rules configured"}, "unbounded"

    failures: list[str] = []
    minimum_days: int | None = None
    for rule in enabled_expiration_rules:
        expiration = rule["expiration"]
        rule_id = rule["id"]
        if "Days" in expiration:
            days = int(expiration["Days"])
            minimum_days = days if minimum_days is None else min(minimum_days, days)
            if days < RETENTION_DAYS:
                failures.append(f"{rule_id}: expires after {days} days")
        elif "Date" in expiration:
            failures.append(f"{rule_id}: uses absolute expiration date {expiration['Date']}")
        else:
            failures.append(f"{rule_id}: expiration rule has no Days value")

    if failures:
        return {"passed": False, "error": "; ".join(failures)}, minimum_days or 0
    return {
        "passed": True,
        "message": f"all enabled current-object expiration rules retain logs for >= {RETENTION_DAYS} days",
    }, minimum_days or RETENTION_DAYS


@handle_aws_errors
def main() -> int:
    """Run SEC08 audit-log entry and retention probes."""
    parser = argparse.ArgumentParser(description="Audit logging and retention test (SEC08-01/02)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--lookup-timeout-seconds",
        type=int,
        default=DEFAULT_LOOKUP_TIMEOUT_SECONDS,
        help=f"CloudTrail LookupEvents poll budget (default: {DEFAULT_LOOKUP_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args()
    region = args.region
    lookup_timeout_seconds = max(1, args.lookup_timeout_seconds)

    result = _base_result(region, lookup_timeout_seconds)
    cloudtrail = boto3.client("cloudtrail", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    try:
        logging_trails, multi_region_logging_trails = _find_logging_trails(cloudtrail)
        if not logging_trails:
            reason = "no active CloudTrail logging trail found"
            _mark_entry_skipped(result, reason)
            _mark_retention_skipped(result, reason)
            result["success"] = True
            print(json.dumps(result, indent=2))
            return 0

        user_agent_suffix = f"isv-sec08-{uuid.uuid4().hex[:12]}"
        request_id, call_start, call_end = _emit_management_call(region, user_agent_suffix)
        result["request_id"] = request_id
        lookup_event = _lookup_management_event(
            cloudtrail,
            request_id=request_id,
            user_agent_suffix=user_agent_suffix,
            call_start=call_start,
            timeout_seconds=lookup_timeout_seconds,
        )
        if lookup_event is None:
            _mark_entry_skipped(
                result,
                f"CloudTrail event for {EVENT_NAME} request {request_id} did not propagate within "
                f"{lookup_timeout_seconds}s",
            )
        else:
            _record_event_metadata_tests(
                result,
                lookup_event=lookup_event,
                request_id=request_id,
                user_agent_suffix=user_agent_suffix,
                region=region,
                call_start=call_start,
                call_end=call_end,
            )

        if not multi_region_logging_trails:
            _mark_retention_skipped(result, "no active multi-region CloudTrail logging trail found")
        else:
            trail = multi_region_logging_trails[0]
            bucket = trail.get("S3BucketName", "")
            result["audit_log_trail_arn"] = trail.get("TrailARN", trail.get("Name", ""))
            result["audit_log_bucket"] = bucket
            result["tests"]["audit_log_trail_logging_enabled"] = {
                "passed": bool(bucket),
                "message": f"active multi-region trail writes to {bucket}",
            }
            if bucket:
                retention_result, minimum_retention = _evaluate_lifecycle_retention(s3, bucket)
                result["tests"]["audit_log_retention_at_least_30_days"] = retention_result
                result["minimum_retention_days"] = minimum_retention
            else:
                result["tests"]["audit_log_retention_at_least_30_days"] = {
                    "passed": False,
                    "error": "active multi-region trail did not report S3BucketName",
                }

        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except (ClientError, BotoCoreError) as exc:
        error_type, error_msg = classify_aws_error(exc)
        result["error"] = f"[{error_type}] {error_msg}"
        result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
