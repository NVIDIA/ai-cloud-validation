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

"""Slurm partition validations."""

import json
from typing import ClassVar

import pytest

from isvtest.core.slurm import get_partition_names, get_partitions
from isvtest.core.validation import BaseValidation


class SlurmInfoAvailable(BaseValidation):
    """Verify sinfo command is available and cluster has expected partitions.

    Config options:
        expected_partitions (int): Exact number of partitions expected (optional)
        min_partitions (int): Minimum number of partitions required (optional)
        required_partitions (list[str]): List of partition names that must exist (optional)
    """

    description: ClassVar[str] = "Check that sinfo command works"
    timeout: ClassVar[int] = 30
    labels: ClassVar[tuple[str, ...]] = ("slurm",)
    test_ids: ClassVar[tuple[str, ...]] = ("SLURM-XX-02",)

    def run(self) -> None:
        result = self.run_command("sinfo -o '%P %a %l %D %N'")

        if result.exit_code != 0:
            self.set_failed(f"sinfo command failed: {result.stderr}")
            return

        if not result.stdout.strip():
            self.set_failed("sinfo returned no output")
            return

        partition_names = get_partition_names(result.stdout)
        partition_count = len(partition_names)

        # Get config options (convert to int for Jinja2 templated values)
        expected_partitions = self.config.get("expected_partitions")
        min_partitions = self.config.get("min_partitions")
        required_partitions = self.config.get("required_partitions")

        if expected_partitions is not None:
            expected_partitions = int(expected_partitions)
        if min_partitions is not None:
            min_partitions = int(min_partitions)
        # Handle JSON string from Jinja2 tojson filter
        if isinstance(required_partitions, str):
            required_partitions = json.loads(required_partitions)

        # Validate expected partition count
        if expected_partitions is not None and partition_count != expected_partitions:
            self.set_failed(f"Expected {expected_partitions} partition(s), found {partition_count}: {partition_names}")
            return

        # Validate minimum partition count
        if min_partitions is not None and partition_count < min_partitions:
            self.set_failed(
                f"Expected at least {min_partitions} partition(s), found {partition_count}: {partition_names}"
            )
            return

        # Validate required partitions exist
        if required_partitions:
            missing = [p for p in required_partitions if p not in partition_names]
            if missing:
                self.set_failed(f"Missing required partition(s): {missing}. Found: {partition_names}")
                return

        self.set_passed(f"sinfo available, found {partition_count} partition(s): {', '.join(partition_names)}")


class SlurmPartition(BaseValidation):
    """Verify a Slurm partition exists and is available.

    Config options:
        partition_name (str): Partition name to check (required)
        expected_nodes (int): Expected number of nodes in partition (optional)
        min_nodes (int): Minimum number of nodes required (optional)
        required_nodes (list[str]): List of specific node names that must be in the partition (optional)
        require_available (bool): Require partition state to be "up" (default: True)
    """

    description: ClassVar[str] = "Check that a Slurm partition exists and is properly configured"
    timeout: ClassVar[int] = 30
    labels: ClassVar[tuple[str, ...]] = ("slurm",)
    test_ids: ClassVar[tuple[str, ...]] = ("SLURM-XX-03",)

    def run(self) -> None:
        partitions = get_partitions(self)

        if partitions is None:
            self.set_failed("sinfo command failed")
            return

        # Get config options (convert to int for Jinja2 templated values)
        partition_name = self.config.get("partition_name")
        expected_nodes = self.config.get("expected_nodes")
        min_nodes = self.config.get("min_nodes")
        required_nodes = self.config.get("required_nodes")
        require_available = self.config.get("require_available", True)

        if expected_nodes is not None:
            expected_nodes = int(expected_nodes)
        if min_nodes is not None:
            min_nodes = int(min_nodes)
        # Handle JSON string from Jinja2 tojson filter
        if isinstance(required_nodes, str):
            required_nodes = json.loads(required_nodes)

        if partition_name:
            # Check for specific partition
            if partition_name not in partitions:
                no_nodes_expected = expected_nodes == 0 and not required_nodes and not min_nodes
                if no_nodes_expected:
                    pytest.skip(
                        f"Partition '{partition_name}' not present on this cluster "
                        f"(available: {list(partitions.keys())})"
                    )
                self.set_failed(f"Partition '{partition_name}' not found. Available: {list(partitions.keys())}")
                return

            partition = partitions[partition_name]
            messages = [f"Partition '{partition_name}' found"]

            # Check availability state
            if require_available and partition.avail != "up":
                self.set_failed(f"Partition '{partition_name}' is not available (state: {partition.avail})")
                return
            messages.append(f"state={partition.avail}")

            # Check expected node count
            if expected_nodes is not None and partition.node_count != expected_nodes:
                self.set_failed(
                    f"Partition '{partition_name}' has {partition.node_count} nodes, expected {expected_nodes}"
                )
                return

            # Check minimum node count
            if min_nodes is not None and partition.node_count < min_nodes:
                self.set_failed(
                    f"Partition '{partition_name}' has {partition.node_count} nodes, minimum required: {min_nodes}"
                )
                return

            # Check required nodes are present (inventory validation)
            if required_nodes:
                missing_nodes = [n for n in required_nodes if n not in partition.nodes]
                if missing_nodes:
                    self.set_failed(
                        f"Missing required nodes in partition '{partition_name}': {missing_nodes}. "
                        f"Found: {partition.nodes}"
                    )
                    return
                messages.append(f"required_nodes={len(required_nodes)} verified")

            messages.append(f"nodes={partition.node_count}")
            if partition.nodelist:
                messages.append(f"nodelist={partition.nodelist}")

            self.set_passed(", ".join(messages))
        else:
            # Check for any partition with "gpu" in the name
            gpu_partitions = {k: v for k, v in partitions.items() if "gpu" in k.lower()}

            if not gpu_partitions:
                self.set_failed("No GPU partition found in sinfo output")
                return

            # Report all found GPU partitions
            partition_info = [f"{name}({p.node_count} nodes, {p.avail})" for name, p in gpu_partitions.items()]
            self.set_passed(f"Found GPU partition(s): {', '.join(partition_info)}")
