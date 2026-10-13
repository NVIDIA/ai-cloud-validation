# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise EKS audit provenance, blocked drains and cleanup without cloud mutation."""

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "eks_upgrade_test", ROOT / "isvctl/configs/providers/aws/scripts/eks/upgrade_test.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
with pytest.MonkeyPatch.context() as patch:
    patch.syspath_prepend(str(Path(SPEC.origin).parent))
    SPEC.loader.exec_module(MODULE)


def observations() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Provide an original node and pod identified before its drain."""
    return (
        [
            {
                "at": 100,
                "pdb": {"at": 100, "uid": "budget", "disruptions_allowed": 0},
                "pods": [{"name": "canary-1", "uid": "pod-1", "node": "worker-1"}],
            }
        ],
        [{"name": "worker-1", "uid": "node-1"}],
    )


def audit(code: int = 429, username: str = "eks:node-manager") -> dict[str, Any]:
    """Represent a real audit response, separate from provider operation status."""
    return {
        "auditID": "request-1",
        "stage": "ResponseComplete",
        "verb": "create",
        "user": {"username": username},
        "requestReceivedTimestamp": "1970-01-01T00:01:41Z",
        "objectRef": {"resource": "pods", "subresource": "eviction", "name": "canary-1"},
        "responseStatus": {
            "code": code,
            "message": "Cannot evict pod as it would violate the pod's disruption budget.",
        },
    }


def test_actual_updater_budget_rejection() -> None:
    """Only a budget rejection by the managed updater establishes protection."""
    samples, nodes = observations()
    result = MODULE.drain_evidence([audit()], samples, nodes)
    assert result["blocked_eviction"] == {
        "at": 101,
        "node_uid": "node-1",
        "pod_uid": "pod-1",
        "pdb_uid": "budget",
        "status_code": 429,
        "method": "eviction",
        "reason": "DisruptionBudget",
        "disruptions_allowed": 0,
    }


@pytest.mark.parametrize("fault", ["manual", "rate-limit", "successful", "not-eviction", "future-status"])
def test_false_budget_evidence(fault: str) -> None:
    """Manual eviction, generic throttling and later status cannot prove updater behavior."""
    samples, nodes = observations()
    record = audit()
    if fault == "manual":
        record["user"]["username"] = "cluster-admin"
    elif fault == "rate-limit":
        record["responseStatus"]["message"] = "Too many requests"
    elif fault == "successful":
        record["responseStatus"]["code"] = 201
    elif fault == "not-eviction":
        record["objectRef"]["subresource"] = "status"
    else:
        samples[0]["pdb"]["at"] = 102
    assert MODULE.drain_evidence([record], samples, nodes)["blocked_eviction"] is None


def test_force_delete_is_preserved() -> None:
    """A successful direct deletion must remain visible for the validator to reject."""
    samples, nodes = observations()
    record = audit(200)
    record["verb"] = "delete"
    record["objectRef"].pop("subresource")
    assert MODULE.drain_evidence([record], samples, nodes)["evictions"][0]["method"] == "delete"


def test_unmapped_updater_mutation_fails() -> None:
    """Incomplete pod observations cannot silently discard destructive updater requests."""
    samples, nodes = observations()
    record = audit(201)
    record["objectRef"]["name"] = "unobserved-pod"
    with pytest.raises(RuntimeError, match="mapping"):
        MODULE.drain_evidence([record], samples, nodes)


def test_surge_rollout_has_no_invented_node_pairing() -> None:
    """Drain completion follows pod removal, independently of early replacement readiness."""
    samples, nodes = observations()
    pdb = MODULE.drain_evidence(
        [audit(), {**audit(201), "requestReceivedTimestamp": "1970-01-01T00:01:42Z"}], samples, nodes
    )
    samples.append({"at": 103, "pods": []})
    assert MODULE.rollouts(samples, nodes, pdb) == [{"old_uid": "node-1", "drain_started_at": 101, "drained_at": 103}]


def test_audit_pagination_and_stage_deduplication() -> None:
    """Read every page and retain only completed responses for each audit request."""
    record = audit()

    class Logs:
        """Return paginated CloudWatch audit evidence."""

        def get_paginator(self, name: str) -> Any:
            """Expose the expected complete-pagination API."""
            assert name == "filter_log_events"
            return self

        def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
            """Include a request stage and duplicated response across separate pages."""
            assert kwargs["startTime"] == 100000
            return [
                {"events": [{"message": json.dumps({**record, "stage": "RequestReceived"})}]},
                {"events": [{"message": json.dumps(record)}, {"message": json.dumps(record)}]},
            ]

    assert MODULE.audit_events(Logs(), "cluster", "namespace", 100, 110) == [record]


def test_namespace_cleanup_checks_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not delete a namespace whose ownership token changed."""
    canary = MODULE.UpgradeCanary(["kubectl"], node_selector={"pool": "test"}, source_cidrs=["192.0.2.1/32"])
    canary.created = True
    calls = []

    def command(args: list[str], **kwargs: Any) -> str:
        """Record all attempts; return a namespace owned by someone else."""
        calls.append(args)
        return json.dumps({"metadata": {"labels": {"isv-upgrade-owner": "someone-else"}}})

    monkeypatch.setattr(canary, "command", command)
    with pytest.raises(RuntimeError, match="ownership"):
        canary.cleanup()
    assert len(calls) == 1 and calls[0][0] == "get"


def test_partial_create_registers_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost create response still leaves cleanup registered for the unique namespace."""
    canary = MODULE.UpgradeCanary(["kubectl"], node_selector={"pool": "test"}, source_cidrs=["192.0.2.1/32"])

    def fail(*args: Any, **kwargs: Any) -> str:
        """Simulate an API timeout after create was submitted."""
        raise TimeoutError("lost response")

    monkeypatch.setattr(canary, "command", fail)
    with pytest.raises(TimeoutError):
        canary.create()
    assert canary.created


def test_eks_configuration_is_opt_in() -> None:
    """Bind all three canonical contracts without adding a default provider config."""
    path = ROOT / "isvctl/configs/providers/aws/config/eks-upgrades.yaml"
    merged = merge_yaml_files([str(path.with_name("eks.yaml")), str(path)])
    RunConfig.model_validate(merged)
    assert all("../scripts/eks/upgrade_test.py" in s["command"] for s in merged["commands"]["kubernetes"]["steps"])


def test_eks_command_imports_from_config_directory() -> None:
    """The provider command loads its adjacent helpers without the removed shared directory."""
    path = ROOT / "isvctl/configs/providers/aws/config/eks-upgrades.yaml"
    merged = merge_yaml_files([str(path.with_name("eks.yaml")), str(path)])
    command = shlex.split(merged["commands"]["kubernetes"]["steps"][0]["command"])
    result = subprocess.run(
        [sys.executable, *command[1:], "--help"],
        cwd=path.parent,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "node_pool_upgrade" in result.stdout


def test_requires_exact_cluster_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing/mismatched opt-in fails before contacting AWS or creating artifacts."""
    for key, value in {
        "EKS_UPGRADE_CLUSTER": "disposable",
        "EKS_UPGRADE_NODE_POOL": "pool",
        "EKS_UPGRADE_TARGET_VERSION": "1.36",
        "AWS_REGION": "us-east-2",
        "EKS_UPGRADE_ALLOW": "another-cluster",
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="exact disposable cluster"):
        MODULE.Experiment("control_plane_upgrade")
