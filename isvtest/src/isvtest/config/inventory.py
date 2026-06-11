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

"""Cluster inventory schema definitions.

This module defines the schema for cluster inventory files that are generated
by external provisioning tools and merged into test configurations at runtime.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SlurmPartitionInventory:
    """Inventory for a single Slurm partition.

    Attributes:
        nodes: List of node names in this partition
        node_count: Number of nodes (computed from nodes if not provided)
    """

    nodes: list[str] = field(default_factory=list)
    node_count: int | None = None

    def __post_init__(self) -> None:
        """Compute node_count from nodes if not explicitly provided."""
        if self.node_count is None and self.nodes:
            self.node_count = len(self.nodes)


@dataclass
class SlurmInventory:
    """Inventory for Slurm clusters.

    Attributes:
        partitions: Dictionary of partition name to partition inventory
        cuda_arch: CUDA compute capability (e.g., "90" for H100, "100" for GB200)
        storage_path: Path for job storage/scratch space
        default_partition: Name of the default partition
    """

    partitions: dict[str, SlurmPartitionInventory] = field(default_factory=dict)
    cuda_arch: str | None = None
    storage_path: str = "/tmp"
    default_partition: str | None = None


@dataclass
class KubernetesInventory:
    """Inventory for Kubernetes clusters.

    Attributes:
        driver_version: NVIDIA driver version (e.g., "580.82.07")
        node_count: Total number of nodes in the cluster
        nodes: List of node names
        gpu_node_count: Number of nodes that have GPUs (for mixed CPU/GPU clusters)
        gpu_per_node: Number of GPUs per GPU node
        total_gpus: Total number of GPUs in the cluster
        gpu_operator_namespace: Namespace where GPU operator is deployed
        runtime_class: Kubernetes RuntimeClass for GPU pods (e.g., "nvidia")
        gpu_resource_name: Resource name for GPU requests (e.g., "nvidia.com/gpu")
    """

    driver_version: str | None = None
    node_count: int | None = None
    nodes: list[str] = field(default_factory=list)
    gpu_node_count: int | None = None
    gpu_per_node: int | None = None
    total_gpus: int | None = None
    gpu_operator_namespace: str = "nvidia-gpu-operator"
    runtime_class: str = "nvidia"
    gpu_resource_name: str = "nvidia.com/gpu"

    def __post_init__(self) -> None:
        """Compute derived values if not explicitly provided."""
        if self.node_count is None and self.nodes:
            self.node_count = len(self.nodes)
        if self.total_gpus is None and self.gpu_node_count and self.gpu_per_node:
            self.total_gpus = self.gpu_node_count * self.gpu_per_node


@dataclass
class VmInventory:
    """Inventory for VM instances.

    Attributes:
        region: ISV region where the instance is running
        account_id: ISV account ID
        instance_id: Instance ID
        instance_type: Instance type (e.g., "g5.xlarge")
        ami_id: AMI ID used to launch the instance
        public_ip: Public IP address of the instance
        private_ip: Private IP address of the instance
        ssh_user: SSH username for connecting to the instance
        ssh_key_path: Path to the SSH private key file
        vpc_id: VPC ID where the instance is running
        subnet_id: Subnet ID where the instance is running
        security_group_id: Security group ID attached to the instance
        igw_id: Internet Gateway ID
        route_table_id: Route table ID
        key_name: ISV key pair name
        availability_zone: Availability zone of the instance
        gpu_count: Number of GPUs on the instance
        gpu_name: GPU model name (e.g., "NVIDIA A10G")
        driver_version: NVIDIA driver version
    """

    region: str = "us-west-2"
    account_id: str | None = None
    instance_id: str | None = None
    instance_type: str | None = None
    ami_id: str | None = None
    public_ip: str | None = None
    private_ip: str | None = None
    ssh_user: str = "ubuntu"
    ssh_key_path: str | None = None
    vpc_id: str | None = None
    subnet_id: str | None = None
    security_group_id: str | None = None
    igw_id: str | None = None
    route_table_id: str | None = None
    key_name: str | None = None
    availability_zone: str | None = None
    gpu_count: int = 0
    gpu_name: str | None = None
    driver_version: str | None = None


@dataclass
class ClusterInventory:
    """Complete cluster inventory.

    This is the top-level schema for inventory files generated by
    cluster provisioning tools.

    Attributes:
        platform: Platform type ("slurm", "kubernetes", "vm", or "network")
        cluster_name: Name of the cluster
        slurm: Slurm-specific inventory (if platform is slurm)
        kubernetes: Kubernetes-specific inventory (if platform is kubernetes)
        vm: VM-specific inventory (if platform is vm)
    """

    platform: str
    cluster_name: str = ""
    slurm: SlurmInventory | None = None
    kubernetes: KubernetesInventory | None = None
    vm: VmInventory | None = None


def parse_inventory(data: dict[str, Any]) -> ClusterInventory:
    """Parse inventory data from a dictionary (loaded from JSON/YAML).

    Args:
        data: Dictionary containing inventory data

    Returns:
        Parsed ClusterInventory object

    Raises:
        ValueError: If required fields are missing or invalid
    """
    platform = data.get("platform")
    if not platform:
        raise ValueError("Inventory must specify 'platform' (slurm, kubernetes, vm, or network)")

    cluster_name = data.get("cluster_name", "")

    slurm_inventory = None
    k8s_inventory = None
    vm_inventory = None

    # Parse Slurm inventory
    if slurm_data := data.get("slurm"):
        partitions = {}
        for partition_name, partition_data in slurm_data.get("partitions", {}).items():
            if isinstance(partition_data, dict):
                partitions[partition_name] = SlurmPartitionInventory(
                    nodes=partition_data.get("nodes", []),
                    node_count=partition_data.get("node_count"),
                )
            elif isinstance(partition_data, list):
                # Allow shorthand: partition_name: [node1, node2]
                partitions[partition_name] = SlurmPartitionInventory(nodes=partition_data)

        slurm_inventory = SlurmInventory(
            partitions=partitions,
            cuda_arch=slurm_data.get("cuda_arch"),
            storage_path=slurm_data.get("storage_path", "/tmp"),
            default_partition=slurm_data.get("default_partition"),
        )

    # Parse Kubernetes inventory
    if k8s_data := data.get("kubernetes"):
        k8s_inventory = KubernetesInventory(
            driver_version=k8s_data.get("driver_version"),
            node_count=k8s_data.get("node_count"),
            nodes=k8s_data.get("nodes", []),
            gpu_node_count=k8s_data.get("gpu_node_count"),
            gpu_per_node=k8s_data.get("gpu_per_node"),
            total_gpus=k8s_data.get("total_gpus"),
            gpu_operator_namespace=k8s_data.get("gpu_operator_namespace", "nvidia-gpu-operator"),
            runtime_class=k8s_data.get("runtime_class", "nvidia"),
            gpu_resource_name=k8s_data.get("gpu_resource_name", "nvidia.com/gpu"),
        )

    # Parse VM inventory
    if vm_data := data.get("vm"):
        vm_inventory = VmInventory(
            region=vm_data.get("region", "us-west-2"),
            account_id=vm_data.get("account_id"),
            instance_id=vm_data.get("instance_id"),
            instance_type=vm_data.get("instance_type"),
            ami_id=vm_data.get("ami_id"),
            public_ip=vm_data.get("public_ip"),
            private_ip=vm_data.get("private_ip"),
            ssh_user=vm_data.get("ssh_user", "ubuntu"),
            ssh_key_path=vm_data.get("ssh_key_path"),
            vpc_id=vm_data.get("vpc_id"),
            subnet_id=vm_data.get("subnet_id"),
            security_group_id=vm_data.get("security_group_id"),
            igw_id=vm_data.get("igw_id"),
            route_table_id=vm_data.get("route_table_id"),
            key_name=vm_data.get("key_name"),
            availability_zone=vm_data.get("availability_zone"),
            gpu_count=vm_data.get("gpu_count", 0),
            gpu_name=vm_data.get("gpu_name"),
            driver_version=vm_data.get("driver_version"),
        )

    return ClusterInventory(
        platform=platform,
        cluster_name=cluster_name,
        slurm=slurm_inventory,
        kubernetes=k8s_inventory,
        vm=vm_inventory,
    )


def inventory_to_dict(inventory: ClusterInventory) -> dict[str, Any]:
    """Convert a ClusterInventory object back to a dictionary.

    This is useful for merging inventory data into test configurations.

    Args:
        inventory: ClusterInventory object

    Returns:
        Dictionary representation of the inventory
    """
    result: dict[str, Any] = {
        "platform": inventory.platform,
        "cluster_name": inventory.cluster_name,
    }

    if inventory.slurm:
        slurm_dict: dict[str, Any] = {
            "storage_path": inventory.slurm.storage_path,
        }
        if inventory.slurm.cuda_arch:
            slurm_dict["cuda_arch"] = inventory.slurm.cuda_arch
        if inventory.slurm.default_partition:
            slurm_dict["default_partition"] = inventory.slurm.default_partition
        if inventory.slurm.partitions:
            slurm_dict["partitions"] = {
                name: {
                    "nodes": partition.nodes,
                    "node_count": partition.node_count,
                }
                for name, partition in inventory.slurm.partitions.items()
            }
        result["slurm"] = slurm_dict

    if inventory.kubernetes:
        k8s_dict: dict[str, Any] = {
            "gpu_operator_namespace": inventory.kubernetes.gpu_operator_namespace,
            "runtime_class": inventory.kubernetes.runtime_class,
            "gpu_resource_name": inventory.kubernetes.gpu_resource_name,
        }
        if inventory.kubernetes.driver_version:
            k8s_dict["driver_version"] = inventory.kubernetes.driver_version
        if inventory.kubernetes.node_count is not None:
            k8s_dict["node_count"] = inventory.kubernetes.node_count
        if inventory.kubernetes.nodes:
            k8s_dict["nodes"] = inventory.kubernetes.nodes
        if inventory.kubernetes.gpu_node_count is not None:
            k8s_dict["gpu_node_count"] = inventory.kubernetes.gpu_node_count
        if inventory.kubernetes.gpu_per_node is not None:
            k8s_dict["gpu_per_node"] = inventory.kubernetes.gpu_per_node
        if inventory.kubernetes.total_gpus is not None:
            k8s_dict["total_gpus"] = inventory.kubernetes.total_gpus
        result["kubernetes"] = k8s_dict

    if inventory.vm:
        vm_dict: dict[str, Any] = {
            "region": inventory.vm.region,
            "ssh_user": inventory.vm.ssh_user,
            "gpu_count": inventory.vm.gpu_count,
        }
        if inventory.vm.account_id:
            vm_dict["account_id"] = inventory.vm.account_id
        if inventory.vm.instance_id:
            vm_dict["instance_id"] = inventory.vm.instance_id
        if inventory.vm.instance_type:
            vm_dict["instance_type"] = inventory.vm.instance_type
        if inventory.vm.ami_id:
            vm_dict["ami_id"] = inventory.vm.ami_id
        if inventory.vm.public_ip:
            vm_dict["public_ip"] = inventory.vm.public_ip
        if inventory.vm.private_ip:
            vm_dict["private_ip"] = inventory.vm.private_ip
        if inventory.vm.ssh_key_path:
            vm_dict["ssh_key_path"] = inventory.vm.ssh_key_path
        if inventory.vm.vpc_id:
            vm_dict["vpc_id"] = inventory.vm.vpc_id
        if inventory.vm.subnet_id:
            vm_dict["subnet_id"] = inventory.vm.subnet_id
        if inventory.vm.security_group_id:
            vm_dict["security_group_id"] = inventory.vm.security_group_id
        if inventory.vm.igw_id:
            vm_dict["igw_id"] = inventory.vm.igw_id
        if inventory.vm.route_table_id:
            vm_dict["route_table_id"] = inventory.vm.route_table_id
        if inventory.vm.key_name:
            vm_dict["key_name"] = inventory.vm.key_name
        if inventory.vm.availability_zone:
            vm_dict["availability_zone"] = inventory.vm.availability_zone
        if inventory.vm.gpu_name:
            vm_dict["gpu_name"] = inventory.vm.gpu_name
        if inventory.vm.driver_version:
            vm_dict["driver_version"] = inventory.vm.driver_version
        result["vm"] = vm_dict

    return result
