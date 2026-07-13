#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS storage telemetry availability probes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.cloudwatch import SAMPLE_WINDOW_SECONDS, newest_datapoint_timestamp, scan_recent_datapoints
from common.errors import handle_aws_errors

ASPECT_TESTS: dict[str, list[str]] = {
    "storage_capacity_telemetry": [
        "telemetry_endpoint_reachable",
        "capacity_metrics_present",
        "samples_recent",
    ],
    "storage_performance_telemetry": [
        "telemetry_endpoint_reachable",
        "performance_metrics_present",
        "samples_recent",
    ],
}

PERFORMANCE_METRICS = [
    "VolumeReadBytes",
    "VolumeWriteBytes",
    "VolumeReadOps",
    "VolumeWriteOps",
    "VolumeTotalReadTime",
    "VolumeTotalWriteTime",
]

PERFORMANCE_KINDS_BY_METRIC = {
    "VolumeReadBytes": "bandwidth",
    "VolumeWriteBytes": "bandwidth",
    "VolumeReadOps": "iops",
    "VolumeWriteOps": "iops",
    "VolumeTotalReadTime": "latency",
    "VolumeTotalWriteTime": "latency",
}

HIDDEN_ASPECTS = {"storage_capacity_telemetry"}

AWS_NO_CUSTOMER_CAPACITY_MESSAGE = (
    "AWS EC2/EKS tenants do not receive customer-accessible storage capacity telemetry (used/free/total)"
)

DEFAULT_POLL_TIMEOUT_SECONDS = 240
DEFAULT_POLL_INTERVAL_SECONDS = 20


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
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


def _provider_hidden(test_name: str, *, region: str) -> dict[str, Any]:
    """Build a passing provider-hidden subtest result."""
    return {
        "passed": True,
        "provider_hidden": True,
        "probes": {"volumes_checked": 0, "telemetry_source": "", "metric_names": []},
        "message": (
            f"{test_name}: {AWS_NO_CUSTOMER_CAPACITY_MESSAGE} in region {region}; storage plane is provider-owned."
        ),
    }


def _list_instance_volume_ids(ec2: Any, instance_id: str) -> list[str]:
    """Return EBS volume IDs attached to an instance."""
    if not instance_id:
        return []
    reservations = ec2.describe_instances(InstanceIds=[instance_id]).get("Reservations", [])
    volume_ids: list[str] = []
    for reservation in reservations:
        for instance in reservation.get("Instances", []):
            for mapping in instance.get("BlockDeviceMappings", []):
                ebs = mapping.get("Ebs", {})
                if volume_id := ebs.get("VolumeId"):
                    volume_ids.append(volume_id)
    return volume_ids


def _collect_recent_samples(cloudwatch: Any, metrics: list[dict[str, Any]]) -> tuple[int, str]:
    """Return (sample_count, latest_iso_timestamp) across the given metrics."""
    batches = scan_recent_datapoints(cloudwatch, metrics)
    sample_count = sum(len(datapoints) for datapoints in batches)
    timestamps = [ts for datapoints in batches if (ts := newest_datapoint_timestamp(datapoints)) is not None]
    latest = max(timestamps, default=None)
    return sample_count, latest.isoformat() if latest else ""


def _poll_recent_samples(
    metrics: list[dict[str, Any]],
    list_metrics: Callable[[], list[dict[str, Any]]],
    cloudwatch: Any,
    *,
    poll_timeout_seconds: int,
    poll_interval_seconds: int,
    sleep: Callable[[float], None],
) -> tuple[list[dict[str, Any]], int, str]:
    """Poll CloudWatch until recent samples appear or the timeout elapses."""
    sample_count, latest_timestamp = _collect_recent_samples(cloudwatch, metrics)
    deadline = time.monotonic() + max(poll_timeout_seconds, 0)
    while sample_count == 0 and time.monotonic() < deadline:
        sleep(poll_interval_seconds)
        if not metrics:
            try:
                metrics = list_metrics()
            except ClientError:
                continue
        sample_count, latest_timestamp = _collect_recent_samples(cloudwatch, metrics)
    return metrics, sample_count, latest_timestamp


def _list_volume_metrics(
    cloudwatch: Any,
    *,
    metric_names: list[str],
    volume_ids: list[str],
) -> list[dict[str, Any]]:
    """Return CloudWatch metrics scoped to the given EBS volume IDs."""
    metrics: list[dict[str, Any]] = []
    for volume_id in volume_ids:
        dimensions = [{"Name": "VolumeId", "Value": volume_id}]
        for metric_name in metric_names:
            metrics.extend(
                cloudwatch.list_metrics(
                    Namespace="AWS/EBS",
                    MetricName=metric_name,
                    Dimensions=dimensions,
                ).get("Metrics", [])
            )
    return metrics


def _performance_kinds(metric_names: list[str]) -> list[str]:
    """Map discovered EBS metric names to provider-neutral performance kinds."""
    kinds: list[str] = []
    for metric_name in metric_names:
        kind = PERFORMANCE_KINDS_BY_METRIC.get(metric_name)
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def _check_storage_performance_telemetry(
    cloudwatch: Any,
    ec2: Any,
    *,
    instance_id: str,
    poll_timeout_seconds: int = 0,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Validate customer-visible EBS performance telemetry for attached volumes."""
    aspect = "storage_performance_telemetry"
    result = _base_result(aspect)
    volume_ids = _list_instance_volume_ids(ec2, instance_id)
    probes: dict[str, Any] = {
        "telemetry_source": "cloudwatch",
        "metric_names": [],
        "performance_kinds": [],
        "volumes_checked": len(volume_ids),
        "sample_count": 0,
        "latest_timestamp": "",
        "probe_resource_id": instance_id,
    }

    if not volume_ids:
        error = f"No EBS volumes attached to instance {instance_id}"
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    def list_metrics() -> list[dict[str, Any]]:
        return _list_volume_metrics(cloudwatch, metric_names=PERFORMANCE_METRICS, volume_ids=volume_ids)

    try:
        metrics = list_metrics()
    except ClientError:
        error = "AWS API error while querying CloudWatch EBS metrics"
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    discovered_names = sorted({metric["MetricName"] for metric in metrics})
    performance_kinds = _performance_kinds(discovered_names)
    probes = {
        **probes,
        "metric_names": discovered_names,
        "performance_kinds": performance_kinds,
    }

    result["tests"]["telemetry_endpoint_reachable"] = _passed(
        f"CloudWatch EBS metrics endpoint reachable ({len(metrics)} metric(s) visible)", probes
    )

    metrics, sample_count, latest_timestamp = _poll_recent_samples(
        metrics,
        list_metrics,
        cloudwatch,
        poll_timeout_seconds=poll_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )
    # Recompute after poll: freshly launched hosts may discover metrics only on refresh.
    discovered_names = sorted({metric["MetricName"] for metric in metrics})
    performance_kinds = _performance_kinds(discovered_names)
    probes = {
        **probes,
        "metric_names": discovered_names,
        "performance_kinds": performance_kinds,
        "sample_count": sample_count,
        "latest_timestamp": latest_timestamp,
    }

    required_kinds = {"bandwidth", "iops", "latency"}
    if discovered_names and required_kinds.issubset(set(performance_kinds)):
        result["tests"]["performance_metrics_present"] = _passed(
            "EBS performance telemetry metrics are configured", probes
        )
    elif discovered_names:
        missing = sorted(required_kinds - set(performance_kinds))
        result["tests"]["performance_metrics_present"] = _failed(
            f"Missing EBS performance metric kinds: {', '.join(missing)}", probes
        )
    else:
        result["tests"]["performance_metrics_present"] = _failed("No EBS performance metrics are configured", probes)

    if sample_count > 0:
        result["tests"]["samples_recent"] = _passed(f"{sample_count} recent EBS telemetry sample(s) found", probes)
    else:
        result["tests"]["samples_recent"] = _failed(
            f"No recent EBS telemetry samples found in the last {SAMPLE_WINDOW_SECONDS} seconds", probes
        )

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "storage performance telemetry checks failed"
    return result


def _check_hidden_storage_capacity(*, region: str) -> dict[str, Any]:
    """Emit provider-hidden evidence for tenant-inaccessible storage capacity telemetry."""
    aspect = "storage_capacity_telemetry"
    result = _base_result(aspect)
    result["success"] = True
    result["tests"] = {name: _provider_hidden(name, region=region) for name in ASPECT_TESTS[aspect]}
    return result


@handle_aws_errors
def main() -> int:
    """Run the selected AWS storage telemetry probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="AWS storage telemetry availability test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--instance-id", default="", help="Scope the probe to a specific EC2 instance")
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_TESTS))
    parser.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=DEFAULT_POLL_TIMEOUT_SECONDS,
        help="Seconds to wait for CloudWatch samples to appear before giving up",
    )
    parser.add_argument("--poll-interval-seconds", type=int, default=DEFAULT_POLL_INTERVAL_SECONDS)
    args = parser.parse_args()

    if args.aspect in HIDDEN_ASPECTS:
        result = _check_hidden_storage_capacity(region=args.region)
    else:
        result = _check_storage_performance_telemetry(
            boto3.client("cloudwatch", region_name=args.region),
            boto3.client("ec2", region_name=args.region),
            instance_id=args.instance_id,
            poll_timeout_seconds=args.poll_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
