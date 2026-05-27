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

import json
from typing import ClassVar

import pytest

from isvtest.core.k8s import get_kubectl_base_shell
from isvtest.core.validation import BaseValidation


class K8sMigConfigCheck(BaseValidation):
    description = "Check if MIG (Multi-Instance GPU) labels are available and match configuration."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes", "gpu")

    def run(self) -> None:
        require_mig = self.config.get("require_mig", False)
        expected_labels = self.config.get("expected_labels", {})

        kubectl_base = get_kubectl_base_shell()

        # Get all nodes JSON to check labels
        cmd = f"{kubectl_base} get nodes -o json"
        result = self.run_command(cmd)

        if result.exit_code != 0:
            self.set_failed(f"Failed to get nodes: {result.stderr}")
            return

        try:
            nodes_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.set_failed("Failed to parse kubectl output")
            return

        mig_nodes = []
        mismatch_nodes = []

        for node in nodes_data.get("items", []):
            name = node.get("metadata", {}).get("name", "unknown")
            labels = node.get("metadata", {}).get("labels", {})

            # A node has MIG enabled when nvidia.com/mig.capable is explicitly "true"
            mig_capable = labels.get("nvidia.com/mig.capable", "false")
            has_mig = str(mig_capable).lower() == "true"
            if has_mig:
                mig_nodes.append(name)

            # Verify expected labels only on nodes that have MIG enabled,
            # or on all nodes when require_mig is set.
            if expected_labels and (require_mig or has_mig):
                for key, expected_value in expected_labels.items():
                    if key not in labels:
                        mismatch_nodes.append(f"{name} (missing label {key})")
                    elif str(labels[key]) != str(expected_value):
                        mismatch_nodes.append(f"{name} ({key}: {labels[key]} != {expected_value})")

        if mismatch_nodes:
            self.set_failed(f"MIG label mismatch on nodes: {', '.join(mismatch_nodes)}")
            return

        if mig_nodes:
            msg = f"MIG labels found on {len(mig_nodes)} nodes: {', '.join(mig_nodes)}"
            if expected_labels:
                msg += f" (verified {len(expected_labels)} expected values)"
            self.set_passed(msg)
        else:
            if require_mig:
                self.set_failed("No MIG labels found on any node")
            else:
                pytest.skip("No MIG-capable nodes found (require_mig is false)")
