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

"""Tests for inventory module."""

import pytest

from isvtest.config.inventory import (
    ClusterInventory,
    KubernetesInventory,
    SlurmInventory,
    SlurmPartitionInventory,
    inventory_to_dict,
    parse_inventory,
)


class TestSlurmPartitionInventory:
    """Tests for SlurmPartitionInventory dataclass."""

    def test_node_count_computed_from_nodes(self) -> None:
        """Test that node_count is computed from nodes list."""
        partition = SlurmPartitionInventory(nodes=["node1", "node2", "node3"])
        assert partition.node_count == 3

    def test_explicit_node_count_preserved(self) -> None:
        """Test that explicit node_count is preserved."""
        partition = SlurmPartitionInventory(nodes=["node1", "node2"], node_count=10)
        assert partition.node_count == 10

    def test_empty_nodes_no_node_count(self) -> None:
        """Test that empty nodes list doesn't set node_count."""
        partition = SlurmPartitionInventory()
        assert partition.node_count is None


class TestKubernetesInventory:
    """Tests for KubernetesInventory dataclass."""

    def test_node_count_computed_from_nodes(self) -> None:
        """Test that node_count is computed from nodes list."""
        inventory = KubernetesInventory(nodes=["node1", "node2"])
        assert inventory.node_count == 2

    def test_total_gpus_computed(self) -> None:
        """Test that total_gpus is computed from gpu_node_count * gpu_per_node."""
        inventory = KubernetesInventory(gpu_node_count=4, gpu_per_node=8)
        assert inventory.total_gpus == 32

    def test_total_gpus_not_computed_if_explicit(self) -> None:
        """Test that explicit total_gpus is preserved."""
        inventory = KubernetesInventory(
            gpu_node_count=4,
            gpu_per_node=8,
            total_gpus=24,  # Explicit value
        )
        assert inventory.total_gpus == 24

    def test_defaults(self) -> None:
        """Test default values."""
        inventory = KubernetesInventory()
        assert inventory.gpu_operator_namespace == "nvidia-gpu-operator"
        assert inventory.runtime_class == "nvidia"
        assert inventory.gpu_resource_name == "nvidia.com/gpu"


class TestParseInventory:
    """Tests for parse_inventory function."""

    def test_parse_minimal_inventory(self) -> None:
        """Test parsing minimal inventory with just platform."""
        data = {"platform": "kubernetes"}
        result = parse_inventory(data)
        assert result.platform == "kubernetes"
        assert result.cluster_name == ""

    def test_parse_kubernetes_inventory(self) -> None:
        """Test parsing Kubernetes inventory."""
        data = {
            "platform": "kubernetes",
            "cluster_name": "my-cluster",
            "kubernetes": {
                "driver_version": "580.82.07",
                "node_count": 4,
                "gpu_per_node": 8,
            },
        }
        result = parse_inventory(data)
        assert result.platform == "kubernetes"
        assert result.cluster_name == "my-cluster"
        assert result.kubernetes is not None
        assert result.kubernetes.driver_version == "580.82.07"
        assert result.kubernetes.node_count == 4
        assert result.kubernetes.gpu_per_node == 8

    def test_parse_slurm_inventory(self) -> None:
        """Test parsing Slurm inventory."""
        data = {
            "platform": "slurm",
            "slurm": {
                "cuda_arch": "90",
                "storage_path": "/scratch",
                "default_partition": "gpu",
                "partitions": {
                    "gpu": {
                        "nodes": ["gpu1", "gpu2"],
                        "node_count": 2,
                    },
                    "cpu": ["cpu1", "cpu2", "cpu3"],  # Shorthand format
                },
            },
        }
        result = parse_inventory(data)
        assert result.platform == "slurm"
        assert result.slurm is not None
        assert result.slurm.cuda_arch == "90"
        assert result.slurm.storage_path == "/scratch"
        assert result.slurm.default_partition == "gpu"
        assert "gpu" in result.slurm.partitions
        assert result.slurm.partitions["gpu"].nodes == ["gpu1", "gpu2"]
        assert "cpu" in result.slurm.partitions
        assert result.slurm.partitions["cpu"].nodes == ["cpu1", "cpu2", "cpu3"]

    def test_parse_inventory_missing_platform_raises(self) -> None:
        """Test that missing platform raises ValueError."""
        with pytest.raises(ValueError, match="platform"):
            parse_inventory({})

    def test_parse_inventory_empty_platform_raises(self) -> None:
        """Test that empty platform raises ValueError."""
        with pytest.raises(ValueError, match="platform"):
            parse_inventory({"platform": ""})


class TestInventoryToDict:
    """Tests for inventory_to_dict function."""

    def test_minimal_inventory(self) -> None:
        """Test converting minimal inventory."""
        inventory = ClusterInventory(platform="kubernetes")
        result = inventory_to_dict(inventory)
        assert result["platform"] == "kubernetes"
        assert result["cluster_name"] == ""

    def test_kubernetes_inventory(self) -> None:
        """Test converting Kubernetes inventory."""
        k8s = KubernetesInventory(
            driver_version="580.82.07",
            node_count=4,
            nodes=["node1", "node2"],
            gpu_per_node=8,
        )
        inventory = ClusterInventory(
            platform="kubernetes",
            cluster_name="test-cluster",
            kubernetes=k8s,
        )
        result = inventory_to_dict(inventory)

        assert result["platform"] == "kubernetes"
        assert result["cluster_name"] == "test-cluster"
        assert "kubernetes" in result
        assert result["kubernetes"]["driver_version"] == "580.82.07"
        assert result["kubernetes"]["node_count"] == 4
        assert result["kubernetes"]["nodes"] == ["node1", "node2"]

    def test_slurm_inventory(self) -> None:
        """Test converting Slurm inventory."""
        partition = SlurmPartitionInventory(nodes=["node1", "node2"], node_count=2)
        slurm = SlurmInventory(
            partitions={"gpu": partition},
            cuda_arch="90",
            storage_path="/scratch",
            default_partition="gpu",
        )
        inventory = ClusterInventory(
            platform="slurm",
            slurm=slurm,
        )
        result = inventory_to_dict(inventory)

        assert result["platform"] == "slurm"
        assert "slurm" in result
        assert result["slurm"]["cuda_arch"] == "90"
        assert result["slurm"]["storage_path"] == "/scratch"
        assert result["slurm"]["default_partition"] == "gpu"
        assert "gpu" in result["slurm"]["partitions"]

    def test_roundtrip(self) -> None:
        """Test that parse -> to_dict -> parse produces same result."""
        original_data = {
            "platform": "kubernetes",
            "cluster_name": "roundtrip-test",
            "kubernetes": {
                "driver_version": "580.82.07",
                "node_count": 2,
                "nodes": ["node1", "node2"],
                "gpu_per_node": 4,
                "gpu_node_count": 2,
            },
        }
        inventory = parse_inventory(original_data)
        dict_result = inventory_to_dict(inventory)
        inventory2 = parse_inventory(dict_result)

        assert inventory.platform == inventory2.platform
        assert inventory.cluster_name == inventory2.cluster_name
        assert inventory.kubernetes.driver_version == inventory2.kubernetes.driver_version
