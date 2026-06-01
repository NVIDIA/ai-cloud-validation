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

from isvtest.config.settings import get_k8s_gpu_operator_namespace
from isvtest.core.k8s import get_kubectl_base_shell, kubectl_items_or_fail, pod_status_reason
from isvtest.core.validation import BaseValidation


class K8sGpuOperatorNamespaceCheck(BaseValidation):
    description = "Verify GPU Operator namespace exists."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)
    test_ids: ClassVar[tuple[str, ...]] = ("K8S25-01",)

    def run(self) -> None:
        # Prefer config value, fall back to global setting
        namespace = self.config.get("namespace") or get_k8s_gpu_operator_namespace()

        kubectl_base = get_kubectl_base_shell()

        result = self.run_command(f"{kubectl_base} get namespace {shlex.quote(namespace)}")

        if result.exit_code != 0:
            self.set_failed(f"GPU Operator namespace '{namespace}' not found: {result.stderr}")
            return

        self.set_passed(f"GPU Operator namespace '{namespace}' exists")


class K8sGpuOperatorPodsCheck(BaseValidation):
    description = "Check if NVIDIA GPU Operator pods are running."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)
    test_ids: ClassVar[tuple[str, ...]] = ("K8S25-01",)

    def run(self) -> None:
        # Prefer config value, fall back to global setting
        namespace = self.config.get("namespace") or get_k8s_gpu_operator_namespace()

        kubectl_base = get_kubectl_base_shell()

        result = self.run_command(f"{kubectl_base} get pods -n {shlex.quote(namespace)} -o json")
        pods = kubectl_items_or_fail(self, result, "GPU Operator pod list")
        if pods is None:
            return

        running_pods = []
        for pod in pods:
            if pod_status_reason(pod) == "Running":
                running_pods.append((pod.get("metadata") or {}).get("name", "unknown"))

        if not running_pods:
            self.set_failed(f"No GPU Operator pods are running in namespace '{namespace}'")
            return

        self.set_passed(f"Found {len(running_pods)} running pods in '{namespace}'")
