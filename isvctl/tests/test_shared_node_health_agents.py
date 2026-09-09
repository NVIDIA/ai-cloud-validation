# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared BFX04-01 node-health-agent reference."""

from __future__ import annotations

import functools
import importlib.util
import json
import subprocess
import sys
import threading
import time
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
SCRIPT = ISVCTL_ROOT / "configs" / "providers" / "shared" / "breakfix" / "query_node_health_agents.py"
BARE_METAL_SUITE = ISVCTL_ROOT / "configs" / "suites" / "bare_metal.yaml"
NICO_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "bare_metal.yaml"


@functools.cache
def _load_script() -> ModuleType:
    """Load the shared node-health-agent script as a module for direct testing.

    Cached: every patch a test applies to the module goes through ``monkeypatch``
    and is unwound afterwards, so re-executing the script per test buys nothing.
    """
    spec = importlib.util.spec_from_file_location("test_shared_node_health_agents_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ssh_stub(
    states_by_node: dict[str, str],
    calls: list[list[str]] | None = None,
) -> object:
    """Return a ``subprocess.run`` stub answering per-node ``systemctl`` probes."""

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Answer one probe with the canned states for the node it targets."""
        if calls is not None:
            calls.append(command)
        node = command[-2]
        return subprocess.CompletedProcess(command, 0, stdout=states_by_node[node], stderr="")

    return fake_run


def _states(*active_units: str) -> str:
    """Return ``systemctl is-active`` output marking ``active_units`` active.

    Reads the unit list off the module so extending ``AGENT_UNITS`` cannot leave
    the fixtures returning too few states, which the script reads as unreadable.
    """
    return "".join(f"{'active' if unit in active_units else 'inactive'}\n" for unit in _load_script().AGENT_UNITS)


def test_bare_metal_suite_declares_the_provider_neutral_validation() -> None:
    """Keep BFX04-01 in the bare-metal suite while providers own executable steps."""
    config = yaml.safe_load(BARE_METAL_SUITE.read_text())
    validation = config["tests"]["validations"]["node_health_agents"]

    assert validation["step"] == "query_node_health_agents"
    assert validation["checks"]["NodeHealthAgentCheck"]["test_id"] == "BFX04-01"
    assert validation["checks"]["NodeHealthAgentCheck"]["labels"] == ["bare_metal", "breakfix"]


def test_nico_provider_wires_the_shared_reference() -> None:
    """The NICo bare-metal provider must execute the shared reference."""
    config = yaml.safe_load(NICO_CONFIG.read_text())
    step = next(
        item for item in config["commands"]["bare_metal"]["steps"] if item["name"] == "query_node_health_agents"
    )

    assert step["command"] == "python ../../shared/breakfix/query_node_health_agents.py"
    assert step["phase"] == "test"
    assert step["timeout"] == 300
    assert step["args"][0] == "--nodes={{health_agent_nodes}}"
    # Sourced from the inventory step, not from the node list, so a partial
    # list cannot be measured against itself.
    assert step["args"][1].startswith("--expected-nodes={{steps.query_fleet_inventory.nodes")
    assert config["tests"]["settings"]["health_agent_nodes"] == "{{env.NICO_HEALTH_AGENT_NODES | default('', true)}}"


def _render_nico_args(inventory: dict[str, Any] | None = None) -> list[str]:
    """Render the NICo step's args, optionally with a fleet inventory in context."""
    config = RunConfig.model_validate(merge_yaml_files([NICO_CONFIG]))
    step = next(item for item in config.commands["bare_metal"].steps if item.name == "query_node_health_agents")
    context = Context(config)
    if inventory is not None:
        context.set_step_output("query_fleet_inventory", inventory)
    return StepExecutor()._render_args(step.args, context)


def test_nodes_render_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset target list renders empty so the provider can skip safely."""
    monkeypatch.delenv("NICO_HEALTH_AGENT_NODES", raising=False)

    assert _render_nico_args()[0] == "--nodes="


def test_nodes_accept_configured_environment_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured GPU nodes are rendered into the provider command."""
    monkeypatch.setenv("NICO_HEALTH_AGENT_NODES", "gpu-01,gpu-02")

    assert _render_nico_args()[0] == "--nodes=gpu-01,gpu-02"


def test_expected_nodes_counts_only_the_fleet_gpu_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Machines without GPUs are outside BFX04-01, so they must not inflate coverage."""
    monkeypatch.setenv("NICO_HEALTH_AGENT_NODES", "gpu-01,gpu-02")
    inventory = {"nodes": [{"gpu_count": 8}, {"gpu_count": 0}, {"gpu_count": 4}]}

    assert _render_nico_args(inventory)[1] == "--expected-nodes=2"


def test_a_missing_inventory_renders_a_zero_the_script_must_reject(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The two halves of the silent-zero trap, asserted together.

    ``| length`` over a step that never ran renders "0" rather than empty, so
    the orchestrator's missing-step guard does not fire and the step is invoked
    with a fleet of nobody. Nothing upstream catches that, which is why the
    script treats a zero as a failure rather than as full coverage.
    """
    monkeypatch.setenv("NICO_HEALTH_AGENT_NODES", "gpu-01,gpu-02")

    assert _render_nico_args()[1] == "--expected-nodes=0"

    module = _load_script()
    monkeypatch.setattr(sys, "argv", [SCRIPT.name, "--nodes=gpu-01,gpu-02", "--expected-nodes=0"])
    assert module.main() == 1
    assert "at least 1" in json.loads(capsys.readouterr().out)["error"]


def test_unconfigured_nodes_skip_before_any_ssh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With nothing configured the step must skip rather than claim a pass."""
    module = _load_script()

    def refuse(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        """Fail the test if the script attempts any SSH probe."""
        raise AssertionError("no SSH probe may run without configured nodes")

    monkeypatch.setattr(module.subprocess, "run", refuse)
    monkeypatch.setattr(sys, "argv", [SCRIPT.name, "--nodes="])

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "success": True,
        "platform": "bare_metal",
        "test_name": "query_node_health_agents",
        "skipped": True,
        "skip_reason": "No GPU nodes configured for health-agent inspection",
        "agents_observable": False,
        "agents": [],
    }


@pytest.mark.parametrize("unit", _load_script().AGENT_UNITS)
def test_any_supported_agent_unit_covers_its_node(unit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Either accepted agent, under any of its unit names, proves coverage."""
    module = _load_script()
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub({"gpu-01": _states(unit)}))

    result = module._query(["gpu-01"], expected=1)

    assert result["agents_observable"] is True
    assert result["agents"] == [{"node_id": "gpu-01", "agent_name": unit, "running": True}]


def test_node_without_an_active_agent_is_reported_as_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inactive fleet cannot produce a false pass."""
    module = _load_script()
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub({"gpu-01": _states()}))

    result = module._query(["gpu-01"], expected=1)

    assert result["agents"] == [{"node_id": "gpu-01", "agent_name": "", "running": False}]


def test_evidence_stays_aligned_with_its_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent probes must not misattribute one node's agent to another."""
    module = _load_script()
    states = {"gpu-01": _states("gpud"), "gpu-02": _states(), "gpu-03": _states("nvsentinel")}
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub(states))

    result = module._query(["gpu-01", "gpu-02", "gpu-03"], expected=3)

    assert result["agents"] == [
        {"node_id": "gpu-01", "agent_name": "gpud", "running": True},
        {"node_id": "gpu-02", "agent_name": "", "running": False},
        {"node_id": "gpu-03", "agent_name": "nvsentinel", "running": True},
    ]


def test_repeated_nodes_are_probed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicated target must not inflate the reported node count."""
    module = _load_script()
    calls: list[list[str]] = []
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub({"gpu-01": _states("gpud")}, calls))

    result = module._query(module._parse_nodes("gpu-01, gpu-01"), expected=1)

    assert len(calls) == 1
    assert [agent["node_id"] for agent in result["agents"]] == ["gpu-01"]


def test_probe_ends_ssh_options_before_the_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--`` must precede the host so a target can never be read as an option."""
    module = _load_script()
    calls: list[list[str]] = []
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub({"gpu-01": _states("gpud")}, calls))

    module._query(["gpu-01"], expected=1)

    assert calls[0][:1] == ["ssh"]
    assert calls[0][-3:] == [
        "--",
        "gpu-01",
        f"systemctl is-active {' '.join(module.AGENT_UNITS)} 2>/dev/null || true",
    ]
    assert "BatchMode=yes" in calls[0]


def test_declared_fleet_size_reaches_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check compares coverage against the declared size, so it must be reported."""
    module = _load_script()
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub({"gpu-01": _states("gpud")}))

    result = module._query(["gpu-01"], expected=64)

    assert result["nodes_expected"] == 64


def test_a_probed_fleet_must_declare_its_size() -> None:
    """Omitting the count would let a subset probe pass for the whole fleet."""
    module = _load_script()

    with pytest.raises(module.NodeHealthQueryError, match="--expected-nodes is required"):
        module._parse_expected("", ["gpu-01"])


def test_a_zero_fleet_size_is_rejected_rather_than_covered() -> None:
    """A YAML aggregate over a step that never ran renders 0, not empty.

    The orchestrator's missing-step guard only fires on an empty render, so
    ``| length`` over a failed inventory step would slip through as a fleet of
    nobody that any number of records covers. It has to fail here instead.

    Asserted on ``_query`` rather than on the CLI parser because that is where
    the invariant belongs: every caller is held to it, not just this one, and the
    rejection lands before any node is probed.
    """
    module = _load_script()

    with pytest.raises(module.NodeHealthQueryError, match="at least 1"):
        module._query(["gpu-01"], expected=0)


def test_a_probed_fleet_size_cannot_come_from_the_probed_list() -> None:
    """``expected`` has no default, so ``len(nodes)`` cannot quietly become the fleet.

    The size exists to be compared against the list under test; sourcing it from
    that list compares it to itself and lets any subset cover the fleet.
    """
    module = _load_script()

    with pytest.raises(TypeError, match="expected"):
        module._query(["gpu-01"])  # type: ignore[call-arg]


def test_unconfigured_nodes_need_no_fleet_size() -> None:
    """The skip path asserts nothing, so it demands nothing to compare against."""
    module = _load_script()

    assert module._parse_expected("", []) == 0


def test_a_fleet_can_name_its_own_agent_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """A site running another agent configures it rather than editing shared/."""
    module = _load_script()
    calls: list[list[str]] = []
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub({"gpu-01": "active\n"}, calls))

    result = module._query(["gpu-01"], expected=1, units=("acme-gpu-watch",))

    assert calls[0][-1] == "systemctl is-active acme-gpu-watch 2>/dev/null || true"
    assert result["agents"] == [{"node_id": "gpu-01", "agent_name": "acme-gpu-watch", "running": True}]


def test_unset_units_fall_back_to_the_reference_default() -> None:
    """An unconfigured --units keeps this reference's own answer to the question."""
    module = _load_script()

    assert module._parse_units("") == module.AGENT_UNITS
    assert module._parse_units("acme-watch, other.service") == ("acme-watch", "other.service")


@pytest.mark.parametrize("value", ["unit;reboot", "$(reboot)", "-oProxyCommand=x"])
def test_unsafe_unit_names_are_rejected(value: str) -> None:
    """Units reach a remote shell, so they cannot smuggle shell syntax either."""
    module = _load_script()

    with pytest.raises(module.NodeHealthQueryError, match="Invalid health agent unit name"):
        module._parse_units(value)


@pytest.mark.parametrize("value", ["-oProxyCommand=x", "gpu-01;reboot", "$(reboot)", "root@gpu-01"])
def test_unsafe_node_names_are_rejected(value: str) -> None:
    """Node input cannot smuggle SSH options or remote shell syntax."""
    module = _load_script()

    with pytest.raises(module.NodeHealthQueryError, match="Invalid bare-metal node name"):
        module._parse_nodes(value)


def test_transport_failure_is_reported_without_leaking_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SSH failures fail the step without copying host output into the payload."""
    module = _load_script()
    failure = subprocess.CompletedProcess([], 255, stdout="private inventory", stderr="private auth error")
    monkeypatch.setattr(module.subprocess, "run", lambda *_, **__: failure)
    monkeypatch.setattr(sys, "argv", [SCRIPT.name, "--nodes=gpu-01"])

    assert module.main() == 1
    output = capsys.readouterr().out
    assert "private" not in output
    payload = json.loads(output)
    assert payload["success"] is False
    assert payload["error_type"] == "node_health_query_failed"


def test_unreadable_node_never_becomes_a_not_running_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node we could not reach must not be reported as lacking an agent.

    Emitting a record for an unreachable node would blame a missing agent for an
    access problem, so the step fails naming the node instead.
    """
    module = _load_script()
    states = {"gpu-01": _states("gpud")}

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Answer known nodes with canned states; fail the SSH transport otherwise."""
        node = command[-2]
        if node not in states:
            return subprocess.CompletedProcess(command, 255, stdout="", stderr="no route to host")
        return subprocess.CompletedProcess(command, 0, stdout=states[node], stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(module.NodeHealthQueryError) as raised:
        module._query(["gpu-01", "gpu-99"], expected=2)

    assert "gpu-99" in str(raised.value)


def test_every_unreadable_node_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """One run must diagnose a fleet-wide access problem, not just its first node."""
    module = _load_script()
    unreachable = subprocess.CompletedProcess([], 255, stdout="", stderr="")
    monkeypatch.setattr(module.subprocess, "run", lambda *_, **__: unreachable)

    with pytest.raises(module.NodeHealthQueryError) as raised:
        module._query(["gpu-01", "gpu-02", "gpu-03"], expected=3)

    assert str(raised.value) == "Health agent query failed for 3 node(s): gpu-01, gpu-02, gpu-03"


def test_sweep_abandons_probes_once_the_budget_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fleet too large to probe in the budget must fail by name, not be killed.

    Without the deadline the step outlives its timeout and the orchestrator kills
    it, discarding stdout and with it the JSON contract -- so the run reports no
    finding at all rather than naming the nodes it never reached. The elapsed
    assertion is the other half: returning the finding is only useful if it
    arrives without waiting on the stragglers it is reporting.
    """
    module = _load_script()
    release = threading.Event()

    def crawl(node: str, *_: object) -> str:
        """Block until the test releases it, far beyond the sweep's budget."""
        release.wait(timeout=10)
        return "gpud"

    monkeypatch.setattr(module, "_probe", crawl)

    started = time.monotonic()
    try:
        with pytest.raises(module.NodeHealthQueryError) as raised:
            module._query(["gpu-01", "gpu-02"], expected=2, budget_seconds=0.05)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    # Generous next to the 10s probes it abandons, so a loaded CI runner cannot
    # fail this, while still proving the sweep did not wait for them.
    assert elapsed < 2.0
    assert "exceeded its 0.05s budget" in str(raised.value)
    assert "gpu-01" in str(raised.value)
    assert "gpu-02" in str(raised.value)


def test_a_completed_probe_survives_a_budget_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Abandoning the sweep must not discard evidence already collected.

    Asserts on the sweep rather than the error text: ``_query`` reports abandoned
    nodes before it looks at unreadable ones, so the same message would come back
    had the completed probe been retained as ``None``.
    """
    module = _load_script()
    release = threading.Event()

    def uneven(node: str, *_: object) -> str:
        """Answer immediately for the first node and block on the second."""
        if node == "gpu-02":
            release.wait(timeout=10)
        return "gpud"

    monkeypatch.setattr(module, "_probe", uneven)

    started = time.monotonic()
    try:
        units = module._sweep(["gpu-01", "gpu-02"], budget_seconds=0.5)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert elapsed < 2.5
    assert units == {"gpu-01": "gpud"}


def test_unreadable_systemctl_output_fails_rather_than_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node without systemd yields no evidence, so it cannot be counted covered."""
    module = _load_script()
    monkeypatch.setattr(module.subprocess, "run", _ssh_stub({"gpu-01": ""}))

    with pytest.raises(module.NodeHealthQueryError, match="gpu-01"):
        module._query(["gpu-01"], expected=1)

    assert module._probe("gpu-01") is None


def test_invalid_arguments_emit_failure_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argument errors must preserve the provider JSON contract."""
    module = _load_script()
    monkeypatch.setattr(sys, "argv", [SCRIPT.name, "--unknown-option"])

    assert module.main() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert captured.err == ""
    assert payload["success"] is False
    assert "unrecognized arguments" in payload["error"]
