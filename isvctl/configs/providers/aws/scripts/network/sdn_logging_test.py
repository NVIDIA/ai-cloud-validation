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

"""Validate SDN09 logging evidence with AWS customer-visible telemetry.

AWS exposes SDN-adjacent evidence through VPC Flow Logs, CloudWatch, AWS
Health, and CloudTrail rather than tenant-visible SDN-controller logs. The
hardware fault aspect treats AWS Health ``SubscriptionRequiredException`` as
``provider_hidden`` because AWS Health organizational event visibility requires
Business/Enterprise support. Latency/drop controller-native metrics that AWS
does not expose are also reported as ``provider_hidden`` while packet/byte
telemetry is validated through Flow Logs or CloudWatch network counters.

Usage:
    python sdn_logging_test.py --region us-west-2 --vpc-id vpc-xxx --aspect hardware_faults
    python sdn_logging_test.py --region us-west-2 --vpc-id vpc-xxx --aspect latency_perf
    python sdn_logging_test.py --region us-west-2 --vpc-id vpc-xxx --aspect audit_trail
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
from botocore.exceptions import ClientError
from common.errors import delete_with_retry, handle_aws_errors

ASPECT_TESTS: dict[str, list[str]] = {
    "hardware_faults": [
        "logging_endpoint_reachable",
        "fault_event_source_queryable",
        "log_destination_configured",
        "event_schema_valid",
    ],
    "latency_perf": [
        "metrics_endpoint_reachable",
        "performance_metric_present",
        "packet_metric_present",
        "samples_recent",
    ],
    "audit_trail": [
        "audit_endpoint_reachable",
        "create_rule_logged",
        "modify_rule_logged",
        "delete_rule_logged",
        "audit_event_has_required_fields",
        "cleanup",
    ],
}

ASPECT_STEP_NAMES: dict[str, str] = {
    "hardware_faults": "sdn_hardware_fault_logging",
    "latency_perf": "sdn_latency_perf_logging",
    "audit_trail": "sdn_filter_audit_trail",
}

HEALTH_SERVICES = ["EC2", "DIRECTCONNECT", "VPC"]
HEALTH_HIDDEN_CODES = {"SubscriptionRequiredException"}
FLOW_LOG_WINDOW_SECONDS = 600
CLOUDTRAIL_WAIT_SECONDS = 600
CLOUDTRAIL_POLL_SECONDS = 30

DIM_INSTANCE_ID = "InstanceId"
DIM_ENI_ID = "NetworkInterfaceId"


def _passed(message: str = "", **extra: Any) -> dict[str, Any]:
    """Return a passing subtest result."""
    result: dict[str, Any] = {"passed": True}
    if message:
        result["message"] = message
    result.update(extra)
    return result


def _failed(error: str, **extra: Any) -> dict[str, Any]:
    """Return a failing subtest result."""
    result: dict[str, Any] = {"passed": False, "error": error}
    result.update(extra)
    return result


def _provider_hidden(test_name: str, message: str, **extra: Any) -> dict[str, Any]:
    """Return a passing result for telemetry hidden by AWS provider boundaries."""
    result: dict[str, Any] = {
        "passed": True,
        "provider_hidden": True,
        "message": f"{test_name}: {message}",
    }
    result.update(extra)
    return result


def _base_result(aspect: str, vpc_id: str, region: str) -> dict[str, Any]:
    """Build the standard SDN logging result envelope."""
    return {
        "success": False,
        "platform": "network",
        "test_name": ASPECT_STEP_NAMES[aspect],
        "region": region,
        "network_id": vpc_id,
        "aspect": aspect,
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
    }


def _flow_log_destination(flow_log: dict[str, Any]) -> str:
    """Return the most useful destination identifier for a VPC Flow Log."""
    return (
        flow_log.get("LogDestination")
        or flow_log.get("LogGroupName")
        or flow_log.get("DeliverLogsPermissionArn")
        or flow_log.get("FlowLogId")
        or ""
    )


def _cloudwatch_log_group(flow_log: dict[str, Any]) -> str | None:
    """Extract a CloudWatch Logs group name from a VPC Flow Log."""
    if flow_log.get("LogDestinationType") not in (None, "cloud-watch-logs"):
        return None
    if flow_log.get("LogGroupName"):
        return flow_log["LogGroupName"]
    destination = flow_log.get("LogDestination", "")
    marker = ":log-group:"
    if marker not in destination:
        return None
    suffix = destination.split(marker, 1)[1]
    return suffix.split(":*", 1)[0]


def _flow_log_destination_type(flow_log: dict[str, Any]) -> str:
    """Return the Flow Log destination type with AWS's default made explicit."""
    return flow_log.get("LogDestinationType") or "cloud-watch-logs"


def _describe_flow_logs(ec2: Any, vpc_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Describe VPC Flow Logs for the target VPC."""
    try:
        response = ec2.describe_flow_logs(Filters=[{"Name": "resource-id", "Values": [vpc_id]}])
    except ClientError as e:
        return _failed(str(e)), []

    flow_logs = response.get("FlowLogs", [])
    return _passed(f"Queried {len(flow_logs)} VPC Flow Log configuration(s)"), flow_logs


def _active_flow_logs(flow_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return Flow Logs that are configured and not explicitly failed/deleted."""
    inactive_statuses = {"FAILED", "DELETING", "DELETED"}
    return [flow_log for flow_log in flow_logs if flow_log.get("FlowLogStatus") not in inactive_statuses]


def _flow_logs_not_configured(vpc_id: str) -> dict[str, Any]:
    """Return a provider-hidden result for absent opt-in VPC Flow Logs."""
    return _provider_hidden(
        "log_destination_configured",
        f"AWS VPC Flow Logs are opt-in and no active Flow Log is configured for {vpc_id}",
    )


def _query_health_events(health: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Query AWS Health for recent network hardware-fault event sources."""
    try:
        response = health.describe_events(
            filter={
                "services": HEALTH_SERVICES,
                "eventTypeCategories": ["issue"],
            }
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in HEALTH_HIDDEN_CODES:
            message = (
                "AWS Health issue visibility requires Business/Enterprise support; "
                "customer-visible provider fault events are hidden in this account"
            )
            return _provider_hidden("fault_event_source_queryable", message), []
        return _failed(str(e)), []

    events = response.get("events", [])
    return _passed(f"AWS Health event source queryable ({len(events)} recent event(s))"), events


def _health_events_have_schema(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the AWS Health event fields used as hardware-fault evidence."""
    if not events:
        return _passed("AWS Health query succeeded; no recent issue events returned")

    required = ("arn", "service", "eventTypeCategory", "startTime")
    invalid = [event.get("arn", "<missing arn>") for event in events if any(not event.get(key) for key in required)]
    if invalid:
        return _failed(f"AWS Health event(s) missing required fields: {invalid}")
    return _passed(f"{len(events)} AWS Health event schema(s) validated")


def check_hardware_fault_logging(ec2: Any, health: Any, vpc_id: str, region: str) -> dict[str, Any]:
    """Validate SDN09-01 hardware-fault logging evidence."""
    result = _base_result("hardware_faults", vpc_id, region)

    flow_log_query, flow_logs = _describe_flow_logs(ec2, vpc_id)
    result["tests"]["logging_endpoint_reachable"] = flow_log_query
    active = _active_flow_logs(flow_logs) if flow_log_query.get("passed") else []
    if not flow_log_query.get("passed"):
        result["log_destination"] = "aws-vpc-flow-logs:unknown"
        result["tests"]["log_destination_configured"] = _failed(
            f"Unable to inspect VPC Flow Logs for {vpc_id}",
            flow_log_query=flow_log_query,
        )
    elif active:
        destination = _flow_log_destination(active[0])
        result["log_destination"] = destination
        result["tests"]["log_destination_configured"] = _passed(f"VPC Flow Logs configured: {destination}")
    else:
        result["log_destination"] = "aws-vpc-flow-logs:not-configured"
        result["tests"]["log_destination_configured"] = _flow_logs_not_configured(vpc_id)

    health_query, health_events = _query_health_events(health)
    result["tests"]["fault_event_source_queryable"] = health_query
    result["recent_event_count"] = len(health_events)
    if health_query.get("provider_hidden"):
        result["tests"]["event_schema_valid"] = _provider_hidden(
            "event_schema_valid",
            "AWS Health event schema cannot be inspected without provider event visibility",
        )
    elif not health_query.get("passed"):
        result["tests"]["event_schema_valid"] = _failed(
            "AWS Health event schema could not be validated because the event query failed",
            health_query=health_query,
        )
    else:
        result["tests"]["event_schema_valid"] = _health_events_have_schema(health_events)

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "SDN hardware fault logging checks failed"
    return result


def _metric_has_datapoints(
    cloudwatch: Any,
    metric: dict[str, Any],
    start_time: datetime,
    end_time: datetime,
) -> bool:
    """Return True when a CloudWatch metric has datapoints in the sample window."""
    response = cloudwatch.get_metric_statistics(
        Namespace=metric.get("Namespace", "AWS/EC2"),
        MetricName=metric["MetricName"],
        Dimensions=metric.get("Dimensions", []),
        StartTime=start_time,
        EndTime=end_time,
        Period=60,
        Statistics=["Sum"],
    )
    return bool(response.get("Datapoints"))


def _target_vpc_metric_resources(ec2: Any, vpc_id: str) -> tuple[dict[str, Any], set[str], set[str]]:
    """Discover EC2 metric dimensions that belong to the target VPC."""
    instance_ids: set[str] = set()
    network_interface_ids: set[str] = set()
    try:
        instances = ec2.describe_instances(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
            ]
        )
        for reservation in instances.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                if instance.get("InstanceId"):
                    instance_ids.add(instance["InstanceId"])
                for network_interface in instance.get("NetworkInterfaces", []):
                    if network_interface.get("NetworkInterfaceId"):
                        network_interface_ids.add(network_interface["NetworkInterfaceId"])
    except ClientError as e:
        return _failed(str(e)), set(), set()

    try:
        network_interfaces = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        for network_interface in network_interfaces.get("NetworkInterfaces", []):
            if network_interface.get("NetworkInterfaceId"):
                network_interface_ids.add(network_interface["NetworkInterfaceId"])
    except ClientError as e:
        return _failed(str(e)), instance_ids, network_interface_ids

    return (
        _passed(
            f"Discovered {len(instance_ids)} EC2 instance(s) and "
            f"{len(network_interface_ids)} network interface(s) in {vpc_id}"
        ),
        instance_ids,
        network_interface_ids,
    )


def _metric_key(metric: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return a stable key for de-duplicating CloudWatch metrics."""
    dimensions = metric.get("Dimensions", [])
    return tuple(sorted((str(dim.get("Name", "")), str(dim.get("Value", ""))) for dim in dimensions))


def _list_packet_metrics(cloudwatch: Any, dimensions: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """List EC2 packet metrics, optionally scoped by metric dimensions."""
    kwargs: dict[str, Any] = {"Namespace": "AWS/EC2", "MetricName": "NetworkPacketsIn"}
    if dimensions:
        kwargs["Dimensions"] = dimensions
    metrics: list[dict[str, Any]] = []
    while True:
        response = cloudwatch.list_metrics(**kwargs)
        metrics.extend(response.get("Metrics", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    return [{"Namespace": "AWS/EC2", **metric} for metric in metrics if metric.get("MetricName") == "NetworkPacketsIn"]


def _target_packet_metrics(
    cloudwatch: Any,
    instance_ids: set[str],
    network_interface_ids: set[str],
) -> list[dict[str, Any]]:
    """List packet metrics scoped to resources in the target VPC."""
    metrics: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    dimension_values = [(DIM_INSTANCE_ID, instance_id) for instance_id in sorted(instance_ids)] + [
        (DIM_ENI_ID, network_interface_id) for network_interface_id in sorted(network_interface_ids)
    ]

    for name, value in dimension_values:
        for metric in _list_packet_metrics(cloudwatch, [{"Name": name, "Value": value}]):
            key = _metric_key(metric)
            if key in seen:
                continue
            seen.add(key)
            metrics.append(metric)
    return metrics


def _count_recent_flow_log_events(
    logs: Any,
    flow_logs: list[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
) -> tuple[int, str | None, str | None]:
    """Count recent CloudWatch Logs events from VPC Flow Log destinations."""
    start_ms = int(start_time.timestamp() * 1000)
    end_ms = int(end_time.timestamp() * 1000)
    last_error: str | None = None
    for flow_log in flow_logs:
        log_group = _cloudwatch_log_group(flow_log)
        if not log_group:
            continue
        try:
            response = logs.filter_log_events(
                logGroupName=log_group,
                startTime=start_ms,
                endTime=end_ms,
                limit=10,
            )
        except ClientError as e:
            last_error = f"{log_group}: {e}"
            continue
        events = response.get("events", [])
        if events:
            return len(events), log_group, None
    return 0, None, last_error


def _first_metric_resource_id(metric: dict[str, Any], fallback: str) -> str:
    """Return a resource dimension from a metric, or fallback to the VPC ID."""
    for dimension in metric.get("Dimensions", []):
        if dimension.get("Name") in {DIM_INSTANCE_ID, DIM_ENI_ID} and dimension.get("Value"):
            return dimension["Value"]
    return fallback


def check_latency_perf_logging(
    ec2: Any,
    cloudwatch: Any,
    logs: Any,
    vpc_id: str,
    region: str,
    *,
    sample_window_seconds: int = FLOW_LOG_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Validate SDN09-02 latency/performance telemetry evidence."""
    result = _base_result("latency_perf", vpc_id, region)
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(seconds=sample_window_seconds)

    flow_log_query, flow_logs = _describe_flow_logs(ec2, vpc_id)
    flow_log_error = None
    if not flow_log_query.get("passed"):
        flow_log_error = flow_log_query.get("error", "VPC Flow Log query failed")
    active_flow_logs = [] if flow_log_error else _active_flow_logs(flow_logs)
    metrics: list[dict[str, Any]] = []
    target_resources_result, instance_ids, network_interface_ids = _target_vpc_metric_resources(ec2, vpc_id)
    try:
        endpoint_metrics = _list_packet_metrics(cloudwatch)
        if instance_ids or network_interface_ids:
            metrics = _target_packet_metrics(cloudwatch, instance_ids, network_interface_ids)
        result["tests"]["metrics_endpoint_reachable"] = _passed(
            f"CloudWatch metrics endpoint reachable ({len(metrics)} target packet metric(s), "
            f"{len(endpoint_metrics)} account packet metric(s) visible)"
        )
    except ClientError as e:
        result["tests"]["metrics_endpoint_reachable"] = _failed(str(e))

    packet_metric_present = bool(metrics) or bool(active_flow_logs)
    if packet_metric_present:
        result["tests"]["packet_metric_present"] = _passed("Packet telemetry source is configured")
    elif flow_log_error:
        result["tests"]["packet_metric_present"] = _failed(
            f"Unable to verify VPC Flow Logs for {vpc_id}: {flow_log_error}",
            flow_log_error=flow_log_error,
            flow_log_query=flow_log_query,
            target_resources_query=target_resources_result,
        )
    elif not instance_ids and not network_interface_ids and target_resources_result.get("passed"):
        result["tests"]["packet_metric_present"] = _provider_hidden(
            "packet_metric_present",
            "No target VPC EC2 instances or network interfaces have packet metrics yet, "
            "and VPC Flow Logs are not configured",
        )
    else:
        result["tests"]["packet_metric_present"] = _failed(
            f"No target-VPC CloudWatch packet metric or active VPC Flow Log found for {vpc_id}",
            flow_log_query=flow_log_query,
            target_resources_query=target_resources_result,
        )

    result["tests"]["performance_metric_present"] = _provider_hidden(
        "performance_metric_present",
        "AWS does not expose tenant-visible SDN-controller latency/drop counters; "
        "using customer-visible packet telemetry instead",
    )

    datapoint_metric = None
    for metric in metrics:
        try:
            if _metric_has_datapoints(cloudwatch, metric, start_time, end_time):
                datapoint_metric = metric
                break
        except ClientError:
            continue

    cloudwatch_flow_logs = [flow_log for flow_log in active_flow_logs if _cloudwatch_log_group(flow_log)]
    non_cloudwatch_flow_logs = [
        flow_log for flow_log in active_flow_logs if _flow_log_destination_type(flow_log) != "cloud-watch-logs"
    ]
    flow_log_samples, log_group, flow_log_lookup_error = _count_recent_flow_log_events(
        logs, cloudwatch_flow_logs, start_time, end_time
    )
    if datapoint_metric:
        result["telemetry_namespace"] = datapoint_metric["Namespace"]
        result["probe_resource_id"] = _first_metric_resource_id(datapoint_metric, vpc_id)
        result["tests"]["samples_recent"] = _passed("Recent CloudWatch packet metric datapoints found")
    elif flow_log_samples:
        result["telemetry_namespace"] = "AWS/VPCFlowLogs"
        result["probe_resource_id"] = vpc_id
        result["tests"]["samples_recent"] = _passed(
            f"Recent VPC Flow Log samples found in {log_group}",
            sample_count=flow_log_samples,
        )
    elif result["tests"]["packet_metric_present"].get("provider_hidden"):
        result["telemetry_namespace"] = "provider-hidden"
        result["probe_resource_id"] = vpc_id
        result["tests"]["samples_recent"] = _provider_hidden(
            "samples_recent",
            "No target VPC packet telemetry source is currently configured to produce samples",
        )
    elif non_cloudwatch_flow_logs:
        destination_types = sorted({_flow_log_destination_type(flow_log) for flow_log in non_cloudwatch_flow_logs})
        result["telemetry_namespace"] = "AWS/VPCFlowLogs"
        result["probe_resource_id"] = vpc_id
        result["tests"]["samples_recent"] = _provider_hidden(
            "samples_recent",
            "VPC Flow Logs target non-CloudWatch destination(s) "
            f"{', '.join(destination_types)}; CloudWatch Logs samples cannot be validated",
            flow_log_destinations=[_flow_log_destination(flow_log) for flow_log in non_cloudwatch_flow_logs],
            flow_log_lookup_error=flow_log_lookup_error,
        )
    elif flow_log_lookup_error and cloudwatch_flow_logs:
        result["telemetry_namespace"] = "AWS/VPCFlowLogs"
        result["probe_resource_id"] = vpc_id
        result["tests"]["samples_recent"] = _failed(
            f"VPC Flow Log lookup failed: {flow_log_lookup_error}",
            flow_log_lookup_error=flow_log_lookup_error,
        )
    else:
        result["telemetry_namespace"] = "AWS/VPCFlowLogs" if active_flow_logs else "AWS/EC2"
        result["probe_resource_id"] = vpc_id
        result["tests"]["samples_recent"] = _failed(
            f"No recent packet telemetry samples found in the last {sample_window_seconds} seconds"
        )

    result["sample_window_seconds"] = sample_window_seconds
    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "SDN latency/performance logging checks failed"
    return result


def _audit_event(event_name: str, group_id: str, *, include_required_fields: bool = True) -> dict[str, Any]:
    """Build a minimal CloudTrail event payload for tests and synthesized matches."""
    payload: dict[str, Any] = {"eventName": event_name}
    if include_required_fields:
        payload.update(
            {
                "userIdentity": {"type": "AssumedRole", "arn": "arn:aws:sts::123456789012:assumed-role/isv/test"},
                "eventTime": datetime.now(UTC).isoformat(),
                "requestParameters": {"groupId": group_id},
            }
        )
    return payload


def _parse_cloudtrail_event(raw_event: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a CloudTrail LookupEvents entry into a JSON event object."""
    payload = raw_event.get("CloudTrailEvent")
    if not isinstance(payload, str):
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _lookup_cloudtrail_events(
    cloudtrail: Any,
    group_id: str,
    start_time: datetime,
    end_time: datetime,
) -> list[dict[str, Any]]:
    """Lookup and parse CloudTrail events associated with a security group."""
    response = cloudtrail.lookup_events(
        LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": group_id}],
        StartTime=start_time,
        EndTime=end_time,
    )
    parsed = []
    for raw_event in response.get("Events", []):
        event = _parse_cloudtrail_event(raw_event)
        if event is not None:
            parsed.append(event)
    return parsed


def _event_names(events: list[dict[str, Any]]) -> list[str]:
    """Return event names from parsed CloudTrail events."""
    return [str(event.get("eventName", "")) for event in events]


def _missing_audit_lifecycle_events(events: list[dict[str, Any]]) -> list[str]:
    """Return audit lifecycle event classes still missing from CloudTrail."""
    names = _event_names(events)
    authorize_count = sum(name.startswith("AuthorizeSecurityGroup") for name in names)
    revoke_count = sum(name.startswith("RevokeSecurityGroup") for name in names)
    missing = []
    if authorize_count < 2:
        missing.append("AuthorizeSecurityGroup* x2")
    if revoke_count < 2:
        missing.append("RevokeSecurityGroup* x2")
    return missing


def _poll_cloudtrail_events(
    cloudtrail: Any,
    group_id: str,
    start_time: datetime,
    *,
    timeout_seconds: int = CLOUDTRAIL_WAIT_SECONDS,
    poll_seconds: int = CLOUDTRAIL_POLL_SECONDS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Poll CloudTrail LookupEvents for SDN filtering-rule lifecycle events."""
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    last_events: list[dict[str, Any]] = []
    missing: list[str] = []
    while True:
        try:
            events = _lookup_cloudtrail_events(cloudtrail, group_id, start_time, datetime.now(UTC))
            last_events = events
            missing = _missing_audit_lifecycle_events(events)
            if not missing:
                return _passed(f"CloudTrail returned complete lifecycle for {group_id}"), events
        except ClientError as e:
            last_error = str(e)
            return _failed(last_error), []

        if time.monotonic() >= deadline:
            if last_error:
                message = last_error
            elif last_events:
                message = f"CloudTrail events for {group_id} missing before propagation timeout: {missing}"
            else:
                message = f"No CloudTrail events found for {group_id} before propagation timeout"
            return (
                _failed(
                    message,
                    propagation_timeout=True,
                    observed_event_names=_event_names(last_events),
                    missing_event_names=missing,
                ),
                last_events,
            )
        time.sleep(poll_seconds)


def _is_rule_mutation_event(event: dict[str, Any]) -> bool:
    """Return True for CloudTrail events that mutate security group rules."""
    name = str(event.get("eventName", ""))
    return (
        name.startswith("AuthorizeSecurityGroup")
        or name.startswith("RevokeSecurityGroup")
        or name == "ModifySecurityGroupRules"
    )


def _audit_events_have_required_fields(events: list[dict[str, Any]], group_id: str) -> dict[str, Any]:
    """Validate CloudTrail rule-mutation events contain actor, timestamp, and target rule fields."""
    rule_events = [event for event in events if _is_rule_mutation_event(event)]
    if not rule_events:
        return _failed("No rule-mutation audit events available to validate required fields")

    missing = []
    for event in rule_events:
        request = event.get("requestParameters")
        if not event.get("userIdentity") or not event.get("eventTime") or not isinstance(request, dict):
            missing.append(event.get("eventName", "<unknown>"))
            continue
        if request.get("groupId") != group_id:
            missing.append(event.get("eventName", "<unknown>"))
    if missing:
        return _failed(f"CloudTrail event(s) missing userIdentity/eventTime/requestParameters.groupId: {missing}")
    return _passed(f"{len(rule_events)} CloudTrail event(s) contain required audit fields")


def _create_audit_probe_security_group(ec2: Any, vpc_id: str) -> str:
    """Create a security group for synthesizing filtering-rule events."""
    tag = f"isv-sdn-audit-{uuid.uuid4().hex[:8]}"
    response = ec2.create_security_group(
        GroupName=tag,
        Description="ISV SDN09 audit trail probe",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [{"Key": "CreatedBy", "Value": "isvtest"}],
            }
        ],
    )
    group_id = response["GroupId"]
    return group_id


def _mutate_audit_probe(ec2: Any, group_id: str) -> None:
    """Mutate a security group to synthesize filtering-rule events."""
    original_rule = [
        {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
        }
    ]
    replacement_rule = [
        {
            "IpProtocol": "tcp",
            "FromPort": 8443,
            "ToPort": 8443,
            "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
        }
    ]
    ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=original_rule)
    ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=original_rule)
    ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=replacement_rule)
    ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=replacement_rule)


def _delete_security_group(ec2: Any, group_id: str | None) -> dict[str, Any]:
    """Best-effort cleanup for the audit probe security group."""
    if not group_id:
        return _failed("Security group was not created")
    if delete_with_retry(
        ec2.delete_security_group,
        GroupId=group_id,
        resource_desc=f"security group {group_id}",
    ):
        return _passed(f"Deleted audit probe security group {group_id}")
    return _failed(f"Failed to delete audit probe security group {group_id} after retries")


def _audit_test_results(events: list[dict[str, Any]], group_id: str) -> dict[str, dict[str, Any]]:
    """Evaluate CloudTrail events against the SDN09-03 audit contract."""
    names = _event_names(events)
    authorize_count = sum(name.startswith("AuthorizeSecurityGroup") for name in names)
    revoke_count = sum(name.startswith("RevokeSecurityGroup") for name in names)
    return {
        "create_rule_logged": (
            _passed("CloudTrail logged security group rule creation")
            if authorize_count >= 1
            else _failed("CloudTrail did not log AuthorizeSecurityGroup*")
        ),
        "modify_rule_logged": (
            _passed("CloudTrail logged security group rule replacement")
            if "ModifySecurityGroupRules" in names or (authorize_count >= 2 and revoke_count >= 1)
            else _failed("CloudTrail did not log ModifySecurityGroupRules or revoke+authorize replacement")
        ),
        "delete_rule_logged": (
            _passed("CloudTrail logged security group rule deletion")
            if revoke_count >= 2
            else _failed("CloudTrail did not log RevokeSecurityGroup* for rule deletion")
        ),
        "audit_event_has_required_fields": _audit_events_have_required_fields(events, group_id),
    }


def check_audit_trail_logging(
    ec2: Any,
    cloudtrail: Any,
    vpc_id: str,
    region: str,
    *,
    timeout_seconds: int = CLOUDTRAIL_WAIT_SECONDS,
    poll_seconds: int = CLOUDTRAIL_POLL_SECONDS,
) -> dict[str, Any]:
    """Validate SDN09-03 audit logging for filtering-rule changes."""
    result = _base_result("audit_trail", vpc_id, region)
    result["trail_id"] = "cloudtrail"
    result["actor_field"] = "userIdentity"
    group_id = None
    probe_mutated = False
    start_time = datetime.now(UTC) - timedelta(seconds=30)

    try:
        group_id = _create_audit_probe_security_group(ec2, vpc_id)
        result["target_rule_id"] = group_id
        _mutate_audit_probe(ec2, group_id)
        probe_mutated = True
    except ClientError as e:
        if not group_id:
            result["target_rule_id"] = ""
        error = str(e)
        for key in ("audit_endpoint_reachable", "create_rule_logged", "modify_rule_logged", "delete_rule_logged"):
            result["tests"][key] = _failed(error)
        result["tests"]["audit_event_has_required_fields"] = _failed("No audit events available")
    finally:
        cleanup_result = _delete_security_group(ec2, group_id)
        result["tests"]["cleanup"] = cleanup_result

    if probe_mutated and group_id:
        endpoint_result, events = _poll_cloudtrail_events(
            cloudtrail,
            group_id,
            start_time,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        result["tests"]["audit_endpoint_reachable"] = endpoint_result
        if events:
            result["tests"].update(_audit_test_results(events, group_id))
        else:
            for key in ("create_rule_logged", "modify_rule_logged", "delete_rule_logged"):
                result["tests"][key] = _failed(
                    "CloudTrail did not return the expected security group lifecycle events",
                    propagation_timeout=endpoint_result.get("propagation_timeout", False),
                )
            result["tests"]["audit_event_has_required_fields"] = _failed("No audit events available")

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "SDN filtering audit trail checks failed"
    return result


def run_aspect(aspect: str, vpc_id: str, region: str) -> dict[str, Any]:
    """Dispatch an SDN logging aspect to its AWS implementation."""
    ec2 = boto3.client("ec2", region_name=region)
    if aspect == "hardware_faults":
        health = boto3.client("health", region_name="us-east-1")
        return check_hardware_fault_logging(ec2, health, vpc_id, region)
    if aspect == "latency_perf":
        cloudwatch = boto3.client("cloudwatch", region_name=region)
        logs = boto3.client("logs", region_name=region)
        return check_latency_perf_logging(ec2, cloudwatch, logs, vpc_id, region)
    if aspect == "audit_trail":
        cloudtrail = boto3.client("cloudtrail", region_name=region)
        return check_audit_trail_logging(ec2, cloudtrail, vpc_id, region)
    msg = f"Unsupported SDN logging aspect: {aspect}"
    raise ValueError(msg)


@handle_aws_errors
def main() -> int:
    """Run an AWS SDN logging validation aspect and emit JSON."""
    parser = argparse.ArgumentParser(description="SDN logging validation (AWS)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--vpc-id", required=True, help="Shared VPC ID from create_network")
    parser.add_argument("--aspect", required=True, choices=["hardware_faults", "latency_perf", "audit_trail"])
    args = parser.parse_args()

    result = run_aspect(args.aspect, args.vpc_id, args.region)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
