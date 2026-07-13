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

from isvtest.core.validation import BaseValidation
from isvtest.validations.observability import (
    BmcGpuTelemetryCheck,
    BmcSelLogsCheck,
    FabricManagerLogsCheck,
    GeneralSwitchLogsCheck,
    GpuNvlinkTelemetryCheck,
    HostNicNetworkTelemetryCheck,
    HostSyslogCheck,
    NorthSouthNetworkTelemetryCheck,
    StorageCapacityTelemetryCheck,
    StoragePerformanceTelemetryCheck,
    SwitchKernelLogsCheck,
    SwitchNvlinkTelemetryCheck,
    SwitchSyslogCheck,
    TelemetryDeliveryLatencyCheck,
    UfmEventLogsCheck,
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


def _provider_hidden_tests(
    names: list[str],
    probe_field: str = "bmc_endpoints_checked",
    message: str = "AWS BMC plane is provider-owned",
) -> dict[str, dict[str, Any]]:
    """Build a passing tests map for provider-hidden evidence."""
    return {
        name: {
            "passed": True,
            "provider_hidden": True,
            "probes": {probe_field: 0},
            "message": message,
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


def _ufm_event_logs_output(**overrides: Any) -> dict[str, Any]:
    """Build passing UFM event log step output."""
    probes: dict[str, Any] = {
        "log_endpoints_checked": 1,
        "log_source": "ufm-event-history",
        "entry_count": 5,
        "latest_timestamp": "2026-05-20T13:19:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "ufm_event_logs",
        "tests": _tests(
            ["event_log_endpoint_reachable", "event_log_source_present", "event_entries_queryable"],
            probes,
        ),
    }
    output.update(overrides)
    return output


def _general_switch_logs_output(**overrides: Any) -> dict[str, Any]:
    """Build passing general switch log step output."""
    probes: dict[str, Any] = {
        "switches_checked": 2,
        "log_source": "switch-operational-log",
        "entry_count": 8,
        "latest_timestamp": "2026-05-20T13:18:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "general_switch_logs",
        "tests": _tests(
            ["log_endpoint_reachable", "switch_log_source_present", "entries_queryable"],
            probes,
        ),
    }
    output.update(overrides)
    return output


def _switch_syslog_output(**overrides: Any) -> dict[str, Any]:
    """Build passing switch syslog step output."""
    probes: dict[str, Any] = {
        "switches_checked": 2,
        "log_source": "switch-syslog",
        "entry_count": 10,
        "latest_timestamp": "2026-05-20T13:17:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "switch_syslogs",
        "tests": _tests(
            ["syslog_endpoint_reachable", "switch_syslog_source_present", "entries_recent"],
            probes,
        ),
    }
    output.update(overrides)
    return output


def _switch_kernel_logs_output(**overrides: Any) -> dict[str, Any]:
    """Build passing switch kernel log step output."""
    probes: dict[str, Any] = {
        "switches_checked": 2,
        "log_source": "switch-kernel-log",
        "entry_count": 3,
        "latest_timestamp": "2026-05-20T13:16:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    output: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": "switch_kernel_logs",
        "tests": _tests(
            ["log_endpoint_reachable", "kernel_log_source_present", "entries_queryable"],
            probes,
        ),
    }
    output.update(overrides)
    return output


def _ufm_event_logs_provider_hidden_output() -> dict[str, Any]:
    """Build provider-hidden UFM event log step output."""
    return {
        "success": True,
        "platform": "observability",
        "test_name": "ufm_event_logs",
        "tests": _provider_hidden_tests(
            [
                "event_log_endpoint_reachable",
                "event_log_source_present",
                "event_entries_queryable",
            ],
            probe_field="log_endpoints_checked",
            message="UFM plane is provider-owned",
        ),
    }


def _telemetry_delivery_output(**overrides: Any) -> dict[str, Any]:
    """Build passing telemetry delivery latency step output."""
    probes: dict[str, Any] = {
        "telemetry_source": "cloudwatch",
        "observed_delivery_seconds": 42,
        "max_delivery_seconds": 120,
        "sample_count": 3,
        "latest_timestamp": "2026-05-20T13:21:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": "telemetry_delivery_latency",
        "tests": _tests(
            ["telemetry_endpoint_reachable", "delivery_sample_present", "delivery_within_threshold"],
            probes,
        ),
        **overrides,
    }


def _network_telemetry_output(aspect: str, **overrides: Any) -> dict[str, Any]:
    """Build passing network-plane telemetry step output."""
    probes: dict[str, Any] = {
        "telemetry_source": "cloudwatch",
        "metric_names": ["NetworkPacketsIn", "NetworkPacketsOut"],
        "sample_count": 4,
        "latest_timestamp": "2026-05-20T13:21:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": aspect,
        "tests": _tests(
            ["telemetry_endpoint_reachable", "plane_metrics_present", "samples_recent"],
            probes,
        ),
        **overrides,
    }


def _host_nic_telemetry_output(**overrides: Any) -> dict[str, Any]:
    """Build passing host NIC telemetry step output."""
    probes: dict[str, Any] = {
        "telemetry_source": "cloudwatch",
        "nics_checked": 2,
        "metric_names": ["NetworkPacketsIn", "NetworkPacketsOut"],
        "sample_count": 4,
        "latest_timestamp": "2026-05-20T13:21:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": "host_nic_network_telemetry",
        "tests": _tests(
            ["telemetry_endpoint_reachable", "nic_metrics_present", "samples_recent"],
            probes,
        ),
        **overrides,
    }


def _storage_capacity_telemetry_output(**overrides: Any) -> dict[str, Any]:
    """Build passing storage capacity telemetry step output."""
    probes: dict[str, Any] = {
        "telemetry_source": "demo-storage-capacity",
        "volumes_checked": 2,
        "metric_names": ["storage.used.bytes", "storage.free.bytes", "storage.total.bytes"],
        "capacity_kinds": ["used", "free", "total"],
        "sample_count": 3,
        "latest_timestamp": "2026-05-20T13:21:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": "storage_capacity_telemetry",
        "tests": _tests(
            ["telemetry_endpoint_reachable", "capacity_metrics_present", "samples_recent"],
            probes,
        ),
        **overrides,
    }


def _storage_performance_telemetry_output(**overrides: Any) -> dict[str, Any]:
    """Build passing storage performance telemetry step output."""
    probes: dict[str, Any] = {
        "telemetry_source": "cloudwatch",
        "volumes_checked": 1,
        "metric_names": ["VolumeReadBytes", "VolumeReadOps", "VolumeTotalReadTime"],
        "performance_kinds": ["bandwidth", "iops", "latency"],
        "sample_count": 4,
        "latest_timestamp": "2026-05-20T13:20:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": "storage_performance_telemetry",
        "tests": _tests(
            ["telemetry_endpoint_reachable", "performance_metrics_present", "samples_recent"],
            probes,
        ),
        **overrides,
    }


def _gpu_nvlink_telemetry_output(**overrides: Any) -> dict[str, Any]:
    """Build passing GPU NVLink telemetry step output."""
    probes: dict[str, Any] = {
        "telemetry_source": "demo-gpu-nvlink",
        "links_checked": 4,
        "metric_names": ["nvlink.tx_bytes", "nvlink.rx_bytes", "nvlink.bandwidth_util"],
        "sample_count": 6,
        "latest_timestamp": "2026-05-20T13:19:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": "gpu_nvlink_telemetry",
        "tests": _tests(
            ["telemetry_endpoint_reachable", "link_metrics_present", "samples_recent"],
            probes,
        ),
        **overrides,
    }


def _switch_nvlink_telemetry_output(**overrides: Any) -> dict[str, Any]:
    """Build passing switch NVLink telemetry step output."""
    probes: dict[str, Any] = {
        "telemetry_source": "demo-switch-nvlink",
        "ports_checked": 8,
        "metric_names": ["nvlink.port.rx_errors", "nvlink.port.tx_counters"],
        "sample_count": 5,
        "latest_timestamp": "2026-05-20T13:18:00Z",
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": "switch_nvlink_telemetry",
        "tests": _tests(
            ["telemetry_endpoint_reachable", "port_metrics_present", "samples_recent"],
            probes,
        ),
        **overrides,
    }


def _storage_capacity_provider_hidden_output() -> dict[str, Any]:
    """Build provider-hidden storage capacity telemetry step output."""
    return {
        "success": True,
        "platform": "observability",
        "test_name": "storage_capacity_telemetry",
        "tests": _provider_hidden_tests(
            ["telemetry_endpoint_reachable", "capacity_metrics_present", "samples_recent"],
            probe_field="volumes_checked",
            message="AWS storage plane is provider-owned",
        ),
    }


def _fabric_manager_logs_output(**overrides: Any) -> dict[str, Any]:
    """Build passing Fabric Manager log step output."""
    probes: dict[str, Any] = {
        "log_endpoints_checked": 1,
        "log_source": "ufm-fabric-manager",
        "entry_count": 7,
    }
    for key in set(overrides) & set(probes):
        probes[key] = overrides.pop(key)
    return {
        "success": True,
        "platform": "observability",
        "test_name": "fabric_manager_logs",
        "tests": _tests(
            ["log_endpoint_reachable", "log_source_present", "log_entries_queryable"],
            probes,
        ),
        **overrides,
    }


@pytest.mark.parametrize(
    ("validation_cls", "step_output", "expected"),
    [
        (VpcFlowLogsCheck, _vpc_flow_logs_output(), "VPC Flow Logs available"),
        (HostSyslogCheck, _host_syslog_output(), "Host syslogs available"),
        (BmcSelLogsCheck, _bmc_sel_output(), "BMC SEL logs queryable"),
        (BmcGpuTelemetryCheck, _bmc_gpu_telemetry_output(), "BMC GPU telemetry available"),
        (StorageCapacityTelemetryCheck, _storage_capacity_telemetry_output(), "3 capacity kinds"),
        (StoragePerformanceTelemetryCheck, _storage_performance_telemetry_output(), "3 performance kinds"),
        (GpuNvlinkTelemetryCheck, _gpu_nvlink_telemetry_output(), "4 link(s)"),
        (SwitchNvlinkTelemetryCheck, _switch_nvlink_telemetry_output(), "8 port(s)"),
        (UfmEventLogsCheck, _ufm_event_logs_output(), "UFM Event logs queryable"),
        (FabricManagerLogsCheck, _fabric_manager_logs_output(), "Fabric Manager logs queryable"),
        (GeneralSwitchLogsCheck, _general_switch_logs_output(), "General switch logs available"),
        (SwitchSyslogCheck, _switch_syslog_output(), "Switch syslogs available"),
        (SwitchKernelLogsCheck, _switch_kernel_logs_output(), "Switch kernel logs available"),
        (TelemetryDeliveryLatencyCheck, _telemetry_delivery_output(), "Telemetry delivery latency"),
        (
            NorthSouthNetworkTelemetryCheck,
            _network_telemetry_output("north_south_network_telemetry"),
            "North-South network telemetry",
        ),
        (
            HostNicNetworkTelemetryCheck,
            _host_nic_telemetry_output(),
            "Host NIC telemetry available",
        ),
    ],
)
def test_observability_checks_pass_with_required_evidence(
    validation_cls: type[BaseValidation],
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
        (StorageCapacityTelemetryCheck, _storage_capacity_provider_hidden_output(), "provider-hidden"),
        (UfmEventLogsCheck, _ufm_event_logs_provider_hidden_output(), "provider-hidden"),
        (
            GeneralSwitchLogsCheck,
            {
                "success": True,
                "platform": "observability",
                "test_name": "general_switch_logs",
                "tests": _provider_hidden_tests(
                    ["log_endpoint_reachable", "switch_log_source_present", "entries_queryable"],
                    probe_field="switches_checked",
                    message="Fabric plane is provider-owned",
                ),
            },
            "provider-hidden",
        ),
    ],
)
def test_bmc_observability_checks_pass_with_provider_hidden_evidence(
    validation_cls: type[BaseValidation],
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


def test_storage_capacity_telemetry_requires_capacity_kinds() -> None:
    """Storage capacity telemetry must report used, free, and total kinds."""
    result = StorageCapacityTelemetryCheck(
        config=_config(_storage_capacity_telemetry_output(capacity_kinds=["used", "free"]))
    ).execute()

    assert result["passed"] is False
    assert "total" in result["error"]


def test_storage_performance_telemetry_requires_performance_kinds() -> None:
    """Storage performance telemetry must report bandwidth, IOPS, and latency kinds."""
    result = StoragePerformanceTelemetryCheck(
        config=_config(_storage_performance_telemetry_output(performance_kinds=["bandwidth", "iops"]))
    ).execute()

    assert result["passed"] is False
    assert "latency" in result["error"]


def test_gpu_nvlink_telemetry_requires_link_count() -> None:
    """GPU NVLink telemetry validation fails without a positive link count."""
    result = GpuNvlinkTelemetryCheck(config=_config(_gpu_nvlink_telemetry_output(links_checked=0))).execute()

    assert result["passed"] is False
    assert "links_checked" in result["error"]


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


def test_telemetry_delivery_latency_rejects_slow_delivery() -> None:
    """Telemetry delivery validation fails when latency exceeds the threshold."""
    result = TelemetryDeliveryLatencyCheck(
        config={**_config(_telemetry_delivery_output(observed_delivery_seconds=180)), "max_delivery_seconds": 120}
    ).execute()

    assert result["passed"] is False
    assert "exceeds threshold" in result["error"]


def test_switch_syslog_requires_recent_entries() -> None:
    """Switch syslog validation fails without a positive recent-entry count."""
    result = SwitchSyslogCheck(config=_config(_switch_syslog_output(entry_count=0))).execute()

    assert result["passed"] is False
    assert "entry_count" in result["error"]


def test_ufm_event_logs_reject_missing_latest_timestamp() -> None:
    """UFM event log validation fails when latest_timestamp is empty."""
    result = UfmEventLogsCheck(config=_config(_ufm_event_logs_output(entry_count=0, latest_timestamp=""))).execute()

    assert result["passed"] is False
    assert "latest_timestamp" in result["error"]


def test_ufm_event_logs_allow_empty_history_with_queryable_source() -> None:
    """UFM event logs can be available even when no events are present."""
    result = UfmEventLogsCheck(config=_config(_ufm_event_logs_output(entry_count=0))).execute()

    assert result["passed"] is True
    assert "0 entries" in result["output"]


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
