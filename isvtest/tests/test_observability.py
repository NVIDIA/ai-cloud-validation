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

"""Tests for observability validations."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.observability import (
    BmcGpuTelemetryCheck,
    BmcSelLogsCheck,
    HostSyslogCheck,
    VpcFlowLogsCheck,
)


def _config(step_output: dict[str, Any]) -> dict[str, Any]:
    """Wrap step output in a validation config."""
    return {"step_output": step_output}


def _tests(names: list[str], probes: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Build a passing tests map for required contract keys."""
    test_result: dict[str, Any] = {"passed": True}
    if probes is not None:
        test_result["probes"] = probes
    return {name: dict(test_result) for name in names}


def _provider_hidden_tests(names: list[str]) -> dict[str, dict[str, Any]]:
    """Build a passing tests map for provider-hidden evidence."""
    return {
        name: {
            "passed": True,
            "provider_hidden": True,
            "probes": {"bmc_endpoints_checked": 0},
            "message": "AWS BMC plane is provider-owned",
        }
        for name in names
    }


def _vpc_flow_logs_output(**overrides: Any) -> dict[str, Any]:
    """Build passing VPC Flow Log step output."""
    probes: dict[str, Any] = {
        "network_id": "vpc-123",
        "log_destination": "arn:aws:logs:us-west-2:123:log-group:vpc-flow",
        "traffic_type": "ALL",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "vpc_flow_logs",
        "tests": _tests(
            [
                "flow_log_endpoint_reachable",
                "flow_logs_configured",
                "traffic_type_all",
                "log_destination_accessible",
            ],
            probes,
        ),
    }
    output.update(overrides)
    return output


def _host_syslog_output(**overrides: Any) -> dict[str, Any]:
    """Build passing host syslog step output."""
    probes: dict[str, Any] = {
        "hosts_checked": 2,
        "log_source": "journalctl",
        "entry_count": 12,
        "latest_timestamp": "2026-05-20T13:21:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "host_syslogs",
        "tests": _tests(["syslog_endpoint_reachable", "host_log_source_present", "entries_recent"], probes),
    }
    output.update(overrides)
    return output


def _bmc_sel_output(**overrides: Any) -> dict[str, Any]:
    """Build passing BMC SEL log step output."""
    probes: dict[str, Any] = {
        "bmc_endpoints_checked": 1,
        "log_source": "redfish-log-services/system-event-log",
        "entry_count": 0,
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "bmc_sel_logs",
        "tests": _tests(["sel_log_endpoint_reachable", "sel_log_source_present", "sel_entries_queryable"], probes),
    }
    output.update(overrides)
    return output


def _bmc_gpu_telemetry_output(**overrides: Any) -> dict[str, Any]:
    """Build passing BMC GPU telemetry step output."""
    probes: dict[str, Any] = {
        "bmc_endpoints_checked": 1,
        "telemetry_endpoint": "redfish-telemetry-service",
        "metric_names": ["gpu.power_state", "gpu.remediation_state"],
        "host_os_unavailable_metrics": ["gpu.power_state", "gpu.remediation_state"],
        "sample_count": 4,
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "bmc_gpu_telemetry",
        "tests": _tests(
            [
                "telemetry_endpoint_reachable",
                "gpu_metrics_present",
                "host_os_gap_identified",
                "telemetry_samples_recent",
            ],
            probes,
        ),
    }
    output.update(overrides)
    return output


def _bmc_sel_provider_hidden_output() -> dict[str, Any]:
    """Build provider-hidden BMC SEL log step output."""
    return {
        "success": True,
        "platform": "observability",
        "test_name": "bmc_sel_logs",
        "tests": _provider_hidden_tests(
            ["sel_log_endpoint_reachable", "sel_log_source_present", "sel_entries_queryable"]
        ),
    }


def _bmc_gpu_telemetry_provider_hidden_output() -> dict[str, Any]:
    """Build provider-hidden BMC GPU telemetry step output."""
    return {
        "success": True,
        "platform": "observability",
        "test_name": "bmc_gpu_telemetry",
        "tests": _provider_hidden_tests(
            [
                "telemetry_endpoint_reachable",
                "gpu_metrics_present",
                "host_os_gap_identified",
                "telemetry_samples_recent",
            ]
        ),
    }


@pytest.mark.parametrize(
    ("validation_cls", "step_output", "expected"),
    [
        (VpcFlowLogsCheck, _vpc_flow_logs_output(), "VPC Flow Logs available"),
        (HostSyslogCheck, _host_syslog_output(), "Host syslogs available"),
        (BmcSelLogsCheck, _bmc_sel_output(), "BMC SEL logs queryable"),
        (BmcGpuTelemetryCheck, _bmc_gpu_telemetry_output(), "BMC GPU telemetry available"),
    ],
)
def test_observability_checks_pass_with_required_evidence(
    validation_cls: type[VpcFlowLogsCheck | HostSyslogCheck | BmcSelLogsCheck | BmcGpuTelemetryCheck],
    step_output: dict[str, Any],
    expected: str,
) -> None:
    """Observability checks pass when required probes and evidence are present."""
    result = validation_cls(config=_config(step_output)).execute()

    assert result["passed"] is True
    assert expected in result["output"]


@pytest.mark.parametrize(
    ("validation_cls", "step_output", "expected"),
    [
        (BmcSelLogsCheck, _bmc_sel_provider_hidden_output(), "provider-hidden"),
        (BmcGpuTelemetryCheck, _bmc_gpu_telemetry_provider_hidden_output(), "provider-hidden"),
    ],
)
def test_bmc_observability_checks_pass_with_provider_hidden_evidence(
    validation_cls: type[BmcSelLogsCheck | BmcGpuTelemetryCheck],
    step_output: dict[str, Any],
    expected: str,
) -> None:
    """BMC observability checks accept provider-hidden evidence without endpoint counts."""
    result = validation_cls(config=_config(step_output)).execute()

    assert result["passed"] is True
    assert expected in result["output"]


def test_vpc_flow_logs_requires_all_traffic_type() -> None:
    """VPC Flow Logs must capture both accepted and rejected traffic."""
    result = VpcFlowLogsCheck(config=_config(_vpc_flow_logs_output(traffic_type="ACCEPT"))).execute()

    assert result["passed"] is False
    assert "ALL traffic" in result["error"]


def test_host_syslog_requires_recent_entries() -> None:
    """Host syslog validation fails without a positive recent-entry count."""
    result = HostSyslogCheck(config=_config(_host_syslog_output(entry_count=0))).execute()

    assert result["passed"] is False
    assert "entry_count" in result["error"]


def test_bmc_sel_allows_empty_log_with_queryable_source() -> None:
    """BMC SEL logs can be available even when no SEL events are present."""
    result = BmcSelLogsCheck(config=_config(_bmc_sel_output(entry_count=0))).execute()

    assert result["passed"] is True
    assert "0 entries" in result["output"]


def test_bmc_gpu_telemetry_requires_non_empty_metric_names() -> None:
    """BMC GPU telemetry evidence must name concrete GPU metrics."""
    result = BmcGpuTelemetryCheck(
        config=_config(_bmc_gpu_telemetry_output(metric_names=["gpu.power_state", ""]))
    ).execute()

    assert result["passed"] is False
    assert "metric_names" in result["error"]


def test_bmc_gpu_telemetry_requires_host_os_gap_metrics() -> None:
    """BMC GPU telemetry must identify metrics not available from the host OS."""
    result = BmcGpuTelemetryCheck(config=_config(_bmc_gpu_telemetry_output(host_os_unavailable_metrics=[]))).execute()

    assert result["passed"] is False
    assert "host_os_unavailable_metrics" in result["error"]


def test_bmc_gpu_telemetry_rejects_string_metric_names() -> None:
    """A scalar string is not accepted as a metric-name list."""
    result = BmcGpuTelemetryCheck(config=_config(_bmc_gpu_telemetry_output(metric_names="gpu.power_state"))).execute()

    assert result["passed"] is False
    assert "metric_names" in result["error"]


def test_missing_required_observability_test_fails() -> None:
    """Missing required test keys are reported by name."""
    output = _vpc_flow_logs_output()
    del output["tests"]["traffic_type_all"]

    result = VpcFlowLogsCheck(config=_config(output)).execute()

    assert result["passed"] is False
    assert "traffic_type_all" in result["error"]


def test_missing_observability_evidence_fails() -> None:
    """Missing evidence fields fail even when subtests passed."""
    output = _host_syslog_output(log_source="")

    result = HostSyslogCheck(config=_config(output)).execute()

    assert result["passed"] is False
    assert "log_source" in result["error"]


def test_top_level_observability_evidence_is_ignored() -> None:
    """Evidence must live in tests.<check>.probes, not top-level fields."""
    output = _host_syslog_output()
    for entry in output["tests"].values():
        entry.pop("probes")
    output.update(
        {
            "hosts_checked": 2,
            "log_source": "journalctl",
            "entry_count": 12,
            "latest_timestamp": "2026-05-20T13:21:00Z",
        }
    )

    result = HostSyslogCheck(config=_config(output)).execute()

    assert result["passed"] is False
    assert "log_source" in result["error"]
