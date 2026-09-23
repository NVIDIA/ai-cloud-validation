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

"""Unit tests for ``isvtest.validations.k8s_dra``."""

from __future__ import annotations

import json
import shlex
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from isvtest.core import k8s as core_k8s
from isvtest.core.runners import CommandResult
from isvtest.validations import k8s_dra
from isvtest.validations.k8s_dra import K8sDraCheck

POD_UID = "pod-uid-1"
CLAIM_NAME = "isvtest-dra-dra-x7k2p"
GPU_LINE = "GPU 0: NVIDIA H100 80GB HBM3 (UUID: GPU-0000)"
CHANNEL_CLASSES = ["compute-domain-daemon.nvidia.com", "compute-domain-default-channel.nvidia.com"]


def _result(stdout: str = "", stderr: str = "", exit_code: int = 0) -> CommandResult:
    """Return a ``CommandResult`` with the given output and exit code."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _listing(names: list[str]) -> str:
    """Return a ``kubectl get -o json`` listing of objects with the given names."""
    return json.dumps({"items": [{"metadata": {"name": name}} for name in names]})


def _pod(
    phase: str,
    *,
    unschedulable: str | None = None,
    waiting: str | None = None,
    claim: str | None = CLAIM_NAME,
) -> dict[str, Any]:
    """Return the DRA pod as ``kubectl get pod -o json`` reports it in ``phase``."""
    status: dict[str, Any] = {"phase": phase}
    if unschedulable:
        status["conditions"] = [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": unschedulable}
        ]
    if waiting:
        status["containerStatuses"] = [{"state": {"waiting": {"reason": waiting}}}]
    if claim:
        status["resourceClaimStatuses"] = [{"name": "dra", "resourceClaimName": claim}]
    return {"metadata": {"name": "isvtest-dra", "uid": POD_UID}, "spec": {"nodeName": "gpu-node-1"}, "status": status}


def _claim(*, allocated: bool = True, reserved_uid: str | None = POD_UID) -> dict[str, Any]:
    """Return the pod's generated ResourceClaim, optionally allocated and reserved."""
    status: dict[str, Any] = {}
    if allocated:
        status["allocation"] = {"devices": {"results": [{"request": "device", "pool": "p", "device": "d-0"}]}}
    if reserved_uid:
        status["reservedFor"] = [{"resource": "pods", "name": "isvtest-dra", "uid": reserved_uid}]
    return {"metadata": {"name": CLAIM_NAME}, "status": status}


class _Clock:
    """Monotonic clock that only advances when the check sleeps."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time instead of blocking."""
        self.now += seconds


class _FakeCluster:
    """Answer the kubectl commands the check issues; defaults describe a passing GPU cluster."""

    def __init__(self) -> None:
        """Describe a GA cluster whose GPU claim pod starts on the second poll."""
        self.api_versions = ["v1", "apps/v1", "resource.k8s.io/v1"]
        self.device_classes = ["gpu.nvidia.com"]
        self.slice_names = ["gpu-node-1-gpu.nvidia.com-abcde"]
        self.pods: list[dict[str, Any]] = [_pod("Pending"), _pod("Running")]
        self.logs = GPU_LINE + "\n"
        self.claim = _claim()
        self.overrides: dict[str, CommandResult] = {}
        self.commands: list[str] = []
        self.applied: list[dict[str, Any]] = []

    def run(self, cmd: str, timeout: int | None = None) -> CommandResult:
        """Record ``cmd`` and answer it from ``overrides`` or the cluster state."""
        self.commands.append(cmd)
        for needle, result in self.overrides.items():
            if needle in cmd:
                return result
        if "api-versions" in cmd:
            return _result("\n".join(self.api_versions) + "\n")
        if "get deviceclasses" in cmd:
            return _result(_listing(self.device_classes))
        if "get resourceslices" in cmd:
            return _result(_listing(self.slice_names))
        if "apply -f -" in cmd:
            self.applied = [doc for doc in yaml.safe_load_all(shlex.split(cmd)[2]) if doc]
            return _result("created")
        if "get pod" in cmd:
            pod = self.pods.pop(0) if len(self.pods) > 1 else self.pods[0]
            return _result(json.dumps(pod))
        if " logs " in cmd:
            return _result(self.logs)
        if f"get resourceclaims.resource.k8s.io {CLAIM_NAME}" in cmd:
            return _result(json.dumps(self.claim))
        return _result()

    def ran(self, needle: str) -> bool:
        """Return whether any issued command contains ``needle``."""
        return any(needle in cmd for cmd in self.commands)

    def applied_kind(self, kind: str) -> dict[str, Any]:
        """Return the applied document of the given ``kind``."""
        return next(doc for doc in self.applied if doc["kind"] == kind)

    def deletions(self) -> list[str]:
        """Return the resource types deleted, in the order they were deleted."""
        return [cmd.split()[2] for cmd in self.commands if cmd.startswith("kubectl delete")]


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Keep kubectl discovery and sleeping off the host."""
    fake = _Clock()
    monkeypatch.setattr(core_k8s, "get_kubectl_command", lambda: ["kubectl"])
    monkeypatch.setattr(k8s_dra, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep))
    return fake


@pytest.fixture
def cluster() -> _FakeCluster:
    """A cluster running the DRA driver's GPU half."""
    return _FakeCluster()


@pytest.fixture
def channel_cluster() -> _FakeCluster:
    """A cluster running only the DRA driver's ComputeDomain half."""
    fake = _FakeCluster()
    fake.device_classes = list(CHANNEL_CLASSES)
    fake.slice_names = ["gpu-node-1-compute-domain.nvidia.com-abcde"]
    fake.logs = "channel0\n"
    return fake


def _run(cluster: _FakeCluster, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the check against ``cluster`` and return its result."""
    return K8sDraCheck(runner=cluster, config=config or {}).execute()


def _requests(cluster: _FakeCluster) -> list[dict[str, Any]]:
    """Return the device requests of the applied ResourceClaimTemplate."""
    return cluster.applied_kind("ResourceClaimTemplate")["spec"]["spec"]["devices"]["requests"]


class TestDraApi:
    """DRA counts as enabled at any beta or GA version, and at no other."""

    def test_passes_with_ga_api_driver_and_claim_backed_gpu(self, cluster: _FakeCluster) -> None:
        result = _run(cluster)
        assert result["passed"] is True, result["error"]
        assert "resource.k8s.io/v1 is served" in result["output"]
        assert "1 gpu.nvidia.com device(s)" in result["output"]
        assert GPU_LINE in result["output"]
        assert cluster.applied_kind("ResourceClaimTemplate")["apiVersion"] == "resource.k8s.io/v1"
        assert _requests(cluster) == [{"name": "device", "exactly": {"deviceClassName": "gpu.nvidia.com"}}]
        pod = cluster.applied_kind("Pod")
        assert pod["spec"]["resourceClaims"][0]["resourceClaimTemplateName"] == "isvtest-dra"
        assert "nvidia-smi -L" in pod["spec"]["containers"][0]["command"][-1]

    def test_v1beta2_uses_the_exactly_request_shape(self, cluster: _FakeCluster) -> None:
        cluster.api_versions = ["v1", "resource.k8s.io/v1beta2", "resource.k8s.io/v1beta1"]
        result = _run(cluster)
        assert result["passed"] is True, result["error"]
        assert cluster.applied_kind("ResourceClaimTemplate")["apiVersion"] == "resource.k8s.io/v1beta2"
        assert _requests(cluster)[0]["exactly"] == {"deviceClassName": "gpu.nvidia.com"}

    def test_v1beta1_is_accepted_with_the_flat_request_shape(self, cluster: _FakeCluster) -> None:
        cluster.api_versions = ["v1", "resource.k8s.io/v1beta1"]
        result = _run(cluster)
        assert result["passed"] is True, result["error"]
        assert cluster.applied_kind("ResourceClaimTemplate")["apiVersion"] == "resource.k8s.io/v1beta1"
        assert _requests(cluster) == [{"name": "device", "deviceClassName": "gpu.nvidia.com"}]

    def test_unserved_api_fails_and_names_what_is_served(self, cluster: _FakeCluster) -> None:
        cluster.api_versions = ["v1", "resource.k8s.io/v1alpha3"]
        result = _run(cluster)
        assert result["passed"] is False
        assert "DRA is not enabled" in result["error"]
        assert "served: resource.k8s.io/v1alpha3" in result["error"]
        assert not cluster.ran("create namespace")

    def test_api_versions_failure_fails(self, cluster: _FakeCluster) -> None:
        cluster.overrides["api-versions"] = _result(stderr="connection refused", exit_code=1)
        result = _run(cluster)
        assert result["passed"] is False
        assert "connection refused" in result["error"]


class TestDraDriver:
    """An empty cluster is a failure, never a vacuous pass."""

    @pytest.mark.parametrize(("classes", "slices"), [([], ["s"]), (["gpu.nvidia.com"], []), ([], [])])
    def test_no_driver_fails(self, cluster: _FakeCluster, classes: list[str], slices: list[str]) -> None:
        cluster.device_classes = classes
        cluster.slice_names = slices
        result = _run(cluster)
        assert result["passed"] is False
        assert "no DRA driver is installed" in result["error"]
        assert not cluster.ran("create namespace")

    def test_neither_gpu_nor_channel_class_fails_and_lists_registered(self, cluster: _FakeCluster) -> None:
        cluster.device_classes = ["fpga.example.com"]
        result = _run(cluster)
        assert result["passed"] is False
        assert "Neither DeviceClass 'gpu.nvidia.com'" in result["error"]
        assert "fpga.example.com" in result["error"]
        assert not cluster.ran("create namespace")

    def test_configured_device_class_is_requested(self, cluster: _FakeCluster) -> None:
        cluster.device_classes = ["gpu.example.com"]
        result = _run(cluster, {"device_class": "gpu.example.com", "image": "mirror.local/ubuntu:22.04"})
        assert result["passed"] is True, result["error"]
        assert _requests(cluster)[0]["exactly"]["deviceClassName"] == "gpu.example.com"
        assert cluster.applied_kind("Pod")["spec"]["containers"][0]["image"] == "mirror.local/ubuntu:22.04"

    def test_gpu_class_is_preferred_when_both_halves_run(self, cluster: _FakeCluster) -> None:
        cluster.device_classes = ["gpu.nvidia.com", *CHANNEL_CLASSES]
        result = _run(cluster)
        assert result["passed"] is True, result["error"]
        assert {doc["kind"] for doc in cluster.applied} == {"ResourceClaimTemplate", "Pod"}


class TestDraComputeDomain:
    """A ComputeDomain-only driver proves DRA through an IMEX channel claim."""

    def test_passes_through_a_channel_claim(self, channel_cluster: _FakeCluster) -> None:
        result = _run(channel_cluster)
        assert result["passed"] is True, result["error"]
        assert "compute-domain-default-channel.nvidia.com device(s)" in result["output"]
        assert "IMEX channel(s): channel0" in result["output"]

    def test_creates_a_compute_domain_and_points_the_pod_at_its_channel_template(
        self, channel_cluster: _FakeCluster
    ) -> None:
        _run(channel_cluster)
        assert {doc["kind"] for doc in channel_cluster.applied} == {"ComputeDomain", "Pod"}
        domain = channel_cluster.applied_kind("ComputeDomain")
        assert domain["spec"]["channel"]["resourceClaimTemplate"]["name"] == "isvtest-dra-channel"
        pod = channel_cluster.applied_kind("Pod")
        assert pod["metadata"]["namespace"] == domain["metadata"]["namespace"]
        assert pod["spec"]["resourceClaims"][0]["resourceClaimTemplateName"] == "isvtest-dra-channel"
        assert "/dev/nvidia-caps-imex-channels" in pod["spec"]["containers"][0]["command"][-1]

    def test_pod_goes_before_the_domain_and_the_namespace_last(self, channel_cluster: _FakeCluster) -> None:
        _run(channel_cluster)
        assert channel_cluster.deletions() == ["pod", "computedomains.resource.nvidia.com", "namespace"]

    def test_missing_channel_device_fails_with_logs(self, channel_cluster: _FakeCluster) -> None:
        channel_cluster.pods = [_pod("Failed")]
        channel_cluster.logs = "ls: cannot access '/dev/nvidia-caps-imex-channels': No such file or directory\n"
        result = _run(channel_cluster)
        assert result["passed"] is False
        assert "instead of showing the IMEX channel" in result["error"]
        assert "No such file or directory" in result["output"]


class TestDraWorkload:
    """The device has to reach the pod through the claim."""

    def test_unschedulable_pod_times_out_with_scheduler_message(self, cluster: _FakeCluster) -> None:
        cluster.pods = [_pod("Pending", unschedulable="0/3 nodes are available: 3 cannot allocate all claims.")]
        result = _run(cluster, {"wait_timeout": 30})
        assert result["passed"] is False
        assert "within 30s" in result["error"]
        assert "cannot allocate all claims" in result["error"]
        assert cluster.ran("delete namespace")

    def test_image_pull_backoff_fails_fast(self, cluster: _FakeCluster, clock: _Clock) -> None:
        cluster.pods = [_pod("Pending", waiting="ImagePullBackOff")]
        result = _run(cluster)
        assert result["passed"] is False
        assert "cannot start: ImagePullBackOff" in result["error"]
        assert clock.now == 0

    def test_pod_that_exits_fails_with_its_logs(self, cluster: _FakeCluster) -> None:
        cluster.pods = [_pod("Failed")]
        cluster.logs = "sh: 1: nvidia-smi: not found\n"
        result = _run(cluster)
        assert result["passed"] is False
        assert "exited (Failed)" in result["error"]
        assert "nvidia-smi: not found" in result["output"]

    def test_running_pod_without_gpu_lines_times_out(self, cluster: _FakeCluster) -> None:
        cluster.pods = [_pod("Running")]
        cluster.logs = ""
        result = _run(cluster, {"wait_timeout": 10})
        assert result["passed"] is False
        assert "no GPU shown yet" in result["error"]

    def test_pod_without_a_claim_fails(self, cluster: _FakeCluster) -> None:
        cluster.pods = [_pod("Running", claim=None)]
        result = _run(cluster)
        assert result["passed"] is False
        assert "carries no ResourceClaim" in result["error"]

    @pytest.mark.parametrize("claim", [_claim(allocated=False), _claim(reserved_uid="someone-else")])
    def test_device_not_from_the_claim_fails(self, cluster: _FakeCluster, claim: dict[str, Any]) -> None:
        cluster.claim = claim
        result = _run(cluster)
        assert result["passed"] is False
        assert "did not come through DRA" in result["error"]

    def test_invalid_wait_timeout_fails_before_touching_cluster(self, cluster: _FakeCluster) -> None:
        result = _run(cluster, {"wait_timeout": 0})
        assert result["passed"] is False
        assert cluster.commands == []


class TestDraCleanup:
    """What the check created is always removed."""

    def test_gpu_path_deletes_pod_then_namespace(self, cluster: _FakeCluster) -> None:
        result = _run(cluster)
        assert result["passed"] is True
        assert cluster.deletions() == ["pod", "namespace"]
        created = next(cmd for cmd in cluster.commands if "create namespace" in cmd).split()[-1]
        assert cluster.ran(f"delete namespace {created}")

    def test_failed_cleanup_fails_a_passing_check(self, cluster: _FakeCluster) -> None:
        cluster.overrides["delete namespace"] = _result(stderr="forbidden", exit_code=1)
        result = _run(cluster)
        assert result["passed"] is False
        assert "Cleanup left objects behind" in result["error"]
        assert "forbidden" in result["error"]

    def test_failed_cleanup_keeps_an_earlier_failure(self, cluster: _FakeCluster) -> None:
        cluster.claim = _claim(allocated=False)
        cluster.overrides["delete pod"] = _result(stderr="timed out", exit_code=1)
        result = _run(cluster)
        assert result["passed"] is False
        assert "did not come through DRA" in result["error"]
