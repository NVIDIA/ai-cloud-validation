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

"""NIM Inference Workload using Kubernetes Job manifest.

This workload deploys NIM using a raw Kubernetes Job manifest (no Helm required)
and runs basic inference validation.
"""

import subprocess
import uuid
from pathlib import Path
from typing import ClassVar

from isvtest.config.settings import get_k8s_namespace
from isvtest.core.k8s import (
    get_gpu_nodes,
    get_job_pods,
    get_kubectl_base_shell,
    get_kubectl_command,
    get_pod_logs,
    wait_for_job_completion,
)
from isvtest.core.ngc import ensure_ngc_secrets
from isvtest.core.workload import BaseWorkloadCheck


class K8sNimInferenceWorkload(BaseWorkloadCheck):
    """Run NIM inference validation using Llama 3.2 3B model.

    This workload uses a raw Kubernetes Job manifest to deploy NIM.
    For Helm-based deployment with GenAI-Perf metrics, use K8sNimHelmWorkload.
    """

    description = "Run NIM inference validation using Llama 3.2 3B model."
    labels: ClassVar[tuple[str, ...]] = ("workload", "kubernetes", "gpu", "slow")

    def run(self) -> None:
        """Execute the NIM inference workload."""
        # Get configuration
        namespace = get_k8s_namespace()
        # Default timeout: 25 minutes (model download + load + inference)
        timeout = self.config.get("timeout", 1500)

        # Verify NGC secrets exist (using shared utility)
        success, error = ensure_ngc_secrets(namespace)
        if not success:
            self.set_passed(f"Skipped: {error}")
            return

        # Verify GPU nodes available
        nodes = get_gpu_nodes()
        if not nodes:
            self.set_passed("Skipped: No GPU nodes found in cluster")
            return

        # Ensure PVC for model cache exists and is usable
        if not self._ensure_pvc(namespace):
            return

        # Generate unique job name
        job_name = f"nim-llama-3b-test-{uuid.uuid4().hex[:8]}"

        # Read job YAML template
        yaml_path = Path(__file__).parent / "manifests" / "k8s" / "nim_llama_3b_inference_job.yaml"
        if not yaml_path.exists():
            self.set_failed(f"Job manifest not found: {yaml_path}")
            return

        yaml_content = yaml_path.read_text()

        # Replace job name
        yaml_content = yaml_content.replace("name: nim-llama-3b-inference-test", f"name: {job_name}")

        self.log.info(f"Starting NIM inference workload (timeout: {timeout}s)")
        self.log.info("Steps: 1. Pull images, 2. Download model, 3. Load model (10-12m), 4. Run inference")

        kubectl_parts = get_kubectl_command()
        kubectl_base = get_kubectl_base_shell()

        try:
            result = subprocess.run(
                kubectl_parts + ["apply", "-f", "-", "-n", namespace],
                input=yaml_content,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                self.set_failed(f"Failed to create job: {result.stderr}")
                return
        except Exception as e:
            self.set_failed(f"Exception creating job: {e}")
            return

        # Wait for completion
        try:
            completed, job_status = wait_for_job_completion(job_name=job_name, namespace=namespace, timeout=timeout)

            if not completed:
                self.set_failed(f"Job timed out after {timeout}s (status: {job_status})")
                self._dump_nim_logs(job_name, namespace)
                return

            if job_status == "Failed":
                self.set_failed("NIM inference job failed")
                self._dump_nim_logs(job_name, namespace)
                return

            # Get logs from inference-test container
            pods = get_job_pods(job_name=job_name, namespace=namespace)
            if not pods:
                self.set_failed("No pods found for job")
                return

            pod_name = pods[0]
            logs = get_pod_logs(pod_name=pod_name, namespace=namespace, container="inference-test", timeout=60)

            # Validate success
            if "[SUCCESS]" in logs and "[ERROR]" not in logs:
                self.set_passed("NIM inference validation passed!")
            else:
                self.set_failed("Inference test reported failure. Check logs.")
                self.log.error(f"Inference logs:\n{logs}")

        finally:
            # Cleanup
            self.run_command(f"{kubectl_base} delete job {job_name} -n {namespace} --wait=false")

    def _ensure_pvc(self, namespace: str) -> bool:
        """Ensure PVC for model cache exists.

        With ``WaitForFirstConsumer`` storage classes (e.g. EKS gp3), the PVC
        stays Pending until a pod references it, so we only verify the resource
        exists - not that it's Bound.

        Returns:
            True if PVC exists or was created, False on error.
        """
        pvc_name = "nim-model-cache"
        kubectl_base = get_kubectl_base_shell()

        res = self.run_command(f"{kubectl_base} get pvc {pvc_name} -n {namespace}")
        if res.exit_code == 0:
            return True

        self.log.info(f"Creating PVC {pvc_name}...")
        pvc_path = Path(__file__).parent / "manifests" / "k8s" / "nim_cache_pvc.yaml"
        if not pvc_path.exists():
            self.set_failed(f"PVC manifest not found: {pvc_path}")
            return False

        kubectl_parts = get_kubectl_command()
        try:
            subprocess.run(
                kubectl_parts + ["apply", "-f", "-", "-n", namespace],
                input=pvc_path.read_text(),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except Exception as e:
            self.set_failed(f"Failed to create PVC: {e}")
            return False

        return True

    def _dump_nim_logs(self, job_name: str, namespace: str) -> None:
        """Dump logs from NIM containers for debugging."""
        pods = get_job_pods(job_name=job_name, namespace=namespace)
        if not pods:
            return

        pod_name = pods[0]
        # NIM Server logs
        nim_logs = get_pod_logs(pod_name=pod_name, namespace=namespace, container="nim-server", timeout=30)
        if nim_logs:
            self.log.error(f"NIM Server Logs (tail):\n{nim_logs[-1000:]}")

        # Inference Test logs
        test_logs = get_pod_logs(pod_name=pod_name, namespace=namespace, container="inference-test", timeout=30)
        if test_logs:
            self.log.error(f"Inference Test Logs (tail):\n{test_logs[-1000:]}")
