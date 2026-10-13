# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise upgrade evidence against outages, incomplete upgrades and PDB bypasses."""

import json
from pathlib import Path
from typing import Any

import pytest

from isvtest.validations.k8s_upgrades import (
    K8sControlPlaneUpgradeCheck,
    K8sManagedUpgradePolicyCheck,
    K8sNodePoolUpgradeCheck,
)

CHECKS = {
    "managed_upgrade_policy": K8sManagedUpgradePolicyCheck,
    "control_plane_upgrade": K8sControlPlaneUpgradeCheck,
    "node_pool_upgrade": K8sNodePoolUpgradeCheck,
}
EXAMPLES = Path(__file__).resolve().parents[2] / "isvctl/configs/providers/shared/k8s/upgrade-example.json"


def evidence(name: str) -> dict[str, Any]:
    """Load a fresh copy of the documented provider contract example."""
    return json.loads(EXAMPLES.read_text())[name]


def grade(name: str, output: Any, **config: Any) -> bool:
    """Run the real check with provider step output and optional policy settings."""
    check = CHECKS[name](config={"step_output": output, **config})
    check.run()
    return check.passed


@pytest.mark.parametrize("name", CHECKS)
def test_valid_evidence(name: str) -> None:
    """All three documented observation contracts pass."""
    assert grade(name, evidence(name))


@pytest.mark.parametrize("name", CHECKS)
def test_unconfigured_skips(name: str) -> None:
    """No configured upgrade is not a successful upgrade."""
    with pytest.raises(pytest.skip.Exception):
        CHECKS[name](config={}).run()


@pytest.mark.parametrize("name", CHECKS)
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_failed_step(name: str, value: Any) -> None:
    """Strict execution success cannot be replaced by truthy values."""
    output = evidence(name)
    output["success"] = value
    assert not grade(name, output)


@pytest.mark.parametrize("name", CHECKS)
def test_missing_evidence(name: str) -> None:
    """Every required top-level field must be present, not only a passed flag."""
    for field in evidence(name):
        if field == "platform":
            continue
        output = evidence(name)
        output.pop(field)
        assert not grade(name, output), field


@pytest.mark.parametrize("name", ["control_plane_upgrade", "node_pool_upgrade"])
@pytest.mark.parametrize("fault", ["cleanup", "running", "reverse", "nan", "boolean", "wrong-name", "skip"])
def test_invalid_operation_or_cleanup(name: str, fault: str) -> None:
    """No success for in-progress, malformed or unclean experiments."""
    output = evidence(name)
    if fault == "cleanup":
        output["cleanup_errors"] = ["canary namespace still exists"]
    elif fault == "running":
        output["operation"]["status"] = "running"
    elif fault in ("reverse", "nan", "boolean"):
        output["operation"]["completed_at"] = {"reverse": 99, "nan": float("nan"), "boolean": True}[fault]
    elif fault == "wrong-name":
        output["test_name"] = "another-check"
    else:
        output["skipped"] = True
    assert not grade(name, output)


@pytest.mark.parametrize("name", ["control_plane_upgrade", "node_pool_upgrade"])
@pytest.mark.parametrize(
    "fault", ["outage", "wrong-body", "slow", "empty", "before-only", "after-only", "gap", "unordered", "duplicate"]
)
def test_continuity_failures(name: str, fault: str) -> None:
    """Availability requires correct responses throughout a bounded observation window."""
    output = evidence(name)
    probes = output["application"]["probes"]
    if fault == "outage":
        probes[2]["status_code"] = 503
    elif fault == "wrong-body":
        probes[2]["response"] = "load balancer default page"
    elif fault == "slow":
        probes[2]["duration_seconds"] = 2.1
    elif fault == "empty":
        probes.clear()
    elif fault == "before-only":
        probes[:] = [{**probes[0], "at": t} for t in (80, 85, 90, 95)]
    elif fault == "after-only":
        probes[:] = [{**probes[0], "at": t} for t in (125, 130, 135)]
    elif fault == "gap":
        probes.pop(2)
    elif fault == "unordered":
        probes.reverse()
    else:
        probes[2]["at"] = probes[1]["at"]
    assert not grade(name, output)


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed_version_after", "1.34.3"),
        ("observed_version_after", "1.35.2"),
        ("observed_version_before", "1.36.1"),
        ("observed_version_before", "1.33.1"),
        ("observed_version_after", "1.35"),
        ("maintenance_window_required", True),
        ("maintenance_window_required", "false"),
    ],
)
def test_control_plane_requires_minor_transition(field: str, value: Any) -> None:
    """A patch, no-op, downgrade, skipped minor or wrong target cannot pass."""
    output = evidence("control_plane_upgrade")
    output[field] = value
    assert not grade("control_plane_upgrade", output)


def test_vendor_version_suffix() -> None:
    """Provider version suffixes do not change the semantic release comparison."""
    output = evidence("control_plane_upgrade")
    output["observed_version_after"] = "v1.35.1-provider.123"
    assert grade("control_plane_upgrade", output)


@pytest.mark.parametrize(
    "fault", ["tenant", "missing-security", "empty-policy", "unsupported", "status", "unmanaged", "wrong-transition"]
)
def test_policy_requires_operator_evidence(fault: str) -> None:
    """Published documentation alone does not prove this service is maintained."""
    output = evidence("managed_upgrade_policy")
    policy = output["policy"]
    if fault == "tenant":
        policy["owner"] = "tenant"
    elif fault == "missing-security":
        policy["maintenance_types"] = ["routine"]
    elif fault == "empty-policy":
        policy["supported_transitions"] = []
    elif fault == "unsupported":
        policy["supported"] = False
    elif fault == "unmanaged":
        policy["managed_control_plane"] = False
    elif fault == "wrong-transition":
        policy["supported_transitions"][0]["from"] = "1.33.1"
    else:
        policy["status_reference"] = ""
    assert not grade("managed_upgrade_policy", output)


@pytest.mark.parametrize(
    "fault",
    [
        "automatic",
        "no-op",
        "wrong-target",
        "not-ready",
        "cordoned",
        "missing-node",
        "duplicate-node",
        "partial-rollout",
        "overlap",
        "out-of-window",
        "budget-breach",
        "stale",
        "missing-pdb",
        "missing-samples",
        "no-block",
        "wrong-rejection",
        "wrong-operation",
        "audit-gap",
        "delete",
        "no-drain",
    ],
)
def test_node_upgrade_false_positives(fault: str) -> None:
    """Reject skipped nodes, parallel replacement, PDB violations and force deletion."""
    output = evidence("node_pool_upgrade")
    if fault == "automatic":
        output["operation"]["initiated_by"] = "provider"
    elif fault == "no-op":
        output["nodes_before"][0]["revision"] = "image-v2"
    elif fault == "wrong-target":
        output["nodes_after"][0]["revision"] = "image-v1"
    elif fault in ("not-ready", "cordoned"):
        output["nodes_after"][0]["ready" if fault == "not-ready" else "schedulable"] = False
    elif fault == "missing-node":
        output["nodes_after"].pop()
    elif fault == "duplicate-node":
        output["nodes_after"][1]["uid"] = output["nodes_after"][0]["uid"]
    elif fault == "partial-rollout":
        output["rollouts"].pop()
    elif fault == "overlap":
        output["rollouts"][1]["drain_started_at"] = 105
    elif fault == "out-of-window":
        output["rollouts"][1]["drained_at"] = 121
    elif fault == "budget-breach":
        output["pdb"]["samples"][2]["current_healthy"] = 1
    elif fault == "stale":
        output["pdb"]["samples"][2]["observed_generation"] = 0
    elif fault == "missing-pdb":
        output["pdb"]["samples"][2]["uid"] = "another-pdb"
    elif fault == "missing-samples":
        output["pdb"]["samples"].pop(2)
    elif fault == "no-block":
        output["pdb"].pop("blocked_eviction")
    elif fault == "wrong-rejection":
        output["pdb"]["blocked_eviction"]["reason"] = "RateLimit"
    elif fault == "wrong-operation":
        output["pdb"]["blocked_eviction"]["at"] = 90
    elif fault == "audit-gap":
        output["pdb"]["audit_complete"] = False
    elif fault == "delete":
        output["pdb"]["evictions"][0]["method"] = "delete"
    else:
        output["pdb"]["evictions"].pop()
    assert not grade("node_pool_upgrade", output)


def test_in_place_node_upgrade() -> None:
    """Replacement UIDs are optional when an upgrade updates the same nodes."""
    output = evidence("node_pool_upgrade")
    for before, after in zip(output["nodes_before"], output["nodes_after"]):
        after["uid"] = before["uid"]
    assert grade("node_pool_upgrade", output)


def test_budget_relaxation_allows_brief_status_reconciliation() -> None:
    """An asynchronous generation transition still preserves the stricter healthy count."""
    output = evidence("node_pool_upgrade")
    sample = output["pdb"]["samples"][2]
    sample["generation"] = sample["observed_generation"] + 1
    assert grade("node_pool_upgrade", output)


def test_persistent_stale_budget_fails() -> None:
    """Generation lag cannot persist for a whole observation bound."""
    output = evidence("node_pool_upgrade")
    for sample in output["pdb"]["samples"][2:4]:
        sample["generation"] = sample["observed_generation"] + 1
    assert not grade("node_pool_upgrade", output)


def test_terminating_pods_temporarily_increase_expected_count() -> None:
    """Reproduce live integer-minAvailable status while a replacement starts."""
    output = evidence("node_pool_upgrade")
    sample = output["pdb"]["samples"][2]
    sample.update(expected_pods=4, desired_healthy=2, current_healthy=2)
    assert grade("node_pool_upgrade", output)


@pytest.mark.parametrize("index,count", [(2, 2), (0, 4), (-1, 4)])
def test_budget_coverage_and_final_convergence(index: int, count: int) -> None:
    """Missing coverage or unrecovered extra pods cannot masquerade as normal replacement."""
    output = evidence("node_pool_upgrade")
    output["pdb"]["samples"][index]["expected_pods"] = count
    assert not grade("node_pool_upgrade", output)


@pytest.mark.parametrize(
    "fault", ["unhealthy-start", "unrecovered-canary", "late-eviction", "late-rejection", "other-pod"]
)
def test_drain_evidence_belongs_to_recovered_rollout(fault: str) -> None:
    """Historical evictions or an unrecovered canary cannot substantiate this rollout."""
    output = evidence("node_pool_upgrade")
    if fault == "unhealthy-start":
        output["nodes_before"][0]["ready"] = False
    elif fault == "unrecovered-canary":
        output["pdb"]["samples"][-1]["current_healthy"] = 2
    elif fault == "late-eviction":
        output["pdb"]["evictions"][0]["at"] = 112
    elif fault == "late-rejection":
        output["pdb"]["blocked_eviction"]["at"] = 111
    else:
        output["pdb"]["evictions"][0]["pod_uid"] = "unrelated-pod"
    assert not grade("node_pool_upgrade", output)


@pytest.mark.parametrize("name", ["control_plane_upgrade", "node_pool_upgrade"])
@pytest.mark.parametrize(
    "config",
    [
        {"max_probe_gap_seconds": 0},
        {"max_probe_gap_seconds": float("inf")},
        {"max_probe_gap_seconds": True},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": "2"},
    ],
)
def test_invalid_observer_policy(name: str, config: dict[str, Any]) -> None:
    """Invalid validation-side bounds cannot silently disable evidence checks."""
    assert not grade(name, evidence(name), **config)
