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

import shlex
from typing import ClassVar

from isvtest.core.k8s import get_kubectl_base_shell, kubectl_items_or_fail
from isvtest.core.validation import BaseValidation


class K8sGpuLabelsCheck(BaseValidation):
    description = "Verify GPU nodes have proper NVIDIA labels."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes", "gpu")

    def run(self) -> None:
        label_selector = self.config.get("label_selector", "nvidia.com/gpu.present=true")

        kubectl_base = get_kubectl_base_shell()

        cmd = f"{kubectl_base} get nodes -l {shlex.quote(label_selector)} -o json"
        result = self.run_command(cmd)
        nodes = kubectl_items_or_fail(self, result, "GPU node list")
        if nodes is None:
            return

        if not nodes:
            self.set_failed(f"No GPU nodes found with label '{label_selector}'")
            return

        self.set_passed(f"Found {len(nodes)} nodes with label '{label_selector}'")


class K8sGpuCapacityCheck(BaseValidation):
    """Check GPU capacity at the node level by querying Kubernetes resources.

    This check queries node capacity directly via kubectl, providing accurate
    GPU counts without the limitations of pod-level resource isolation.

    Config options:
        resource_name: Resource name to check (default: nvidia.com/gpu)
        expected_total: Expected total GPU count across all nodes (optional)
        expected_per_node: Expected GPU count per GPU node (optional)
    """

    description = "Verify node GPU capacity matches expected counts."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes", "gpu")

    def run(self) -> None:
        resource_name = self.config.get("resource_name", "nvidia.com/gpu")
        expected_total = self.config.get("expected_total")
        expected_per_node = self.config.get("expected_per_node")

        # Convert to int for Jinja2 templated values
        try:
            if expected_total is not None:
                expected_total = int(expected_total)
            if expected_per_node is not None:
                expected_per_node = int(expected_per_node)
        except (TypeError, ValueError):
            self.set_failed(
                f"Invalid expected GPU capacity values: "
                f"expected_total={expected_total!r}, expected_per_node={expected_per_node!r}"
            )
            return

        kubectl_base = get_kubectl_base_shell()

        result = self.run_command(f"{kubectl_base} get nodes -o json")
        nodes = kubectl_items_or_fail(self, result, "node list")
        if nodes is None:
            return

        gpu_nodes_count = 0
        total_gpus = 0
        per_node_mismatches = []

        for node in nodes:
            capacity = (node.get("status") or {}).get("capacity") or {}
            val_str = str(capacity.get(resource_name) or "")
            if not val_str:
                continue
            if val_str.isdigit():
                count = int(val_str)
                if count > 0:
                    gpu_nodes_count += 1
                    total_gpus += count
                    if expected_per_node is not None and count != expected_per_node:
                        node_name = (node.get("metadata") or {}).get("name", "unknown")
                        per_node_mismatches.append(f"{node_name} ({count} != {expected_per_node})")

        if gpu_nodes_count == 0:
            self.set_failed(f"No '{resource_name}' resources found in node capacity")
            return

        # Check per-node count
        if per_node_mismatches:
            self.set_failed(f"GPU count mismatch on nodes: {', '.join(per_node_mismatches)}")
            return

        # Check total count
        if expected_total is not None and total_gpus != expected_total:
            self.set_failed(f"Total GPU count mismatch: found {total_gpus}, expected {expected_total}")
            return

        self.set_passed(f"Found {total_gpus} total '{resource_name}' across {gpu_nodes_count} nodes")
