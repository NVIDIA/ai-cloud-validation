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

"""Shared CloudWatch metric-scan helpers for AWS observability probes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

SAMPLE_WINDOW_SECONDS = 600
MAX_SCANNED_METRICS = 20


def scan_recent_datapoints(
    cloudwatch: Any,
    metrics: list[dict[str, Any]],
    *,
    window_seconds: int = SAMPLE_WINDOW_SECONDS,
) -> list[list[dict[str, Any]]]:
    """Return recent 1-minute Sum datapoints for each scanned metric.

    Scans at most ``MAX_SCANNED_METRICS`` metrics, skips metrics whose
    statistics query fails, and omits metrics with no datapoints.
    """
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(seconds=window_seconds)
    batches: list[list[dict[str, Any]]] = []
    for metric in metrics[:MAX_SCANNED_METRICS]:
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
            batches.append(datapoints)
    return batches


def newest_datapoint_timestamp(datapoints: list[dict[str, Any]]) -> datetime | None:
    """Return the timezone-aware timestamp of the newest datapoint, or None."""
    if not datapoints:
        return None
    timestamp = max(datapoints, key=lambda point: point["Timestamp"])["Timestamp"]
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp
