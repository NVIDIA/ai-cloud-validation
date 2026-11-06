# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Self-certification test sets reported as static catalog tests."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.k8s_gpu_operator_cert import checks
from isvtest.validations.k8s_gpu_operator_cert.checks import (
    K8sGpuOperatorDefaultSettings,
    K8sGpuOperatorDriverUpdate,
)

pytestmark = pytest.mark.unit

_ENABLED = {"enabled": "True", "image": "ghcr.io/nvidia/gpu-operator:main-latest", "namespace": "gpu-ns"}


class _FakeRun:
    """Stand in for the Job run; each test sets what it returned."""

    def __init__(self) -> None:
        self.output: dict[str, Any] = {"result": {"status": "passed", "tests": [{"name": "t"}]}}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, test_set: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"test_set": test_set, **kwargs})
        return self.output


@pytest.fixture(autouse=True)
def run(monkeypatch: pytest.MonkeyPatch) -> _FakeRun:
    fake = _FakeRun()
    monkeypatch.setattr(checks, "run_test_set", fake)
    return fake


def _result(status: str, tests: list[dict[str, Any]], error: str = "") -> dict[str, Any]:
    return {"result": {"id": "TS1000", "status": status, "error": error, "tests": tests}}


@pytest.mark.parametrize("enabled", [False, "False", "", None])
def test_disabled_skips_without_running(run: _FakeRun, enabled: Any) -> None:
    with pytest.raises(pytest.skip.Exception, match="opt-in"):
        K8sGpuOperatorDefaultSettings(config={**_ENABLED, "enabled": enabled}).execute()
    assert run.calls == []


@pytest.mark.parametrize("enabled", [True, "True", "true"])
def test_enabled_runs_its_own_test_set(run: _FakeRun, enabled: Any) -> None:
    assert K8sGpuOperatorDriverUpdate(config={**_ENABLED, "enabled": enabled}).execute()["passed"]
    [call] = run.calls
    assert call["test_set"] == "TS1003"
    assert call["namespace"] == "gpu-ns"
    assert call["timeout"] == 3600


def test_every_catalog_test_runs_a_distinct_test_set() -> None:
    classes = [cls for cls in vars(checks).values() if isinstance(cls, type) and cls.__name__.startswith("K8sGpu")]
    assert sorted(cls.test_set for cls in classes) == [f"TS100{i}" for i in range(9)]


def test_only_set_binary_inputs_override_the_manifest(run: _FakeRun) -> None:
    config = {**_ENABLED, "target_driver_version": " 580.126.16 ", "target_driver_image": "", "unrelated": "x"}

    K8sGpuOperatorDriverUpdate(config=config).execute()

    assert run.calls[0]["env"] == {"TARGET_DRIVER_VERSION": "580.126.16"}


def test_empty_namespace_falls_back_to_the_setting(run: _FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_GPU_OPERATOR_NAMESPACE", "operator-ns")

    K8sGpuOperatorDefaultSettings(config={**_ENABLED, "namespace": ""}).execute()

    assert run.calls[0]["namespace"] == "operator-ns"


@pytest.mark.parametrize(("key", "value"), [("image", ""), ("timeout", "0")])
def test_invalid_inputs_fail_without_running(run: _FakeRun, key: str, value: str) -> None:
    assert not K8sGpuOperatorDefaultSettings(config={**_ENABLED, key: value}).execute()["passed"]
    assert run.calls == []


def test_passed_set_reports_each_test_as_a_subtest(run: _FakeRun) -> None:
    run.output = _result(
        "passed",
        [
            {"name": "operand-verification", "status": "passed", "duration": "1m30s", "details": ["→ 7 operands"]},
            {"name": "driver-update", "status": "passed", "duration": "0s"},
            {"name": "operand-verification", "status": "passed", "duration": "2s"},
        ],
    )

    result = K8sGpuOperatorDriverUpdate(config=_ENABLED).execute()

    assert result["passed"]
    assert [(s["name"], s["duration"]) for s in result["subtests"]] == [
        ("operand-verification", 90),
        ("driver-update", 0),
        ("operand-verification (2)", 2),
    ]
    assert result["subtests"][0]["message"] == "7 operands"


def test_failed_set_fails_with_its_error(run: _FakeRun) -> None:
    run.output = _result(
        "failed",
        [
            {"name": "operand-verification", "status": "passed"},
            {"name": "dcgm", "status": "failed", "error": "no metrics"},
        ],
        error="no metrics",
    )

    result = K8sGpuOperatorDefaultSettings(config=_ENABLED).execute()

    assert not result["passed"]
    assert result["error"] == "TS1000 failed: no metrics"
    assert [s["passed"] for s in result["subtests"]] == [True, False]
    assert result["subtests"][1]["message"] == "no metrics"


def test_skipped_set_skips_with_the_binary_reason(run: _FakeRun) -> None:
    run.output = _result(
        "skipped",
        [
            {"name": "operand-verification", "status": "passed"},
            {"name": "driver-update", "status": "skipped", "details": ["→ TARGET_DRIVER_VERSION not set"]},
        ],
    )

    with pytest.raises(pytest.skip.Exception, match="TARGET_DRIVER_VERSION not set"):
        K8sGpuOperatorDriverUpdate(config=_ENABLED).execute()


def test_run_error_fails_with_the_test_set(run: _FakeRun) -> None:
    run.output = {"error": "certification Job failed: boom"}

    result = K8sGpuOperatorDefaultSettings(config=_ENABLED).execute()

    assert result["error"] == "TS1000: certification Job failed: boom"


@pytest.mark.parametrize(("status", "error"), [("passed", "passed without running"), ("weird", "unknown status")])
def test_never_passes_without_a_known_result(run: _FakeRun, status: str, error: str) -> None:
    run.output = _result(status, [])

    result = K8sGpuOperatorDefaultSettings(config=_ENABLED).execute()

    assert not result["passed"]
    assert error in result["error"]
