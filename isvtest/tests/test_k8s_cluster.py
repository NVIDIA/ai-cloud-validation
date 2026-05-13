# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for cluster-wide pod-health validations in k8s_cluster.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_cluster import K8sNoErrorPodsCheck, K8sPodHealthCheck


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _pod(name: str, phase: str = "Running", waiting_reason: str = "") -> dict[str, Any]:
    """Return a minimal pod object."""
    status: dict[str, Any] = {"phase": phase}
    if waiting_reason:
        status["containerStatuses"] = [{"state": {"waiting": {"reason": waiting_reason}}}]
    return {"metadata": {"namespace": "default", "name": name}, "status": status}


def _items_json(items: list[dict[str, Any]]) -> str:
    """Wrap Kubernetes list items in a JSON payload."""
    return json.dumps({"items": items})


def test_pod_health_uses_json_and_passes_for_running_or_succeeded_pods() -> None:
    """Verify the pod-health check reads structured pod JSON."""
    check = K8sPodHealthCheck(config={})
    payload = _items_json([_pod("running", "Running"), _pod("done", "Succeeded")])

    with (
        patch("isvtest.validations.k8s_cluster.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok(payload)) as mock_run,
    ):
        check.run()

    assert check.passed
    assert mock_run.call_args[0][0] == "kubectl get pods -A -o json"


def test_pod_health_fails_on_invalid_json() -> None:
    """Verify invalid pod JSON is surfaced as a validation failure."""
    check = K8sPodHealthCheck(config={})
    with patch.object(check, "run_command", return_value=_ok("not-json")):
        check.run()
    assert not check.passed
    assert "Failed to parse pod list" in check.message


def test_no_error_pods_detects_waiting_reason_from_json() -> None:
    """Verify CrashLoopBackOff is detected from containerStatuses instead of table output."""
    check = K8sNoErrorPodsCheck(config={})
    payload = _items_json([_pod("bad", "Running", waiting_reason="CrashLoopBackOff")])

    with patch.object(check, "run_command", return_value=_ok(payload)):
        check.run()

    assert not check.passed
    assert "CrashLoopBackOff" in check.message


def test_no_error_pods_detects_evicted_via_pod_level_reason() -> None:
    """Regression: ``error_states: [Evicted]`` matches pods with status.reason set but no container state."""
    check = K8sNoErrorPodsCheck(config={"error_states": ["Evicted"]})
    evicted = {
        "metadata": {"namespace": "default", "name": "doomed"},
        "status": {"phase": "Failed", "reason": "Evicted"},
    }
    payload = _items_json([evicted])

    with patch.object(check, "run_command", return_value=_ok(payload)):
        check.run()

    assert not check.passed
    assert "Evicted" in check.message
    assert "doomed" in check.message
