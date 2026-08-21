# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared BFX01-02 Kubernetes maintenance reference."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.context import Context
from isvctl.orchestrator.step_executor import StepExecutor

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ISVCTL_ROOT / "configs" / "providers" / "shared" / "breakfix" / "return_node_maintenance.py"
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "kubernetes-node-maintenance.yaml"
MINIKUBE_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "minikube.yaml"


def _load_script() -> ModuleType:
    """Load the script as a module for direct testing."""
    spec = importlib.util.spec_from_file_location("test_shared_return_node_maintenance_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _completed(
    args: tuple[str, ...] = (),
    *,
    payload: dict[str, Any] | None = None,
    error: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a completed kubectl call."""
    return subprocess.CompletedProcess(
        ["kubectl", *args],
        1 if error else 0,
        stdout=json.dumps(payload) if payload is not None else "",
        stderr=error,
    )


def _node(*, unschedulable: bool = False) -> dict[str, Any]:
    """Return one Ready test node."""
    return {
        "metadata": {
            "name": "worker-1",
            "labels": {"kubernetes.io/hostname": "worker-1-host"},
        },
        "spec": {
            "unschedulable": unschedulable,
            "taints": [{"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"}],
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def test_explicit_config_wires_only_the_maintenance_reference() -> None:
    """Keep the mutating step behind its explicit provider config."""
    config = yaml.safe_load(CONFIG.read_text())
    steps = config["commands"]["bare_metal"]["steps"]

    assert steps == [
        {
            "name": "return_node_maintenance",
            "phase": "test",
            "command": "python shared/breakfix/return_node_maintenance.py",
            "args": [
                "--node={{ env.ISVTEST_BREAKFIX_NODE | default('', true) }}",
                "--timeout-seconds=120",
            ],
            "timeout": 1200,
            "requires_available_validations": ["ReturnNodeMaintenanceCheck"],
        }
    ]


def test_empty_node_renders_as_one_safe_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset target must not become a dangling command-line flag."""
    monkeypatch.delenv("ISVTEST_BREAKFIX_NODE", raising=False)
    config = RunConfig.model_validate(merge_yaml_files([CONFIG]))
    step = next(item for item in config.commands["bare_metal"].steps if item.name == "return_node_maintenance")

    assert StepExecutor()._render_args(step.args, Context(config)) == ["--node=", "--timeout-seconds=120"]


def test_normal_minikube_config_never_runs_maintenance() -> None:
    """Ordinary Kubernetes validation must not request maintenance."""
    config = yaml.safe_load(MINIKUBE_CONFIG.read_text())
    steps = config["commands"]["kubernetes"]["steps"]

    assert all(step["name"] != "return_node_maintenance" for step in steps)


def test_missing_mutation_opt_in_fails_before_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The focused config alone is not mutation authorization."""
    module = _load_script()
    monkeypatch.delenv(module.MUTATION_OPT_IN_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["return_node_maintenance.py", "--node=worker-1"])
    monkeypatch.setattr(
        module,
        "_kubectl_command",
        lambda: pytest.fail("kubectl must not run without opt-in"),
    )

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["operation"]["requested"] is False
    assert "ISVTEST_BREAKFIX_ALLOW_MUTATION=1" in payload["error"]


def test_explicit_node_is_required_before_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never choose a maintenance target on the caller's behalf."""
    module = _load_script()
    monkeypatch.setenv(module.MUTATION_OPT_IN_ENV, "1")
    monkeypatch.setattr(sys, "argv", ["return_node_maintenance.py"])
    monkeypatch.setattr(
        module,
        "_kubectl_command",
        lambda: pytest.fail("kubectl must not run without a target"),
    )

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert "requires an explicit --node" in payload["error"]


def test_permission_denial_uses_the_scoped_rbac_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal `can-i` denial remains distinct from command failure."""
    module = _load_script()
    denied = subprocess.CompletedProcess(["kubectl"], 1, stdout="no\n", stderr="")
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: denied)

    with pytest.raises(module.MaintenanceTestError, match="RBAC does not allow create on deployments in default"):
        module._require_permission(["kubectl"], "create", "deployments", namespace="default")


def test_permission_denial_with_reason_uses_the_scoped_rbac_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reason-bearing can-i denial remains an RBAC diagnosis."""
    module = _load_script()
    denied = subprocess.CompletedProcess(
        ["kubectl"],
        1,
        stdout="no - RBAC: access denied by cluster policy\n",
        stderr="",
    )
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: denied)

    with pytest.raises(module.MaintenanceTestError, match="RBAC does not allow create on deployments in default"):
        module._require_permission(["kubectl"], "create", "deployments", namespace="default")


def test_permission_query_error_is_not_reported_as_rbac_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connectivity failures preserve their command-error diagnosis."""
    module = _load_script()
    failed = subprocess.CompletedProcess(["kubectl"], 1, stdout="", stderr="connection refused")
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: failed)

    with pytest.raises(
        module.MaintenanceTestError,
        match=r"kubectl auth can-i create deployments -n default failed",
    ):
        module._require_permission(["kubectl"], "create", "deployments", namespace="default")


def test_manifests_limit_drain_to_the_owned_probe() -> None:
    """The operator must never drain pre-existing workloads."""
    module = _load_script()
    maintenance = json.loads(module._maintenance_manifest("maintenance-1", "default", "worker-1", "run-1", 30))
    deployment = json.loads(
        module._deployment_manifest(
            "probe-1",
            "default",
            "worker-1-host",
            "run-1",
            module.DEFAULT_IMAGE,
            [{"key": "nvidia.com/gpu", "operator": "Equal", "value": "present", "effect": "NoSchedule"}],
        )
    )

    assert maintenance["spec"] == {
        "requestorID": module.REQUESTOR_ID,
        "nodeName": "worker-1",
        "cordon": True,
        "drainSpec": {
            "force": False,
            "deleteEmptyDir": False,
            "podSelector": f"{module.RUN_LABEL}=run-1",
            "timeoutSeconds": 30,
        },
    }
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["nodeSelector"] == {"kubernetes.io/hostname": "worker-1-host"}
    assert pod_spec["tolerations"][0]["key"] == "nvidia.com/gpu"


def test_create_owned_resource_captures_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creation must return the API-assigned UID and exact ownership label."""
    module = _load_script()
    payload = {
        "metadata": {
            "name": "probe-1",
            "namespace": "default",
            "uid": "uid-1",
            "labels": {module.RUN_LABEL: "run-1"},
        }
    }
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: _completed(payload=payload),
    )

    created = module._create_owned_resource(
        ["kubectl"],
        "deployment",
        "probe-1",
        "default",
        "run-1",
        "{}",
    )
    assert created["metadata"]["uid"] == "uid-1"


def test_delete_owned_resource_rejects_replaced_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup must not delete a same-name object that replaced this run's UID."""
    module = _load_script()
    payload = {
        "metadata": {
            "name": "probe-1",
            "namespace": "default",
            "uid": "replacement-uid",
            "labels": {module.RUN_LABEL: "run-1"},
        }
    }
    monkeypatch.setattr(module, "_read_owned_resource", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        module,
        "_delete_resource",
        lambda *args, **kwargs: pytest.fail("replacement must not be deleted"),
    )

    with pytest.raises(module.MaintenanceTestError, match="replaced deployment"):
        module._delete_owned_resource(
            ["kubectl"],
            "deployment",
            "probe-1",
            "default",
            "run-1",
            expected_uid="original-uid",
            timeout_seconds=30,
        )


@pytest.mark.parametrize(
    ("kind", "expected_uri"),
    [
        ("deployment", "/apis/apps/v1/namespaces/test%20ns/deployments/probe%2F1"),
        (
            "nodemaintenances.maintenance.nvidia.com",
            "/apis/maintenance.nvidia.com/v1alpha1/namespaces/test%20ns/nodemaintenances/probe%2F1",
        ),
    ],
)
def test_delete_resource_uses_server_side_uid_precondition(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_uri: str,
) -> None:
    """The API server must reject deletion if a same-name object replaced ours."""
    module = _load_script()
    observed: dict[str, Any] = {}

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["args"] = args
        observed["body"] = json.loads(kwargs["input_text"])
        return _completed()

    monkeypatch.setattr(module, "_run", fake_run)

    module._delete_resource(
        ["kubectl"],
        kind,
        "probe/1",
        "test ns",
        uid="owned-uid",
    )

    assert observed["args"] == ("delete", f"--raw={expected_uri}", "-f", "-")
    assert observed["body"]["preconditions"] == {"uid": "owned-uid"}


def test_delete_owned_resource_waits_for_exact_uid_to_disappear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup should poll the exact owned object after its atomic delete."""
    module = _load_script()
    payload = {
        "metadata": {
            "name": "probe-1",
            "namespace": "default",
            "uid": "owned-uid",
            "labels": {module.RUN_LABEL: "run-1"},
        }
    }
    reads = iter([payload, payload, None])
    deleted: dict[str, Any] = {}
    monkeypatch.setattr(module, "_read_owned_resource", lambda *args, **kwargs: next(reads))
    monkeypatch.setattr(
        module,
        "_delete_resource",
        lambda *args, **kwargs: deleted.update(kwargs),
    )
    monkeypatch.setattr(module.time, "sleep", lambda *_args: None)

    module._delete_owned_resource(
        ["kubectl"],
        "deployment",
        "probe-1",
        "default",
        "run-1",
        expected_uid="owned-uid",
        timeout_seconds=30,
    )

    assert deleted["uid"] == "owned-uid"


def test_preflight_rejects_an_existing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not compete with another requestor for the same node."""
    module = _load_script()

    def fake_run(
        kubectl: list[str],
        *args: str,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("auth", "can-i"):
            return subprocess.CompletedProcess(["kubectl", *args], 0, stdout="yes\n", stderr="")
        return _completed(args)

    monkeypatch.setattr(module, "_run", fake_run)
    responses = iter(
        [
            _node(),
            {
                "items": [
                    {
                        "metadata": {"name": "other", "namespace": "ops"},
                        "spec": {"nodeName": "worker-1"},
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(module, "_get_json", lambda *args, **kwargs: next(responses))

    with pytest.raises(module.MaintenanceTestError, match="already has a NodeMaintenance"):
        module._preflight(["kubectl"], "worker-1", "default")


def test_ready_condition_must_match_current_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignore stale Ready evidence from an older object generation."""
    module = _load_script()
    payload = {
        "metadata": {"generation": 2},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "Ready",
                    "observedGeneration": 1,
                }
            ]
        },
    }
    calls = 0

    def fake_get(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            payload["status"]["conditions"][0]["observedGeneration"] = 2
        return payload

    monkeypatch.setattr(module, "_get_json", fake_get)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    assert (
        module._wait_for_maintenance_ready(
            ["kubectl"],
            "default",
            "maintenance-1",
            module.time.monotonic() + 1,
            0.01,
        )
        is payload
    )
    assert calls == 2


def test_requestor_failed_condition_aborts_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat the operator's terminal RequestorFailed condition as failure."""
    module = _load_script()
    payload = {
        "metadata": {"generation": 3},
        "status": {
            "conditions": [
                {
                    "type": "RequestorFailed",
                    "status": "True",
                    "reason": "FailedMaintenance",
                    "observedGeneration": 3,
                }
            ]
        },
    }
    monkeypatch.setattr(module, "_get_json", lambda *args, **kwargs: payload)

    with pytest.raises(module.MaintenanceTestError, match="FailedMaintenance"):
        module._wait_for_maintenance_ready(
            ["kubectl"],
            "default",
            "maintenance-1",
            module.time.monotonic() + 1,
            0.01,
        )


def test_ready_maintenance_failed_reason_aborts_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Ready condition's MaintenanceFailed reason is terminal evidence."""
    module = _load_script()
    payload = {
        "metadata": {"generation": 3},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "MaintenanceFailed",
                    "observedGeneration": 3,
                }
            ]
        },
    }
    monkeypatch.setattr(module, "_get_json", lambda *args, **kwargs: payload)

    with pytest.raises(module.MaintenanceTestError, match="MaintenanceFailed"):
        module._wait_for_maintenance_ready(
            ["kubectl"],
            "default",
            "maintenance-1",
            module.time.monotonic() + 1,
            0.01,
        )


def test_ambiguous_deployment_create_still_runs_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A timed-out create must not leak the uniquely named probe."""
    module = _load_script()
    deleted: list[str] = []
    monkeypatch.setenv(module.MUTATION_OPT_IN_ENV, "1")
    monkeypatch.setattr(sys, "argv", ["return_node_maintenance.py", "--node=worker-1"])
    monkeypatch.setattr(module, "_kubectl_command", lambda: ["kubectl"])
    monkeypatch.setattr(module, "_preflight", lambda *args: _node())

    def timeout_create(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise module.KubectlTimeoutError("create timed out")

    monkeypatch.setattr(module, "_create_owned_resource", timeout_create)
    monkeypatch.setattr(
        module,
        "_delete_owned_resource",
        lambda kubectl, kind, name, namespace, run_id, **kwargs: deleted.append(kind),
    )
    monkeypatch.setattr(module, "_wait_for_probe_absent", lambda *args: True)

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["error"] == "create timed out"
    assert deleted == ["deployment"]


def test_successful_workflow_reports_behavior_and_restoration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PASS requires operator readiness, evacuation, blocking, and recovery."""
    module = _load_script()
    deleted: list[tuple[str, str]] = []
    monkeypatch.setenv(module.MUTATION_OPT_IN_ENV, "1")
    monkeypatch.setattr(sys, "argv", ["return_node_maintenance.py", "--node=worker-1"])
    monkeypatch.setattr(module, "_kubectl_command", lambda: ["kubectl"])
    monkeypatch.setattr(module, "_preflight", lambda *args: _node())
    monkeypatch.setattr(module, "_require_unclaimed_node", lambda *args: _node())
    monkeypatch.setattr(
        module,
        "_create_owned_resource",
        lambda kubectl, kind, *args, **kwargs: {
            "metadata": {"uid": "maintenance-uid" if kind == module.NODE_MAINTENANCE_RESOURCE else "deployment-uid"}
        },
    )
    monkeypatch.setattr(
        module,
        "_maintenance_requests",
        lambda *args: [{"metadata": {"uid": "maintenance-uid"}}],
    )
    monkeypatch.setattr(module, "_wait_for_initial_probe", lambda *args: "old-uid")
    monkeypatch.setattr(
        module,
        "_wait_for_maintenance_ready",
        lambda *args: {"status": {"drain": {"evictionPods": 1, "drainProgress": 100}}},
    )
    monkeypatch.setattr(module, "_get_json", lambda *args, **kwargs: _node(unschedulable=True))
    monkeypatch.setattr(module, "_wait_for_replacement_blocked", lambda *args: (True, True))
    monkeypatch.setattr(module, "_wait_for_node_restored", lambda *args: True)
    monkeypatch.setattr(module, "_wait_for_recovery", lambda *args: True)
    monkeypatch.setattr(
        module,
        "_delete_owned_resource",
        lambda kubectl, kind, name, namespace, run_id, **kwargs: deleted.append((kind, name)),
    )
    monkeypatch.setattr(module, "_wait_for_probe_absent", lambda *args: True)

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["success"] is True
    assert payload["operation"] == {
        "requested": True,
        "accepted": True,
        "maintenance_mode": "Maintenance",
        "workload_evacuated": True,
        "replacement_blocked": True,
        "workload_recovered": True,
        "restored": True,
        "node_id": "worker-1",
    }
    assert len(deleted) == 2
    assert deleted[0][0] == module.NODE_MAINTENANCE_RESOURCE
    assert deleted[1][0] == "deployment"


def test_incomplete_drain_evidence_fails_workflow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ready=True cannot pass before the owned drain reaches completion."""
    module = _load_script()
    monkeypatch.setenv(module.MUTATION_OPT_IN_ENV, "1")
    monkeypatch.setattr(sys, "argv", ["return_node_maintenance.py", "--node=worker-1"])
    monkeypatch.setattr(module, "_kubectl_command", lambda: ["kubectl"])
    monkeypatch.setattr(module, "_preflight", lambda *args: _node())
    monkeypatch.setattr(module, "_require_unclaimed_node", lambda *args: _node())
    monkeypatch.setattr(
        module,
        "_create_owned_resource",
        lambda kubectl, kind, *args, **kwargs: {
            "metadata": {"uid": "maintenance-uid" if kind == module.NODE_MAINTENANCE_RESOURCE else "deployment-uid"}
        },
    )
    monkeypatch.setattr(module, "_maintenance_requests", lambda *args: [{"metadata": {"uid": "maintenance-uid"}}])
    monkeypatch.setattr(module, "_wait_for_initial_probe", lambda *args: "old-uid")
    monkeypatch.setattr(
        module,
        "_wait_for_maintenance_ready",
        lambda *args: {"status": {"drain": {"evictionPods": 1, "drainProgress": 99, "waitForEviction": []}}},
    )
    monkeypatch.setattr(module, "_get_json", lambda *args, **kwargs: _node(unschedulable=True))
    monkeypatch.setattr(
        module,
        "_wait_for_replacement_blocked",
        lambda *args: pytest.fail("incomplete drain must fail before replacement polling"),
    )
    monkeypatch.setattr(module, "_delete_owned_resource", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_wait_for_node_restored", lambda *args: True)
    monkeypatch.setattr(module, "_wait_for_recovery", lambda *args: True)
    monkeypatch.setattr(module, "_wait_for_probe_absent", lambda *args: True)

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["success"] is False
    assert payload["error"] == "NodeMaintenance reported Ready without completing its drain"


def test_cleanup_failure_forces_failed_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ready maintenance evidence cannot pass if restoration is unconfirmed."""
    module = _load_script()
    monkeypatch.setenv(module.MUTATION_OPT_IN_ENV, "1")
    monkeypatch.setattr(sys, "argv", ["return_node_maintenance.py", "--node=worker-1"])
    monkeypatch.setattr(module, "_kubectl_command", lambda: ["kubectl"])
    monkeypatch.setattr(module, "_preflight", lambda *args: _node())
    monkeypatch.setattr(module, "_require_unclaimed_node", lambda *args: _node())
    monkeypatch.setattr(
        module,
        "_create_owned_resource",
        lambda kubectl, kind, *args, **kwargs: {
            "metadata": {"uid": "maintenance-uid" if kind == module.NODE_MAINTENANCE_RESOURCE else "deployment-uid"}
        },
    )
    monkeypatch.setattr(
        module,
        "_maintenance_requests",
        lambda *args: [{"metadata": {"uid": "maintenance-uid"}}],
    )
    monkeypatch.setattr(module, "_wait_for_initial_probe", lambda *args: "old-uid")
    monkeypatch.setattr(
        module,
        "_wait_for_maintenance_ready",
        lambda *args: {"status": {"drain": {"evictionPods": 1, "drainProgress": 100}}},
    )
    monkeypatch.setattr(module, "_get_json", lambda *args, **kwargs: _node(unschedulable=True))
    monkeypatch.setattr(module, "_wait_for_replacement_blocked", lambda *args: (True, True))
    monkeypatch.setattr(module, "_wait_for_node_restored", lambda *args: False)
    monkeypatch.setattr(module, "_delete_owned_resource", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_wait_for_probe_absent", lambda *args: True)

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["operation"]["accepted"] is True
    assert payload["operation"]["restored"] is False
    assert payload["success"] is False
    assert "restore node schedulability" in payload["cleanup_errors"][0]
