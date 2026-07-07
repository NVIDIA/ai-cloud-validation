#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS network-plane telemetry availability probes."""

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

ASPECT_TESTS: dict[str, list[str]] = {
    "north_south_network_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "east_west_network_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "management_network_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "nvswitch_fabric_telemetry": [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ],
    "host_nic_network_telemetry": [
        "telemetry_endpoint_reachable",
        "nic_metrics_present",
        "samples_recent",
    ],
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

SAMPLE_WINDOW_SECONDS = 600
PACKET_METRICS = ["NetworkPacketsIn", "NetworkPacketsOut"]


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "network_plane": NETWORK_PLANES[aspect],
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


def _provider_hidden(test_name: str, *, region: str, probe_field: str) -> dict[str, Any]:
    """Build a passing provider-hidden subtest result."""
    return {
        "passed": True,
        "provider_hidden": True,
        "probes": {probe_field: 0, "telemetry_source": "", "metric_names": [], "sample_count": 0},
        "message": (
            f"{test_name}: {AWS_NO_CUSTOMER_FABRIC_MESSAGE} in region {region}; fabric plane is provider-owned."
        ),
    }


def _newest_datapoint_timestamp(datapoints: list[dict[str, Any]]) -> str:
    """Return the ISO timestamp for the newest datapoint."""
    if not datapoints:
        return ""
    newest = max(datapoints, key=lambda point: point["Timestamp"])
    timestamp = newest["Timestamp"]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.isoformat()


def _list_target_metrics(cloudwatch: Any, *, dimension_name: str) -> list[dict[str, Any]]:
    """Return packet metrics for the requested CloudWatch dimension."""
    metrics: list[dict[str, Any]] = []
    for metric_name in PACKET_METRICS:
        response = cloudwatch.list_metrics(
            Namespace="AWS/EC2",
            MetricName=metric_name,
            Dimensions=[{"Name": dimension_name}],
        )
        metrics.extend(response.get("Metrics", []))
    return metrics


def _count_recent_samples(cloudwatch: Any, metric: dict[str, Any], *, start_time: datetime, end_time: datetime) -> int:
    """Return the number of recent datapoints for a metric."""
    response = cloudwatch.get_metric_statistics(
        Namespace=metric["Namespace"],
        MetricName=metric["MetricName"],
        Dimensions=metric.get("Dimensions", []),
        StartTime=start_time,
        EndTime=end_time,
        Period=60,
        Statistics=["Sum"],
    )
    return len(response.get("Datapoints", []))


def _check_plane_telemetry(
    cloudwatch: Any,
    *,
    aspect: str,
    region: str,
    network_id: str,
    dimension_name: str,
) -> dict[str, Any]:
    """Validate customer-visible packet telemetry for a network plane."""
    result = _base_result(aspect)
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(seconds=SAMPLE_WINDOW_SECONDS)
    probes = {
        "telemetry_source": "cloudwatch",
        "metric_names": PACKET_METRICS,
        "sample_count": 0,
        "latest_timestamp": "",
        "probe_resource_id": network_id,
    }

    try:
        metrics = _list_target_metrics(cloudwatch, dimension_name=dimension_name)
        result["tests"]["telemetry_endpoint_reachable"] = _passed(
            f"CloudWatch metrics endpoint reachable ({len(metrics)} packet metric(s) visible)", probes
        )
    except ClientError:
        error = "AWS API error while querying CloudWatch metrics"
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    if metrics:
        result["tests"]["plane_metrics_present"] = _passed("Packet telemetry metrics are configured", probes)
        sample_count = 0
        latest_timestamp = ""
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
            if datapoints:
                sample_count += len(datapoints)
                candidate = _newest_datapoint_timestamp(datapoints)
                if candidate and (not latest_timestamp or candidate > latest_timestamp):
                    latest_timestamp = candidate
        probes = {
            **probes,
            "sample_count": sample_count,
            "latest_timestamp": latest_timestamp,
        }
        if sample_count > 0:
            result["tests"]["samples_recent"] = _passed(
                f"{sample_count} recent packet telemetry sample(s) found", probes
            )
        else:
            result["tests"]["samples_recent"] = _failed(
                f"No recent packet telemetry samples found in the last {SAMPLE_WINDOW_SECONDS} seconds", probes
            )
    else:
        result["tests"]["plane_metrics_present"] = _failed("No packet telemetry metrics are configured", probes)
        result["tests"]["samples_recent"] = _failed("No packet telemetry metrics available to sample", probes)

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = f"{NETWORK_PLANES[aspect]} network telemetry checks failed"
    return result


def _check_hidden_plane_telemetry(*, aspect: str, region: str) -> dict[str, Any]:
    """Emit provider-hidden evidence for tenant-inaccessible network planes."""
    result = _base_result(aspect)
    result["success"] = True
    result["tests"] = {
        name: _provider_hidden(name, region=region, probe_field="sample_count") for name in ASPECT_TESTS[aspect]
    }
    return result


def _check_host_nic_telemetry(cloudwatch: Any, *, region: str, network_id: str) -> dict[str, Any]:
    """Validate host NIC-level packet telemetry."""
    aspect = "host_nic_network_telemetry"
    result = _base_result(aspect)
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(seconds=SAMPLE_WINDOW_SECONDS)
    probes = {
        "telemetry_source": "cloudwatch",
        "metric_names": PACKET_METRICS,
        "nics_checked": 0,
        "sample_count": 0,
        "latest_timestamp": "",
        "probe_resource_id": network_id,
    }

    try:
        metrics = _list_target_metrics(cloudwatch, dimension_name="NetworkInterfaceId")
        probes["nics_checked"] = len({tuple(metric.get("Dimensions", [])) for metric in metrics})
        result["tests"]["telemetry_endpoint_reachable"] = _passed(
            f"CloudWatch metrics endpoint reachable ({len(metrics)} NIC packet metric(s) visible)", probes
        )
    except ClientError:
        error = "AWS API error while querying CloudWatch metrics"
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    if metrics:
        result["tests"]["nic_metrics_present"] = _passed("Host NIC packet telemetry metrics are configured", probes)
        sample_count = 0
        latest_timestamp = ""
        for metric in metrics[:20]:
            count = _count_recent_samples(cloudwatch, metric, start_time=start_time, end_time=end_time)
            if count:
                sample_count += count
                response = cloudwatch.get_metric_statistics(
                    Namespace=metric["Namespace"],
                    MetricName=metric["MetricName"],
                    Dimensions=metric.get("Dimensions", []),
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=60,
                    Statistics=["Sum"],
                )
                candidate = _newest_datapoint_timestamp(response.get("Datapoints", []))
                if candidate and (not latest_timestamp or candidate > latest_timestamp):
                    latest_timestamp = candidate
        probes = {**probes, "sample_count": sample_count, "latest_timestamp": latest_timestamp}
        if sample_count > 0:
            result["tests"]["samples_recent"] = _passed(
                f"{sample_count} recent host NIC telemetry sample(s) found", probes
            )
        else:
            result["tests"]["samples_recent"] = _failed(
                f"No recent host NIC telemetry samples found in the last {SAMPLE_WINDOW_SECONDS} seconds", probes
            )
    else:
        result["tests"]["nic_metrics_present"] = _failed("No host NIC packet telemetry metrics are configured", probes)
        result["tests"]["samples_recent"] = _failed("No host NIC telemetry metrics available to sample", probes)

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "Host NIC network telemetry checks failed"
    return result


@handle_aws_errors
def main() -> int:
    """Run the selected AWS network telemetry probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="AWS network telemetry availability test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--network-id", default="")
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_TESTS))
    args = parser.parse_args()

    cloudwatch = boto3.client("cloudwatch", region_name=args.region)
    if args.aspect in HIDDEN_ASPECTS:
        result = _check_hidden_plane_telemetry(aspect=args.aspect, region=args.region)
    elif args.aspect == "host_nic_network_telemetry":
        result = _check_host_nic_telemetry(cloudwatch, region=args.region, network_id=args.network_id)
    else:
        result = _check_plane_telemetry(
            cloudwatch,
            aspect=args.aspect,
            region=args.region,
            network_id=args.network_id,
            dimension_name="InstanceId",
        )

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
