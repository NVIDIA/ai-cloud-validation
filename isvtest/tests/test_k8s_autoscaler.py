# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the Cluster Autoscaler integration validation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_autoscaler import K8sClusterAutoscalerCheck


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failed ``CommandResult``."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _items_json(items: list[dict[str, object]]) -> str:
    """Wrap Kubernetes list items in a JSON payload."""
    return json.dumps({"items": items})


def _deployment(
    *,
    namespace: str = "kube-system",
    name: str = "cluster-autoscaler",
    replicas: int = 1,
    available: int = 1,
    labels: dict[str, str] | None = None,
    container_name: str = "cluster-autoscaler",
) -> dict[str, object]:
    """Build a minimal Deployment object."""
    match_labels = labels or {"app.kubernetes.io/name": "cluster-autoscaler"}
    return {
        "metadata": {"namespace": namespace, "name": name, "labels": match_labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": match_labels},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container_name,
                            "image": "registry.k8s.io/autoscaling/cluster-autoscaler:v1.30.0",
                        }
                    ]
                }
            },
        },
        "status": {"availableReplicas": available},
    }


def _pod(name: str = "cluster-autoscaler-abc", phase: str = "Running") -> dict[str, object]:
    """Build a minimal Pod object."""
    return {"metadata": {"namespace": "kube-system", "name": name}, "status": {"phase": phase}}


def test_cluster_autoscaler_passes_with_labeled_available_deployment_and_running_pod() -> None:
    """Verify the happy path discovers a standard upstream-labeled deployment."""
    check = K8sClusterAutoscalerCheck(config={})
    deployment = _deployment()
    responses: Iterator[CommandResult] = iter(
        [
            _ok(_items_json([deployment])),
            _ok(_items_json([])),
            _ok(_items_json([])),
            _fail(stderr='Error from server (NotFound): deployments.apps "cluster-autoscaler" not found'),
            _ok(_items_json([_pod()])),
        ]
    )

    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", side_effect=lambda *a, **k: next(responses)) as mock_run,
    ):
        check.run()

    assert check.passed, check.message
    assert "healthy Cluster Autoscaler" in check.message
    assert mock_run.call_args_list[0][0][0] == (
        "kubectl get deployments -A -l app.kubernetes.io/name=cluster-autoscaler -o json"
    )
    assert mock_run.call_args_list[-1][0][0] == (
        "kubectl get pods -n kube-system -l app.kubernetes.io/name=cluster-autoscaler -o json"
    )


def test_cluster_autoscaler_falls_back_to_configured_deployment_name() -> None:
    """Verify name-based discovery catches installs without common upstream labels."""
    check = K8sClusterAutoscalerCheck(config={"label_selectors": [], "namespace": "autoscaler-system"})
    deployment = _deployment(namespace="autoscaler-system", labels={"app": "ca"})
    responses: Iterator[CommandResult] = iter(
        [
            _ok(json.dumps(deployment)),
            _ok(_items_json([_pod()])),
        ]
    )

    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", side_effect=lambda *a, **k: next(responses)) as mock_run,
    ):
        check.run()

    assert check.passed, check.message
    assert mock_run.call_args_list[0][0][0] == (
        "kubectl get deployment -n autoscaler-system cluster-autoscaler -o json"
    )


def test_cluster_autoscaler_skips_when_no_deployment_is_found_by_default() -> None:
    """Verify absent Cluster Autoscaler is treated as 'feature not installed' (skip)."""
    check = K8sClusterAutoscalerCheck(config={"label_selectors": [], "namespaces": ["kube-system"]})
    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(
            check,
            "run_command",
            return_value=_fail(stderr='Error from server (NotFound): deployments.apps "cluster-autoscaler" not found'),
        ),
        pytest.raises(pytest.skip.Exception) as excinfo,
    ):
        check.run()

    assert "No Cluster Autoscaler deployment found" in str(excinfo.value)
    assert "require_autoscaler is false" in str(excinfo.value)


def test_cluster_autoscaler_fails_when_no_deployment_is_found_and_required() -> None:
    """Verify require_autoscaler=True turns an absent deployment into a failure."""
    check = K8sClusterAutoscalerCheck(
        config={"label_selectors": [], "namespaces": ["kube-system"], "require_autoscaler": True}
    )
    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(
            check,
            "run_command",
            return_value=_fail(stderr='Error from server (NotFound): deployments.apps "cluster-autoscaler" not found'),
        ),
    ):
        check.run()

    assert not check.passed
    assert "No Cluster Autoscaler deployment found" in check.message


def test_cluster_autoscaler_fails_when_deployment_is_unavailable() -> None:
    """Verify unavailable replicas are reported."""
    check = K8sClusterAutoscalerCheck(config={"label_selectors": ["app=cluster-autoscaler"], "deployment_names": []})
    deployment = _deployment(replicas=2, available=1)
    responses: Iterator[CommandResult] = iter(
        [
            _ok(_items_json([deployment])),
            _ok(_items_json([_pod("cluster-autoscaler-1"), _pod("cluster-autoscaler-2", phase="Pending")])),
        ]
    )

    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", side_effect=lambda *a, **k: next(responses)),
    ):
        check.run()

    assert not check.passed
    assert "1/2 replicas available" in check.message
    assert "1/2 matching pods Running" in check.message


def test_cluster_autoscaler_fails_without_match_labels_for_pod_check() -> None:
    """Verify deployments without matchLabels do not pass without pod verification."""
    check = K8sClusterAutoscalerCheck(config={"label_selectors": ["app=cluster-autoscaler"], "deployment_names": []})
    deployment = _deployment()
    spec = deployment["spec"]
    assert isinstance(spec, dict)
    spec["selector"] = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["cluster-autoscaler"]}]}

    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok(_items_json([deployment]))) as mock_run,
    ):
        check.run()

    assert not check.passed
    assert "selector has no matchLabels" in check.message
    assert mock_run.call_count == 1


def test_cluster_autoscaler_fails_on_invalid_deployment_json() -> None:
    """Verify malformed kubectl JSON is surfaced as a validation failure."""
    check = K8sClusterAutoscalerCheck(config={"label_selectors": ["app=cluster-autoscaler"], "deployment_names": []})
    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok("not-json")),
    ):
        check.run()

    assert not check.passed
    assert "Failed to parse deployments" in check.message


def test_cluster_autoscaler_rejects_bad_config() -> None:
    """Verify bad config fails before invoking kubectl."""
    check = K8sClusterAutoscalerCheck(config={"namespaces": [123]})
    with patch.object(check, "run_command") as mock_run:
        check.run()

    assert not check.passed
    assert "Invalid config" in check.message
    mock_run.assert_not_called()


def _managed_evidence(
    *,
    enabled: object = True,
    node_pool: object = "system-pool",
    min_nodes: object = 1,
    max_nodes: object = 4,
) -> str:
    """Return provider-neutral managed-autoscaler command output."""
    return json.dumps(
        {
            "enabled": enabled,
            "node_pool": node_pool,
            "min_nodes": min_nodes,
            "max_nodes": max_nodes,
        }
    )


def test_cluster_autoscaler_provider_managed_mode_passes_without_kubectl() -> None:
    """Verify command readback can prove a managed control-plane autoscaler."""
    check = K8sClusterAutoscalerCheck(
        config={"mode": "provider_managed", "provider_managed_command": "probe-managed-autoscaler"}
    )
    with (
        patch("isvtest.validations.k8s_autoscaler.get_kubectl_base_shell") as mock_kubectl,
        patch.object(check, "run_command", return_value=_ok(_managed_evidence())) as mock_run,
    ):
        check.run()

    assert check.passed, check.message
    assert "node_pool=system-pool" in check.message
    assert "min_nodes=1" in check.message
    assert "max_nodes=4" in check.message
    mock_run.assert_called_once_with("probe-managed-autoscaler")
    mock_kubectl.assert_not_called()


def test_cluster_autoscaler_provider_managed_mode_requires_command() -> None:
    """Verify explicit managed mode cannot silently fall back to Deployment discovery."""
    check = K8sClusterAutoscalerCheck(config={"mode": "provider_managed"})
    with patch.object(check, "run_command") as mock_run:
        check.run()

    assert not check.passed
    assert "provider_managed_command must be a non-empty string" in check.message
    mock_run.assert_not_called()


def test_cluster_autoscaler_provider_managed_mode_fails_on_command_error() -> None:
    """Verify provider API or permission failures remain validation failures."""
    check = K8sClusterAutoscalerCheck(
        config={"mode": "provider_managed", "provider_managed_command": "probe-managed-autoscaler"}
    )
    with patch.object(check, "run_command", return_value=_fail(stderr="permission denied")):
        check.run()

    assert not check.passed
    assert "command failed: permission denied" in check.message


@pytest.mark.parametrize("stdout", ["not-json", "[]"])
def test_cluster_autoscaler_provider_managed_mode_rejects_invalid_json(stdout: str) -> None:
    """Verify only a provider-neutral JSON object can establish managed evidence."""
    check = K8sClusterAutoscalerCheck(
        config={"mode": "provider_managed", "provider_managed_command": "probe-managed-autoscaler"}
    )
    with patch.object(check, "run_command", return_value=_ok(stdout)):
        check.run()

    assert not check.passed
    assert "Invalid provider-managed autoscaler evidence" in check.message


@pytest.mark.parametrize(
    ("stdout", "error"),
    [
        (_managed_evidence(enabled=False), "enabled must be the JSON boolean true"),
        (_managed_evidence(node_pool=""), "node_pool must be a non-empty string"),
        (_managed_evidence(min_nodes=-1), "min_nodes must be a non-negative JSON integer"),
        (_managed_evidence(max_nodes=0), "max_nodes must be at least 1"),
        (_managed_evidence(min_nodes=5, max_nodes=4), "min_nodes (5) cannot exceed max_nodes (4)"),
    ],
)
def test_cluster_autoscaler_provider_managed_mode_rejects_invalid_evidence(stdout: str, error: str) -> None:
    """Verify managed proof fails closed on disabled or incoherent evidence."""
    check = K8sClusterAutoscalerCheck(
        config={"mode": "provider_managed", "provider_managed_command": "probe-managed-autoscaler"}
    )
    with patch.object(check, "run_command", return_value=_ok(stdout)):
        check.run()

    assert not check.passed
    assert error in check.message


def test_cluster_autoscaler_rejects_unknown_mode() -> None:
    """Verify a typo cannot silently select the Deployment path."""
    check = K8sClusterAutoscalerCheck(config={"mode": "provider-managed"})
    with patch.object(check, "run_command") as mock_run:
        check.run()

    assert not check.passed
    assert "Invalid mode" in check.message
    mock_run.assert_not_called()
