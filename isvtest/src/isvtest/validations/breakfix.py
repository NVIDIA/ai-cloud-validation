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

"""Break-fix / break-fix validations (BFX01-BFX06).

Provider-agnostic checks over step JSON output. Lifecycle steps may emit
``skipped`` when a platform lacks the mutating break-fix API (for example
Maestro/GPUd integrations not yet wired). Query steps assert observability
of maintenance, repair, and diagnostic signals where the provider exposes them.

Every "is this signal observable" requirement (BFX02, BFX05, BFX06) is held to
the same evidence bar by ``_QueryableRecordsCheck``: the provider must report
the signal as observable *and* return at least one record demonstrating it. A
self-declared boolean is not evidence -- a provider could emit it for an API it
never called -- so a capability claimed with no records skips rather than
passes, and the requirement stays visibly unproven.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

import pytest

from isvtest.core.validation import BaseValidation


def _record_label(record: dict[str, Any], *keys: str) -> str:
    """Return the first non-blank string value among ``keys``, or ``"unknown"``."""
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _step_output(check: BaseValidation) -> dict[str, Any] | None:
    """Return the step payload, or None when the check should stop.

    Skips when the provider step reported a structured skip, and fails the check
    when the step itself failed. ``BaseValidation.execute`` also honours
    ``skipped`` before calling ``run``; repeating it here keeps a directly
    invoked ``run`` consistent with the sibling validation modules.
    """
    step_output = check.config.get("step_output", {})
    if step_output.get("skipped") is True:
        pytest.skip(step_output.get("skip_reason") or "Break-fix step skipped (not configured on this platform)")
    if not step_output.get("success"):
        check.set_failed(step_output.get("error") or "Break-fix step failed")
        return None
    return step_output


class _QueryableRecordsCheck(BaseValidation):
    """Shared machinery for the "is this signal observable" checks.

    Subclasses supply the step-output keys and the wording; the policy is the
    same for all of them: the provider must report the signal as observable,
    and must return at least one record that actually demonstrates it. A list
    with no usable records skips rather than passes, because a site with no
    records is indistinguishable from one with no API at all -- a pass there
    would assert nothing beyond the provider's own say-so.
    """

    _exclude_from_discovery: ClassVar[bool] = True
    timeout: ClassVar[int] = 120

    queryable_key: ClassVar[str]
    records_key: ClassVar[str]
    unavailable_message: ClassVar[str]
    absent_noun: ClassVar[str]
    api_label: ClassVar[str]
    record_noun: ClassVar[str]

    def _is_evidence(self, record: Any) -> bool:
        """Return whether one record demonstrates the signal.

        Defaults to "any non-empty record". Subclasses tighten this when the
        record needs specific content to prove anything.
        """
        return bool(record)

    def run(self) -> None:
        """Assert the signal is reported observable and backed by real records."""
        step_output = _step_output(self)
        if step_output is None:
            return
        if not step_output.get(self.queryable_key):
            self.set_failed(self.unavailable_message)
            return
        records = [r for r in (step_output.get(self.records_key) or []) if self._is_evidence(r)]
        if not records:
            pytest.skip(f"No {self.absent_noun} at the site; the query API cannot be demonstrated")
        self.set_passed(f"{self.api_label} query API returned {len(records)} {self.record_noun}(s)")


class MaintenanceEventsCheck(_QueryableRecordsCheck):
    """Validate upcoming/current maintenance events are queryable (BFX02-01).

    Step output:
        success, events: list[{machine_id, status, message}]
        events_queryable: bool -- API exposes maintenance event records
    """

    description: ClassVar[str] = "Query upcoming or current maintenance events for a node"

    queryable_key: ClassVar[str] = "events_queryable"
    records_key: ClassVar[str] = "events"
    unavailable_message: ClassVar[str] = "Maintenance events are not queryable via the break-fix API"
    absent_noun: ClassVar[str] = "maintenance events"
    api_label: ClassVar[str] = "Maintenance event"
    record_noun: ClassVar[str] = "event"


class RetirementNoticesCheck(_QueryableRecordsCheck):
    """Validate retirement notices for a node/rack are queryable (BFX02-02).

    Step output:
        success, notices_queryable: bool,
        notices: list[{target_type, target_id, notice_type, origin_kind,
                       origin, not_before}]
    """

    description: ClassVar[str] = "Query retirement notices for a node or rack"

    queryable_key: ClassVar[str] = "notices_queryable"
    records_key: ClassVar[str] = "notices"
    unavailable_message: ClassVar[str] = "Retirement notices are not queryable via the break-fix API"
    absent_noun: ClassVar[str] = "retirement notices"
    api_label: ClassVar[str] = "Retirement notice"
    record_noun: ClassVar[str] = "notice"

    def _is_evidence(self, record: Any) -> bool:
        """Require an authoritative retirement notice with target and time evidence."""
        if not isinstance(record, dict):
            return False
        if record.get("origin_kind") != "provider":
            return False
        if _record_label(record, "origin", "provider", "source") == "unknown":
            return False
        if record.get("target_type") not in {"instance", "machine", "node", "rack"}:
            return False
        if _record_label(record, "target_id", "machine_id", "node_id", "rack_id") == "unknown":
            return False
        if record.get("notice_type") not in {
            "retirement",
            "instance-retirement",
            "machine-retirement",
            "node-retirement",
            "rack-retirement",
        }:
            return False

        timestamp = _record_label(record, "not_before", "scheduled_at", "retire_after", "deadline")
        if timestamp == "unknown":
            return False
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except ValueError:
            return False
        return parsed_timestamp.tzinfo is not None


class RepairHistoryCheck(_QueryableRecordsCheck):
    """Validate historical repair status is queryable for a node (BFX02-03).

    Step output:
        success, history_queryable: bool, records: list[{machine_id, entries: list[dict]}]
    """

    description: ClassVar[str] = "Query historical repair status for a node"

    queryable_key: ClassVar[str] = "history_queryable"
    records_key: ClassVar[str] = "records"
    unavailable_message: ClassVar[str] = "Repair history is not queryable via the break-fix API"
    absent_noun: ClassVar[str] = "repair history"
    api_label: ClassVar[str] = "Repair history"
    record_noun: ClassVar[str] = "machine record"

    def _is_evidence(self, record: Any) -> bool:
        """A machine record proves nothing without at least one history entry."""
        return bool(isinstance(record, dict) and record.get("entries"))


class NvSwitchFirmwareCheck(BaseValidation):
    """Validate NV switch tray firmware versions are inspectable (BFX03-02).

    Step output:
        success, trays: list[{tray_id, firmware_version}]
    """

    description: ClassVar[str] = "Inspect firmware versions of NV switch trays"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Assert every reported NV switch tray exposes a firmware version."""
        step_output = _step_output(self)
        if step_output is None:
            return
        trays = step_output.get("trays")
        if not isinstance(trays, list):
            self.set_failed("Switch firmware step output is missing the 'trays' list")
            return
        min_trays = self._parse_positive_int("min_trays", default=1)
        if min_trays is None:
            return
        missing = [t for t in trays if not (isinstance(t, dict) and (t.get("firmware_version") or "").strip())]
        if missing:
            self.set_failed(f"{len(missing)} switch tray(s) missing firmware_version")
            return
        if len(trays) < min_trays:
            self.set_failed(f"Expected at least {min_trays} NV switch tray(s), got {len(trays)}")
            return
        self.set_passed(f"Firmware version queryable for {len(trays)} NV switch tray(s)")


class BmcKernelLogCheck(BaseValidation):
    """Validate BMC kernel log messages are obtainable for a node (BFX03-03).

    Step output:
        success, hosts: list[{host_id, kernel_log_available: bool}]
    """

    description: ClassVar[str] = "Obtain BMC kernel log messages for a node"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Assert BMC kernel logs are obtainable for every reported host."""
        step_output = _step_output(self)
        if step_output is None:
            return
        hosts = step_output.get("hosts")
        if not isinstance(hosts, list):
            self.set_failed("BMC kernel log step output is missing the 'hosts' list")
            return
        min_hosts = self._parse_positive_int("min_hosts", default=1)
        if min_hosts is None:
            return
        if len(hosts) < min_hosts:
            self.set_failed(f"Expected at least {min_hosts} host(s), got {len(hosts)}")
            return
        unavailable = [h for h in hosts if not h.get("kernel_log_available")]
        if unavailable:
            labels = ", ".join(_record_label(h, "host_id", "machine_id") for h in unavailable[:3])
            self.set_failed(f"BMC kernel logs unavailable for {len(unavailable)} host(s): {labels}")
            return
        self.set_passed(f"BMC kernel logs obtainable for {len(hosts)} host(s)")


class _OperationCheck(BaseValidation):
    """Shared machinery for the BFX01 mutating-operation checks.

    Subclasses name the ``operation`` flag that marks the operation as having
    taken effect and supply the wording. The provider's own ``operation.message``
    wins over the generic failure text when the operation did not take effect.
    """

    _exclude_from_discovery: ClassVar[bool] = True
    timeout: ClassVar[int] = 600

    completion_key: ClassVar[str]
    failure_message: ClassVar[str]
    label_keys: ClassVar[tuple[str, ...]]
    pass_template: ClassVar[str]

    def _pass_message(self, label: str, operation: dict[str, Any]) -> str:
        """Return the success message for a completed operation."""
        return self.pass_template.format(label=label)

    def run(self) -> None:
        """Assert the provider reported the operation as having taken effect."""
        step_output = _step_output(self)
        if step_output is None:
            return
        operation = step_output.get("operation") or {}
        if not operation.get(self.completion_key):
            self.set_failed(operation.get("message") or self.failure_message)
            return
        self.set_passed(self._pass_message(_record_label(operation, *self.label_keys), operation))


class GpuResetCheck(_OperationCheck):
    """Validate GPU reset via the break-fix API (BFX01-01).

    Step output:
        success, operation: {requested, completed, node_id}
    """

    description: ClassVar[str] = "Reset GPUs on an individual node via the breakfix API"

    completion_key: ClassVar[str] = "completed"
    failure_message: ClassVar[str] = "GPU reset did not complete"
    label_keys: ClassVar[tuple[str, ...]] = ("node_id", "machine_id")
    pass_template: ClassVar[str] = "GPU reset completed for node {label}"


class ReturnNodeMaintenanceCheck(_OperationCheck):
    """Validate returning an individual node for maintenance (BFX01-02).

    Step output:
        success, operation: {requested, accepted, machine_id, maintenance_mode}
    """

    description: ClassVar[str] = "Return an individual node to the provider for maintenance via the API"

    completion_key: ClassVar[str] = "accepted"
    failure_message: ClassVar[str] = "Node maintenance return was not accepted"
    label_keys: ClassVar[tuple[str, ...]] = ("machine_id", "node_id")
    pass_template: ClassVar[str] = "Node {label} accepted for maintenance"

    def _pass_message(self, label: str, operation: dict[str, Any]) -> str:
        """Report the maintenance mode the provider placed the node into."""
        return f"{super()._pass_message(label, operation)} (maintenance_mode={operation.get('maintenance_mode')})"


class ReturnRackMaintenanceCheck(_OperationCheck):
    """Validate returning a rack for maintenance (BFX01-03).

    Step output:
        success, operation: {requested, accepted, rack_id}
    """

    description: ClassVar[str] = "Return a rack to the provider for maintenance via the API"

    completion_key: ClassVar[str] = "accepted"
    failure_message: ClassVar[str] = "Rack maintenance return was not accepted"
    label_keys: ClassVar[tuple[str, ...]] = ("rack_id",)
    pass_template: ClassVar[str] = "Rack {label} accepted for maintenance"


class HostReplacementCheck(_OperationCheck):
    """Validate host replacement when health thresholds are breached (BFX01-05).

    Step output:
        success, operation: {requested, node_removed_from_pool, machine_id}
    """

    description: ClassVar[str] = "Request host replacement and verify node removed from pool"
    timeout: ClassVar[int] = 900

    completion_key: ClassVar[str] = "node_removed_from_pool"
    failure_message: ClassVar[str] = "Node was not removed from the allocatable pool"
    label_keys: ClassVar[tuple[str, ...]] = ("machine_id", "node_id")
    pass_template: ClassVar[str] = "Host replacement removed {label} from the pool"


class CordonNodeCheck(BaseValidation):
    """Validate cordon: unschedulable with existing workloads continuing (BFX01-04).

    Step output:
        success, operation: {cordoned, new_workloads_blocked, existing_workloads_running}
    """

    description: ClassVar[str] = "Cordon a node and verify scheduling behavior"
    timeout: ClassVar[int] = 600

    def run(self) -> None:
        """Assert the node is cordoned, blocks new work, and keeps existing work running."""
        step_output = _step_output(self)
        if step_output is None:
            return
        operation = step_output.get("operation") or {}
        if not operation.get("cordoned"):
            self.set_failed(operation.get("message") or "Node was not cordoned")
            return
        if not operation.get("new_workloads_blocked"):
            self.set_failed("New workloads were not blocked on the cordoned node")
            return
        if operation.get("existing_workloads_running") is not True:
            self.set_failed("Existing workloads were not confirmed still running on the cordoned node")
            return
        self.set_passed("Node cordoned: new workloads blocked, existing workloads continue")


class NodeHealthAgentCheck(BaseValidation):
    """Validate GPUd or Sentinel (node health agent) is running (BFX04-01).

    Step output:
        success, agents: list[{node_id, agent_name, running: bool}]
        agents_observable: bool
    """

    description: ClassVar[str] = "Check that GPUd or Sentinel is running"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Assert a node health agent is reported and running on every node."""
        step_output = _step_output(self)
        if step_output is None:
            return
        if not step_output.get("agents_observable"):
            self.set_failed("Node health agents (GPUd/Sentinel) are not observable on this platform")
            return
        agents = step_output.get("agents") or []
        if not agents:
            self.set_failed("No node health agent (GPUd/Sentinel) records returned; none is running")
            return
        not_running = [a for a in agents if isinstance(a, dict) and not a.get("running")]
        if not_running:
            labels = ", ".join(_record_label(a, "node_id", "machine_id") for a in not_running[:3])
            self.set_failed(f"Health agent not running on {len(not_running)} node(s): {labels}")
            return
        self.set_passed(f"Node health agent running on {len(agents)} node(s)")


class PlannedMaintenanceNotificationCheck(_QueryableRecordsCheck):
    """Validate tenants can be notified of planned maintenance (BFX05-01).

    Step output:
        success, notification_channel_observable: bool
        notifications: list[{machine_id, type, message, notified_at}]

    Requires a real notification record, not just the observable flag: the flag
    alone is the provider asserting its own capability.
    """

    description: ClassVar[str] = "Verify tenants can be notified of planned future node maintenance"

    queryable_key: ClassVar[str] = "notification_channel_observable"
    records_key: ClassVar[str] = "notifications"
    unavailable_message: ClassVar[str] = "Planned maintenance notification channel is not observable"
    absent_noun: ClassVar[str] = "planned maintenance notifications"
    api_label: ClassVar[str] = "Planned maintenance notification"
    record_noun: ClassVar[str] = "notification"


class FailureNotificationCheck(_QueryableRecordsCheck):
    """Validate tenants can be notified of immediate node failure (BFX06-01).

    Step output:
        success, notification_channel_observable: bool
        notifications: list[{machine_id, type, message, notified_at}]

    Requires a real notification record, for the same reason as
    PlannedMaintenanceNotificationCheck.
    """

    description: ClassVar[str] = "Verify tenants can be notified of immediate node failure"

    queryable_key: ClassVar[str] = "notification_channel_observable"
    records_key: ClassVar[str] = "notifications"
    unavailable_message: ClassVar[str] = "Immediate failure notification channel is not observable"
    absent_noun: ClassVar[str] = "immediate failure notifications"
    api_label: ClassVar[str] = "Immediate failure notification"
    record_noun: ClassVar[str] = "notification"
