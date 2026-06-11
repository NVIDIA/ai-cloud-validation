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

from typing import ClassVar

from isvtest.core.k8s import (
    get_kubectl_base_shell,
    kubectl_items_or_fail,
    pod_status_reason,
)
from isvtest.core.validation import BaseValidation


class K8sPodHealthCheck(BaseValidation):
    description = "Verify all pods in the cluster are in a healthy state (Running or Succeeded)."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        ignore_phases = self.config.get("ignore_phases", [])

        kubectl_base = get_kubectl_base_shell()

        cmd = f"{kubectl_base} get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded -o json"
        result = self.run_command(cmd)
        pods = kubectl_items_or_fail(self, result, "pod list")
        if pods is None:
            return

        unhealthy_pods = []
        for pod in pods:
            metadata = pod.get("metadata") or {}
            status = (pod.get("status") or {}).get("phase") or "Unknown"

            if status in ignore_phases:
                continue

            namespace = metadata.get("namespace", "default")
            name = metadata.get("name", "unknown")
            unhealthy_pods.append(f"{namespace}/{name} ({status})")

        if unhealthy_pods:
            self.set_failed(
                f"Found {len(unhealthy_pods)} unhealthy pods: {', '.join(unhealthy_pods[:10])}"
                + (f"... and {len(unhealthy_pods) - 10} more" if len(unhealthy_pods) > 10 else "")
            )
            return

        self.set_passed("All pods are Running or Succeeded")


class K8sNoPendingPodsCheck(BaseValidation):
    description = "Verify no pods are stuck in Pending state."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        kubectl_base = get_kubectl_base_shell()

        cmd = f"{kubectl_base} get pods -A --field-selector=status.phase=Pending -o json"
        result = self.run_command(cmd)
        pods = kubectl_items_or_fail(self, result, "pod list")
        if pods is None:
            return

        pending_pods = []
        for pod in pods:
            metadata = pod.get("metadata") or {}
            pending_pods.append(f"{metadata.get('namespace', 'default')}/{metadata.get('name', 'unknown')}")

        if pending_pods:
            self.set_failed(f"Found {len(pending_pods)} pending pods: {', '.join(pending_pods)}")
            return

        self.set_passed("No pending pods found")


class K8sNoErrorPodsCheck(BaseValidation):
    description = "Verify no pods are in Error or CrashLoopBackOff state."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        kubectl_base = get_kubectl_base_shell()

        # Configurable error states
        error_states = self.config.get(
            "error_states",
            [
                "Error",
                "CrashLoopBackOff",
                "ImagePullBackOff",
                "ErrImagePull",
                "CreateContainerConfigError",
            ],
        )

        result = self.run_command(f"{kubectl_base} get pods -A -o json")
        pods = kubectl_items_or_fail(self, result, "pod list")
        if pods is None:
            return

        error_pods = []
        for pod in pods:
            metadata = pod.get("metadata") or {}
            status = pod_status_reason(pod)

            if status in error_states:
                namespace = metadata.get("namespace", "default")
                name = metadata.get("name", "unknown")
                error_pods.append(f"{namespace}/{name} ({status})")

        if error_pods:
            self.set_failed(f"Found {len(error_pods)} pods in error state: {', '.join(error_pods)}")
            return

        self.set_passed("No pods in error state found")
