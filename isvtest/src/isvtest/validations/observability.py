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
    detail = messages[0] if messages else "BMC plane is provider-owned"
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
    labels: ClassVar[tuple[str, ...]] = ("network",)

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
    labels: ClassVar[tuple[str, ...]] = ("bare_metal",)

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
    labels: ClassVar[tuple[str, ...]] = ("security",)

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
    labels: ClassVar[tuple[str, ...]] = ("security", "gpu")

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
