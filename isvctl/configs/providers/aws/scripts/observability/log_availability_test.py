#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS observability log and telemetry availability tests."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.exceptions import ClientError
from common.errors import classify_aws_error, handle_aws_errors
from common.ssh_utils import ssh_run, wait_for_ssh

ASPECT_TESTS: dict[str, list[str]] = {
    "vpc_flow_logs": [
        "flow_log_endpoint_reachable",
        "flow_logs_configured",
        "traffic_type_all",
        "log_destination_accessible",
    ],
    "host_syslogs": [
        "syslog_endpoint_reachable",
        "host_log_source_present",
        "entries_recent",
    ],
    "bmc_sel_logs": [
        "sel_log_endpoint_reachable",
        "sel_log_source_present",
        "sel_entries_queryable",
    ],
    "bmc_gpu_telemetry": [
        "telemetry_endpoint_reachable",
        "gpu_metrics_present",
        "host_os_gap_identified",
        "telemetry_samples_recent",
    ],
    "ufm_event_logs": [
        "event_log_endpoint_reachable",
        "event_log_source_present",
        "event_entries_queryable",
    ],
    "general_switch_logs": [
        "log_endpoint_reachable",
        "switch_log_source_present",
        "entries_queryable",
    ],
    "switch_syslogs": [
        "syslog_endpoint_reachable",
        "switch_syslog_source_present",
        "entries_recent",
    ],
    "switch_kernel_logs": [
        "log_endpoint_reachable",
        "kernel_log_source_present",
        "entries_queryable",
    ],
}

JOURNALCTL_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{4})")
DMESG_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:,\d+)?(?:[+\-]\d{2}:?\d{2}|Z)?)")
AWS_NO_CUSTOMER_BMC_MESSAGE = (
    "AWS EC2/EKS tenants do not receive customer-accessible BMC SEL logs or Redfish GPU telemetry"
)
AWS_NO_CUSTOMER_FABRIC_MESSAGE = (
    "AWS EC2/EKS tenants do not receive customer-accessible UFM event logs or switch fabric logs"
)


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
    """Build a passing provider-hidden subtest result for AWS BMC observability."""
    return {
        "passed": True,
        "provider_hidden": True,
        "probes": {"bmc_endpoints_checked": 0},
        "message": (f"{test_name}: {AWS_NO_CUSTOMER_BMC_MESSAGE} in region {region}; BMC plane is provider-owned."),
    }


def _fabric_provider_hidden(
    test_name: str,
    *,
    region: str,
    probe_field: str,
) -> dict[str, Any]:
    """Build a passing provider-hidden subtest result for AWS fabric observability."""
    return {
        "passed": True,
        "provider_hidden": True,
        "probes": {probe_field: 0},
        "message": (
            f"{test_name}: {AWS_NO_CUSTOMER_FABRIC_MESSAGE} in region {region}; fabric plane is provider-owned."
        ),
    }


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
    }


def _flow_log_destination(flow_log: dict[str, Any]) -> str:
    """Return the provider-neutral destination identifier for a Flow Log."""
    return flow_log.get("LogGroupName") or flow_log.get("LogDestination") or flow_log.get("FlowLogId") or ""


def _active_flow_logs(flow_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return Flow Logs that are not in terminal or deleting states."""
    inactive_statuses = {"FAILED", "DELETING", "DELETED"}
    return [flow_log for flow_log in flow_logs if flow_log.get("FlowLogStatus") not in inactive_statuses]


def _select_flow_log(active_flow_logs: list[dict[str, Any]], flow_log_id: str) -> dict[str, Any] | None:
    """Select the requested Flow Log or fall back to the first active Flow Log."""
    if flow_log_id:
        return next((flow_log for flow_log in active_flow_logs if flow_log.get("FlowLogId") == flow_log_id), None)
    return active_flow_logs[0] if active_flow_logs else None


def _cloudwatch_log_group_exists(logs: Any, log_group_name: str) -> bool:
    """Return True when the named CloudWatch Logs group exists."""
    response = logs.describe_log_groups(logGroupNamePrefix=log_group_name)
    return any(group.get("logGroupName") == log_group_name for group in response.get("logGroups", []))


def check_vpc_flow_logs(ec2: Any, logs: Any, *, network_id: str, flow_log_id: str = "") -> dict[str, Any]:
    """Check AWS VPC Flow Logs and emit the observability contract."""
    result = _base_result("vpc_flow_logs")

    try:
        response = ec2.describe_flow_logs(Filters=[{"Name": "resource-id", "Values": [network_id]}])
    except ClientError as e:
        error_type, error = classify_aws_error(e)
        probes = {"network_id": network_id, "flow_log_id": flow_log_id}
        result["error_type"] = error_type
        result["error"] = error
        for name in ASPECT_TESTS["vpc_flow_logs"]:
            result["tests"][name] = _failed(error, probes)
        return result

    flow_logs = response.get("FlowLogs", [])
    active = _active_flow_logs(flow_logs)
    result["tests"]["flow_log_endpoint_reachable"] = _passed(f"Queried {len(flow_logs)} VPC Flow Log configuration(s)")

    if not active:
        probes = {"network_id": network_id, "flow_log_id": flow_log_id, "log_destination": "", "traffic_type": ""}
        result["tests"]["flow_logs_configured"] = _failed(f"No active VPC Flow Log configured for {network_id}", probes)
        result["tests"]["traffic_type_all"] = _failed("No active VPC Flow Log to inspect", probes)
        result["tests"]["log_destination_accessible"] = _failed("No active VPC Flow Log destination to inspect", probes)
        result["error"] = "VPC Flow Log checks failed"
        return result

    selected = _select_flow_log(active, flow_log_id)
    if selected is None:
        probes = {"network_id": network_id, "flow_log_id": flow_log_id, "log_destination": "", "traffic_type": ""}
        result["tests"]["flow_logs_configured"] = _failed(
            f"Requested VPC Flow Log {flow_log_id} is not active for {network_id}", probes
        )
        result["tests"]["traffic_type_all"] = _failed("Requested VPC Flow Log to inspect was not found", probes)
        result["tests"]["log_destination_accessible"] = _failed(
            "Requested VPC Flow Log destination was not found", probes
        )
        result["error"] = "VPC Flow Log checks failed"
        return result

    destination = _flow_log_destination(selected)
    traffic_type = str(selected.get("TrafficType", ""))
    selected_flow_log_id = str(selected.get("FlowLogId", ""))
    probes = {
        "network_id": network_id,
        "flow_log_id": selected_flow_log_id,
        "log_destination": destination,
        "traffic_type": traffic_type,
    }

    result["tests"]["flow_logs_configured"] = _passed(
        f"Active VPC Flow Log configured: {selected.get('FlowLogId')}", probes
    )
    if traffic_type.upper() == "ALL":
        result["tests"]["traffic_type_all"] = _passed("VPC Flow Log captures ALL traffic", probes)
    else:
        result["tests"]["traffic_type_all"] = _failed(
            f"VPC Flow Log traffic type is {traffic_type}, expected ALL", probes
        )

    destination_type = selected.get("LogDestinationType") or "cloud-watch-logs"
    if destination_type == "cloud-watch-logs":
        try:
            destination_ok = bool(destination) and _cloudwatch_log_group_exists(logs, destination)
        except ClientError:
            result["tests"]["log_destination_accessible"] = _failed(
                "AWS API error while checking log destination accessibility", probes
            )
        else:
            result["tests"]["log_destination_accessible"] = (
                _passed(f"CloudWatch Logs destination exists: {destination}", probes)
                if destination_ok
                else _failed(f"CloudWatch Logs destination not found: {destination}", probes)
            )
    elif destination:
        result["tests"]["log_destination_accessible"] = _passed(
            f"VPC Flow Log destination configured: {destination}", probes
        )
    else:
        result["tests"]["log_destination_accessible"] = _failed("VPC Flow Log destination is empty", probes)

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "VPC Flow Log checks failed"
    return result


def _parse_journalctl(stdout: str) -> tuple[int, str]:
    """Count journalctl lines and return the latest ISO timestamp."""
    entry_count = 0
    latest_timestamp = ""
    for line in stdout.splitlines():
        match = JOURNALCTL_ISO_TS.match(line)
        if match:
            entry_count += 1
            latest_timestamp = match.group(1)
    return entry_count, latest_timestamp


def _parse_dmesg_recent(stdout: str, *, max_age_minutes: int) -> tuple[int, str]:
    """Count recent ISO-timestamped dmesg lines and return the latest timestamp."""
    cutoff = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
    entry_count = 0
    latest_timestamp = ""
    for line in stdout.splitlines():
        match = DMESG_ISO_TS.match(line)
        if not match:
            continue
        raw = match.group(1)
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed >= cutoff:
            entry_count += 1
            latest_timestamp = raw
    return entry_count, latest_timestamp


def _sample_host_logs(host: str, ssh_user: str, key_file: str, *, max_age_minutes: int) -> tuple[str, int, str, str]:
    """Sample journalctl or dmesg over SSH and return provider-neutral evidence."""
    journal_cmd = (
        f"journalctl --since '{max_age_minutes} minutes ago' --no-pager -o short-iso 2>/dev/null | tail -n 500"
    )
    exit_code, stdout, stderr = ssh_run(host, ssh_user, key_file, journal_cmd)
    if exit_code == 0:
        entry_count, latest_timestamp = _parse_journalctl(stdout)
        if entry_count > 0:
            return "journalctl", entry_count, latest_timestamp, ""

    dmesg_cmd = "dmesg --time-format=iso 2>/dev/null | tail -n 1000"
    exit_code, stdout, stderr = ssh_run(host, ssh_user, key_file, dmesg_cmd)
    if exit_code != 0 or not stdout.strip():
        exit_code, stdout, stderr = ssh_run(
            host,
            ssh_user,
            key_file,
            "sudo -n dmesg --time-format=iso 2>/dev/null | tail -n 1000",
        )
    if exit_code == 0:
        entry_count, latest_timestamp = _parse_dmesg_recent(stdout, max_age_minutes=max_age_minutes)
        if entry_count > 0:
            return "dmesg", entry_count, latest_timestamp, ""

    return "", 0, "", stderr.strip()[:200] or "No recent journalctl or dmesg entries found"


def check_host_syslogs(host: str, ssh_user: str, key_file: str, *, max_age_minutes: int = 5) -> dict[str, Any]:
    """Check guest host syslog availability over SSH."""
    result = _base_result("host_syslogs")
    probes = {"host": host, "hosts_checked": 1, "log_source": "", "entry_count": 0, "latest_timestamp": ""}

    if not wait_for_ssh(host, ssh_user, key_file, max_attempts=20, interval=10):
        error = f"SSH did not become ready on {host}"
        result["tests"]["syslog_endpoint_reachable"] = _failed(error, probes)
        result["tests"]["host_log_source_present"] = _failed(error, probes)
        result["tests"]["entries_recent"] = _failed(error, probes)
        result["error"] = error
        return result

    result["tests"]["syslog_endpoint_reachable"] = _passed(f"SSH syslog endpoint reachable on {host}", probes)
    log_source, entry_count, latest_timestamp, error = _sample_host_logs(
        host,
        ssh_user,
        key_file,
        max_age_minutes=max_age_minutes,
    )
    probes = {
        "host": host,
        "hosts_checked": 1,
        "log_source": log_source,
        "entry_count": entry_count,
        "latest_timestamp": latest_timestamp,
    }
    if log_source:
        result["tests"]["host_log_source_present"] = _passed(f"Host log source present: {log_source}", probes)
        result["tests"]["entries_recent"] = _passed(f"{entry_count} recent host log entries found", probes)
    else:
        result["tests"]["host_log_source_present"] = _failed(error, probes)
        result["tests"]["entries_recent"] = _failed(error, probes)

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        result["error"] = "Host syslog checks failed"
    return result


def check_bmc_sel_logs(*, region: str) -> dict[str, Any]:
    """Emit AWS provider-hidden evidence for customer-inaccessible BMC SEL logs."""
    result = _base_result("bmc_sel_logs")
    result["success"] = True
    result["tests"] = {name: _provider_hidden(name, region=region) for name in ASPECT_TESTS["bmc_sel_logs"]}
    return result


def check_bmc_gpu_telemetry(*, region: str) -> dict[str, Any]:
    """Emit AWS provider-hidden evidence for customer-inaccessible BMC GPU telemetry."""
    result = _base_result("bmc_gpu_telemetry")
    result["success"] = True
    result["tests"] = {name: _provider_hidden(name, region=region) for name in ASPECT_TESTS["bmc_gpu_telemetry"]}
    return result


def check_ufm_event_logs(*, region: str) -> dict[str, Any]:
    """Emit AWS provider-hidden evidence for customer-inaccessible UFM event logs."""
    result = _base_result("ufm_event_logs")
    result["success"] = True
    result["tests"] = {
        name: _fabric_provider_hidden(name, region=region, probe_field="log_endpoints_checked")
        for name in ASPECT_TESTS["ufm_event_logs"]
    }
    return result


def check_general_switch_logs(*, region: str) -> dict[str, Any]:
    """Emit AWS provider-hidden evidence for customer-inaccessible switch logs."""
    result = _base_result("general_switch_logs")
    result["success"] = True
    result["tests"] = {
        name: _fabric_provider_hidden(name, region=region, probe_field="switches_checked")
        for name in ASPECT_TESTS["general_switch_logs"]
    }
    return result


def check_switch_syslogs(*, region: str) -> dict[str, Any]:
    """Emit AWS provider-hidden evidence for customer-inaccessible switch syslogs."""
    result = _base_result("switch_syslogs")
    result["success"] = True
    result["tests"] = {
        name: _fabric_provider_hidden(name, region=region, probe_field="switches_checked")
        for name in ASPECT_TESTS["switch_syslogs"]
    }
    return result


def check_switch_kernel_logs(*, region: str) -> dict[str, Any]:
    """Emit AWS provider-hidden evidence for customer-inaccessible switch kernel logs."""
    result = _base_result("switch_kernel_logs")
    result["success"] = True
    result["tests"] = {
        name: _fabric_provider_hidden(name, region=region, probe_field="switches_checked")
        for name in ASPECT_TESTS["switch_kernel_logs"]
    }
    return result


@handle_aws_errors
def main() -> int:
    """Run the selected AWS observability probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="AWS observability log availability test")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--network-id", default="")
    parser.add_argument("--flow-log-id", default="")
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_TESTS))
    parser.add_argument("--host", default="")
    parser.add_argument("--key-file", default="")
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--max-age-minutes", type=int, default=5)
    args = parser.parse_args()

    if args.max_age_minutes <= 0:
        print(
            json.dumps(
                {
                    "success": False,
                    "platform": "observability",
                    "test_name": args.aspect,
                    "error": "--max-age-minutes must be greater than 0",
                },
                indent=2,
            )
        )
        return 1

    if args.aspect == "vpc_flow_logs":
        result = check_vpc_flow_logs(
            boto3.client("ec2", region_name=args.region),
            boto3.client("logs", region_name=args.region),
            network_id=args.network_id,
            flow_log_id=args.flow_log_id,
        )
    elif args.aspect == "host_syslogs":
        result = check_host_syslogs(
            args.host,
            args.ssh_user,
            args.key_file,
            max_age_minutes=args.max_age_minutes,
        )
    elif args.aspect == "bmc_sel_logs":
        result = check_bmc_sel_logs(region=args.region)
    elif args.aspect == "bmc_gpu_telemetry":
        result = check_bmc_gpu_telemetry(region=args.region)
    elif args.aspect == "ufm_event_logs":
        result = check_ufm_event_logs(region=args.region)
    elif args.aspect == "general_switch_logs":
        result = check_general_switch_logs(region=args.region)
    elif args.aspect == "switch_syslogs":
        result = check_switch_syslogs(region=args.region)
    elif args.aspect == "switch_kernel_logs":
        result = check_switch_kernel_logs(region=args.region)
    else:
        raise ValueError(f"unsupported aspect: {args.aspect}")

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
