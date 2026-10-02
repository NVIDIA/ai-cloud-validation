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

"""Tests for the compute-domain capability validation (SDN17-02).

The check reads the cluster API directly, so every test here answers its
kubectl probes from a canned cluster rather than from step output.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.network import ImexComputeDomainCapabilityCheck

API_RESOURCES_COMMAND = "kubectl api-resources --api-group=resource.nvidia.com -o name"
DEVICE_CLASSES_COMMAND = "kubectl get deviceclasses.resource.k8s.io -o json"
RESOURCE_SLICES_COMMAND = "kubectl get resourceslices.resource.k8s.io -o json"
NODES_COMMAND = "kubectl get nodes -o json"

DAEMON_CLASS = "compute-domain-daemon.nvidia.com"
CHANNEL_CLASS = "compute-domain-default-channel.nvidia.com"
COMPUTE_DOMAIN_DRIVER = "compute-domain.nvidia.com"


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failed ``CommandResult``."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _node(
    name: str,
    *,
    clique: str | None = "fabric-1.3",
    gpus: int = 8,
    gpu_label: bool = True,
) -> dict[str, Any]:
    """Return one node object as the cluster API reports it."""
    labels: dict[str, str] = {}
    if gpu_label:
        labels["nvidia.com/gpu.present"] = "true"
    if clique is not None:
        labels["nvidia.com/gpu.clique"] = clique
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"allocatable": {"nvidia.com/gpu": str(gpus)} if gpus else {}},
    }


def _resource_slice(node: str, driver: str = COMPUTE_DOMAIN_DRIVER) -> dict[str, Any]:
    """Return one ResourceSlice published by a per-node plugin."""
    return {"metadata": {"name": f"{node}-{driver}"}, "spec": {"driver": driver, "nodeName": node}}


def _items(*items: dict[str, Any]) -> str:
    """Return a kubectl list payload wrapping ``items``."""
    return json.dumps({"items": list(items)})


def _run(
    *,
    nodes: list[dict[str, Any]] | None = None,
    device_classes: list[str] | None = None,
    slices: list[dict[str, Any]] | None = None,
    api_resources: CommandResult | None = None,
    overrides: dict[str, CommandResult] | None = None,
) -> tuple[ImexComputeDomainCapabilityCheck, list[str]]:
    """Run the check against a canned cluster, returning it and the probes it issued."""
    nodes = [_node("gpu-1"), _node("gpu-2")] if nodes is None else nodes
    device_classes = [DAEMON_CLASS, CHANNEL_CLASS] if device_classes is None else device_classes
    slices = [_resource_slice("gpu-1"), _resource_slice("gpu-2")] if slices is None else slices

    responses = {
        API_RESOURCES_COMMAND: api_resources
        if api_resources is not None
        else _ok("computedomains.resource.nvidia.com\n"),
        DEVICE_CLASSES_COMMAND: _ok(_items(*({"metadata": {"name": name}} for name in device_classes))),
        RESOURCE_SLICES_COMMAND: _ok(_items(*slices)),
        NODES_COMMAND: _ok(_items(*nodes)),
    }
    responses.update(overrides or {})

    check = ImexComputeDomainCapabilityCheck(config={})
    with (
        patch("isvtest.validations.network.get_kubectl_base_shell", side_effect=lambda *a: " ".join(("kubectl", *a))),
        patch.object(check, "run_command", side_effect=lambda command, **_: responses[command]) as mock_run,
    ):
        check.run()
    return check, [call[0][0] for call in mock_run.call_args_list]


def test_healthy_cluster_passes() -> None:
    """Registered device classes plus published resources on every node passes."""
    check, _ = _run()

    assert check.passed, check.message
    assert "all 2 clique-labelled GPU node(s)" in check.message
    assert "IMEX daemon ownership: driver" in check.message


def test_probe_reads_only_cluster_objects() -> None:
    """Everything asserted is a cluster object: nothing shells into a node, and
    an IMEX tooling probe would need the command service enabled by default."""
    _, commands = _run()

    assert commands == [
        API_RESOURCES_COMMAND,
        DEVICE_CLASSES_COMMAND,
        RESOURCE_SLICES_COMMAND,
        NODES_COMMAND,
    ]


def test_missing_compute_domain_crd_skips() -> None:
    """A cluster advertising no multi-node NVLink capability is out of scope."""
    with pytest.raises(pytest.skip.Exception, match="no multi-node NVLink capability"):
        _run(api_resources=_ok(""))


def test_unreadable_api_group_fails_instead_of_skipping() -> None:
    """An unserved API group is an empty successful listing, so a failed read is
    a cluster the check could not reach - never one without multi-node NVLink."""
    check, commands = _run(api_resources=_fail(stderr="connection refused"))

    assert not check.passed
    assert "connection refused" in check.message
    assert commands == [API_RESOURCES_COMMAND]


def test_skip_gate_is_a_different_object_from_the_assertion() -> None:
    """Registered device classes are asserted, not used as the in-scope gate, so
    a cluster missing them fails rather than skipping itself out of the run."""
    check, _ = _run(device_classes=[])

    assert not check.passed
    assert "device classes are not registered cluster-wide" in check.message


def test_unpublished_node_fails_and_is_named() -> None:
    """A clique node whose plugin published nothing fails, and is named."""
    check, _ = _run(slices=[_resource_slice("gpu-1")])

    assert not check.passed
    assert "gpu-2: the driver's per-node plugin publishes no compute-domain resources" in check.message
    assert "gpu-1:" not in check.message


def test_other_drivers_slices_do_not_count_as_publication() -> None:
    """The GPU driver's own slices are not compute-domain resources."""
    check, _ = _run(slices=[_resource_slice("gpu-1", "gpu.nvidia.com")])

    assert not check.passed
    assert "failed on 2 count(s)" in check.message


def test_unlabelled_gpu_node_fails_rather_than_leaving_scope() -> None:
    """A GPU node in scope without the clique label FAILS: it must not be able
    to report its way out of being tested."""
    check, _ = _run(nodes=[_node("gpu-1"), _node("gpu-2", clique=None)])

    assert not check.passed
    assert "gpu-2: GPU node in scope carries no NVLink clique label" in check.message


def test_blank_clique_label_is_not_a_clique() -> None:
    """An empty label value carries no clique identity, so the node fails the
    same way one carrying no label at all does."""
    check, _ = _run(nodes=[_node("gpu-1"), _node("gpu-2", clique="  ")])

    assert not check.passed
    assert "gpu-2: GPU node in scope carries no NVLink clique label" in check.message


def test_zero_asserted_nodes_fails() -> None:
    """An 8-node run that asserts against none of them must FAIL rather than
    report 8 examined nodes and pass vacuously."""
    check, _ = _run(nodes=[_node(f"gpu-{n}", clique=None) for n in range(8)])

    assert not check.passed
    assert "8 GPU node(s) reported, none carrying the NVLink clique label" in check.message


def test_no_gpu_nodes_fails() -> None:
    """A cluster with no GPU nodes at all is a failure, not a vacuous pass."""
    check, _ = _run(nodes=[])

    assert not check.passed
    assert "No nodes were asserted against" in check.message


def test_non_gpu_nodes_are_out_of_scope() -> None:
    """Scope is the cluster's GPU accounting, so CPU nodes are not examined."""
    check, _ = _run(nodes=[_node("gpu-1"), _node("cpu-1", clique=None, gpus=0, gpu_label=False)])

    assert check.passed, check.message
    assert "all 1 clique-labelled GPU node(s)" in check.message


def test_dra_only_gpu_node_is_in_scope() -> None:
    """A cluster exposing GPUs only through DRA publishes no extended resource,
    so the feature-discovery label has to count as GPU accounting too."""
    check, _ = _run(nodes=[_node("gpu-1", gpus=0)], slices=[_resource_slice("gpu-1")])

    assert check.passed, check.message
    assert "all 1 clique-labelled GPU node(s)" in check.message


@pytest.mark.parametrize(
    ("device_classes", "expected"),
    [
        ([DAEMON_CLASS, CHANNEL_CLASS], "driver"),
        ([CHANNEL_CLASS], "host"),
    ],
)
def test_daemon_ownership_mode_is_reported_not_asserted(device_classes: list[str], expected: str) -> None:
    """Both ownership modes pass; the mode is surfaced as evidence only."""
    check, _ = _run(device_classes=device_classes)

    assert check.passed, check.message
    assert f"IMEX daemon ownership: {expected}" in check.message


def test_unrelated_device_classes_are_ignored() -> None:
    """Only compute-domain classes count towards the cluster-wide assertion."""
    check, _ = _run(device_classes=["gpu.nvidia.com", "mig.nvidia.com"])

    assert not check.passed
    assert "device classes are not registered cluster-wide" in check.message


def test_device_classes_matched_by_role_not_exact_name() -> None:
    """Class names move with the driver version; the role they play does not."""
    check, _ = _run(device_classes=["compute-domain-v2-channel.nvidia.com"])

    assert check.passed, check.message
    assert "IMEX daemon ownership: host" in check.message


@pytest.mark.parametrize(
    ("command", "response", "message"),
    [
        (DEVICE_CLASSES_COMMAND, _fail(stderr="forbidden"), "forbidden"),
        (NODES_COMMAND, _ok("not json"), "Failed to parse"),
        (RESOURCE_SLICES_COMMAND, _ok(json.dumps({})), "expected 'items' list"),
    ],
)
def test_unreadable_listing_fails_readably(command: str, response: CommandResult, message: str) -> None:
    """A listing the check cannot read fails by name rather than raising."""
    check, _ = _run(overrides={command: response})

    assert not check.passed
    assert message in check.message
