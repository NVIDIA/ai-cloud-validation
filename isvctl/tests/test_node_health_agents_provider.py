# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the read-only node-health-agent provider (BFX04-01)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.context import Context
from isvctl.orchestrator.step_executor import StepExecutor

CONFIGS_ROOT = Path(__file__).resolve().parents[1] / "configs"
SCRIPT = CONFIGS_ROOT / "providers" / "shared" / "breakfix" / "query_node_health_agents.py"
CONFIG = CONFIGS_ROOT / "providers" / "node-health-agents.yaml"


def _load_script() -> ModuleType:
    """Load the provider script as an isolated module."""
    spec = importlib.util.spec_from_file_location("test_kubernetes_node_health_agents_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _nodes(*names: str) -> dict[str, object]:
    """Return GPU node inventory for the supplied names."""
    return {
        "items": [
            {
                "metadata": {"name": name, "labels": {"nvidia.com/gpu.present": "true"}},
                "status": {"capacity": {"nvidia.com/gpu": "4"}},
            }
            for name in names
        ]
    }


def _daemonset(agent_name: str = "fleet-intelligence-agent") -> dict[str, object]:
    """Return one supported agent DaemonSet inventory item."""
    return {
        "metadata": {
            "namespace": "nvidia-system",
            "name": agent_name,
            "uid": f"uid-{agent_name}",
            "labels": {"app.kubernetes.io/name": agent_name},
        }
    }


def _pod(
    node: str,
    *,
    agent_name: str = "fleet-intelligence-agent",
    phase: str = "Running",
    ready: bool = True,
    owned: bool = True,
) -> dict[str, object]:
    """Return one candidate agent pod inventory item."""
    owner_references = []
    if owned:
        owner_references.append(
            {
                "kind": "DaemonSet",
                "name": agent_name,
                "uid": f"uid-{agent_name}",
                "controller": True,
            }
        )
    return {
        "metadata": {
            "namespace": "nvidia-system",
            "name": f"{agent_name}-{node}",
            "labels": {"app.kubernetes.io/name": agent_name},
            "ownerReferences": owner_references,
        },
        "spec": {"nodeName": node},
        "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def test_config_wires_only_read_only_node_health_query() -> None:
    """The provider overlay binds exactly one test-phase query step."""
    config = RunConfig.model_validate(merge_yaml_files([CONFIG]))
    steps = config.get_steps("bare_metal")

    assert config.get_phases("bare_metal") == ["test"]
    assert len(steps) == 1
    assert steps[0].name == "query_node_health_agents"
    assert steps[0].command == "python shared/breakfix/query_node_health_agents.py"
    assert steps[0].args == ["--nodes={{env.BFX04_NODES}}"]
    assert steps[0].requires_available_validations == ["NodeHealthAgentCheck"]


def test_config_keeps_empty_nodes_value_for_kubernetes_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent BFX04_NODES value must not leave a dangling argparse flag."""
    monkeypatch.delenv("BFX04_NODES", raising=False)
    config = RunConfig.model_validate(merge_yaml_files([CONFIG]))
    step = config.get_steps("bare_metal")[0]

    rendered = StepExecutor()._render_args(step.args, Context(config))

    assert rendered == ["--nodes="]


@pytest.mark.parametrize("agent_name", ["fleet-intelligence-agent", "gpu-health-monitor"])
def test_supported_agent_covers_every_gpu_node(agent_name: str) -> None:
    """Either supported NVIDIA agent can prove complete GPU-node coverage."""
    module = _load_script()
    result = module._evaluate(
        _nodes("gpu-1", "gpu-2"),
        {"items": [_daemonset(agent_name)]},
        {"items": [_pod("gpu-1", agent_name=agent_name), _pod("gpu-2", agent_name=agent_name)]},
    )

    assert result["success"] is True
    assert result.get("skipped") is not True
    assert result["agents_observable"] is True
    assert result["agents"] == [
        {"node_id": "gpu-1", "agent_name": agent_name, "running": True},
        {"node_id": "gpu-2", "agent_name": agent_name, "running": True},
    ]


def test_missing_supported_agent_returns_failing_record_per_gpu_node() -> None:
    """No supported DaemonSet is observable evidence that the agents are absent."""
    module = _load_script()
    result = module._evaluate(_nodes("gpu-1", "gpu-2"), {"items": []}, {"items": []})

    assert result.get("skipped") is not True
    assert result["agents_observable"] is True
    assert [record["running"] for record in result["agents"]] == [False, False]


def test_unready_pod_fails_only_its_gpu_node() -> None:
    """A pod must be both Running and Ready to cover its node."""
    module = _load_script()
    result = module._evaluate(
        _nodes("gpu-1", "gpu-2"),
        {"items": [_daemonset()]},
        {"items": [_pod("gpu-1"), _pod("gpu-2", ready=False)]},
    )

    assert result["agents"][0]["running"] is True
    assert result["agents"][1] == {"node_id": "gpu-2", "agent_name": "GPUd/Sentinel", "running": False}


def test_generic_dcgm_exporter_is_not_accepted() -> None:
    """DCGM telemetry alone does not satisfy the GPUd/Sentinel requirement."""
    module = _load_script()
    result = module._evaluate(
        _nodes("gpu-1"),
        {"items": [_daemonset("dcgm-exporter")]},
        {"items": [_pod("gpu-1", agent_name="dcgm-exporter")]},
    )

    assert result["agents"] == [{"node_id": "gpu-1", "agent_name": "GPUd/Sentinel", "running": False}]


def test_unowned_pod_is_not_accepted() -> None:
    """An exact label is insufficient without a matching DaemonSet owner."""
    module = _load_script()
    result = module._evaluate(
        _nodes("gpu-1"),
        {"items": [_daemonset()]},
        {"items": [_pod("gpu-1", owned=False)]},
    )

    assert result["agents"][0]["running"] is False


def test_no_gpu_nodes_is_a_structured_skip() -> None:
    """An environment without GPU nodes is outside BFX04-01's scope."""
    module = _load_script()
    result = module._evaluate({"items": []}, {"items": [_daemonset()]}, {"items": []})

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["agents"] == []


def test_bare_metal_fleetintd_running_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A running Fleet Intelligence systemd service is direct node evidence."""
    module = _load_script()
    response = subprocess.CompletedProcess([], 0, stdout="active\ninactive\ninactive\ninactive\n", stderr="")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return response

    monkeypatch.setattr(module, "_ssh_command", lambda: ["ssh"])
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = module._evaluate_systemd(["a05-p01-dgx-03-c01"])

    assert result["platform"] == "bare_metal"
    assert result["agents"] == [{"node_id": "a05-p01-dgx-03-c01", "agent_name": "fleetintd", "running": True}]
    assert commands[0][-1] == ("systemctl is-active fleetintd gpud nvsentinel gpu-health-monitor 2>/dev/null || true")


def test_bare_metal_missing_agent_returns_failing_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inactive supported services cannot produce a false bare-metal pass."""
    module = _load_script()
    response = subprocess.CompletedProcess([], 0, stdout="inactive\ninactive\ninactive\ninactive\n", stderr="")
    monkeypatch.setattr(module, "_ssh_command", lambda: ["ssh"])
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: response)

    result = module._evaluate_systemd(["a05-p01-dgx-03-c01"])

    assert result["agents"] == [{"node_id": "a05-p01-dgx-03-c01", "agent_name": "GPUd/Sentinel", "running": False}]


def test_bare_metal_node_names_are_validated() -> None:
    """Node input cannot add SSH options or remote shell syntax."""
    module = _load_script()

    with pytest.raises(module.NodeQueryError, match="Invalid bare-metal node name"):
        module._parse_nodes("--proxycommand=bad,node;touch")


def test_query_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """kubectl output is not copied into the structured failure payload."""
    module = _load_script()
    monkeypatch.setattr(module, "_kubectl_command", lambda: ["kubectl"])
    response = subprocess.CompletedProcess([], 1, stdout="private inventory", stderr="private auth error")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: response)
    monkeypatch.setattr(sys, "argv", [SCRIPT.name])

    assert module.main() == 1
    output = capsys.readouterr().out
    assert "private" not in output
    payload = json.loads(output)
    assert payload["success"] is False
    assert payload["error_type"] == "node_health_query_failed"


@pytest.mark.parametrize(
    ("env_name", "argv"),
    [
        ("KUBECTL", [SCRIPT.name]),
        ("SSH", [SCRIPT.name, "--nodes", "gpu-1"]),
    ],
)
def test_malformed_command_override_is_structured(
    env_name: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unmatched quote in a command override emits JSON, not a traceback."""
    module = _load_script()
    monkeypatch.setenv(env_name, "unmatched-'")
    monkeypatch.setattr(sys, "argv", argv)

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["error_type"] == "node_health_query_failed"
    assert payload["error"] == f"Invalid {env_name} command override"
