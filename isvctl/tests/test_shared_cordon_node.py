# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared BFX01-04 Kubernetes cordon reference."""

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
CORDON_SCRIPT = ISVCTL_ROOT / "configs" / "providers" / "shared" / "breakfix" / "cordon_node.py"
AWS_EKS_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "aws" / "config" / "eks.yaml"
MY_ISV_K8S_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "my-isv" / "config" / "k8s.yaml"
K8S_SUITE = ISVCTL_ROOT / "configs" / "suites" / "k8s.yaml"


def _load_script() -> ModuleType:
    """Load the shared cordon script as a module for direct testing."""
    spec = importlib.util.spec_from_file_location("test_shared_cordon_node_script", CORDON_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _completed(
    args: tuple[str, ...],
    *,
    payload: dict[str, Any] | None = None,
    error: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a completed kubectl command with optional JSON output."""
    return subprocess.CompletedProcess(
        args=["kubectl", *args],
        returncode=1 if error else 0,
        stdout=json.dumps(payload) if payload is not None else "",
        stderr=error,
    )


def _node(
    *,
    unschedulable: bool = False,
    resource_version: str = "10",
    owner: str | None = None,
) -> dict[str, Any]:
    """Return one Ready fake node with concurrency metadata."""
    annotations = {} if owner is None else {"isvtest.nvidia.com/bfx01-04-owner": owner}
    return {
        "metadata": {
            "name": "worker-1",
            "resourceVersion": resource_version,
            "annotations": annotations,
            "labels": {"kubernetes.io/hostname": "worker-1-host"},
        },
        "spec": {"unschedulable": unschedulable},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def _running_pod() -> dict[str, Any]:
    """Return a Ready pod bound to the selected node."""
    return {
        "spec": {"nodeName": "worker-1"},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }


def _unschedulable_pod() -> dict[str, Any]:
    """Return an unbound pod rejected by the scheduler."""
    return {
        "spec": {},
        "status": {
            "phase": "Pending",
            "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
        },
    }


def _patch_from_args(args: tuple[str, ...]) -> list[dict[str, Any]]:
    """Decode the JSON Patch argument from a fake kubectl call."""
    return json.loads(args[args.index("-p") + 1])


def test_k8s_suite_declares_the_provider_neutral_validation() -> None:
    """Keep BFX01-04 in the suite while providers own executable steps."""
    config = yaml.safe_load(K8S_SUITE.read_text())
    steps = config["commands"]["kubernetes"]["steps"]
    validation = config["tests"]["validations"]["cordon_node"]

    assert all(step["name"] != "cordon_node" for step in steps)
    assert validation["step"] == "cordon_node"
    assert validation["checks"]["CordonNodeCheck"]["test_id"] == "BFX01-04"


@pytest.mark.parametrize(
    ("provider_config", "command"),
    [
        (AWS_EKS_CONFIG, "python3 ../../shared/breakfix/cordon_node.py"),
        (MY_ISV_K8S_CONFIG, "python3 ../../shared/breakfix/cordon_node.py"),
    ],
)
def test_kubernetes_providers_wire_the_shared_reference(provider_config: Path, command: str) -> None:
    """Normal Kubernetes provider configs must execute the shared reference."""
    config = yaml.safe_load(provider_config.read_text())
    step = next(item for item in config["commands"]["kubernetes"]["steps"] if item["name"] == "cordon_node")

    assert step["command"] == command
    assert step["phase"] == "test"
    assert step["timeout"] == 1200
    assert step["args"] == ["--node={{breakfix_node}}"]
    assert config["tests"]["settings"]["breakfix_node"] == "{{env.ISVTEST_BREAKFIX_NODE | default('', true)}}"


def test_cordon_settings_render_an_empty_node_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset target renders an empty node so the provider can skip safely."""
    monkeypatch.delenv("ISVTEST_BREAKFIX_NODE", raising=False)
    config = RunConfig.model_validate(merge_yaml_files([AWS_EKS_CONFIG]))
    cordon_step = next(step for step in config.commands["kubernetes"].steps if step.name == "cordon_node")

    rendered = StepExecutor()._render_args(cordon_step.args, Context(config))

    assert rendered == ["--node="]


def test_cordon_settings_accept_an_explicit_environment_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured dedicated node is rendered into the provider command."""
    monkeypatch.setenv("ISVTEST_BREAKFIX_NODE", "dedicated-worker")
    config = RunConfig.model_validate(merge_yaml_files([AWS_EKS_CONFIG]))
    cordon_step = next(step for step in config.commands["kubernetes"].steps if step.name == "cordon_node")

    rendered = StepExecutor()._render_args(cordon_step.args, Context(config))

    assert rendered == ["--node=dedicated-worker"]


def test_missing_node_skips_before_kubectl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The safe default must skip without running kubectl."""
    module = _load_script()
    monkeypatch.setattr(sys, "argv", ["cordon_node.py"])
    monkeypatch.setattr(
        module,
        "_kubectl_command",
        lambda: pytest.fail("kubectl must not run without a dedicated node"),
    )

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["skip_reason"] == "No dedicated break-fix node configured"
    assert result["operation"] == {
        "cordoned": False,
        "new_workloads_blocked": False,
        "existing_workloads_running": False,
    }


def test_node_taints_are_tolerated_without_bypassing_cordon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe pods tolerate the selected GPU taint but never the cordon taint."""
    module = _load_script()
    node = _node()
    node["spec"]["taints"] = [
        {"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"},
        {"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"},
    ]
    monkeypatch.setattr(
        module,
        "_run",
        lambda kubectl, *args, **kwargs: _completed(args, payload={"items": [node]}),
    )

    selection = module._select_node(["kubectl"], None)

    assert (selection.name, selection.hostname, selection.resource_version) == ("worker-1", "worker-1-host", "10")
    assert selection.tolerations == [
        {"key": "nvidia.com/gpu", "operator": "Equal", "value": "present", "effect": "NoSchedule"}
    ]


def test_probe_manifest_has_a_finite_active_deadline() -> None:
    """An abandoned probe expires even if client-side cleanup cannot run."""
    module = _load_script()

    manifest = json.loads(module._pod_manifest("probe", "default", "worker-1-host", "pause", []))

    assert manifest["spec"]["activeDeadlineSeconds"] == module.PROBE_ACTIVE_DEADLINE_SECONDS


def test_multiple_nodes_require_an_explicit_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never choose a control-plane or shared-cluster node on the user's behalf."""
    module = _load_script()
    second = _node()
    second["metadata"]["name"] = "worker-2"
    second["metadata"]["labels"]["kubernetes.io/hostname"] = "worker-2-host"
    monkeypatch.setattr(
        module,
        "_run",
        lambda kubectl, *args, **kwargs: _completed(args, payload={"items": [_node(), second]}),
    )

    with pytest.raises(module.CordonTestError, match="pass --node with a dedicated test node"):
        module._select_node(["kubectl"], None)


def test_run_bounds_the_process_and_api_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every kubectl process has both subprocess and API request timeouts."""
    module = _load_script()
    observed: dict[str, Any] = {}

    def fake_subprocess_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Capture the bounded command invocation."""
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    module._run(
        ["kubectl"],
        "get",
        "nodes",
        command_timeout_seconds=7,
        request_timeout_seconds=3,
    )

    assert observed["command"] == ["kubectl", "get", "nodes", "--request-timeout=3s"]
    assert observed["timeout"] == 7


def test_run_translates_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung kubectl process becomes a structured workflow error."""
    module = _load_script()

    def fake_subprocess_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Model a kubectl process that exceeds its finite deadline."""
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    with pytest.raises(module.CordonTestError, match="timed out after 4 seconds"):
        module._run(["kubectl"], "get", "nodes", command_timeout_seconds=4)


def test_atomic_claim_uses_resource_version_and_unschedulable_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node claim is one conditional JSON Patch, not idempotent kubectl cordon."""
    module = _load_script()
    selection = module.NodeSelection(
        name="worker-1",
        hostname="worker-1-host",
        resource_version="10",
        spec={"unschedulable": False},
        annotations_present=True,
        tolerations=[],
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Capture the atomic claim patch."""
        calls.append(args)
        return _completed(args)

    monkeypatch.setattr(module, "_run", fake_run)

    ownership = module._claim_node(["kubectl"], selection, "owner-token")
    patch = _patch_from_args(calls[0])

    assert ownership == module.CordonOwnership("worker-1", "owner-token")
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "10"},
        {"op": "test", "path": "/spec/unschedulable", "value": False},
    ]
    assert {"op": "add", "path": module.OWNER_ANNOTATION_PATH, "value": "owner-token"} in patch
    assert {"op": "add", "path": "/spec/unschedulable", "value": True} in patch


def test_claim_timeout_after_apply_recovers_verified_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost PATCH response still yields cleanup ownership after a confirming GET."""
    module = _load_script()
    selection = module.NodeSelection(
        name="worker-1",
        hostname="worker-1-host",
        resource_version="10",
        spec={"unschedulable": False},
        annotations_present=True,
        tolerations=[],
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Model the API committing a claim before the client times out."""
        calls.append(args)
        if args[:3] == ("patch", "node", "worker-1"):
            raise module.KubectlTimeoutError("claim response timed out")
        return _completed(args, payload=_node(unschedulable=True, resource_version="11", owner="owner-token"))

    monkeypatch.setattr(module, "_run", fake_run)

    ownership = module._claim_node(["kubectl"], selection, "owner-token")

    assert ownership == module.CordonOwnership("worker-1", "owner-token")
    assert [call[:3] for call in calls] == [("patch", "node", "worker-1"), ("get", "node", "worker-1")]


def test_claim_request_timeout_after_apply_recovers_verified_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kubectl request deadline also verifies whether the atomic claim landed."""
    module = _load_script()
    selection = module.NodeSelection(
        name="worker-1",
        hostname="worker-1-host",
        resource_version="10",
        spec={"unschedulable": False},
        annotations_present=True,
        tolerations=[],
    )

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return kubectl's nonzero form of an ambiguous request timeout."""
        if args[:3] == ("patch", "node", "worker-1"):
            return _completed(args, error="context deadline exceeded")
        return _completed(args, payload=_node(unschedulable=True, resource_version="11", owner="owner-token"))

    monkeypatch.setattr(module, "_run", fake_run)

    assert module._claim_node(["kubectl"], selection, "owner-token") == module.CordonOwnership(
        "worker-1", "owner-token"
    )


def test_unverified_claim_timeout_still_runs_conditional_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lost claim and verification response still retains a safe cleanup candidate."""
    module = _load_script()
    calls: list[tuple[str, ...]] = []
    owner_token = ""
    node_gets = 0

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Lose both claim responses, then let cleanup observe and release the claim."""
        nonlocal node_gets, owner_token
        calls.append(args)
        if args[:4] == ("get", "nodes", "-o", "json"):
            return _completed(args, payload={"items": [_node()]})
        if args[:3] == ("patch", "node", "worker-1"):
            patch = _patch_from_args(args)
            owner_operation = next(
                (operation for operation in patch if operation.get("path") == module.OWNER_ANNOTATION_PATH),
                None,
            )
            if owner_operation and owner_operation["op"] == "add":
                owner_token = owner_operation["value"]
                raise module.KubectlTimeoutError("claim response timed out")
            return _completed(args)
        if args[:3] == ("get", "node", "worker-1"):
            node_gets += 1
            if node_gets == 1:
                raise module.KubectlTimeoutError("claim verification timed out")
            return _completed(args, payload=_node(unschedulable=True, resource_version="11", owner=owner_token))
        return _completed(args)

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: type("Uuid", (), {"hex": "deadbeefcafebabe"})())
    monkeypatch.setattr(sys, "argv", ["cordon_node.py", "--node=worker-1", "--timeout-seconds", "1"])

    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)

    assert result["error"] == "claim response timed out"
    patches = [_patch_from_args(call) for call in calls if call[:3] == ("patch", "node", "worker-1")]
    assert len(patches) == 2
    assert {"op": "replace", "path": "/spec/unschedulable", "value": False} in patches[1]


def test_concurrent_claim_failure_never_uncordons(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale resourceVersion conflict acquires no cleanup ownership."""
    module = _load_script()
    calls: list[tuple[str, ...]] = []

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Let another actor win between selection and the conditional patch."""
        calls.append(args)
        if args[:4] == ("get", "nodes", "-o", "json"):
            return _completed(args, payload={"items": [_node()]})
        if args[:3] == ("patch", "node", "worker-1"):
            return _completed(args, error="Conflict: object has been modified")
        return _completed(args)

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["cordon_node.py", "--node=worker-1", "--timeout-seconds", "1"])

    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)

    assert "Could not atomically claim" in result["error"]
    patches = [_patch_from_args(call) for call in calls if call[:3] == ("patch", "node", "worker-1")]
    assert len(patches) == 1
    assert not any(
        operation.get("path") == "/spec/unschedulable"
        and operation.get("op") in {"add", "replace"}
        and operation.get("value") is False
        for operation in patches[0]
    )


def test_cordon_workflow_proves_requirements_and_conditionally_restores_node(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful run proves all requirements and releases only its own claim."""
    module = _load_script()
    calls: list[tuple[str, ...]] = []
    pod_gets = iter([_running_pod(), _unschedulable_pod()])
    owner_token = ""

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return deterministic Kubernetes state for the happy path."""
        nonlocal owner_token
        calls.append(args)
        if args[:4] == ("get", "nodes", "-o", "json"):
            return _completed(args, payload={"items": [_node()]})
        if args[:3] == ("patch", "node", "worker-1"):
            patch = _patch_from_args(args)
            owner_operation = next(
                (operation for operation in patch if operation.get("path") == module.OWNER_ANNOTATION_PATH),
                None,
            )
            if owner_operation and owner_operation["op"] == "add":
                owner_token = owner_operation["value"]
            return _completed(args)
        if args[:3] == ("get", "node", "worker-1"):
            return _completed(args, payload=_node(unschedulable=True, resource_version="11", owner=owner_token))
        if args[:2] == ("get", "pod"):
            return _completed(args, payload=next(pod_gets))
        return _completed(args)

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: type("Uuid", (), {"hex": "deadbeefcafebabe"})())
    monkeypatch.setattr(sys, "argv", ["cordon_node.py", "--node=worker-1", "--timeout-seconds", "1"])

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)

    assert result["success"] is True
    assert result["platform"] == "kubernetes"
    assert result["operation"] == {
        "cordoned": True,
        "new_workloads_blocked": True,
        "existing_workloads_running": True,
        "node_id": "worker-1",
    }
    patches = [_patch_from_args(call) for call in calls if call[:3] == ("patch", "node", "worker-1")]
    assert len(patches) == 2
    assert {"op": "add", "path": "/spec/unschedulable", "value": True} in patches[0]
    assert {"op": "test", "path": module.OWNER_ANNOTATION_PATH, "value": owner_token} in patches[1]
    assert {"op": "replace", "path": "/spec/unschedulable", "value": False} in patches[1]
    assert {"op": "remove", "path": module.OWNER_ANNOTATION_PATH} in patches[1]
    assert [call[:3] for call in calls].count(("delete", "pod", "isvtest-bfx-existing-deadbeefcafebabe")) == 1
    assert [call[:3] for call in calls].count(("delete", "pod", "isvtest-bfx-blocked-deadbeefcafebabe")) == 1


def test_changed_ownership_preserves_a_later_cordon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup refuses to uncordon after another actor replaces the ownership marker."""
    module = _load_script()
    ownership = module.CordonOwnership("worker-1", "our-token")
    monkeypatch.setattr(
        module,
        "_get_node",
        lambda kubectl, node_name: _node(unschedulable=True, owner="later-token"),
    )
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: pytest.fail("ownership change must not issue an uncordon patch"),
    )

    with pytest.raises(module.CordonTestError, match="ownership changed; leaving schedulability unchanged"):
        module._release_node(["kubectl"], ownership)


def test_release_timeout_after_apply_is_confirmed_by_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost uncordon response is accepted only after observing released state."""
    module = _load_script()
    ownership = module.CordonOwnership("worker-1", "our-token")
    observed_nodes = iter(
        [
            _node(unschedulable=True, resource_version="10", owner="our-token"),
            _node(unschedulable=False, resource_version="11"),
        ]
    )
    patch_calls: list[tuple[str, ...]] = []

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Model the API applying release before its response is lost."""
        patch_calls.append(args)
        raise module.KubectlTimeoutError("release response timed out")

    monkeypatch.setattr(module, "_get_node", lambda kubectl, node_name: next(observed_nodes))
    monkeypatch.setattr(module, "_run", fake_run)

    module._release_node(["kubectl"], ownership)

    assert [call[:3] for call in patch_calls] == [("patch", "node", "worker-1")]


def test_release_retries_a_timed_out_state_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient cleanup GET timeout must not prevent conditional release."""
    module = _load_script()
    ownership = module.CordonOwnership("worker-1", "our-token")
    get_calls = 0
    patch_calls: list[tuple[str, ...]] = []
    sleeps: list[float] = []

    def fake_get_node(kubectl: list[str], node_name: str) -> dict[str, Any]:
        """Fail the first read and return the owned state on retry."""
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            raise module.KubectlTimeoutError("cleanup state read timed out")
        return _node(unschedulable=True, resource_version="10", owner="our-token")

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Accept the conditional release patch."""
        patch_calls.append(args)
        return _completed(args)

    monkeypatch.setattr(module, "_get_node", fake_get_node)
    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    module._release_node(["kubectl"], ownership)

    assert get_calls == 2
    assert [call[:3] for call in patch_calls] == [("patch", "node", "worker-1")]
    assert sleeps == [module.UNCORDON_RETRY_DELAY_SECONDS]


def test_unschedulable_poll_retries_a_transient_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient GET failure does not end the scheduling assertion early."""
    module = _load_script()
    calls = 0

    def fake_get_pod(*_args: Any) -> dict[str, Any]:
        """Fail one read before returning scheduler evidence."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise module.CordonTestError("temporary API error")
        return _unschedulable_pod()

    monkeypatch.setattr(module, "_get_pod", fake_get_pod)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    assert module._wait_for_unschedulable(["kubectl"], "default", "probe", 1, 0.01)
    assert calls == 2


def test_cleanup_records_timeout_and_still_releases_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out pod delete is reported without skipping node restoration."""
    module = _load_script()
    ownership = module.CordonOwnership("worker-1", "our-token")
    released: list[Any] = []

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Model a bounded delete timeout."""
        raise module.CordonTestError("kubectl delete timed out after 30 seconds")

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_release_node", lambda kubectl, claim: released.append(claim))

    errors = module._cleanup(["kubectl"], "default", ["probe"], ownership)

    assert errors == ["delete pod default/probe: kubectl delete timed out after 30 seconds"]
    assert released == [ownership]


def test_create_timeout_preregisters_probe_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ambiguous create timeout still schedules ignore-not-found cleanup."""
    module = _load_script()
    calls: list[tuple[str, ...]] = []

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Time out after the API may have created the first probe pod."""
        calls.append(args)
        if args[:4] == ("get", "nodes", "-o", "json"):
            return _completed(args, payload={"items": [_node()]})
        if args[:3] == ("create", "-f", "-"):
            raise module.KubectlTimeoutError("create response timed out")
        return _completed(args)

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: type("Uuid", (), {"hex": "deadbeefcafebabe"})())
    monkeypatch.setattr(sys, "argv", ["cordon_node.py", "--node=worker-1", "--timeout-seconds", "1"])

    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)

    assert result["error"] == "create response timed out"
    assert ("delete", "pod", "isvtest-bfx-existing-deadbeefcafebabe") in [call[:3] for call in calls]


def test_unexpected_failure_still_emits_structured_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unexpected provider failures preserve the JSON stdout contract."""
    module = _load_script()
    monkeypatch.setattr(module, "_kubectl_command", lambda: ["kubectl"])
    monkeypatch.setattr(module, "_select_node", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sys, "argv", ["cordon_node.py", "--node=worker-1"])

    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)

    assert result["success"] is False
    assert result["error"] == "Unexpected cordon test failure: boom"


def test_blocked_probe_create_timeout_preregisters_both_pods(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A timeout creating the post-cordon probe cleans both possible pods."""
    module = _load_script()
    calls: list[tuple[str, ...]] = []
    create_count = 0
    owner_token = ""

    def fake_run(kubectl: list[str], *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Reach the second create, then lose its response."""
        nonlocal create_count, owner_token
        calls.append(args)
        if args[:4] == ("get", "nodes", "-o", "json"):
            return _completed(args, payload={"items": [_node()]})
        if args[:3] == ("create", "-f", "-"):
            create_count += 1
            if create_count == 2:
                raise module.KubectlTimeoutError("blocked create response timed out")
            return _completed(args)
        if args[:3] == ("patch", "node", "worker-1"):
            patch = _patch_from_args(args)
            owner_operation = next(
                (operation for operation in patch if operation.get("path") == module.OWNER_ANNOTATION_PATH),
                None,
            )
            if owner_operation and owner_operation["op"] == "add":
                owner_token = owner_operation["value"]
            return _completed(args)
        if args[:3] == ("get", "node", "worker-1"):
            return _completed(args, payload=_node(unschedulable=True, resource_version="11", owner=owner_token))
        if args[:2] == ("get", "pod"):
            return _completed(args, payload=_running_pod())
        return _completed(args)

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: type("Uuid", (), {"hex": "deadbeefcafebabe"})())
    monkeypatch.setattr(sys, "argv", ["cordon_node.py", "--node=worker-1", "--timeout-seconds", "1"])

    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)

    assert result["error"] == "blocked create response timed out"
    deleted_pods = [call[:3] for call in calls if call[:2] == ("delete", "pod")]
    assert deleted_pods == [
        ("delete", "pod", "isvtest-bfx-existing-deadbeefcafebabe"),
        ("delete", "pod", "isvtest-bfx-blocked-deadbeefcafebabe"),
    ]


def test_requested_precordoned_node_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow never claims a node already cordoned by someone else."""
    module = _load_script()
    monkeypatch.setattr(
        module,
        "_run",
        lambda kubectl, *args, **kwargs: _completed(args, payload={"items": [_node(unschedulable=True)]}),
    )

    with pytest.raises(module.CordonTestError, match="not Ready, schedulable, and unclaimed"):
        module._select_node(["kubectl"], "worker-1")
