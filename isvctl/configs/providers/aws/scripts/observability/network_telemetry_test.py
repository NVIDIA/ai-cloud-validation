#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS network-plane telemetry availability probes."""

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

_PLANE_TESTS = ["telemetry_endpoint_reachable", "plane_metrics_present", "samples_recent"]

ASPECT_TESTS: dict[str, list[str]] = {
    "north_south_network_telemetry": _PLANE_TESTS,
    "east_west_network_telemetry": _PLANE_TESTS,
    "management_network_telemetry": _PLANE_TESTS,
    "nvswitch_fabric_telemetry": _PLANE_TESTS,
    "host_nic_network_telemetry": ["telemetry_endpoint_reachable", "nic_metrics_present", "samples_recent"],
}

NETWORK_PLANES = {
    "north_south_network_telemetry": "north_south",
    "east_west_network_telemetry": "east_west",
    "management_network_telemetry": "management",
    "nvswitch_fabric_telemetry": "nvswitch_fabric",
    "host_nic_network_telemetry": "host_nic",
}

HIDDEN_ASPECTS = {
    "east_west_network_telemetry",
    "management_network_telemetry",
    "nvswitch_fabric_telemetry",
}

AWS_NO_CUSTOMER_FABRIC_MESSAGE = (
    "AWS EC2/EKS tenants do not receive customer-accessible fabric or management-plane telemetry"
)

PACKET_METRICS = ["NetworkPacketsIn", "NetworkPacketsOut"]

# A freshly launched host has no CloudWatch datapoints until the first metric is
# ingested, so the customer-visible probes poll until samples appear rather than
# taking a single-shot reading right after launch.
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
        "probes": {"sample_count": 0, "telemetry_source": "", "metric_names": []},
        "message": (
            f"{test_name}: {AWS_NO_CUSTOMER_FABRIC_MESSAGE} in region {region}; fabric plane is provider-owned."
        ),
    }


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
    """Poll CloudWatch until recent samples appear or the timeout elapses.

    A freshly launched instance appears in ListMetrics only after its first
    datapoint is ingested, so the metric list is refreshed while it is empty.
    """
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


def _count_instance_nics(ec2: Any, instance_id: str) -> int:
    """Return the number of network interfaces attached to an instance."""
    if not instance_id:
        return 0
    reservations = ec2.describe_instances(InstanceIds=[instance_id]).get("Reservations", [])
    return sum(
        len(instance.get("NetworkInterfaces", []))
        for reservation in reservations
        for instance in reservation.get("Instances", [])
    )


def _count_metric_nics(metrics: list[dict[str, Any]]) -> int:
    """Return the number of distinct dimension sets across the given metrics."""
    return len(
        {
            tuple((dimension.get("Name", ""), dimension.get("Value", "")) for dimension in metric.get("Dimensions", []))
            for metric in metrics
        }
    )


def _check_plane_telemetry(
    cloudwatch: Any,
    *,
    aspect: str,
    network_id: str,
    instance_id: str = "",
    ec2: Any = None,
    poll_timeout_seconds: int = 0,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Validate customer-visible packet telemetry for a network plane.

    When ``instance_id`` is provided the probe is scoped to that instance so it
    measures the host under test rather than unrelated account instances, and it
    polls until samples appear to absorb CloudWatch ingestion delay. AWS EC2
    publishes packet metrics at the instance level rather than per-ENI, so the
    host NIC aspect reads that same instance packet telemetry and reports the
    attached NIC count (via ``ec2``) as evidence.
    """
    result = _base_result(aspect)
    host_nic = aspect == "host_nic_network_telemetry"
    metrics_present_test = ASPECT_TESTS[aspect][1]
    if instance_id:
        dimensions = [{"Name": "InstanceId", "Value": instance_id}]
    else:
        dimensions = [{"Name": "NetworkInterfaceId" if host_nic else "InstanceId"}]
    probes = {
        "telemetry_source": "cloudwatch",
        "metric_names": PACKET_METRICS,
        "sample_count": 0,
        "latest_timestamp": "",
        "probe_resource_id": instance_id or network_id,
    }
    if host_nic:
        probes["nics_checked"] = 0

    def list_metrics() -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        for metric_name in PACKET_METRICS:
            metrics.extend(
                cloudwatch.list_metrics(Namespace="AWS/EC2", MetricName=metric_name, Dimensions=dimensions).get(
                    "Metrics", []
                )
            )
        return metrics

    try:
        metrics = list_metrics()
        if host_nic:
            probes["nics_checked"] = (
                _count_instance_nics(ec2, instance_id)
                if instance_id and ec2 is not None
                else _count_metric_nics(metrics)
            )
    except ClientError:
        error = "AWS API error while querying CloudWatch metrics"
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    result["tests"]["telemetry_endpoint_reachable"] = _passed(
        f"CloudWatch metrics endpoint reachable ({len(metrics)} packet metric(s) visible)", probes
    )

    metrics, sample_count, latest_timestamp = _poll_recent_samples(
        metrics,
        list_metrics,
        cloudwatch,
        poll_timeout_seconds=poll_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )
    probes = {**probes, "sample_count": sample_count, "latest_timestamp": latest_timestamp}

    if metrics:
        result["tests"][metrics_present_test] = _passed("Packet telemetry metrics are configured", probes)
        if sample_count > 0:
            result["tests"]["samples_recent"] = _passed(
                f"{sample_count} recent packet telemetry sample(s) found", probes
            )
        else:
            result["tests"]["samples_recent"] = _failed(
                f"No recent packet telemetry samples found in the last {SAMPLE_WINDOW_SECONDS} seconds", probes
            )
    else:
        result["tests"][metrics_present_test] = _failed("No packet telemetry metrics are configured", probes)
        result["tests"]["samples_recent"] = _failed("No packet telemetry metrics available to sample", probes)

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = f"{NETWORK_PLANES[aspect]} network telemetry checks failed"
    return result


def _check_hidden_plane_telemetry(*, aspect: str, region: str) -> dict[str, Any]:
    """Emit provider-hidden evidence for tenant-inaccessible network planes."""
    result = _base_result(aspect)
    result["success"] = True
    result["tests"] = {name: _provider_hidden(name, region=region) for name in ASPECT_TESTS[aspect]}
    return result


@handle_aws_errors
def main() -> int:
    """Run the selected AWS network telemetry probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="AWS network telemetry availability test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--network-id", default="")
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
        result = _check_hidden_plane_telemetry(aspect=args.aspect, region=args.region)
    else:
        needs_nic_count = args.aspect == "host_nic_network_telemetry" and args.instance_id
        result = _check_plane_telemetry(
            boto3.client("cloudwatch", region_name=args.region),
            aspect=args.aspect,
            network_id=args.network_id,
            instance_id=args.instance_id,
            ec2=boto3.client("ec2", region_name=args.region) if needs_nic_count else None,
            poll_timeout_seconds=args.poll_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
