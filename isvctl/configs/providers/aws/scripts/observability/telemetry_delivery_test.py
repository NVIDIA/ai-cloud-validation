#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS telemetry delivery latency probe for observability validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.errors import handle_aws_errors

ASPECT_TESTS = [
    "telemetry_endpoint_reachable",
    "delivery_sample_present",
    "delivery_within_threshold",
]

# EC2 detailed monitoring publishes at a 1-minute cadence, but CloudWatch adds
# its own ingestion delay, so a realistic "delivery latency" budget is a few
# minutes rather than seconds. A freshly launched host also has no datapoints
# until the first metric lands, so the probe polls until one appears.
DEFAULT_MAX_DELIVERY_SECONDS = 300
DEFAULT_POLL_TIMEOUT_SECONDS = 240
DEFAULT_POLL_INTERVAL_SECONDS = 20
SAMPLE_WINDOW_SECONDS = 600


def _base_result() -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": "telemetry_delivery_latency",
        "tests": {name: {"passed": False} for name in ASPECT_TESTS},
    }


def _passed(message: str, probes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a passing subtest result."""
    result: dict[str, Any] = {"passed": True, "message": message}
    if probes is not None:
        result["probes"] = probes
    return result


def _failed(error: str, probes: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a failing subtest result."""
    result: dict[str, Any] = {"passed": False, "error": error}
    if probes is not None:
        result["probes"] = probes
    return result


def _newest_datapoint_age_seconds(datapoints: list[dict[str, Any]]) -> tuple[int, str]:
    """Return age in seconds and ISO timestamp for the newest datapoint."""
    if not datapoints:
        return -1, ""
    newest = max(datapoints, key=lambda point: point["Timestamp"])
    timestamp = newest["Timestamp"]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age = int((datetime.now(UTC) - timestamp).total_seconds())
    return age, timestamp.isoformat()


def _scan_newest_datapoint(cloudwatch: Any, metrics: list[dict[str, Any]]) -> tuple[int, str, int]:
    """Return (age_seconds, iso_timestamp, sample_count) for the freshest datapoint."""
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(seconds=SAMPLE_WINDOW_SECONDS)
    newest_age = -1
    newest_timestamp = ""
    sample_count = 0
    for metric in metrics[:20]:
        try:
            response = cloudwatch.get_metric_statistics(
                Namespace=metric["Namespace"],
                MetricName=metric["MetricName"],
                Dimensions=metric.get("Dimensions", []),
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=["Sum"],
            )
        except ClientError:
            continue
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            continue
        age, timestamp = _newest_datapoint_age_seconds(datapoints)
        if age >= 0 and (newest_age < 0 or age < newest_age):
            newest_age = age
            newest_timestamp = timestamp
            sample_count = len(datapoints)
    return newest_age, newest_timestamp, sample_count


def check_telemetry_delivery_latency(
    cloudwatch: Any,
    *,
    network_id: str,
    instance_id: str = "",
    max_delivery_seconds: int = DEFAULT_MAX_DELIVERY_SECONDS,
    poll_timeout_seconds: int = 0,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Measure CloudWatch packet metric delivery latency for the launched host.

    When ``instance_id`` is provided the probe is scoped to that instance so it
    measures the host under test rather than unrelated account instances. Since a
    freshly launched host has no datapoints until the first metric is ingested,
    the probe polls up to ``poll_timeout_seconds`` for a datapoint to appear.
    """
    result = _base_result()
    dimensions: list[dict[str, str]] = (
        [{"Name": "InstanceId", "Value": instance_id}] if instance_id else [{"Name": "InstanceId"}]
    )
    probes = {
        "telemetry_source": "cloudwatch",
        "observed_delivery_seconds": -1,
        "max_delivery_seconds": max_delivery_seconds,
        "sample_count": 0,
        "latest_timestamp": "",
        "probe_resource_id": instance_id or network_id,
    }

    def _list_metrics() -> list[dict[str, Any]]:
        return cloudwatch.list_metrics(
            Namespace="AWS/EC2",
            MetricName="NetworkPacketsIn",
            Dimensions=dimensions,
        ).get("Metrics", [])

    try:
        metrics = _list_metrics()
    except ClientError as e:
        error = "AWS API error while querying CloudWatch metrics"
        for name in ASPECT_TESTS:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        result["error_type"] = type(e).__name__
        return result

    result["tests"]["telemetry_endpoint_reachable"] = _passed(
        f"CloudWatch metrics endpoint reachable ({len(metrics)} packet metric(s) visible)",
        probes,
    )

    deadline = time.monotonic() + max(poll_timeout_seconds, 0)
    newest_age, newest_timestamp, sample_count = _scan_newest_datapoint(cloudwatch, metrics)
    while newest_age < 0 and time.monotonic() < deadline:
        sleep(poll_interval_seconds)
        try:
            metrics = _list_metrics()
        except ClientError:
            continue
        newest_age, newest_timestamp, sample_count = _scan_newest_datapoint(cloudwatch, metrics)

    probes = {
        **probes,
        "observed_delivery_seconds": max(newest_age, 0),
        "sample_count": sample_count,
        "latest_timestamp": newest_timestamp,
    }

    if newest_age < 0:
        result["tests"]["delivery_sample_present"] = _failed(
            "No recent CloudWatch packet metric datapoints found", probes
        )
        result["tests"]["delivery_within_threshold"] = _failed(
            "Cannot measure delivery latency without recent samples", probes
        )
        result["error"] = "Telemetry delivery latency checks failed"
        return result

    result["tests"]["delivery_sample_present"] = _passed(
        f"{sample_count} recent CloudWatch packet metric datapoint(s) found", probes
    )
    if newest_age <= max_delivery_seconds:
        result["tests"]["delivery_within_threshold"] = _passed(
            f"Telemetry delivery latency {newest_age}s within {max_delivery_seconds}s", probes
        )
    else:
        result["tests"]["delivery_within_threshold"] = _failed(
            f"Telemetry delivery latency {newest_age}s exceeds {max_delivery_seconds}s", probes
        )

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "Telemetry delivery latency checks failed"
    return result


@handle_aws_errors
def main() -> int:
    """Run the AWS telemetry delivery latency probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="AWS telemetry delivery latency test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--network-id", default="")
    parser.add_argument("--instance-id", default="", help="Scope the probe to a specific EC2 instance")
    parser.add_argument("--max-delivery-seconds", type=int, default=DEFAULT_MAX_DELIVERY_SECONDS)
    parser.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=DEFAULT_POLL_TIMEOUT_SECONDS,
        help="Seconds to wait for a CloudWatch datapoint to appear before giving up",
    )
    parser.add_argument("--poll-interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS)
    args = parser.parse_args()

    if args.max_delivery_seconds <= 0:
        print(
            json.dumps(
                {
                    "success": False,
                    "platform": "observability",
                    "test_name": "telemetry_delivery_latency",
                    "error": "--max-delivery-seconds must be greater than 0",
                },
                indent=2,
            )
        )
        return 1

    result = check_telemetry_delivery_latency(
        boto3.client("cloudwatch", region_name=args.region),
        network_id=args.network_id,
        instance_id=args.instance_id,
        max_delivery_seconds=args.max_delivery_seconds,
        poll_timeout_seconds=args.poll_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
