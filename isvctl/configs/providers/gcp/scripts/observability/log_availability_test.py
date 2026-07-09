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

"""GCP observability log and telemetry availability tests (test phase).

The GCP port of the AWS oracle ``log_availability_test.py``. It emits the same
four provider-neutral aspects, each with the validator-named subtests, derived
from REAL Google Cloud signals:

  * ``vpc_flow_logs``     — live ``Subnetwork.log_config`` read-back (there is no
                            standalone flow-log object or native traffic-type
                            field on Compute Engine) PLUS a project-scoped Cloud
                            Logging query against ``compute.googleapis.com/vpc_flows``.
                            ``traffic_type=ALL`` is projected ONLY when every
                            target subnetwork reads back enable=true,
                            flow_sampling=1.0, and an empty filter — it means
                            every record after uncontrollable primary sampling is
                            retained for inbound + outbound flows, never
                            packet-complete capture or a native GCP field.
  * ``host_syslogs``      — same SSH guest boundary as AWS after metadata key
                            injection: sample journalctl (dmesg fallback) and
                            report real source / count / latest-timestamp.
  * ``bmc_sel_logs``      — provider-hidden: Compute Engine's managed BMC plane
                            exposes no customer SEL API. A real Resource Manager
                            project-identity probe precedes the provider-hidden
                            evidence (bmc_endpoints_checked=0).
  * ``bmc_gpu_telemetry`` — provider-hidden for the same reason. Google documents
                            guest GPU metrics via Ops Agent / NVML / DCGM only;
                            those are never relabeled as BMC telemetry.

AWS reference implementation:
    ../../aws/scripts/observability/log_availability_test.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, short_name
from common.errors import classify_gcp_error, handle_gcp_errors, retry_idempotent
from common.network import (
    FLOW_LOG_AGGREGATION_INTERVAL,
    FLOW_LOG_FLOW_SAMPLING,
    FLOW_LOG_METADATA,
    list_subnetworks_for_network,
)
from common.ssh_utils import ssh_run, wait_for_ssh
from google.cloud import logging_v2, resourcemanager_v3

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
}

JOURNALCTL_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{4})")
DMESG_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:,\d+)?(?:[+\-]\d{2}:?\d{2}|Z)?)")

# Compute Engine writes VPC Flow Logs to this project Cloud Logging log on
# gce_subnetwork resources. The %2F is the URL-encoded slash used in logName.
VPC_FLOWS_LOG_ID = "compute.googleapis.com%2Fvpc_flows"
VPC_FLOWS_LOG_DESTINATION = "compute.googleapis.com/vpc_flows"
# Canonical projected traffic_type for the availability probe: with
# flow_sampling=1.0, INCLUDE_ALL_METADATA, and no export filter, the subnet's
# flow logs retain both inbound and outbound records, so the effective scope is
# "ALL".
TRAFFIC_TYPE_ALL = "ALL"
# Bounded sample for the flow-log query — a successful query (even with 0
# entries) proves endpoint + destination accessibility; the count is a real
# sample, never fabricated.
_LOG_SAMPLE_MAX = 5
_LOG_LOOKBACK_HOURS = 24
# The interval represented by sample_count, emitted so provider-neutral
# consumers can interpret the flow-log sample. Mirrors the scaffold's
# sample_window_seconds probe; a static property of the query window, so it is
# reported even when the subnetwork read-back fails before the query runs.
_LOG_LOOKBACK_SECONDS = _LOG_LOOKBACK_HOURS * 3600

GCP_NO_CUSTOMER_BMC_MESSAGE = (
    "Compute Engine bare metal is fully managed by Google; no customer-accessible BMC SEL logs "
    "or Redfish GPU telemetry API is exposed (guest GPU metrics use Ops Agent / NVML / DCGM)"
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


def _provider_hidden(test_name: str, *, project: str) -> dict[str, Any]:
    """Build a passing provider-hidden subtest result for the managed BMC plane."""
    return {
        "passed": True,
        "provider_hidden": True,
        "probes": {"bmc_endpoints_checked": 0},
        "message": f"{test_name}: {GCP_NO_CUSTOMER_BMC_MESSAGE} (project {project} reachable).",
    }


def _base_result(aspect: str) -> dict[str, Any]:
    """Build the common observability result envelope."""
    return {
        "success": False,
        "platform": "observability",
        "test_name": aspect,
        "tests": {name: {"passed": False} for name in ASPECT_TESTS[aspect]},
    }


def _is_all_config(log_config: Any) -> bool:
    """Return True iff log_config reads back EXACTLY as the requested ALL config.

    Mirrors the enable step's strict gate: enable=true, flow_sampling==1.0
    (exact), empty export filter, metadata==INCLUDE_ALL_METADATA, and
    aggregation_interval==INTERVAL_5_SEC. A weaker effective configuration must
    NOT be projected as traffic_type=ALL, so every requested dimension is
    asserted from the live read-back.
    """
    if not getattr(log_config, "enable", False):
        return False
    if float(getattr(log_config, "flow_sampling", 0.0) or 0.0) != FLOW_LOG_FLOW_SAMPLING:
        return False
    if getattr(log_config, "filter_expr", "") or "":
        return False
    if (getattr(log_config, "metadata", "") or "") != FLOW_LOG_METADATA:
        return False
    return (getattr(log_config, "aggregation_interval", "") or "") == FLOW_LOG_AGGREGATION_INTERVAL


def _query_vpc_flow_logs(project: str, region: str, subnets: list[Any]) -> tuple[bool, int, str, str]:
    """Run a fully scoped Cloud Logging query for the vpc_flows log.

    Scope-binding (so unrelated same-named, cross-region, or future-dated
    entries can never stand in as evidence for the target network):

      * REJECT an empty target set — an unscoped query is refused outright.
      * Use ONE fixed start/end window (a single ``now`` snapshot bounds both
        ends; a lower-bound-only filter would admit future-dated entries).
      * Bind ``resource.type=gce_subnetwork`` to the target region
        (``resource.labels.location``) AND the exact live subnet identity — the
        stable numeric ``subnetwork_id`` when every target exposes one, else
        ``subnetwork_name`` (still region-bound, so a same-named subnet in
        another region cannot match).

    Returns ``(ok, sample_count, error, error_type)``. ``ok`` is True when the
    scoped query executes (a successful query — even with zero entries — proves
    the Cloud Logging endpoint and vpc_flows destination are accessible). The
    sample count is a REAL bounded sample, never fabricated. On failure,
    ``error`` carries the classified ``[bucket=<name>]`` message and
    ``error_type`` carries the bare bucket, so the caller can preserve WHY the
    query failed (access denial vs invalid credentials vs transient) rather than
    collapsing it to a generic string. The idempotent list is retried on the
    typed transient bucket and raw transport drops via ``retry_idempotent``.
    ``error_type`` is empty for the pre-query unscoped-target refusal, which is
    not a classified backend error.
    """
    if not subnets:
        return (
            False,
            0,
            "no target subnetworks resolved for the vpc_flows query; refusing an unscoped query",
            "",
        )
    log_name = f"projects/{project}/logs/{VPC_FLOWS_LOG_ID}"
    now = datetime.now(UTC)
    start = (now - timedelta(hours=_LOG_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_parts = [
        f'logName="{log_name}"',
        'resource.type="gce_subnetwork"',
        f'resource.labels.location="{region}"',
        f'timestamp >= "{start}"',
        f'timestamp <= "{end}"',
    ]
    subnet_ids = [str(s.id) for s in subnets if getattr(s, "id", 0)]
    if len(subnet_ids) == len(subnets):
        identity_clause = " OR ".join(f'"{sid}"' for sid in subnet_ids)
        filter_parts.append(f"resource.labels.subnetwork_id=({identity_clause})")
    else:
        name_clause = " OR ".join(f'"{short_name(s.name)}"' for s in subnets)
        filter_parts.append(f"resource.labels.subnetwork_name=({name_clause})")
    filter_str = " AND ".join(filter_parts)
    try:
        client = logging_v2.Client(project=project)
        entries = retry_idempotent(
            lambda: list(
                client.list_entries(
                    resource_names=[f"projects/{project}"],
                    filter_=filter_str,
                    max_results=_LOG_SAMPLE_MAX,
                )
            ),
            op_desc=f"cloud logging list_entries {VPC_FLOWS_LOG_DESTINATION}",
        )
        return True, len(entries), "", ""
    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        return False, 0, error_msg, error_type


def check_vpc_flow_logs(project: str, *, region: str, network_id: str) -> dict[str, Any]:
    """Check Compute Engine VPC Flow Logs and emit the observability contract."""
    result = _base_result("vpc_flow_logs")

    try:
        subnets = list_subnetworks_for_network(project, region, network_id)
    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        result["error_type"] = error_type
        result["error"] = error_msg
        probes = {
            "network_id": network_id,
            "log_destination": "",
            "traffic_type": "",
            "sample_window_seconds": _LOG_LOOKBACK_SECONDS,
        }
        for name in ASPECT_TESTS["vpc_flow_logs"]:
            result["tests"][name] = _failed(error_msg, probes)
        return result

    enabled_subnets = [short_name(s.name) for s in subnets if getattr(s.log_config, "enable", False)]
    all_all_configured = bool(subnets) and all(_is_all_config(s.log_config) for s in subnets)
    traffic_type = TRAFFIC_TYPE_ALL if all_all_configured else ""

    # Independent proof of endpoint + destination accessibility — the query is
    # bound to the exact live subnet identity and region (never subnet name
    # alone), so it cannot admit unrelated cross-region or future-dated entries.
    #
    # Target limitation (recorded here for future maintainers): on Compute Engine
    # VPC Flow Logs write to an IMPLICIT project Cloud Logging destination
    # (compute.googleapis.com/vpc_flows) with NO standalone resource peer — unlike
    # the AWS oracle's materialized CloudWatch log group. So a correctly-scoped,
    # successful query (including a zero-entry query) is the strongest available
    # proof of destination accessibility on this target; it proves query
    # accessibility, not existence of a separate destination object. If stronger
    # parity is ever required, the validator contract must distinguish query
    # accessibility from standalone destination materialization.
    query_ok, sample_count, query_error, query_error_type = _query_vpc_flow_logs(project, region, subnets)

    probes = {
        "network_id": network_id,
        "log_destination": VPC_FLOWS_LOG_DESTINATION,
        "traffic_type": traffic_type,
        "subnets_checked": len(subnets),
        "subnets_enabled": len(enabled_subnets),
        "sample_count": sample_count,
        "sample_window_seconds": _LOG_LOOKBACK_SECONDS,
    }

    # 1. Endpoint reachable — the Cloud Logging query executed.
    if query_ok:
        result["tests"]["flow_log_endpoint_reachable"] = _passed(
            f"Cloud Logging query for {VPC_FLOWS_LOG_DESTINATION} succeeded ({sample_count} sample entry(ies))", probes
        )
        result["tests"]["log_destination_accessible"] = _passed(
            f"vpc_flows log destination accessible: {VPC_FLOWS_LOG_DESTINATION}", probes
        )
    else:
        result["tests"]["flow_log_endpoint_reachable"] = _failed(f"Cloud Logging query failed: {query_error}", probes)
        result["tests"]["log_destination_accessible"] = _failed(
            f"vpc_flows log destination query failed: {query_error}", probes
        )

    # 2. flow_logs_configured — at least one target subnetwork logging enabled.
    if enabled_subnets:
        result["tests"]["flow_logs_configured"] = _passed(
            f"VPC Flow Logs enabled on {len(enabled_subnets)} subnetwork(s) of {network_id}", probes
        )
    else:
        result["tests"]["flow_logs_configured"] = _failed(
            f"No subnetwork of {network_id} has VPC Flow Logs enabled", probes
        )

    # 3. traffic_type_all — derived ONLY from live log_config read-back, never
    # from a request body or a native field.
    if all_all_configured:
        result["tests"]["traffic_type_all"] = _passed(
            f"Every target subnetwork retains ALL flow records (enable, flow_sampling=1.0, no filter); "
            f"traffic_type={TRAFFIC_TYPE_ALL}",
            probes,
        )
    else:
        result["tests"]["traffic_type_all"] = _failed(
            "Not every target subnetwork reads back enable=true, flow_sampling=1.0, and empty filter", probes
        )

    result["success"] = all(test.get("passed") for test in result["tests"].values())
    if not result["success"]:
        if not query_ok and query_error_type:
            # Preserve the Cloud Logging query's structured classification
            # (access denial vs invalid credentials vs transient service
            # failure) rather than collapsing to a generic string, so the
            # validation verdict keeps the disposition. query_error already
            # carries the leading [bucket=<name>] token.
            result["error_type"] = query_error_type
            result["error"] = query_error
        else:
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
    exit_code, stdout, _ = ssh_run(host, ssh_user, key_file, journal_cmd)
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

    if not host:
        error = "no host address was forwarded from launch_host"
        for name in ASPECT_TESTS["host_syslogs"]:
            result["tests"][name] = _failed(error, probes)
        result["error"] = error
        return result

    if not wait_for_ssh(host, ssh_user, key_file, max_attempts=20, interval=10):
        error = f"SSH did not become ready on {host}"
        for name in ASPECT_TESTS["host_syslogs"]:
            result["tests"][name] = _failed(error, probes)
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


def _probe_project_identity(project: str) -> str:
    """Prove the active GCP project is reachable; return its project id.

    The idempotent Resource Manager read is retried on the typed transient
    bucket and raw transport drops via ``retry_idempotent`` (a single 429/5xx no
    longer aborts the provider-hidden BMC path). Once transient budget is spent,
    or on any non-transient error, it raises the underlying Resource Manager
    error so the caller records a real control-plane failure instead of emitting
    provider-hidden evidence for an unreachable project.
    """
    proj = retry_idempotent(
        resourcemanager_v3.ProjectsClient().get_project,
        name=f"projects/{project}",
        op_desc=f"resourcemanager get_project {project}",
    )
    return proj.project_id or project


def check_bmc_sel_logs(project: str) -> dict[str, Any]:
    """Emit provider-hidden BMC SEL evidence after a real project-identity probe."""
    return _check_provider_hidden_bmc(project, "bmc_sel_logs")


def check_bmc_gpu_telemetry(project: str) -> dict[str, Any]:
    """Emit provider-hidden BMC GPU telemetry evidence after a project-identity probe."""
    return _check_provider_hidden_bmc(project, "bmc_gpu_telemetry")


def _check_provider_hidden_bmc(project: str, aspect: str) -> dict[str, Any]:
    """Shared provider-hidden path: probe project identity, then emit hidden evidence."""
    result = _base_result(aspect)
    try:
        project_id = _probe_project_identity(project)
    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        result["error_type"] = error_type
        result["error"] = error_msg
        probes = {"bmc_endpoints_checked": 0}
        for name in ASPECT_TESTS[aspect]:
            result["tests"][name] = _failed(f"GCP project identity probe failed: {error_msg}", probes)
        return result

    result["tests"] = {name: _provider_hidden(name, project=project_id) for name in ASPECT_TESTS[aspect]}
    result["success"] = True
    return result


@handle_gcp_errors
def main() -> int:
    """Run the selected GCP observability probe and emit structured JSON."""
    parser = argparse.ArgumentParser(description="GCP observability log availability test")
    parser.add_argument("--region", default="us-central1", help="GCP region (subnet region for the flow-log read)")
    parser.add_argument("--network-id", default="", help="Compute Engine network name (vpc_flow_logs aspect)")
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_TESTS), help="Which observability aspect")
    parser.add_argument("--host", default="", help="Host address for the host_syslogs SSH probe")
    parser.add_argument("--key-file", default="", help="Local SSH private-key path for the host_syslogs probe")
    parser.add_argument("--ssh-user", default="ubuntu", help="Guest SSH user for the host_syslogs probe")
    parser.add_argument("--max-age-minutes", type=int, default=5, help="Recency window for host log entries")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
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
            resolve_project(args.project),
            region=args.region,
            network_id=args.network_id,
        )
    elif args.aspect == "host_syslogs":
        result = check_host_syslogs(
            args.host,
            args.ssh_user,
            args.key_file,
            max_age_minutes=args.max_age_minutes,
        )
    elif args.aspect == "bmc_sel_logs":
        result = check_bmc_sel_logs(resolve_project(args.project))
    else:
        result = check_bmc_gpu_telemetry(resolve_project(args.project))

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
