# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The k8s suite's gpu_operator_certification group, run end to end without a cluster."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from isvtest.core.resolution import State
from isvtest.validations.k8s_gpu_operator_cert import checks

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.loop import Orchestrator, OrchestratorResult, Phase

_K8S_SUITE = Path(__file__).resolve().parents[2] / "isvctl" / "configs" / "suites" / "k8s.yaml"
_GROUP = "gpu_operator_certification"


def _run(settings: dict[str, Any] | None = None) -> OrchestratorResult:
    merged = merge_yaml_files([_K8S_SUITE])
    merged["tests"]["settings"][_GROUP].update(settings or {})
    return Orchestrator(RunConfig.model_validate(merged), working_dir=_K8S_SUITE.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
        include_labels=[_GROUP],
    )


def _states(result: OrchestratorResult) -> dict[str, State]:
    return {v.entry.name: v.state for v in result.validations if _GROUP in v.entry.labels}


def test_default_settings_skip_every_test_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A regular k8s run never reconfigures the GPU Operator."""
    calls: list[str] = []
    monkeypatch.setattr(checks, "run_test_set", lambda test_set, **_: calls.append(test_set))

    result = _run()

    assert result.success is True
    assert len(_states(result)) == 9
    assert set(_states(result).values()) == {State.SKIPPED}
    assert calls == []


def test_enabled_runs_each_test_set_in_its_own_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def run_test_set(test_set: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"test_set": test_set, **kwargs})
        status = "failed" if test_set == "TS1002" else "passed"
        return {
            "result": {"id": test_set, "status": status, "tests": [{"name": "t", "status": status}]},
        }

    monkeypatch.setattr(checks, "run_test_set", run_test_set)

    result = _run({"enabled": True, "target_driver_version": "580.126.16"})

    assert result.success is False
    states = _states(result)
    assert states.pop("K8sGpuOperatorDcgmStandalone") is State.FAILED
    assert set(states.values()) == {State.PASSED}
    assert [call["test_set"] for call in calls] == [f"TS100{i}" for i in range(9)]
    assert {call["image"] for call in calls} == {
        "ghcr.io/nvidia/gpu-operator@sha256:4e6f7f29d8380abd794fbf296082144227d702cc877d8837c3b652a2787e5dc5"
    }
    timeout = {"TEST_TIMEOUT": "15m"}
    workload = {**timeout, "TEST_WORKLOAD_IMAGE": "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0-ubuntu22.04"}
    rdma = {**timeout, "RDMA_BASE_IMAGE": "nvcr.io/nvidia/cuda:13.1.1-base-ubuntu24.04"}
    driver = {"TARGET_DRIVER_REPOSITORY": "", "TARGET_DRIVER_IMAGE": "", "TARGET_DRIVER_VERSION": "580.126.16"}
    assert {call["test_set"]: call["env"] for call in calls} == {
        "TS1000": workload,
        "TS1001": workload,
        "TS1002": timeout,
        "TS1003": {**workload, **driver},
        "TS1004": workload,
        "TS1005": workload,
        "TS1006": rdma,
        "TS1007": rdma,
        "TS1008": workload,
    }
