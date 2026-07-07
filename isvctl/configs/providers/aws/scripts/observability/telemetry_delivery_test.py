#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS telemetry delivery latency probe for observability validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
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

DEFAULT_MAX_DELIVERY_SECONDS = 120
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


def check_telemetry_delivery_latency(
    cloudwatch: Any,
    *,
    network_id: str,
    max_delivery_seconds: int = DEFAULT_MAX_DELIVERY_SECONDS,
) -> dict[str, Any]:
    """Measure CloudWatch packet metric delivery latency for a VPC."""
    result = _base_result()
    probes = {
        "telemetry_source": "cloudwatch",
        "observed_delivery_seconds": -1,
        "max_delivery_seconds": max_delivery_seconds,
        "sample_count": 0,
        "latest_timestamp": "",
    }

    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(seconds=SAMPLE_WINDOW_SECONDS)
    try:
        metrics = cloudwatch.list_metrics(
            Namespace="AWS/EC2",
            MetricName="NetworkPacketsIn",
            Dimensions=[{"Name": "InstanceId"}],
        ).get("Metrics", [])
        result["tests"]["telemetry_endpoint_reachable"] = _passed(
            f"CloudWatch metrics endpoint reachable ({len(metrics)} packet metric(s) visible)",
            probes,
        )
    except ClientError as e:
        error = "AWS API error while querying CloudWatch metrics"
        for name in ASPECT_TESTS:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        result["error_type"] = type(e).__name__
        return result

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

    probes = {
        "telemetry_source": "cloudwatch",
        "observed_delivery_seconds": max(newest_age, 0),
        "max_delivery_seconds": max_delivery_seconds,
        "sample_count": sample_count,
        "latest_timestamp": newest_timestamp,
        "probe_resource_id": network_id,
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
    parser.add_argument("--max-delivery-seconds", type=int, default=DEFAULT_MAX_DELIVERY_SECONDS)
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
        max_delivery_seconds=args.max_delivery_seconds,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
