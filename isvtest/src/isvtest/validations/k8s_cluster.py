# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

from typing import ClassVar

from isvtest.core.k8s import (
    get_kubectl_base_shell,
    kubectl_items_or_fail,
    pod_status_reason,
)
from isvtest.core.validation import BaseValidation


class K8sPodHealthCheck(BaseValidation):
    description = "Verify all pods in the cluster are in a healthy state (Running or Succeeded)."
    markers: ClassVar[list[str]] = ["kubernetes"]

    def run(self) -> None:
        # Configurable ignore phases
        ignore_phases = self.config.get("ignore_phases", [])

        kubectl_base = get_kubectl_base_shell()

        result = self.run_command(f"{kubectl_base} get pods -A -o json")
        pods = kubectl_items_or_fail(self, result, "pod list", exec_label="pod status")
        if pods is None:
            return

        unhealthy_pods = []
        for pod in pods:
            metadata = pod.get("metadata") or {}
            status = (pod.get("status") or {}).get("phase") or "Unknown"

            if status in ignore_phases:
                continue

            if status not in {"Running", "Succeeded"}:
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
    markers: ClassVar[list[str]] = ["kubernetes"]

    def run(self) -> None:
        kubectl_base = get_kubectl_base_shell()

        result = self.run_command(f"{kubectl_base} get pods -A -o json")
        pods = kubectl_items_or_fail(self, result, "pod list", exec_label="pending pods")
        if pods is None:
            return

        pending_pods = []
        for pod in pods:
            if (pod.get("status") or {}).get("phase") != "Pending":
                continue
            metadata = pod.get("metadata") or {}
            pending_pods.append(f"{metadata.get('namespace', 'default')}/{metadata.get('name', 'unknown')}")

        if pending_pods:
            self.set_failed(f"Found {len(pending_pods)} pending pods: {', '.join(pending_pods)}")
            return

        self.set_passed("No pending pods found")


class K8sNoErrorPodsCheck(BaseValidation):
    description = "Verify no pods are in Error or CrashLoopBackOff state."
    markers: ClassVar[list[str]] = ["kubernetes"]

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
        pods = kubectl_items_or_fail(self, result, "pod list", exec_label="pods")
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
