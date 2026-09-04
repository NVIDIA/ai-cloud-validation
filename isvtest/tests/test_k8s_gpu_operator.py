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

"""Tests for the GPU Operator pod-status and tenant-override validations."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_gpu_operator import (
    K8sGpuOperatorOverrideCheck,
    K8sGpuOperatorPodsCheck,
)


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failed ``CommandResult``."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def test_gpu_operator_pods_use_json_phase() -> None:
    """Verify GPU Operator pod status is parsed from JSON."""
    check = K8sGpuOperatorPodsCheck(config={"namespace": "gpu-operator"})
    payload = json.dumps({"items": [{"metadata": {"name": "gpu-operator-1"}, "status": {"phase": "Running"}}]})

    with (
        patch("isvtest.validations.k8s_gpu_operator.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok(payload)) as mock_run,
    ):
        check.run()

    assert check.passed
    assert mock_run.call_args[0][0] == "kubectl get pods -n gpu-operator -o json"


def test_gpu_operator_pods_reject_crashlooping_running_phase() -> None:
    """Verify kubectl STATUS semantics are preserved for crashlooping pods."""
    check = K8sGpuOperatorPodsCheck(config={"namespace": "gpu-operator"})
    payload = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "gpu-operator-1"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}],
                    },
                }
            ]
        }
    )

    with (
        patch("isvtest.validations.k8s_gpu_operator.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=_ok(payload)),
    ):
        check.run()

    assert not check.passed
    assert "No GPU Operator pods are running" in check.message


CLUSTER_POLICY = {
    "metadata": {"name": "cluster-policy"},
    "spec": {"driver": {"version": "550.54.15"}},
}
NVIDIA_DRIVER = {"metadata": {"name": "gpu-driver"}, "spec": {"version": "550.54.15"}}

NO_SUCH_RESOURCE = 'error: the server doesn\'t have a resource type "nvidiadrivers"'

# Eight authorization answers: patch/update/delete/create on the driver
# configuration, then patch/update on Deployments and DaemonSets.
ALL_ALLOWED = [_ok("yes")] * 8


def _override_check(**config: str) -> K8sGpuOperatorOverrideCheck:
    """Build the override check with the suite's default wiring."""
    return K8sGpuOperatorOverrideCheck(config={"namespace": "gpu-operator", "driver_version": "580.82.07", **config})


def _run(check: K8sGpuOperatorOverrideCheck, responses: list[CommandResult]) -> list[str]:
    """Run the check against canned kubectl responses and return the commands it issued."""
    queue: Iterator[CommandResult] = iter(responses)

    with (
        patch("isvtest.validations.k8s_gpu_operator.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", side_effect=lambda *a, **k: next(queue)) as mock_run,
    ):
        check.run()

    return [call[0][0] for call in mock_run.call_args_list]


def test_override_passes_when_admission_keeps_the_tenant_driver_version() -> None:
    """A writable ClusterPolicy whose dry-run keeps the requested version proves the override."""
    check = _override_check()
    admitted = {"metadata": {"name": "cluster-policy"}, "spec": {"driver": {"version": "580.82.07"}}}

    commands = _run(
        check,
        [
            _ok(json.dumps({"items": [CLUSTER_POLICY]})),
            *ALL_ALLOWED,
            _ok(json.dumps(admitted)),
        ],
    )

    assert check.passed, check.message
    assert "accepts '580.82.07'" in check.message
    assert "currently '550.54.15'" in check.message
    assert commands[0] == "kubectl get clusterpolicies.nvidia.com -o json"
    assert commands[1] == "kubectl auth can-i patch clusterpolicies.nvidia.com"
    assert commands[5] == "kubectl auth can-i patch deployments.apps -n gpu-operator"
    assert commands[7] == "kubectl auth can-i patch daemonsets.apps -n gpu-operator"
    # The override must never be persisted: admission runs it, the cluster keeps
    # the provider default.
    assert commands[-1] == (
        "kubectl patch clusterpolicies.nvidia.com cluster-policy --type=merge "
        '--patch \'{"spec": {"driver": {"version": "580.82.07"}}}\' --dry-run=server -o json'
    )


def test_override_falls_back_to_nvidiadriver_when_clusterpolicy_is_absent() -> None:
    """Newer installs express the driver version on NVIDIADriver instead."""
    check = _override_check()
    admitted = {"metadata": {"name": "gpu-driver"}, "spec": {"version": "580.82.07"}}

    commands = _run(
        check,
        [
            _ok(json.dumps({"items": []})),
            _ok(json.dumps({"items": [NVIDIA_DRIVER]})),
            *ALL_ALLOWED,
            _ok(json.dumps(admitted)),
        ],
    )

    assert check.passed, check.message
    assert commands[1] == "kubectl get nvidiadrivers.nvidia.com -o json"
    assert commands[-1] == (
        "kubectl patch nvidiadrivers.nvidia.com gpu-driver --type=merge "
        '--patch \'{"spec": {"version": "580.82.07"}}\' --dry-run=server -o json'
    )


def test_override_fails_when_no_driver_configuration_exists() -> None:
    """With no operator-managed driver version there is no override to prove."""
    check = _override_check()

    _run(check, [_ok(json.dumps({"items": []})), _fail(stderr=NO_SUCH_RESOURCE)])

    assert not check.passed
    assert "No GPU Operator driver configuration found" in check.message


def test_override_fails_when_the_driver_configuration_query_errors() -> None:
    """An unreachable API is reported as a query failure, not as a missing driver config."""
    check = _override_check()

    _run(
        check,
        [
            _fail(stderr="The connection to the server 10.0.0.1:6443 was refused"),
            _fail(stderr=NO_SUCH_RESOURCE),
        ],
    )

    assert not check.passed
    assert "Unable to query the GPU Operator driver configuration" in check.message
    assert "was refused" in check.message


def test_override_fails_when_a_required_verb_is_denied() -> None:
    """A provider that locks the driver configuration down via RBAC fails."""
    check = _override_check()
    answers = list(ALL_ALLOWED)
    answers[2] = _fail("no", exit_code=1)

    _run(check, [_ok(json.dumps({"items": [CLUSTER_POLICY]})), *answers])

    assert not check.passed
    assert "cannot delete clusterpolicies.nvidia.com" in check.message


def test_override_fails_when_the_operator_workloads_are_read_only() -> None:
    """Replacing the operator means rewriting its workloads in its own namespace."""
    check = _override_check()
    answers = list(ALL_ALLOWED)
    answers[6] = _fail("no", exit_code=1)

    _run(check, [_ok(json.dumps({"items": [CLUSTER_POLICY]})), *answers])

    assert not check.passed
    assert "cannot patch daemonsets.apps in gpu-operator" in check.message


def test_override_fails_when_an_authorization_probe_is_inconclusive() -> None:
    """A probe that answers neither yes nor no is an error, not a silent pass."""
    check = _override_check()
    answers = list(ALL_ALLOWED)
    answers[0] = _fail(stderr="error: unknown flag: --subresource")

    _run(check, [_ok(json.dumps({"items": [CLUSTER_POLICY]})), *answers])

    assert not check.passed
    assert "was inconclusive" in check.message


def test_override_fails_when_admission_rejects_the_version() -> None:
    """A validating webhook that refuses tenant driver versions fails the check."""
    check = _override_check()

    _run(
        check,
        [
            _ok(json.dumps({"items": [CLUSTER_POLICY]})),
            *ALL_ALLOWED,
            _fail(stderr='admission webhook "gpu-policy.provider.example" denied the request'),
        ],
    )

    assert not check.passed
    assert "Admission rejected driver version '580.82.07'" in check.message
    assert "denied the request" in check.message


def test_override_fails_when_admission_pins_the_provider_default_version() -> None:
    """A mutating webhook may accept the write and quietly restore its own version."""
    check = _override_check()

    _run(
        check,
        [
            _ok(json.dumps({"items": [CLUSTER_POLICY]})),
            *ALL_ALLOWED,
            _ok(json.dumps(CLUSTER_POLICY)),
        ],
    )

    assert not check.passed
    assert "Admission kept the provider-default driver version" in check.message
    assert "admitted object reports '550.54.15'" in check.message


def test_override_skips_without_a_tenant_required_version() -> None:
    """With no target version configured there is nothing to override to."""
    check = _override_check(driver_version="")

    with pytest.raises(pytest.skip.Exception, match="driver_version is not configured"):
        check.run()
