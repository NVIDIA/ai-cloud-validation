# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for the nvidia-smi GPU validation."""

from __future__ import annotations

from unittest.mock import patch

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_gpu import K8sNvidiaSmiCheck


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def test_gpu_node_discovery_fails_on_invalid_json() -> None:
    """Verify GPU node discovery fails clearly when kubectl emits invalid JSON."""
    check = K8sNvidiaSmiCheck(config={})
    with (
        patch("isvtest.validations.k8s_gpu.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok("not-json")),
    ):
        results = check._run_ephemeral_pods(timeout=1)

    # ``None`` (not ``{}``) signals "hard failure already routed to set_failed"
    # so the caller can distinguish it from "cluster has no GPU nodes".
    assert results is None
    assert not check.passed
    assert "Failed to parse GPU node list" in check.message


def test_run_does_not_overwrite_set_failed_when_node_discovery_fails() -> None:
    """Regression: parse failure must not be clobbered by a trailing set_passed in run()."""
    check = K8sNvidiaSmiCheck(config={})
    with (
        patch("isvtest.validations.k8s_gpu.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok("not-json")),
    ):
        check.run()

    assert not check.passed
    assert "Failed to parse GPU node list" in check.message
