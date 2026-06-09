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

"""Unit tests for multi-cluster Kubernetes step-output validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from isvtest.validations.k8s_multi_cluster import K8sMultiClusterSameVpcCheck


def _valid_output() -> dict[str, Any]:
    """Return a valid K8S26-01 multi-cluster step output."""
    return {
        "success": True,
        "platform": "kubernetes",
        "test_id": "K8S26-01",
        "tenancy_id": "123456789012",
        "network_id": "vpc-123",
        "clusters": [
            {
                "name": "isvtest-eks-dev",
                "role": "primary",
                "tenancy_id": "123456789012",
                "network_id": "vpc-123",
                "status": "ACTIVE",
            },
            {
                "name": "isvtest-eks-dev-shared-vpc",
                "role": "secondary",
                "tenancy_id": "123456789012",
                "network_id": "vpc-123",
                "status": "ACTIVE",
                "ready_node_count": 1,
            },
        ],
    }


def _run_check(step_output: dict[str, Any]) -> K8sMultiClusterSameVpcCheck:
    """Run the validation against ``step_output`` and return the check object."""
    check = K8sMultiClusterSameVpcCheck(config={"step_output": step_output})
    check.run()
    return check


def test_passes_for_two_active_clusters_in_same_tenancy_and_vpc() -> None:
    """Two distinct active clusters in one tenancy/VPC pass."""
    check = _run_check(_valid_output())

    assert check.passed, check.message
    assert "2 cluster(s)" in check.message
    assert "vpc-123" in check.message


def test_fails_when_tenancy_id_is_missing() -> None:
    """The step output must prove tenancy/account identity."""
    output = _valid_output()
    output.pop("tenancy_id")

    check = _run_check(output)

    assert not check.passed
    assert "tenancy_id" in check.message


def test_fails_when_network_id_is_missing() -> None:
    """The step output must prove the shared VPC/network ID."""
    output = _valid_output()
    output.pop("network_id")

    check = _run_check(output)

    assert not check.passed
    assert "network_id" in check.message


def test_fails_with_fewer_than_two_clusters() -> None:
    """K8S26-01 requires at least two clusters."""
    output = _valid_output()
    output["clusters"] = output["clusters"][:1]

    check = _run_check(output)

    assert not check.passed
    assert "at least 2 clusters" in check.message


def test_fails_with_duplicate_cluster_names() -> None:
    """Cluster names must be distinct."""
    output = _valid_output()
    output["clusters"][1]["name"] = output["clusters"][0]["name"]

    check = _run_check(output)

    assert not check.passed
    assert "Duplicate cluster name" in check.message


def test_fails_when_any_cluster_is_not_active() -> None:
    """Every reported cluster must be ACTIVE."""
    output = _valid_output()
    output["clusters"][1]["status"] = "CREATING"

    check = _run_check(output)

    assert not check.passed
    assert "not ACTIVE" in check.message


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("tenancy_id", "999999999999", "Mixed tenancy IDs"),
        ("network_id", "vpc-456", "Mixed network IDs"),
    ],
)
def test_fails_when_cluster_identity_fields_do_not_match_top_level(
    field: str,
    value: str,
    expected: str,
) -> None:
    """Cluster-level tenancy/VPC fields must agree with the top-level proof."""
    output = _valid_output()
    output["clusters"][1][field] = value

    check = _run_check(output)

    assert not check.passed
    assert expected in check.message


def test_fails_when_secondary_cluster_has_no_ready_nodes() -> None:
    """The secondary cluster must have at least one Ready node."""
    output = _valid_output()
    output["clusters"][1]["ready_node_count"] = 0

    check = _run_check(output)

    assert not check.passed
    assert "Cluster" in check.message
    assert "Ready node" in check.message


@pytest.mark.parametrize("ready_node_count", [1.5, "1.5"])
def test_fails_when_secondary_cluster_has_fractional_ready_nodes(ready_node_count: float | str) -> None:
    """The secondary cluster ready-node count must be integral."""
    output = _valid_output()
    output["clusters"][1]["ready_node_count"] = ready_node_count

    check = _run_check(output)

    assert not check.passed
    assert "ready_node_count" in check.message


def test_passes_when_no_secondary_cluster_is_reported() -> None:
    """K8S26 proves two clusters share a VPC regardless of role labels."""
    output = deepcopy(_valid_output())
    output["clusters"][1]["role"] = "primary"

    check = _run_check(output)

    assert check.passed, check.message
