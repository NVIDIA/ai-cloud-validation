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

"""Hardware ingestion and DPU health validations.

Validations for NICo bare metal hardware lifecycle:
- Hardware ingestion verification (expected vs actual machines)
- DPU health checks (agent heartbeat, probes, capabilities)
- DPU network readiness (interfaces, BGP, extension services)
"""

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation


def _machine_label(machine: dict[str, Any]) -> str:
    """Human-facing identifier for a machine.

    Prefers the NICo machine id, then the expected-machine id, then the
    chassis serial. Chassis serial is unreliable as a display label: it is
    only populated for provider-scoped tokens and otherwise falls back to the
    machine id upstream, so it should not be shown as if it were a serial.
    """
    return machine.get("machine_id") or machine.get("expected_machine_id") or machine.get("chassis_serial") or "unknown"


class HardwareIngestionCheck(BaseValidation):
    """Validate that all expected hardware has been ingested and matches the manifest.

    Compares expected-machine records against actually discovered machines.
    Each expected machine should be linked to a discovered machine with
    matching chassis serial number and healthy status.

    Config:
        step_output: The step output containing ingestion data
        min_machines: Minimum number of expected machines (default: 1)
        expected_status: List of acceptable machine statuses (default: ["Ready", "InUse"])
        require_healthy: Whether machines must have healthy health status (default: true)

    Step output (from verify_ingestion.py stub):
        success: bool
        platform: "nico"
        site_id: str
        expected_count: int
        ingested_count: int
        matched_count: int
        missing: list[dict] -- expected machines not linked to discovered machines
        extra: list[dict] -- discovered machines not in expected list
        machines: list[dict] -- per-machine details:
            chassis_serial: str -- manifest serial, debug aid only
            expected_machine_id: str
            machine_id: str | None
            status: str (MachineStatus enum)
            health: str ("healthy" | "unhealthy")
            gpu_count: int
            dpu_count: int
            capabilities: list[str]
    """

    description: ClassVar[str] = "Check all expected hardware is ingested and matches manifest"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate ingestion: every expected machine is linked, healthy, and in an acceptable state."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"Ingestion check step failed: {step_output.get('error', 'Unknown error')}")
            return

        expected_count = step_output.get("expected_count", 0)
        min_machines = self.config.get("min_machines", 1)

        if expected_count < min_machines:
            self.set_failed(f"Expected at least {min_machines} machines in manifest, got {expected_count}")
            return

        # Check for missing machines (expected but not ingested)
        missing = step_output.get("missing", [])
        missing_ids = ", ".join(_machine_label(m) for m in missing)
        if missing:
            self.report_subtest(
                "missing_machines",
                passed=False,
                message=f"{len(missing)} expected machine(s) not ingested: {missing_ids}",
            )

        # Report extra machines as informational (not a failure in shared environments)
        extra = step_output.get("extra", [])
        if extra:
            serial_list = ", ".join(_machine_label(m) for m in extra)
            self.report_subtest(
                "extra_machines",
                passed=True,
                message=f"Info: {len(extra)} additional machine(s) found beyond manifest: {serial_list}",
            )

        # Validate each matched machine
        machines = step_output.get("machines", [])
        acceptable_statuses = self.config.get("expected_status", ["Ready", "InUse"])
        require_healthy = self.config.get("require_healthy", True)

        unhealthy_machines: list[str] = []
        bad_status_machines: list[str] = []

        for machine in machines:
            machine_id = machine.get("machine_id")
            label = _machine_label(machine)

            if not machine_id:
                # Expected machine not linked to discovered machine
                continue  # Already reported in missing check

            status = machine.get("status", "Unknown")
            health = machine.get("health", "unknown")
            serial = machine.get("chassis_serial") or "n/a"

            if status not in acceptable_statuses:
                bad_status_machines.append(f"{label} (serial={serial}, status={status})")
                self.report_subtest(
                    f"machine_status_{label}",
                    passed=False,
                    message=f"Machine {label} status is {status}, expected one of {acceptable_statuses}",
                )
            else:
                self.report_subtest(
                    f"machine_status_{label}",
                    passed=True,
                    message=f"Machine {label} status is {status}",
                )

            if require_healthy:
                if health != "healthy":
                    unhealthy_machines.append(f"{label} (serial={serial}, health={health})")
                    self.report_subtest(
                        f"machine_health_{label}",
                        passed=False,
                        message=f"Machine {label} health is {health}, expected healthy",
                    )
                else:
                    self.report_subtest(
                        f"machine_health_{label}",
                        passed=True,
                        message=f"Machine {label} health is healthy",
                    )

        # Overall result
        matched_count = step_output.get("matched_count", 0)

        if missing or bad_status_machines or (require_healthy and unhealthy_machines):
            issues: list[str] = []
            if missing:
                issues.append(f"{len(missing)} missing [{missing_ids}]")
            if bad_status_machines:
                issues.append(f"{len(bad_status_machines)} bad status [{', '.join(bad_status_machines)}]")
            if unhealthy_machines:
                issues.append(f"{len(unhealthy_machines)} unhealthy [{', '.join(unhealthy_machines)}]")
            self.set_failed(
                f"Hardware ingestion issues: {'; '.join(issues)}. {matched_count}/{expected_count} machines matched."
            )
        else:
            self.set_passed(f"All {matched_count} expected machines ingested and healthy")


class DpuHealthCheck(BaseValidation):
    """Validate DPU health status for ingested machines.

    Checks that DPUs are recognized by NICo, the DPU agent heartbeat
    is active, and health probes are passing. Does NOT require an active
    instance or EVPN overlay -- uses machine-level health data only.

    Config:
        step_output: The step output containing DPU health data
        expected_dpu_count: Expected number of DPUs per machine (default: None = any > 0)
        require_heartbeat: Whether DPU agent heartbeat must be active (default: true)

    Step output (from check_dpu_health.py stub):
        success: bool
        platform: "nico"
        site_id: str
        machines_checked: int
        machines: list[dict]:
            machine_id: str
            chassis_serial: str -- debug aid only, may be empty
            status: str
            dpu_count: int
            dpu_capability: dict | None
            health_summary: str ("healthy" | "unhealthy")
            health_successes: list[str] -- probe IDs that passed
            health_alerts: list[dict]:
                id: str
                target: str
                message: str
            dpu_agent_heartbeat: bool
    """

    description: ClassVar[str] = "Check DPU health and agent heartbeat status"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate DPU presence, agent heartbeat, and machine-level health for each machine."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"DPU health check step failed: {step_output.get('error', 'Unknown error')}")
            return

        machines = step_output.get("machines", [])
        if not machines:
            self.set_failed("No machines found in step output")
            return

        expected_dpu_count = self.config.get("expected_dpu_count")
        require_heartbeat = self.config.get("require_heartbeat", True)

        # Maps the label of each failing machine to its chassis serial (insertion
        # order preserved, so the failure summary lists machines in scan order).
        failed: dict[str, str] = {}

        for machine in machines:
            label = _machine_label(machine)
            serial = machine.get("chassis_serial") or "n/a"

            # Check DPU count
            dpu_count = machine.get("dpu_count", 0)
            if dpu_count == 0:
                self.report_subtest(
                    f"dpu_presence_{label}",
                    passed=False,
                    message=f"Machine {label}: no DPUs detected",
                )
                failed[label] = serial
                # Still check alerts below -- there may be relevant health info

            elif expected_dpu_count is not None and dpu_count != expected_dpu_count:
                self.report_subtest(
                    f"dpu_count_{label}",
                    passed=False,
                    message=(f"Machine {label}: expected {expected_dpu_count} DPUs, got {dpu_count}"),
                )
                failed[label] = serial
                # Don't continue -- still check heartbeat and alerts

            else:
                self.report_subtest(
                    f"dpu_count_{label}",
                    passed=True,
                    message=f"Machine {label}: {dpu_count} DPU(s) detected",
                )

            # Check DPU agent heartbeat
            heartbeat = machine.get("dpu_agent_heartbeat", False)
            if require_heartbeat and not heartbeat:
                self.report_subtest(
                    f"dpu_heartbeat_{label}",
                    passed=False,
                    message=f"Machine {label}: DPU agent heartbeat missing",
                )
                failed[label] = serial
            elif heartbeat:
                self.report_subtest(
                    f"dpu_heartbeat_{label}",
                    passed=True,
                    message=f"Machine {label}: DPU agent heartbeat active",
                )

            # Check health_summary (covers ALL alerts, not just DPU-substring-filtered)
            health_summary = machine.get("health_summary", "unknown")
            if health_summary == "unhealthy":
                alerts = machine.get("health_alerts", [])
                alert_msgs = (
                    "; ".join(f"{a.get('id', '?')}: {a.get('message', '?')}" for a in alerts)
                    if alerts
                    else "unhealthy (see machine health for details)"
                )
                self.report_subtest(
                    f"dpu_health_{label}",
                    passed=False,
                    message=f"Machine {label}: {alert_msgs}",
                )
                failed[label] = serial
            else:
                self.report_subtest(
                    f"dpu_health_{label}",
                    passed=True,
                    message=f"Machine {label}: health status OK",
                )

            # Report successful health probes (informational)
            successes = machine.get("health_successes", [])
            dpu_probes = [s for s in successes if "dpu" in s.lower()]
            if dpu_probes:
                self.report_subtest(
                    f"dpu_probes_{label}",
                    passed=True,
                    message=(f"Machine {label}: {len(dpu_probes)} DPU probe(s) passing: {', '.join(dpu_probes)}"),
                )

        # Overall result
        total = len(machines)
        if failed:
            failed_desc = ", ".join(f"{lbl} (serial={ser})" for lbl, ser in failed.items())
            self.set_failed(f"DPU health issues on {len(failed)}/{total} machine(s): {failed_desc}")
        else:
            self.set_passed(f"All {total} machine(s) have healthy DPUs")


class DpuNetworkCheck(BaseValidation):
    """Validate DPU network connectivity on active instances.

    Requires an active instance with EVPN overlay networking configured.
    Checks DPU-attached network interfaces, BGP daemon status, and
    DPU extension service deployments.

    Config:
        step_output: The step output containing instance network data
        require_bgp: Whether BGP daemon must be enabled (default: true)

    Step output:
        success: bool
        platform: "nico"
        instance_id: str
        machine_id: str
        interfaces: list[dict]:
            name: str
            status: str (InterfaceStatus enum: Pending | Provisioning | Ready | Deleting | Error)
            type: str ("ethernet" | "infiniband" | "nvlink")
        bgp_enabled: bool
        dpu_extension_deployments: list[dict]:
            name: str
            status: str (DpuExtensionServiceDeploymentStatus: Pending | Running | Error | Failed | Terminating)
            version: str
    """

    description: ClassVar[str] = "Check DPU network interfaces and overlay connectivity"
    timeout: ClassVar[int] = 120

    # Valid running states for DPU extension deployments
    _DEPLOYMENT_OK_STATUSES: ClassVar[set[str]] = {"Running", "Pending"}

    def run(self) -> None:
        """Validate DPU network readiness: interface status, BGP, and extension deployments."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"DPU network check step failed: {step_output.get('error', 'Unknown error')}")
            return

        require_bgp = self.config.get("require_bgp", True)
        has_failures = False

        # Check interfaces
        interfaces = step_output.get("interfaces", [])
        if not interfaces:
            self.set_failed("No network interfaces found on instance")
            return

        not_ready = [iface for iface in interfaces if iface.get("status") != "Ready"]

        if not_ready:
            names = ", ".join(f"{i.get('name', '?')}({i.get('status', '?')})" for i in not_ready)
            self.report_subtest(
                "interface_status",
                passed=False,
                message=f"{len(not_ready)} interface(s) not Ready: {names}",
            )
            has_failures = True
        else:
            self.report_subtest(
                "interface_status",
                passed=True,
                message=f"All {len(interfaces)} interface(s) Ready",
            )

        # Check BGP
        if "bgp_enabled" not in step_output:
            self.report_subtest(
                "bgp_status",
                passed=False,
                message="BGP status not reported in step output",
            )
            has_failures = True
        elif require_bgp and not step_output["bgp_enabled"]:
            self.report_subtest(
                "bgp_status",
                passed=False,
                message="BGP daemon not enabled on DPU",
            )
            has_failures = True
        elif step_output.get("bgp_enabled"):
            self.report_subtest(
                "bgp_status",
                passed=True,
                message="BGP daemon enabled",
            )

        # Check DPU extension service deployments
        deployments = step_output.get("dpu_extension_deployments", [])
        failed_deployments = [d for d in deployments if d.get("status") not in self._DEPLOYMENT_OK_STATUSES]

        if failed_deployments:
            names = ", ".join(f"{d.get('name', '?')}({d.get('status', '?')})" for d in failed_deployments)
            self.report_subtest(
                "dpu_extensions",
                passed=False,
                message=f"{len(failed_deployments)} extension(s) not healthy: {names}",
            )
            has_failures = True
        elif deployments:
            self.report_subtest(
                "dpu_extensions",
                passed=True,
                message=f"All {len(deployments)} DPU extension(s) healthy",
            )

        if has_failures:
            self.set_failed("DPU network readiness issues detected")
        else:
            self.set_passed(
                f"DPU network healthy: {len(interfaces)} interface(s), "
                f"BGP={'enabled' if step_output.get('bgp_enabled') else 'n/a'}, "
                f"{len(deployments)} extension(s)"
            )
