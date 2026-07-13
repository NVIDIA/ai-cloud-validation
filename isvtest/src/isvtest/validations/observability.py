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

"""Observability validations for provider log and telemetry evidence."""

from __future__ import annotations

from typing import ClassVar

from isvtest.core.validation import BaseValidation, check_required_tests


def _merged_probes(validation: BaseValidation) -> dict[str, object]:
    """Merge per-test probe evidence from a step output."""
    tests = validation.config.get("step_output", {}).get("tests", {})
    probes: dict[str, object] = {}
    if not isinstance(tests, dict):
        return probes
    for test_result in tests.values():
        if isinstance(test_result, dict) and isinstance(test_result.get("probes"), dict):
            probes.update(test_result["probes"])
    return probes


def _is_non_empty_string(value: object) -> bool:
    """Return True when ``value`` is a string with non-whitespace content."""
    return isinstance(value, str) and bool(value.strip())


def _provider_hidden_message(validation: BaseValidation, required: list[str], label: str) -> str:
    """Return a provider-hidden pass message when every required subtest carries that marker."""
    tests = validation.config.get("step_output", {}).get("tests", {})
    if not isinstance(tests, dict):
        return ""

    required_results = [tests.get(name) for name in required]
    if not all(isinstance(result, dict) and result.get("provider_hidden") is True for result in required_results):
        return ""

    messages = [
        message.strip()
        for result in required_results
        if isinstance(result, dict) and isinstance((message := result.get("message")), str) and message.strip()
    ]
    detail = messages[0] if messages else "plane is provider-owned"
    return f"{label} provider-hidden: {detail}"


def _require_non_empty_strings(
    validation: BaseValidation, probes: dict[str, object], fields: list[str], label: str
) -> bool:
    """Fail validation when any named evidence field is not a non-empty string."""
    missing = [field for field in fields if not _is_non_empty_string(probes.get(field))]
    if missing:
        validation.set_failed(f"Missing non-empty {label} evidence: {', '.join(missing)}")
        return False
    return True


def _require_non_empty_string_list(
    validation: BaseValidation, probes: dict[str, object], field: str, label: str
) -> bool:
    """Fail validation when a list evidence field is empty or contains non-strings."""
    value = probes.get(field)
    if not isinstance(value, list) or not value or not all(_is_non_empty_string(item) for item in value):
        validation.set_failed(f"{label} evidence field '{field}' must be a non-empty list of strings")
        return False
    return True


def _require_non_negative_int(validation: BaseValidation, probes: dict[str, object], field: str, label: str) -> bool:
    """Fail validation when a count-like evidence field is not a non-negative integer."""
    value = probes.get(field)
    if type(value) is not int or value < 0:
        validation.set_failed(f"{label} evidence field '{field}' must be a non-negative integer")
        return False
    return True


def _require_positive_int(validation: BaseValidation, probes: dict[str, object], field: str, label: str) -> bool:
    """Fail validation when a count-like evidence field is not a positive integer."""
    value = probes.get(field)
    if type(value) is not int or value < 1:
        validation.set_failed(f"{label} evidence field '{field}' must be a positive integer")
        return False
    return True


class VpcFlowLogsCheck(BaseValidation):
    """Validate VPC Flow Logs are available for all ingress and egress traffic.

    Config:
        step_output: The vpc_flow_logs step output to check

    Step output:
        tests: dict with flow_log_endpoint_reachable, flow_logs_configured,
               traffic_type_all, log_destination_accessible
        tests.<check>.probes.network_id: Non-empty VPC/network identifier
        tests.<check>.probes.log_destination: Non-empty log destination identifier
        tests.<check>.probes.traffic_type: Must be ``ALL``
    """

    description: ClassVar[str] = "Check VPC Flow Logs capture all ingress and egress traffic"

    def run(self) -> None:
        """Validate required VPC Flow Log results and evidence."""
        required = [
            "flow_log_endpoint_reachable",
            "flow_logs_configured",
            "traffic_type_all",
            "log_destination_accessible",
        ]
        if not check_required_tests(self, required, "VPC Flow Log tests failed"):
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(
            self, probes, ["network_id", "log_destination", "traffic_type"], "VPC Flow Log"
        ):
            return

        traffic_type = str(probes["traffic_type"]).upper()
        if traffic_type != "ALL":
            self.set_failed(f"VPC Flow Logs must capture ALL traffic, got traffic_type={traffic_type!r}")
            return

        self.set_passed(
            f"VPC Flow Logs available for {probes['network_id']} "
            f"(destination={probes['log_destination']}, traffic_type={traffic_type})"
        )


class HostSyslogCheck(BaseValidation):
    """Validate host syslogs are available from at least one host.

    Config:
        step_output: The host_syslogs step output to check

    Step output:
        tests: dict with syslog_endpoint_reachable, host_log_source_present,
               entries_recent
        tests.<check>.probes.hosts_checked: Positive integer count of hosts inspected
        tests.<check>.probes.log_source: Non-empty log source identifier
        tests.<check>.probes.entry_count: Positive integer count of recent log entries
        tests.<check>.probes.latest_timestamp: Non-empty timestamp for the latest entry
    """

    description: ClassVar[str] = "Check host syslogs are available"

    def run(self) -> None:
        """Validate host syslog results and evidence."""
        required = ["syslog_endpoint_reachable", "host_log_source_present", "entries_recent"]
        if not check_required_tests(self, required, "Host syslog tests failed"):
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(self, probes, ["log_source", "latest_timestamp"], "host syslog"):
            return
        if not _require_positive_int(self, probes, "hosts_checked", "host syslog"):
            return
        if not _require_positive_int(self, probes, "entry_count", "host syslog"):
            return

        self.set_passed(
            f"Host syslogs available from {probes['hosts_checked']} host(s) "
            f"via {probes['log_source']} ({probes['entry_count']} recent entries)"
        )


class BmcSelLogsCheck(BaseValidation):
    """Validate BMC SEL logs are queryable.

    Config:
        step_output: The bmc_sel_logs step output to check

    Step output:
        tests: dict with sel_log_endpoint_reachable, sel_log_source_present,
               sel_entries_queryable
        For provider-hidden BMC planes, all required subtests may pass with
        provider_hidden=true instead of concrete endpoint probes.
        tests.<check>.probes.bmc_endpoints_checked: Positive integer count of BMC
            endpoints inspected
        tests.<check>.probes.log_source: Non-empty SEL log source identifier
        tests.<check>.probes.entry_count: Non-negative integer count of SEL entries returned
    """

    description: ClassVar[str] = "Check BMC SEL logs are queryable"

    def run(self) -> None:
        """Validate BMC SEL log results and evidence."""
        required = ["sel_log_endpoint_reachable", "sel_log_source_present", "sel_entries_queryable"]
        if not check_required_tests(self, required, "BMC SEL log tests failed"):
            return
        if message := _provider_hidden_message(self, required, "BMC SEL logs"):
            self.set_passed(message)
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(self, probes, ["log_source"], "BMC SEL log"):
            return
        if not _require_positive_int(self, probes, "bmc_endpoints_checked", "BMC SEL log"):
            return
        if not _require_non_negative_int(self, probes, "entry_count", "BMC SEL log"):
            return

        self.set_passed(
            f"BMC SEL logs queryable on {probes['bmc_endpoints_checked']} endpoint(s) "
            f"via {probes['log_source']} ({probes['entry_count']} entries)"
        )


def _require_metric_kinds(
    validation: BaseValidation,
    probes: dict[str, object],
    field: str,
    required_kinds: list[str],
    label: str,
) -> bool:
    """Fail validation when required metric-kind categories are missing."""
    value = probes.get(field)
    if not isinstance(value, list) or not value or not all(_is_non_empty_string(item) for item in value):
        validation.set_failed(f"{label} evidence field '{field}' must be a non-empty list of strings")
        return False
    normalized = {str(item).strip().lower() for item in value}
    missing = [kind for kind in required_kinds if kind not in normalized]
    if missing:
        validation.set_failed(f"Missing {label} metric kinds: {', '.join(missing)}")
        return False
    return True


class _StorageTelemetryCheck(BaseValidation):
    """Shared validation logic for storage telemetry checks."""

    catalog_exclude: ClassVar[bool] = True
    _required_tests: ClassVar[list[str]] = [
        "telemetry_endpoint_reachable",
        "capacity_metrics_present",
        "samples_recent",
    ]
    _metrics_present_test: ClassVar[str] = "capacity_metrics_present"
    _plane_label: ClassVar[str] = "storage capacity telemetry"
    _kind_field: ClassVar[str] = "capacity_kinds"
    _required_kinds: ClassVar[list[str]] = ["used", "free", "total"]
    _kind_label: ClassVar[str] = "capacity"

    def run(self) -> None:
        """Validate storage telemetry results and evidence."""
        if not check_required_tests(self, self._required_tests, f"{self._plane_label} tests failed"):
            return
        if message := _provider_hidden_message(self, self._required_tests, self._plane_label):
            self.set_passed(message)
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(self, probes, ["telemetry_source"], self._plane_label):
            return
        if not _require_non_empty_string_list(self, probes, "metric_names", self._plane_label):
            return
        if not _require_metric_kinds(self, probes, self._kind_field, self._required_kinds, self._plane_label):
            return
        if not _require_positive_int(self, probes, "volumes_checked", self._plane_label):
            return
        if not _require_positive_int(self, probes, "sample_count", self._plane_label):
            return
        if not _require_non_empty_strings(self, probes, ["latest_timestamp"], self._plane_label):
            return

        metric_names = probes["metric_names"]
        kinds = probes[self._kind_field]
        self.set_passed(
            f"{self._plane_label} available for {probes['volumes_checked']} volume(s) "
            f"via {probes['telemetry_source']} ({len(metric_names)} metrics, "
            f"{len(kinds)} {self._kind_label} kinds, {probes['sample_count']} samples)"
        )


class StorageCapacityTelemetryCheck(_StorageTelemetryCheck):
    """Validate storage resource capacity metrics are available."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check storage capacity telemetry is available (used/free/total)"


class StoragePerformanceTelemetryCheck(_StorageTelemetryCheck):
    """Validate storage performance metrics are available."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check storage performance telemetry is available (bandwidth/IOPS/latency)"
    _required_tests: ClassVar[list[str]] = [
        "telemetry_endpoint_reachable",
        "performance_metrics_present",
        "samples_recent",
    ]
    _metrics_present_test: ClassVar[str] = "performance_metrics_present"
    _plane_label: ClassVar[str] = "storage performance telemetry"
    _kind_field: ClassVar[str] = "performance_kinds"
    _required_kinds: ClassVar[list[str]] = ["bandwidth", "iops", "latency"]
    _kind_label: ClassVar[str] = "performance"


class _NvlinkTelemetryCheck(BaseValidation):
    """Shared validation logic for NVLink telemetry checks."""

    catalog_exclude: ClassVar[bool] = True
    _required_tests: ClassVar[list[str]] = [
        "telemetry_endpoint_reachable",
        "link_metrics_present",
        "samples_recent",
    ]
    _metrics_present_test: ClassVar[str] = "link_metrics_present"
    _plane_label: ClassVar[str] = "GPU NVLink telemetry"
    _count_field: ClassVar[str] = "links_checked"
    _unit_label: ClassVar[str] = "link"

    def run(self) -> None:
        """Validate NVLink telemetry results and evidence."""
        if not check_required_tests(self, self._required_tests, f"{self._plane_label} tests failed"):
            return
        if message := _provider_hidden_message(self, self._required_tests, self._plane_label):
            self.set_passed(message)
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(self, probes, ["telemetry_source"], self._plane_label):
            return
        if not _require_non_empty_string_list(self, probes, "metric_names", self._plane_label):
            return
        if not _require_positive_int(self, probes, self._count_field, self._plane_label):
            return
        if not _require_positive_int(self, probes, "sample_count", self._plane_label):
            return
        if not _require_non_empty_strings(self, probes, ["latest_timestamp"], self._plane_label):
            return

        metric_names = probes["metric_names"]
        self.set_passed(
            f"{self._plane_label} available from {probes[self._count_field]} {self._unit_label}(s) "
            f"via {probes['telemetry_source']} ({len(metric_names)} metrics, {probes['sample_count']} samples)"
        )


class GpuNvlinkTelemetryCheck(_NvlinkTelemetryCheck):
    """Validate NVLink metrics are available from the GPU perspective."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check GPU NVLink telemetry is available"


class SwitchNvlinkTelemetryCheck(_NvlinkTelemetryCheck):
    """Validate NVLink metrics are available from the switch perspective."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check switch NVLink telemetry is available"
    _required_tests: ClassVar[list[str]] = [
        "telemetry_endpoint_reachable",
        "port_metrics_present",
        "samples_recent",
    ]
    _metrics_present_test: ClassVar[str] = "port_metrics_present"
    _plane_label: ClassVar[str] = "switch NVLink telemetry"
    _count_field: ClassVar[str] = "ports_checked"
    _unit_label: ClassVar[str] = "port"


class BmcGpuTelemetryCheck(BaseValidation):
    """Validate BMC or Redfish GPU telemetry is available.

    Config:
        step_output: The bmc_gpu_telemetry step output to check

    Step output:
        tests: dict with telemetry_endpoint_reachable, gpu_metrics_present,
               host_os_gap_identified, telemetry_samples_recent
        For provider-hidden BMC planes, all required subtests may pass with
        provider_hidden=true instead of concrete endpoint probes.
        tests.<check>.probes.bmc_endpoints_checked: Positive integer count of BMC
            endpoints inspected
        tests.<check>.probes.telemetry_endpoint: Non-empty telemetry API/source identifier
        tests.<check>.probes.metric_names: Non-empty list of GPU metric names
        tests.<check>.probes.host_os_unavailable_metrics: Non-empty list of metrics not available
            from the host OS
        tests.<check>.probes.sample_count: Positive integer count of telemetry samples returned
    """

    description: ClassVar[str] = "Check BMC or Redfish GPU telemetry is available"

    def run(self) -> None:
        """Validate BMC GPU telemetry results and evidence."""
        required = [
            "telemetry_endpoint_reachable",
            "gpu_metrics_present",
            "host_os_gap_identified",
            "telemetry_samples_recent",
        ]
        if not check_required_tests(self, required, "BMC GPU telemetry tests failed"):
            return
        if message := _provider_hidden_message(self, required, "BMC GPU telemetry"):
            self.set_passed(message)
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(self, probes, ["telemetry_endpoint"], "BMC GPU telemetry"):
            return
        if not _require_non_empty_string_list(self, probes, "metric_names", "BMC GPU telemetry"):
            return
        if not _require_non_empty_string_list(self, probes, "host_os_unavailable_metrics", "BMC GPU telemetry"):
            return
        if not _require_positive_int(self, probes, "bmc_endpoints_checked", "BMC GPU telemetry"):
            return
        if not _require_positive_int(self, probes, "sample_count", "BMC GPU telemetry"):
            return

        metric_names = probes["metric_names"]
        unavailable_metrics = probes["host_os_unavailable_metrics"]

        self.set_passed(
            f"BMC GPU telemetry available from {probes['bmc_endpoints_checked']} endpoint(s) "
            f"via {probes['telemetry_endpoint']} ({len(metric_names)} metrics, "
            f"{probes['sample_count']} samples, {len(unavailable_metrics)} host-OS gap metrics)"
        )


class TelemetryDeliveryLatencyCheck(BaseValidation):
    """Validate telemetry delivery latency stays within the configured threshold.

    Config:
        step_output: The telemetry_delivery_latency step output to check
        max_delivery_seconds: Maximum allowed delivery latency (default 120)

    Step output:
        tests: dict with telemetry_endpoint_reachable, delivery_sample_present,
               delivery_within_threshold
        tests.<check>.probes.telemetry_source: Non-empty telemetry source identifier
        tests.<check>.probes.observed_delivery_seconds: Non-negative integer latency
        tests.<check>.probes.max_delivery_seconds: Informational echo of the
            script-side threshold; the enforced threshold is the config value above
    """

    description: ClassVar[str] = "Check telemetry delivery latency is within threshold"

    def run(self) -> None:
        """Validate telemetry delivery latency results and evidence."""
        required = ["telemetry_endpoint_reachable", "delivery_sample_present", "delivery_within_threshold"]
        if not check_required_tests(self, required, "Telemetry delivery latency tests failed"):
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(self, probes, ["telemetry_source"], "telemetry delivery"):
            return
        if not _require_non_negative_int(self, probes, "observed_delivery_seconds", "telemetry delivery"):
            return

        max_delivery_seconds = self._parse_positive_int("max_delivery_seconds", default=120)
        if max_delivery_seconds is None:
            return

        observed = probes["observed_delivery_seconds"]
        if observed > max_delivery_seconds:
            self.set_failed(
                f"Telemetry delivery latency {observed}s exceeds threshold {max_delivery_seconds}s "
                f"via {probes['telemetry_source']}"
            )
            return

        self.set_passed(
            f"Telemetry delivery latency {observed}s within {max_delivery_seconds}s via {probes['telemetry_source']}"
        )


class _NetworkTelemetryCheck(BaseValidation):
    """Shared validation logic for network-plane telemetry checks."""

    catalog_exclude: ClassVar[bool] = True
    _required_tests: ClassVar[list[str]] = [
        "telemetry_endpoint_reachable",
        "plane_metrics_present",
        "samples_recent",
    ]
    _plane_label: ClassVar[str] = "network telemetry"
    # When set, the named count probe must additionally be a positive integer.
    _count_field: ClassVar[str] = ""

    def run(self) -> None:
        """Validate network telemetry results and evidence."""
        if not check_required_tests(self, self._required_tests, f"{self._plane_label} tests failed"):
            return
        if message := _provider_hidden_message(self, self._required_tests, self._plane_label):
            self.set_passed(message)
            return
        probes = _merged_probes(self)
        if not _require_non_empty_strings(self, probes, ["telemetry_source"], self._plane_label):
            return
        if not _require_non_empty_string_list(self, probes, "metric_names", self._plane_label):
            return
        if not _require_positive_int(self, probes, "sample_count", self._plane_label):
            return
        if not _require_non_empty_strings(self, probes, ["latest_timestamp"], self._plane_label):
            return
        if self._count_field and not _require_positive_int(self, probes, self._count_field, self._plane_label):
            return

        self.set_passed(self._pass_message(probes))

    def _pass_message(self, probes: dict[str, object]) -> str:
        """Build the pass message from validated evidence."""
        metric_names = probes["metric_names"]
        return (
            f"{self._plane_label} available via {probes['telemetry_source']} "
            f"({len(metric_names)} metrics, {probes['sample_count']} recent samples)"
        )


class NorthSouthNetworkTelemetryCheck(_NetworkTelemetryCheck):
    """Validate North-South (front-end) network telemetry is available."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check North-South network telemetry is available"
    _plane_label: ClassVar[str] = "North-South network telemetry"


class EastWestNetworkTelemetryCheck(_NetworkTelemetryCheck):
    """Validate East-West (GPU interconnect) network telemetry is available."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check East-West network telemetry is available"
    _plane_label: ClassVar[str] = "East-West network telemetry"


class ManagementNetworkTelemetryCheck(_NetworkTelemetryCheck):
    """Validate management network telemetry is available."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check management network telemetry is available"
    _plane_label: ClassVar[str] = "Management network telemetry"


class NvswitchFabricTelemetryCheck(_NetworkTelemetryCheck):
    """Validate NVSwitch fabric telemetry is available."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check NVSwitch fabric telemetry is available"
    _plane_label: ClassVar[str] = "NVSwitch fabric telemetry"


class HostNicNetworkTelemetryCheck(_NetworkTelemetryCheck):
    """Validate host NIC-level network telemetry is available.

    Config:
        step_output: The host_nic_network_telemetry step output to check

    Step output:
        tests: dict with telemetry_endpoint_reachable, nic_metrics_present,
               samples_recent
        tests.<check>.probes.telemetry_source: Non-empty telemetry source identifier
        tests.<check>.probes.nics_checked: Positive integer count of NICs inspected
        tests.<check>.probes.metric_names: Non-empty list of metric names
        tests.<check>.probes.sample_count: Positive integer count of recent samples
        tests.<check>.probes.latest_timestamp: Non-empty timestamp for the latest sample
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check host NIC-level network telemetry is available"
    _required_tests: ClassVar[list[str]] = [
        "telemetry_endpoint_reachable",
        "nic_metrics_present",
        "samples_recent",
    ]
    _plane_label: ClassVar[str] = "Host NIC network telemetry"
    _count_field: ClassVar[str] = "nics_checked"

    def _pass_message(self, probes: dict[str, object]) -> str:
        """Build the pass message from validated evidence."""
        metric_names = probes["metric_names"]
        return (
            f"Host NIC telemetry available from {probes['nics_checked']} NIC(s) "
            f"via {probes['telemetry_source']} ({len(metric_names)} metrics, {probes['sample_count']} samples)"
        )


class _FabricLogCheck(BaseValidation):
    """Shared validation logic for fabric-manager style log checks."""

    catalog_exclude: ClassVar[bool] = True
    _required_tests: ClassVar[list[str]] = [
        "log_endpoint_reachable",
        "log_source_present",
        "log_entries_queryable",
    ]
    _log_label: ClassVar[str] = "fabric log"
    _count_field: ClassVar[str] = "log_endpoints_checked"
    _entry_count_positive: ClassVar[bool] = False
    _require_latest_timestamp: ClassVar[bool] = False
    _entries_label: ClassVar[str] = "entries"

    def run(self) -> None:
        """Validate fabric log results and evidence."""
        if not check_required_tests(self, self._required_tests, f"{self._log_label} tests failed"):
            return
        if message := _provider_hidden_message(self, self._required_tests, self._log_label):
            self.set_passed(message)
            return
        probes = _merged_probes(self)
        string_fields = ["log_source", "latest_timestamp"] if self._require_latest_timestamp else ["log_source"]
        if not _require_non_empty_strings(self, probes, string_fields, self._log_label):
            return
        if not _require_positive_int(self, probes, self._count_field, self._log_label):
            return
        require_entry_count = _require_positive_int if self._entry_count_positive else _require_non_negative_int
        if not require_entry_count(self, probes, "entry_count", self._log_label):
            return

        self.set_passed(self._pass_message(probes))

    def _pass_message(self, probes: dict[str, object]) -> str:
        """Build the pass message from validated evidence."""
        return (
            f"{self._log_label} queryable from {probes[self._count_field]} endpoint(s) "
            f"via {probes['log_source']} ({probes['entry_count']} {self._entries_label})"
        )


class FabricManagerLogsCheck(_FabricLogCheck):
    """Validate Fabric Manager logs are queryable where applicable."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check Fabric Manager logs are available"
    _log_label: ClassVar[str] = "Fabric Manager logs"


class SubnetManagerLogsCheck(_FabricLogCheck):
    """Validate Subnet Manager logs are queryable where applicable."""

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check Subnet Manager logs are available"
    _log_label: ClassVar[str] = "Subnet Manager logs"


class UfmEventLogsCheck(_FabricLogCheck):
    """Validate UFM Event logs are queryable.

    Config:
        step_output: The ufm_event_logs step output to check

    Step output:
        tests: dict with event_log_endpoint_reachable, event_log_source_present,
               event_entries_queryable
        For provider-hidden fabric planes, all required subtests may pass with
        provider_hidden=true instead of concrete endpoint probes.
        tests.<check>.probes.log_endpoints_checked: Positive integer count of
            log endpoints inspected
        tests.<check>.probes.log_source: Non-empty UFM event log source identifier
        tests.<check>.probes.entry_count: Non-negative integer count of event entries
        tests.<check>.probes.latest_timestamp: Non-empty timestamp for the latest entry
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check UFM Event logs are available"
    _required_tests: ClassVar[list[str]] = [
        "event_log_endpoint_reachable",
        "event_log_source_present",
        "event_entries_queryable",
    ]
    _log_label: ClassVar[str] = "UFM Event logs"
    _require_latest_timestamp: ClassVar[bool] = True


class _SwitchLogCheck(_FabricLogCheck):
    """Shared validation logic for switch-level log checks."""

    catalog_exclude: ClassVar[bool] = True
    _count_field: ClassVar[str] = "switches_checked"
    _require_latest_timestamp: ClassVar[bool] = True

    def _pass_message(self, probes: dict[str, object]) -> str:
        """Build the pass message from validated evidence."""
        return (
            f"{self._log_label} available from {probes[self._count_field]} switch(es) "
            f"via {probes['log_source']} ({probes['entry_count']} {self._entries_label})"
        )


class GeneralSwitchLogsCheck(_SwitchLogCheck):
    """Validate general switch logs are available.

    Config:
        step_output: The general_switch_logs step output to check

    Step output:
        tests: dict with log_endpoint_reachable, switch_log_source_present,
               entries_queryable
        For provider-hidden switch planes, all required subtests may pass with
        provider_hidden=true instead of concrete endpoint probes.
        tests.<check>.probes.switches_checked: Positive integer count of switches
            inspected
        tests.<check>.probes.log_source: Non-empty switch log source identifier
        tests.<check>.probes.entry_count: Non-negative integer count of log entries
        tests.<check>.probes.latest_timestamp: Non-empty timestamp for the latest entry
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check general switch logs are available"
    _required_tests: ClassVar[list[str]] = [
        "log_endpoint_reachable",
        "switch_log_source_present",
        "entries_queryable",
    ]
    _log_label: ClassVar[str] = "General switch logs"


class SwitchSyslogCheck(_SwitchLogCheck):
    """Validate switch syslogs are available.

    Config:
        step_output: The switch_syslogs step output to check

    Step output:
        tests: dict with syslog_endpoint_reachable, switch_syslog_source_present,
               entries_recent
        For provider-hidden switch planes, all required subtests may pass with
        provider_hidden=true instead of concrete endpoint probes.
        tests.<check>.probes.switches_checked: Positive integer count of switches
            inspected
        tests.<check>.probes.log_source: Non-empty switch syslog source identifier
        tests.<check>.probes.entry_count: Positive integer count of recent syslog entries
        tests.<check>.probes.latest_timestamp: Non-empty timestamp for the latest entry
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check switch syslogs are available"
    _required_tests: ClassVar[list[str]] = [
        "syslog_endpoint_reachable",
        "switch_syslog_source_present",
        "entries_recent",
    ]
    _log_label: ClassVar[str] = "Switch syslogs"
    _entry_count_positive: ClassVar[bool] = True
    _entries_label: ClassVar[str] = "recent entries"


class SwitchKernelLogsCheck(_SwitchLogCheck):
    """Validate switch kernel logs are available.

    Config:
        step_output: The switch_kernel_logs step output to check

    Step output:
        tests: dict with log_endpoint_reachable, kernel_log_source_present,
               entries_queryable
        For provider-hidden switch planes, all required subtests may pass with
        provider_hidden=true instead of concrete endpoint probes.
        tests.<check>.probes.switches_checked: Positive integer count of switches
            inspected
        tests.<check>.probes.log_source: Non-empty switch kernel log source identifier
        tests.<check>.probes.entry_count: Non-negative integer count of kernel log entries
        tests.<check>.probes.latest_timestamp: Non-empty timestamp for the latest entry
    """

    catalog_exclude: ClassVar[bool] = False
    description: ClassVar[str] = "Check switch kernel logs are available"
    _required_tests: ClassVar[list[str]] = [
        "log_endpoint_reachable",
        "kernel_log_source_present",
        "entries_queryable",
    ]
    _log_label: ClassVar[str] = "Switch kernel logs"
