# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Map GPU Operator self-certification test sets onto static catalog tests.

Each catalog test runs one test set (``TS1000`` to ``TS1008``) of the
``gpu-operator-certification`` binary in its own Job (see :mod:`.runner`) and
reports the binary's tests as subtests. The test sets reconfigure the GPU
Operator and restore it afterwards, so every check skips unless enabled.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, ClassVar

import pytest

from isvtest.config.settings import get_k8s_gpu_operator_namespace
from isvtest.core.validation import BaseValidation
from isvtest.validations.k8s_gpu_operator_cert.runner import run_test_set

# Config keys passed to the binary as its upper-case environment variables.
_ENV_KEYS = (
    "test_timeout",
    "test_workload_image",
    "target_driver_repository",
    "target_driver_image",
    "target_driver_version",
    "rdma_base_image",
)
_DEFAULT_TIMEOUT = 3600
_GO_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")


class _CertificationTestSet(BaseValidation):
    """Run one self-certification test set and report its result."""

    _exclude_from_discovery: ClassVar[bool] = True
    test_set: ClassVar[str] = ""

    def run(self) -> None:
        if str(self.config.get("enabled", "")).strip().lower() != "true":
            pytest.skip(
                "GPU Operator self-certification is opt-in: set tests.settings.gpu_operator_certification.enabled=true"
            )
        image = str(self.config.get("image") or "").strip()
        if not image:
            self.set_failed("image is required: set tests.settings.gpu_operator_certification.image")
            return
        timeout = self._parse_positive_int("timeout", default=_DEFAULT_TIMEOUT)
        if timeout is None:
            return
        namespace = str(self.config.get("namespace") or "").strip() or get_k8s_gpu_operator_namespace()
        env = {key.upper(): value for key in _ENV_KEYS if (value := str(self.config.get(key) or "").strip())}

        output = run_test_set(self.test_set, namespace=namespace, image=image, env=env, timeout=timeout)
        if "error" in output:
            self.set_failed(f"{self.test_set}: {output['error']}")
            return
        self._report(output["result"])

    def _report(self, result: dict[str, Any]) -> None:
        """Report each test of the set as a subtest, then the set's own verdict."""
        tests = [test for test in result.get("tests") or [] if isinstance(test, dict)]
        seen: Counter[str] = Counter()
        skip_reasons: list[str] = []
        for test in tests:
            name = str(test.get("name") or "unnamed")
            # A test set may run the same test twice, e.g. operands before and after a driver update.
            seen[name] += 1
            if seen[name] > 1:
                name = f"{name} ({seen[name]})"
            status = test.get("status")
            details = "\n".join(str(detail).removeprefix("→ ") for detail in test.get("details") or [])
            message = "\n".join(text for text in (str(test.get("error") or ""), details) if text)
            if status == "skipped" and details:
                skip_reasons.append(details.replace("\n", "; "))
            self.report_subtest(
                name,
                passed=status == "passed",
                skipped=status == "skipped",
                message=message,
                duration=_seconds(test.get("duration")),
            )

        status = result.get("status")
        if status == "failed":
            self.set_failed(f"{self.test_set} failed: {result.get('error') or 'no error reported'}")
        elif status == "skipped":
            pytest.skip("; ".join(skip_reasons) or f"{self.test_set} skipped")
        elif status == "passed" and tests:
            self.set_passed(f"{self.test_set} passed ({len(tests)} tests)")
        elif status == "passed":
            self.set_failed(f"{self.test_set} passed without running any test")
        else:
            self.set_failed(f"{self.test_set} reported an unknown status: {status!r}")


def _seconds(value: Any) -> float | None:
    """Parse the binary's Go duration (e.g. ``1m30s``) into seconds."""
    match = _GO_DURATION.fullmatch(str(value or ""))
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


class K8sGpuOperatorDefaultSettings(_CertificationTestSet):
    """TS1000: operands, GPU allocatable, GFD labels, DCGM metrics, CUDA and PyTorch workloads."""

    description: ClassVar[str] = (
        "Verify the default GPU Operator deployment runs GPU workloads (self-certification TS1000)"
    )
    test_set: ClassVar[str] = "TS1000"


class K8sGpuOperatorTimeSlicing(_CertificationTestSet):
    """TS1001: GPU sharing through a time-slicing configuration."""

    description: ClassVar[str] = "Verify GPU sharing via time-slicing (self-certification TS1001)"
    test_set: ClassVar[str] = "TS1001"


class K8sGpuOperatorDcgmStandalone(_CertificationTestSet):
    """TS1002: standalone DCGM and its metrics."""

    description: ClassVar[str] = "Verify standalone DCGM serves GPU metrics (self-certification TS1002)"
    test_set: ClassVar[str] = "TS1002"


class K8sGpuOperatorDriverUpdate(_CertificationTestSet):
    """TS1003: driver update through the GPU Operator; skips without a target driver."""

    description: ClassVar[str] = "Verify a GPU driver update through the GPU Operator (self-certification TS1003)"
    test_set: ClassVar[str] = "TS1003"


class K8sGpuOperatorMigSingle(_CertificationTestSet):
    """TS1004: MIG mode with the single strategy; skips without MIG-capable GPUs."""

    description: ClassVar[str] = "Verify MIG mode with the single strategy (self-certification TS1004)"
    test_set: ClassVar[str] = "TS1004"


class K8sGpuOperatorMigMixed(_CertificationTestSet):
    """TS1005: MIG mode with the mixed strategy; skips without MIG-capable GPUs."""

    description: ClassVar[str] = "Verify MIG mode with the mixed strategy (self-certification TS1005)"
    test_set: ClassVar[str] = "TS1005"


class K8sGpuOperatorGpuDirectRdma(_CertificationTestSet):
    """TS1006: GPUDirect RDMA between two GPU nodes; skips without RDMA hardware."""

    description: ClassVar[str] = "Verify GPUDirect RDMA bandwidth between GPU nodes (self-certification TS1006)"
    test_set: ClassVar[str] = "TS1006"


class K8sGpuOperatorGpuDirectRdmaDmaBuf(_CertificationTestSet):
    """TS1007: GPUDirect RDMA over DMA-BUF between two GPU nodes; skips without RDMA hardware."""

    description: ClassVar[str] = (
        "Verify GPUDirect RDMA bandwidth over DMA-BUF between GPU nodes (self-certification TS1007)"
    )
    test_set: ClassVar[str] = "TS1007"


class K8sGpuOperatorGdrCopy(_CertificationTestSet):
    """TS1008: GDRCopy; skips without operator-managed driver pods."""

    description: ClassVar[str] = "Verify GDRCopy through the GPU Operator (self-certification TS1008)"
    test_set: ClassVar[str] = "TS1008"
