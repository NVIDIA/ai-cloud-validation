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

import subprocess
import time
import uuid
from pathlib import Path

from isvtest.config.settings import (
    get_gpu_cuda_arch,
    get_gpu_memory_gb,
    get_gpu_stress_gpu_count,
    get_gpu_stress_image,
    get_gpu_stress_runtime,
    get_gpu_stress_timeout,
    get_k8s_namespace,
)
from isvtest.core.k8s import get_gpu_nodes, get_kubectl_base_shell, get_kubectl_command, get_node_gpu_count
from isvtest.core.workload import BaseWorkloadCheck


class K8sGpuStressWorkload(BaseWorkloadCheck):
    description = "Run GPU stress test on all GPU nodes in the cluster."

    def run(self) -> None:
        # Get configuration
        namespace = get_k8s_namespace()
        image = self.config.get("image") or get_gpu_stress_image()
        runtime = self.config.get("runtime") or get_gpu_stress_runtime()
        timeout = self.config.get("timeout") or get_gpu_stress_timeout()
        memory_gb = self.config.get("memory_gb") or get_gpu_memory_gb()
        configured_gpu_count = self.config.get("gpu_count") or get_gpu_stress_gpu_count()
        cuda_arch = self.config.get("cuda_arch") or get_gpu_cuda_arch()

        # Get GPU nodes
        # Note: We still rely on k8s_utils here for convenience
        nodes = get_gpu_nodes()
        if not nodes:
            self.set_passed("Skipped: No GPU nodes available")
            return

        self.log.info(f"Running GPU stress test on {len(nodes)} nodes: {', '.join(nodes)}")
        self.log.info(f"CUDA arch setting: {cuda_arch or 'native (default)'}")

        failed_nodes = []

        # Run stress test on each node sequentially (to avoid overloading cluster if resources tight)
        for node_name in nodes:
            # Determine GPU count for this node
            if configured_gpu_count is not None:
                gpu_count = configured_gpu_count
            else:
                gpu_count = get_node_gpu_count(node_name)
                if gpu_count == 0:
                    self.log.warning(f"Could not determine GPU count for node {node_name}, defaulting to 1")
                    gpu_count = 1

            pod_name = f"gpu-stress-test-{node_name}-{uuid.uuid4().hex[:8]}"

            self.log.info(f"Node: {node_name}, GPUs: {gpu_count}, Runtime: {runtime}s")

            # Create pod YAML
            yaml_content = self._create_pod_yaml(
                pod_name=pod_name,
                node_name=node_name,
                gpu_count=gpu_count,
                namespace=namespace,
                image=image,
                runtime=runtime,
                memory_gb=memory_gb,
                cuda_arch=cuda_arch,
            )

            # Run the job (using run_k8s_job logic but adapted for pod since we create raw Pod here)
            # run_k8s_job uses `kubectl apply -f -`, which works for Pods too.
            # It waits for "Complete" condition which Jobs have but Pods don't (Pods have Phase=Succeeded)
            # So we cannot use run_k8s_job directly if we deploy a Pod resource.
            # We should refactor run_k8s_job or just implement pod logic here.
            # Given create_gpu_stress_pod_yaml creates a Pod, let's implement pod logic here.

            # 1. Apply Pod
            kubectl_parts = get_kubectl_command()

            try:
                result = subprocess.run(
                    kubectl_parts + ["apply", "-f", "-", "-n", namespace],
                    input=yaml_content,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    failed_nodes.append(f"{node_name} (create failed: {result.stderr})")
                    continue
            except Exception as e:
                failed_nodes.append(f"{node_name} (create exception: {e})")
                continue

            # 2. Wait for completion (using helper from k8s_utils for now, or reimplement)
            # We can use the logic from run_ephemeral_pods in k8s_gpu.py but simplified
            start_time = time.time()
            end_time = start_time + timeout
            pod_status = "Unknown"
            pod_succeeded = False
            pod_logs = ""

            kubectl_base = get_kubectl_base_shell()

            while time.time() < end_time:
                time.sleep(5)
                cmd = f"{kubectl_base} get pod {pod_name} -n {namespace} -o jsonpath='{{.status.phase}}'"
                res = self.runner.run(cmd)
                phase = res.stdout.strip()

                if phase == "Succeeded":
                    pod_status = "Succeeded"
                    pod_succeeded = True
                    break
                elif phase == "Failed":
                    pod_status = "Failed"
                    break

            # Get logs
            logs_cmd = f"{kubectl_base} logs {pod_name} -n {namespace}"
            logs_res = self.runner.run(logs_cmd)
            pod_logs = logs_res.stdout

            # Cleanup
            self.runner.run(f"{kubectl_base} delete pod {pod_name} -n {namespace} --wait=false")

            if not pod_succeeded:
                error_detail = f"Phase: {pod_status}"
                if pod_status == "Failed":
                    # Check if OOM
                    if "Out of memory" in pod_logs:
                        error_detail += " (OOM)"
                failed_nodes.append(f"{node_name} ({error_detail})")
                self.log.error(f"Stress test failed on {node_name}. Logs:\n{pod_logs[-500:]}")
            elif "SUCCESS" not in pod_logs:
                failed_nodes.append(f"{node_name} (Success marker not found in logs)")
                self.log.error(f"Stress test finished but SUCCESS not found on {node_name}. Logs:\n{pod_logs[-500:]}")
            else:
                self.log.info(f"Stress test passed on {node_name}")

        if failed_nodes:
            self.set_failed(f"GPU stress test failed on nodes: {', '.join(failed_nodes)}")
        else:
            self.set_passed(f"GPU stress test passed on all {len(nodes)} nodes")

    def _create_pod_yaml(
        self,
        pod_name: str,
        node_name: str,
        gpu_count: int,
        namespace: str,
        image: str,
        runtime: int,
        memory_gb: int,
        cuda_arch: str | None = None,
    ) -> str:
        """Create pod YAML for GPU stress test."""
        # Get the path to the gpu_stress_workload.py script
        script_path = Path(__file__).parent / "scripts" / "gpu_stress_workload.py"

        if not script_path.exists():
            raise FileNotFoundError(f"Stress workload script not found at {script_path}")

        with open(script_path) as f:
            script_content = f.read()

        # Indent script content for YAML
        indented_script = "\n".join("      " + line if line.strip() else "" for line in script_content.split("\n"))

        # Build environment variables list
        env_vars = [
            f'    - name: GPU_STRESS_RUNTIME\n      value: "{runtime}"',
            f'    - name: GPU_MEMORY_GB\n      value: "{memory_gb}"',
        ]

        # Add CuPy CUDA architecture settings for ARM64 compatibility
        if cuda_arch:
            # Set specific compute capability
            env_vars.append(f'    - name: CUPY_CUDA_ARCH_LIST\n      value: "{cuda_arch}"')
        else:
            # Use native arch detection (works on most systems)
            env_vars.append('    - name: CUPY_CUDA_ARCH_LIST\n      value: "native"')

        env_section = "\n".join(env_vars)

        pod_yaml = f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {namespace}
  labels:
    app: gpu-stress-test
    test-node: {node_name}
spec:
  restartPolicy: Never
  nodeName: {node_name}
  containers:
  - name: gpu-stress
    image: {image}
    command: ["/bin/bash", "-c"]
    args:
    - |
      cat > /tmp/gpu_stress.py << 'SCRIPT_EOF'
{indented_script}
      SCRIPT_EOF
      python3 /tmp/gpu_stress.py
    env:
{env_section}
    resources:
      limits:
        nvidia.com/gpu: "{gpu_count}"
    imagePullPolicy: IfNotPresent
  runtimeClassName: nvidia
  tolerations:
  - key: "nvidia.com/gpu"
    operator: "Exists"
    effect: "NoSchedule"
"""
        return pod_yaml
