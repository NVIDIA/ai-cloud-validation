# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

from isvtest.config.settings import get_k8s_namespace
from isvtest.core.k8s import (
    get_kubectl_base_shell,
    kubectl_items_or_fail,
    pod_state_from_result,
)
from isvtest.core.nvidia import count_gpus_from_full_output, parse_driver_version
from isvtest.core.validation import BaseValidation


class K8sNvidiaSmiCheck(BaseValidation):
    description = "Verify nvidia-smi is accessible and returns valid output on all GPU nodes."
    markers: ClassVar[list[str]] = ["kubernetes", "gpu"]

    def run(self) -> None:
        # Configurable timeout, defaulting to 60 seconds for robustness
        timeout = int(self.config.get("timeout", 60))
        results = self._run_ephemeral_pods(timeout=timeout)
        if results is None:
            # ``_run_ephemeral_pods`` already routed the failure via set_failed.
            return

        failed_nodes = []
        for node, res in results.items():
            if res["error"]:
                # Include output if available for better debugging (e.g. pod logs)
                error_msg = f"{node} ({res['error']})"
                if res.get("output"):
                    error_msg += f"\nOutput: {res['output'][:500]}..."  # Truncate long output
                failed_nodes.append(error_msg)
            elif "NVIDIA-SMI" not in res["output"]:
                failed_nodes.append(f"{node} (invalid nvidia-smi output)")
            else:
                self.log.debug(f"Node {node}: nvidia-smi check passed")

        if failed_nodes:
            self.set_failed(f"nvidia-smi check failed on nodes: {', '.join(failed_nodes)}")
        else:
            self.set_passed(f"nvidia-smi check passed on all {len(results)} GPU nodes")

    def _run_ephemeral_pods(self, timeout: int = 60) -> dict[str, dict] | None:
        """Run ephemeral pods on all GPU nodes and return results.

        Returns ``None`` (after ``set_failed``) when the GPU-node lookup
        fails, distinguishing it from an empty cluster which returns ``{}``.
        """
        kubectl_base = get_kubectl_base_shell()
        namespace = get_k8s_namespace()

        # 1. Identify GPU nodes
        result = self.run_command(f"{kubectl_base} get nodes -l nvidia.com/gpu.present=true -o json")
        nodes = kubectl_items_or_fail(self, result, "GPU node list", exec_label="GPU nodes")
        if nodes is None:
            return None

        gpu_nodes = [
            str((node.get("metadata") or {}).get("name")) for node in nodes if (node.get("metadata") or {}).get("name")
        ]
        if not gpu_nodes:
            self.log.warning("No GPU nodes found with label nvidia.com/gpu.present=true")
            return {}

        self.log.info(f"Found {len(gpu_nodes)} GPU nodes: {', '.join(gpu_nodes)}")

        # Use a standard base image that definitely has nvidia-smi
        image = self.config.get("cuda_image", "nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04")

        # Get runtime class from config (e.g., "nvidia" for MicroK8s)
        runtime_class = self.config.get("runtime_class")

        # Use ThreadPoolExecutor to run pods in parallel
        with ThreadPoolExecutor(max_workers=len(gpu_nodes)) as executor:
            futures = {
                executor.submit(
                    self._run_single_pod, node, image, kubectl_base, namespace, timeout, runtime_class
                ): node
                for node in gpu_nodes
            }

            results = {}
            for future in futures:
                node = futures[future]
                try:
                    results[node] = future.result()
                except Exception as e:
                    results[node] = {"output": "", "error": f"exception: {e}"}

        return results

    def _run_single_pod(
        self, node: str, image: str, kubectl_base: str, namespace: str, timeout: int, runtime_class: str | None = None
    ) -> dict:
        """Run a single ephemeral pod on a specific node."""
        # Include node name in pod name for easier identification in logs
        node_suffix = node.replace(".", "-")[:20]  # Sanitize and truncate node name
        pod_name = f"isvtest-smi-{node_suffix}-{uuid.uuid4().hex[:4]}"
        self.log.info(f"Creating pod {pod_name} on node {node}")

        # Add tolerations to ensure scheduling on tainted nodes
        overrides: dict = {
            "spec": {
                "nodeName": node,
                "restartPolicy": "Never",
                "tolerations": [
                    {"operator": "Exists"}  # Tolerate everything
                ],
                "containers": [
                    {
                        "name": "test",
                        "image": image,
                        "command": ["nvidia-smi"],
                        "resources": {"limits": {"nvidia.com/gpu": "1"}},
                    }
                ],
            }
        }

        # Add runtimeClassName if specified (required for MicroK8s with GPU operator)
        if runtime_class:
            overrides["spec"]["runtimeClassName"] = runtime_class

        overrides_json = json.dumps(overrides)

        run_cmd = (
            f"{kubectl_base} run {pod_name} "
            f"--image={image} "
            f"--restart=Never "
            f"-n {namespace} "
            f"--overrides='{overrides_json}'"
        )

        run_res = self.run_command(run_cmd)

        if run_res.exit_code != 0:
            return {"output": "", "error": f"failed to start pod: {run_res.stderr}"}

        start_time = time.time()
        end_time = start_time + timeout
        pod_output = ""
        error = None
        last_logged_phase = None
        wait_count = 0

        while time.time() < end_time:
            time.sleep(1)
            wait_count += 1
            get_pod_cmd = f"{kubectl_base} get pod {pod_name} -n {namespace} -o json"
            status_res = self.run_command(get_pod_cmd)

            phase, waiting_reason, waiting_message = pod_state_from_result(status_res)
            detail = f", waiting={waiting_reason}" if waiting_reason else ""

            # Log phase changes and periodic updates for stuck pods
            if (phase, waiting_reason) != last_logged_phase:
                self.log.info(f"Pod {pod_name} on node {node}: phase={phase}{detail}")
                if waiting_message:
                    self.log.debug("Pod %s waiting message: %s", pod_name, waiting_message)
                last_logged_phase = (phase, waiting_reason)
            elif wait_count % 30 == 0:  # Log every 30 seconds if stuck
                elapsed = int(time.time() - start_time)
                self.log.warning(f"Pod {pod_name} on node {node} still in phase={phase}{detail} after {elapsed}s")

            if phase == "Succeeded":
                logs_cmd = f"{kubectl_base} logs {pod_name} -n {namespace}"
                logs_res = self.run_command(logs_cmd)
                pod_output = logs_res.stdout
                break
            elif phase == "Failed":
                logs_cmd = f"{kubectl_base} logs {pod_name} -n {namespace}"
                logs_res = self.run_command(logs_cmd)
                pod_output = logs_res.stdout
                # Check if it's an image pull error or similar
                if not pod_output:
                    # Try to get events or description if logs are empty
                    desc_cmd = f"{kubectl_base} describe pod {pod_name} -n {namespace}"
                    desc_res = self.run_command(desc_cmd)
                    error = f"pod failed (logs empty). Description:\n{desc_res.stdout}"
                else:
                    error = "pod failed"
                break
        else:
            # Timed out - get more details for debugging
            get_pod_cmd = f"{kubectl_base} get pod {pod_name} -n {namespace} -o json"
            status_res = self.run_command(get_pod_cmd)
            phase, waiting_reason, _waiting_message = pod_state_from_result(status_res)
            detail = f" ({waiting_reason})" if waiting_reason else ""
            self.log.error(f"Pod {pod_name} on node {node} TIMED OUT after {timeout}s in phase {phase}")

            # Get pod events for debugging stuck pods
            events_cmd = f"{kubectl_base} get events -n {namespace} --field-selector involvedObject.name={pod_name} --sort-by='.lastTimestamp'"
            events_res = self.run_command(events_cmd)
            if events_res.stdout.strip():
                self.log.error(f"Events for {pod_name}:\n{events_res.stdout}")

            error = f"pod on node {node} timed out after {timeout}s in phase {phase}{detail}"

        # Cleanup
        # Force delete and wait for completion to avoid overwhelming containerd
        self.run_command(f"{kubectl_base} delete pod {pod_name} -n {namespace} --grace-period=0 --force")

        return {"output": pod_output, "error": error}


class K8sDriverVersionCheck(K8sNvidiaSmiCheck):
    description = "Verify NVIDIA driver version matches expected across all GPU nodes."
    markers: ClassVar[list[str]] = ["kubernetes", "gpu"]

    def run(self) -> None:
        expected_driver = self.config.get("driver_version")
        if not expected_driver:
            self.set_passed("Skipped: driver_version not configured")
            return

        # Inherit timeout from config if present, else default to 60
        timeout = int(self.config.get("timeout", 60))
        results = self._run_ephemeral_pods(timeout=timeout)
        if results is None:
            return
        mismatches = []

        for node, res in results.items():
            if res["error"]:
                continue

            # Use shared parser for driver version
            actual_driver = parse_driver_version(res["output"])
            if not actual_driver:
                mismatches.append(f"{node} (could not parse driver version)")
                continue

            if actual_driver != expected_driver:
                mismatches.append(f"{node} ({actual_driver} != {expected_driver})")

        if mismatches:
            self.set_failed(f"Driver version mismatch on nodes: {', '.join(mismatches)}")
        else:
            self.set_passed(f"Driver version {expected_driver} verified on {len(results)} nodes")


class K8sGpuPodAccessCheck(K8sNvidiaSmiCheck):
    """Verify GPU access from pods by running nvidia-smi.

    Note: This check runs nvidia-smi in a pod that requests 1 GPU, so it can only
    verify that 1 GPU is accessible per node (due to Kubernetes resource isolation).
    Use K8sGpuCapacityCheck for actual node-level GPU count validation.
    """

    description = "Verify GPU access from pods by running nvidia-smi (sees 1 allocated GPU per node)."
    markers: ClassVar[list[str]] = ["kubernetes", "gpu"]

    def run(self) -> None:
        gpu_count_per_node = self.config.get("gpu_count")
        total_gpu_count = self.config.get("total_gpu_count")

        if gpu_count_per_node is None and total_gpu_count is None:
            self.set_passed("Skipped: neither gpu_count nor total_gpu_count configured")
            return

        # Convert to int for Jinja2 templated values
        if gpu_count_per_node is not None:
            gpu_count_per_node = int(gpu_count_per_node)
        if total_gpu_count is not None:
            total_gpu_count = int(total_gpu_count)

        # Inherit timeout from config if present, else default to 60
        timeout = int(self.config.get("timeout", 60))
        results = self._run_ephemeral_pods(timeout=timeout)
        if results is None:
            return
        mismatches = []
        total_gpus_found = 0

        for node, res in results.items():
            if res["error"]:
                continue

            # Use shared parser for GPU count from full nvidia-smi output
            actual_count = count_gpus_from_full_output(res["output"])
            total_gpus_found += actual_count

            # Check per-node count if configured
            if gpu_count_per_node is not None and actual_count != gpu_count_per_node:
                mismatches.append(f"{node} (found {actual_count} GPUs, expected {gpu_count_per_node})")

        # Check total count if configured
        if total_gpu_count is not None and total_gpus_found != total_gpu_count:
            self.set_failed(
                f"Total GPU count mismatch: found {total_gpus_found} GPUs across {len(results)} nodes, expected {total_gpu_count}"
            )
            return

        if mismatches:
            self.set_failed(f"GPU count mismatch on nodes: {', '.join(mismatches)}")
        else:
            msg_parts = []
            if gpu_count_per_node is not None:
                msg_parts.append(f"{gpu_count_per_node} GPUs per node")
            if total_gpu_count is not None:
                msg_parts.append(f"{total_gpu_count} total GPUs")
            self.set_passed(f"Verified {' and '.join(msg_parts)} across {len(results)} nodes")
