# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grade provider upgrade evidence; cloud operations belong in the bound scripts.

Continuity means no failed application requests within a bounded sampling interval,
not proof that an arbitrarily short outage could never have occurred. Provider
adapters must collect requests independently of upgrade-status polling.
"""

import math
import re
from itertools import pairwise
from typing import Any, ClassVar

import pytest

from isvtest.core.validation import BaseValidation


def _text(value: Any) -> str:
    """Require a nonempty evidence identifier or observed value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing nonempty text evidence")
    return value


def _number(value: Any) -> float:
    """Reject strings, booleans and nonfinite timestamps or durations."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Expected a finite nonnegative number")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    """Require actual integer counts, not truthy booleans."""
    if type(value) is not int or value < minimum:
        raise ValueError(f"Expected an integer >= {minimum}")
    return value


def _version(value: Any) -> tuple[int, int, int]:
    """Parse release versions, allowing Kubernetes vendor build suffixes."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", _text(value))
    if not match:
        raise ValueError("Expected a full Kubernetes release version")
    return tuple(int(part) for part in match.groups())


def _output(check: BaseValidation, name: str) -> dict[str, Any]:
    """Skip only unconfigured checks; configured failures must remain failures."""
    output = check.config.get("step_output")
    if output is None:
        pytest.skip(f"{name} requires a provider upgrade evidence step")
    if not isinstance(output, dict) or output.get("test_name") != name:
        raise ValueError(f"Invalid {name} evidence")
    if output.get("cleanup_errors"):
        raise ValueError(f"Upgrade test cleanup failed: {output['cleanup_errors']}")
    if output.get("success") is not True or output.get("skipped"):
        raise ValueError(str(output.get("error") or "Upgrade evidence step did not succeed"))
    return output


def _operation(output: dict[str, Any]) -> tuple[float, float]:
    """Require a completed, identified operation and an ordered observation window."""
    operation = output["operation"]
    _text(operation["id"])
    if operation["status"] != "succeeded":
        raise ValueError("Upgrade did not reach its successful terminal state")
    start, end = _number(operation["started_at"]), _number(operation["completed_at"])
    if end <= start:
        raise ValueError("Upgrade observation window is empty or reversed")
    return start, end


def _coverage(samples: Any, start: float, end: float, max_gap: float) -> None:
    """Require ordered samples before, during and after the whole upgrade."""
    if not isinstance(samples, list) or len(samples) < 3 or max_gap <= 0:
        raise ValueError("Insufficient observation coverage")
    times = [_number(sample["at"]) for sample in samples]
    if times[0] >= start or times[-1] <= end or not any(start <= t <= end for t in times):
        raise ValueError("Observations must bracket the upgrade and include its execution")
    if any(not 0 < b - a <= max_gap for a, b in pairwise(times)):
        raise ValueError("Observation gap exceeds the limit or timestamps are unordered")


def _continuity(check: BaseValidation, output: dict[str, Any], start: float, end: float) -> None:
    """Check application responses, timeout bounds and full-window coverage."""
    application = output["application"]
    _text(application["endpoint"])
    expected = _text(application["expected_response"])
    gap = _number(check.config.get("max_probe_gap_seconds", 5))
    timeout = _number(check.config.get("request_timeout_seconds", 2))
    if timeout <= 0:
        raise ValueError("Request timeout must be positive")
    probes = application["probes"]
    _coverage(probes, start, end, gap)
    for probe in probes:
        if _integer(probe["status_code"]) != 200 or probe["response"] != expected:
            raise ValueError("Application request failed or returned the wrong workload response")
        if _number(probe["duration_seconds"]) > timeout:
            raise ValueError("Application request exceeded the configured timeout")


class K8sManagedUpgradePolicyCheck(BaseValidation):
    """Require live managed-service configuration and a supported upgrade process."""

    description: ClassVar[str] = "Verify provider-managed control-plane version policy and supported transitions."

    def run(self) -> None:
        """Check the configured service and catalogue, without inventing maintenance history."""
        try:
            output = _output(self, "managed_upgrade_policy")
            policy = output["policy"]
            if policy["owner"] != "provider" or not {"routine", "security"}.issubset(policy["maintenance_types"]):
                raise ValueError("Provider must own routine maintenance and security updates")
            for field in ("reference", "status_reference", "configuration_reference"):
                _text(policy[field])
            if policy["managed_control_plane"] is not True or policy["supported"] is not True:
                raise ValueError("Control plane is not a supported provider-managed service")
            current = _version(output["observed_version"])
            transitions = policy["supported_transitions"]
            if not isinstance(transitions, list) or not transitions:
                raise ValueError("Missing supported upgrade transitions")
            for transition in transitions:
                before, after = _version(transition["from"]), _version(transition["to"])
                if before != current or after[0] != before[0] or after[1] != before[1] + 1:
                    raise ValueError("Supported transition must advance the current minor by one")
        except (KeyError, TypeError, ValueError) as error:
            self.set_failed(str(error))
            return
        self.set_passed("Live managed control-plane configuration and supported version transition verified")


class K8sControlPlaneUpgradeCheck(BaseValidation):
    """Require a completed minor upgrade and independently sampled application continuity."""

    description: ClassVar[str] = "Verify a minor control-plane upgrade without observed application downtime."

    def run(self) -> None:
        """Reject no-op upgrades, maintenance windows and gaps in application monitoring."""
        try:
            output = _output(self, "control_plane_upgrade")
            start, end = _operation(output)
            before = _version(output["observed_version_before"])
            after = _version(output["observed_version_after"])
            target = _version(output["target_version"])
            if after != target or after[0] != before[0] or after[1] != before[1] + 1:
                raise ValueError("API server must reach the requested next minor version")
            if output["operation"]["executed_by"] != "provider":
                raise ValueError("Control-plane upgrade must be provider managed")
            if output["maintenance_window_required"] is not False:
                raise ValueError("Upgrade required an application maintenance window")
            _continuity(self, output, start, end)
        except (KeyError, TypeError, ValueError) as error:
            self.set_failed(str(error))
            return
        self.set_passed(
            "Minor upgrade completed with no failed application probes within the configured sampling bounds"
        )


class K8sNodePoolUpgradeCheck(BaseValidation):
    """Require a tenant rollout, completed node transitions and exercised PDB protection."""

    description: ClassVar[str] = (
        "Verify user-initiated rolling node upgrades preserve application availability and PDBs."
    )

    def run(self) -> None:
        """Check every pool member, rolling concurrency and budget-blocked drain evidence."""
        try:
            output = _output(self, "node_pool_upgrade")
            start, end = _operation(output)
            if output["operation"]["initiated_by"] != "tenant":
                raise ValueError("Node upgrade must be user initiated")
            _continuity(self, output, start, end)
            target = _text(output["target_revision"])
            before, after = output["nodes_before"], output["nodes_after"]
            if (
                not isinstance(before, list)
                or len(before) < 2
                or not isinstance(after, list)
                or len(after) != len(before)
            ):
                raise ValueError("Rolling upgrade requires at least two nodes and restored pool capacity")
            old_ids = {_text(node["uid"]) for node in before}
            new_ids = {_text(node["uid"]) for node in after}
            if len(old_ids) != len(before) or len(new_ids) != len(after):
                raise ValueError("Duplicate node observations")
            if any(_text(node["revision"]) == target for node in before):
                raise ValueError("Every selected node must require the requested revision")
            if any(node["ready"] is not True or node["schedulable"] is not True for node in before):
                raise ValueError("Selected pool must start healthy and schedulable")
            if any(
                node["revision"] != target or node["ready"] is not True or node["schedulable"] is not True
                for node in after
            ):
                raise ValueError("Pool has not converged to healthy, schedulable nodes at the target revision")
            rollouts = output["rollouts"]
            if len(rollouts) != len(before) or {r["old_uid"] for r in rollouts} != old_ids:
                raise ValueError("Missing or duplicated node drain observations")
            events = []
            for rollout in rollouts:
                drained, ready = _number(rollout["drain_started_at"]), _number(rollout["drained_at"])
                if not start <= drained < ready <= end:
                    raise ValueError("Node rollout lies outside the upgrade window")
                events.extend(((drained, 1), (ready, -1)))
            limit = _integer(self.config.get("max_unavailable_nodes", 1), 1)
            if limit >= len(before):
                raise ValueError("Rolling concurrency must be below the pool size")
            active = 0
            for _, change in sorted(events):
                active += change
                if active > limit:
                    raise ValueError("Node drains exceeded configured rolling concurrency")
            self._pdb(output, start, end, old_ids)
        except (KeyError, TypeError, ValueError) as error:
            self.set_failed(str(error))
            return
        self.set_passed(
            "Rolling node upgrade completed; application probes, budget protection and pool recovery verified"
        )

    def _pdb(self, output: dict[str, Any], start: float, end: float, nodes: set[str]) -> None:
        """Require a restrictive budget to block this updater, then protected evictions."""
        pdb = output["pdb"]
        uid = _text(pdb["uid"])
        minimum = _integer(pdb["min_available"], 1)
        replicas = _integer(pdb["replicas"], minimum + 1)
        samples = pdb["samples"]
        _coverage(samples, start, end, _number(self.config.get("max_probe_gap_seconds", 5)))
        stale_since = None
        for sample in samples:
            # Integer minAvailable counts selected pods, including terminating
            # pods alongside replacements. It is not the Deployment replica target.
            if sample["uid"] != uid or _integer(sample["expected_pods"]) < replicas:
                raise ValueError("PDB disappeared, changed identity or lost workload coverage")
            desired = _integer(sample["desired_healthy"], minimum)
            if not desired <= _integer(sample["current_healthy"]) <= replicas:
                raise ValueError("Workload availability fell below its disruption budget")
            generation = _integer(sample["generation"], 1)
            observed = _integer(sample["observed_generation"], 1)
            if observed > generation or generation - observed > 1:
                raise ValueError("PDB status has invalid or stale generation evidence")
            # Relaxing the test budget is asynchronous. Its previous, stricter
            # status may briefly remain visible; still enforce its healthy count.
            stale_since = (sample["at"] if stale_since is None else stale_since) if observed != generation else None
            if stale_since is not None and sample["at"] - stale_since >= self.config.get("max_probe_gap_seconds", 5):
                raise ValueError("PDB status did not reconcile within the observation bound")
        if any(s["generation"] != s["observed_generation"] for s in (samples[0], samples[-1])):
            raise ValueError("PDB baseline and final state must be reconciled")
        if any(s["expected_pods"] != replicas for s in (samples[0], samples[-1])):
            raise ValueError("PDB coverage must converge to the canary replica count")
        if samples[0]["current_healthy"] != replicas or samples[-1]["current_healthy"] != replicas:
            raise ValueError("Canary must start healthy and recover all replicas after the rollout")
        intervals = {r["old_uid"]: (r["drain_started_at"], r["drained_at"]) for r in output["rollouts"]}
        guard = pdb["blocked_eviction"]
        if not start <= _number(guard["at"]) <= end or guard["node_uid"] not in nodes or guard["pdb_uid"] != uid:
            raise ValueError("Budget-blocked eviction is not part of this rollout")
        _text(guard["pod_uid"])
        drain_start, drain_end = intervals[guard["node_uid"]]
        if not drain_start <= guard["at"] < drain_end:
            raise ValueError("Budget rejection occurred outside the node drain")
        if (
            _integer(guard["status_code"]) != 429
            or guard["reason"] != "DisruptionBudget"
            or _integer(guard["disruptions_allowed"]) != 0
        ):
            raise ValueError("Missing observed budget rejection from the node updater")
        if pdb["audit_complete"] is not True:
            raise ValueError("Incomplete drain audit evidence")
        evicted_nodes = set()
        blocked_pod_released = False
        for eviction in pdb["evictions"]:
            if eviction["method"] != "eviction" or not 200 <= _integer(eviction["status_code"]) < 300:
                raise ValueError("Node updater bypassed eviction protection or failed to drain")
            if eviction["pdb_uid"] != uid or eviction["node_uid"] not in nodes:
                raise ValueError("Eviction is outside the protected test workload")
            _text(eviction["pod_uid"])
            if not _number(guard["at"]) < _number(eviction["at"]) <= end:
                raise ValueError("Expected successful protected evictions after the blocked drain")
            drain_start, drain_end = intervals[eviction["node_uid"]]
            if not drain_start <= eviction["at"] < drain_end:
                raise ValueError("Eviction occurred outside the node drain")
            if eviction["pod_uid"] == guard["pod_uid"] and eviction["node_uid"] == guard["node_uid"]:
                blocked_pod_released = True
            evicted_nodes.add(eviction["node_uid"])
        if evicted_nodes != nodes or not blocked_pod_released:
            raise ValueError("Every selected node must exercise the protected workload drain")
