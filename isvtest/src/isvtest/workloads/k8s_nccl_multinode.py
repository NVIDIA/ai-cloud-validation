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

"""Kubernetes multi-node NCCL workload using MPI Operator.

Runs NCCL AllReduce tests across multiple nodes via MPIJob to verify
GPU-to-GPU communication over NVLink/NVSwitch (intra-node) and network
fabric (inter-node).

Requires the Kubeflow MPI Operator (kubeflow.org/v2beta1) to be installed
in the cluster.

When the NVIDIA DRA driver is available (ComputeDomain CRD registered),
automatically creates a ComputeDomain to enable Multi-Node NVLink (MNNVL)
via IMEX channels for full NVLink bandwidth across nodes.
"""

import subprocess
import time
import uuid
from pathlib import Path
from typing import ClassVar

import pytest

from isvtest.config.settings import (
    get_k8s_namespace,
    get_nccl_hpc_image,
    get_nccl_min_bus_bw_gbps,
    get_nccl_multinode_gpus_per_node,
    get_nccl_multinode_nodes,
    get_nccl_multinode_timeout,
)
from isvtest.core.k8s import (
    get_gpu_nodes,
    get_kubectl_command,
    get_node_gpu_count,
    get_pod_logs,
    run_kubectl,
)
from isvtest.core.workload import BaseWorkloadCheck
from isvtest.workloads.nccl_common import parse_nccl_output

_MPIJOB_LABEL_JOB_NAME = "training.kubeflow.org/job-name"

_COMPUTE_DOMAIN_TEMPLATE = """\
---
apiVersion: resource.nvidia.com/v1beta1
kind: ComputeDomain
metadata:
  name: {cd_name}
spec:
  numNodes: {num_nodes}
  channel:
    resourceClaimTemplate:
      name: {cd_channel_name}
---
"""


class K8sNcclMultiNodeWorkload(BaseWorkloadCheck):
    """Run NCCL AllReduce test across multiple Kubernetes nodes via MPIJob.

    This workload validates GPU-to-GPU communication across multiple nodes
    using the NVIDIA HPC Benchmarks container orchestrated by the Kubeflow
    MPI Operator. It tests:
    - NVLink/NVSwitch bandwidth within nodes
    - Network fabric (InfiniBand/RoCE/RDMA) bandwidth between nodes
    - Data integrity (out-of-bounds check)

    Prerequisites:
        - Kubeflow MPI Operator installed (MPIJob CRD available)
        - At least 2 GPU nodes in the cluster
        - For full NVLink bandwidth across nodes: DRA driver with IMEX channels

    Config options:
        nodes (int): Number of nodes (default: 2 via env or auto-detect)
        gpus_per_node (int): GPUs per node (default: auto-detect, fallback 8)
        min_bus_bw_gbps (float): Minimum expected bus bandwidth in GB/s (default: 0 = no check)
        timeout (int): Job timeout in seconds (default: 900 via env)
        startup_timeout (int): Seconds to wait for launcher pod to appear (default: 300).
            Covers image pulls on workers, SSH key setup, and StatefulSet creation.
        image (str): Container image (default: nvcr.io/nvidia/hpc-benchmarks:25.04)
        quick_mode (bool): Use reduced message sizes for faster execution (default: False)
            - True: 1M-256M range, ~30 seconds (CI/dev validation)
            - False: 8B-4G range, 2-5 minutes (full performance test)
        use_compute_domain (str): "auto" (default) detects DRA driver availability,
            "true" to require ComputeDomain/MNNVL, "false" to skip.
    """

    description: ClassVar[str] = "Run NCCL AllReduce test across multiple K8s nodes (MPIJob)"
    timeout: ClassVar[int] = 1800
    labels: ClassVar[tuple[str, ...]] = ("workload", "kubernetes", "gpu", "slow")
    test_ids: ClassVar[tuple[str, ...]] = ("K8S-XX-05",)

    def run(self) -> None:
        """Execute multi-node NCCL test via MPIJob."""
        namespace = get_k8s_namespace()

        timeout_config = self.config.get("timeout")
        job_timeout = int(timeout_config) if timeout_config is not None else get_nccl_multinode_timeout()

        min_bus_bw_config = self.config.get("min_bus_bw_gbps")
        min_bus_bw = float(min_bus_bw_config) if min_bus_bw_config is not None else get_nccl_min_bus_bw_gbps()

        image = self.config.get("image") or get_nccl_hpc_image()
        quick_mode = self.config.get("quick_mode", False)

        if not self._check_mpi_operator():
            pytest.skip(
                "MPI Operator not found. Install the Kubeflow MPI Operator "
                "(https://github.com/kubeflow/mpi-operator) to run multi-node NCCL tests."
            )

        gpu_nodes = get_gpu_nodes()
        if not gpu_nodes:
            self.set_passed("Skipped: No GPU nodes found in cluster")
            return

        if len(gpu_nodes) < 2:
            self.set_passed(f"Skipped: Multi-node NCCL test requires at least 2 GPU nodes, found {len(gpu_nodes)}")
            return

        use_cd = self._resolve_compute_domain_mode()

        node_count, gpus_per_node = self._resolve_topology(gpu_nodes)
        total_gpus = node_count * gpus_per_node
        job_name = f"nccl-allreduce-mn-{uuid.uuid4().hex[:8]}"
        cd_name = f"{job_name}-cd"
        cd_channel_name = f"{job_name}-cd-channel"

        mode_str = "quick" if quick_mode else "full"
        self.log.info(
            f"Starting multi-node NCCL test ({mode_str} mode): "
            f"{node_count} nodes x {gpus_per_node} GPUs = {total_gpus} total GPUs"
        )
        self.log.info(f"Image: {image}, Min BW: {min_bus_bw} GB/s, Timeout: {job_timeout}s")
        self.log.info(f"ComputeDomain (MNNVL/IMEX): {'enabled' if use_cd else 'disabled'}")

        manifest_path = Path(__file__).parent / "manifests" / "k8s" / "nccl_allreduce_mpijob.yaml"
        if not manifest_path.exists():
            self.set_failed(f"Manifest file not found: {manifest_path}")
            return

        yaml_content = manifest_path.read_text()
        yaml_content = self._patch_manifest(
            yaml_content, job_name, node_count, gpus_per_node, total_gpus, image, quick_mode
        )

        if use_cd:
            yaml_content = self._add_compute_domain(
                yaml_content, job_name, cd_name, cd_channel_name, node_count, gpus_per_node
            )

        self.log.info(f"Deploying MPIJob {job_name} in namespace {namespace}...")
        try:
            result = subprocess.run(
                get_kubectl_command() + ["apply", "-f", "-", "-n", namespace],
                input=yaml_content,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                self.set_failed(f"Failed to create MPIJob: {result.stderr}")
                return
        except Exception as e:
            self.set_failed(f"Exception creating MPIJob: {e}")
            return

        try:
            self._wait_and_report(job_name, namespace, job_timeout, min_bus_bw, node_count, total_gpus)
        finally:
            self.log.info(f"Cleaning up MPIJob {job_name}...")
            run_kubectl(["delete", "mpijob", job_name, "-n", namespace, "--ignore-not-found=true"])
            if use_cd:
                self.log.info(f"Cleaning up ComputeDomain {cd_name}...")
                run_kubectl(["delete", "computedomain", cd_name, "-n", namespace, "--ignore-not-found=true"])

    def _check_mpi_operator(self) -> bool:
        """Check if the Kubeflow MPI Operator CRD is installed."""
        result = run_kubectl(["api-resources", "--api-group=kubeflow.org", "-o", "name"], timeout=10)
        if result.returncode != 0:
            return False
        return "mpijobs.kubeflow.org" in result.stdout

    def _resolve_topology(self, gpu_nodes: list[str]) -> tuple[int, int]:
        """Determine node count and GPUs per node from config or auto-detection.

        Returns:
            (node_count, gpus_per_node) tuple.
        """
        nodes_config = self.config.get("nodes")
        if nodes_config is not None:
            node_count = int(nodes_config)
        else:
            node_count = get_nccl_multinode_nodes()

        if len(gpu_nodes) < node_count:
            self.log.warning(
                f"Requested {node_count} nodes but only {len(gpu_nodes)} GPU nodes available, using {len(gpu_nodes)}"
            )
            node_count = len(gpu_nodes)

        gpus_config = self.config.get("gpus_per_node")
        if gpus_config is not None:
            gpus_per_node = int(gpus_config)
        else:
            detected = get_node_gpu_count(gpu_nodes[0])
            if detected > 0:
                gpus_per_node = detected
                self.log.info(f"Auto-detected {gpus_per_node} GPUs per node from {gpu_nodes[0]}")
            else:
                gpus_per_node = get_nccl_multinode_gpus_per_node()
                self.log.warning(f"Could not detect GPUs per node, using default: {gpus_per_node}")

        return node_count, gpus_per_node

    def _patch_manifest(
        self,
        yaml_content: str,
        job_name: str,
        node_count: int,
        gpus_per_node: int,
        total_gpus: int,
        image: str,
        quick_mode: bool = False,
    ) -> str:
        """Replace placeholder values in the MPIJob manifest."""
        yaml_content = yaml_content.replace("name: nccl-allreduce-multinode", f"name: {job_name}", 1)
        yaml_content = yaml_content.replace("slotsPerWorker: 4", f"slotsPerWorker: {gpus_per_node}")
        yaml_content = yaml_content.replace("replicas: 2", f"replicas: {node_count}")
        yaml_content = yaml_content.replace("nvidia.com/gpu: 4", f"nvidia.com/gpu: {gpus_per_node}")
        yaml_content = yaml_content.replace("-np 8", f"-np {total_gpus}")
        if image != "nvcr.io/nvidia/hpc-benchmarks:25.04":
            yaml_content = yaml_content.replace("nvcr.io/nvidia/hpc-benchmarks:25.04", image)
        if quick_mode:
            yaml_content = yaml_content.replace("-b 8 -e 4G -f 2", "-b 1M -e 256M -f 2")
        return yaml_content

    def _resolve_compute_domain_mode(self) -> bool:
        """Determine whether to use ComputeDomain for MNNVL/IMEX.

        Checks the ``use_compute_domain`` config option:
        - ``"auto"`` (default): enables ComputeDomain if the DRA driver CRD is present.
        - ``"true"``: always enable (fails if CRD is missing).
        - ``"false"``: never enable.

        Returns:
            True if ComputeDomain should be used.
        """
        mode = str(self.config.get("use_compute_domain", "auto")).lower()

        if mode == "false":
            return False

        has_cd = self._has_compute_domain_support()

        if mode == "true" and not has_cd:
            self.log.warning(
                "use_compute_domain=true but ComputeDomain CRD not found. "
                "Install the NVIDIA DRA driver for GPUs to enable MNNVL."
            )

        if mode == "auto":
            if has_cd:
                self.log.info("ComputeDomain CRD detected - enabling MNNVL/IMEX for NVLink fabric access")
            return has_cd

        return has_cd

    def _has_compute_domain_support(self) -> bool:
        """Check if the NVIDIA DRA driver ComputeDomain CRD is registered."""
        result = run_kubectl(["api-resources", "--api-group=resource.nvidia.com", "-o", "name"], timeout=10)
        if result.returncode != 0:
            return False
        return "computedomains.resource.nvidia.com" in result.stdout

    def _add_compute_domain(
        self,
        yaml_content: str,
        job_name: str,
        cd_name: str,
        cd_channel_name: str,
        node_count: int,
        gpus_per_node: int,
    ) -> str:
        """Prepend a ComputeDomain resource and add DRA claims to the MPIJob.

        Modifies the manifest to:
        1. Prepend a ComputeDomain resource for IMEX channel allocation
        2. Add resourceClaims to the Worker container resources
        3. Add resourceClaims to the Worker pod spec

        The DRA controller handles topology-aware scheduling via the
        ComputeDomain, so explicit podAffinity is not needed.
        """
        cd_yaml = _COMPUTE_DOMAIN_TEMPLATE.format(
            cd_name=cd_name,
            num_nodes=node_count,
            cd_channel_name=cd_channel_name,
        )

        # Add resource claim to worker container's resources (alongside GPU limit)
        yaml_content = yaml_content.replace(
            f"nvidia.com/gpu: {gpus_per_node}",
            f"nvidia.com/gpu: {gpus_per_node}\n                claims:\n                  - name: cd-channel",
        )

        # Add resourceClaims at Worker pod spec level (after volumes section)
        yaml_content = yaml_content.replace(
            "                sizeLimit: 8Gi",
            "                sizeLimit: 8Gi\n"
            "          resourceClaims:\n"
            "            - name: cd-channel\n"
            f"              resourceClaimTemplateName: {cd_channel_name}",
        )

        return cd_yaml + yaml_content

    def _wait_and_report(
        self,
        job_name: str,
        namespace: str,
        timeout: int,
        min_bus_bw: float,
        node_count: int,
        total_gpus: int,
    ) -> None:
        """Wait for MPIJob launcher completion, collect logs, and report."""
        startup_timeout = int(self.config.get("startup_timeout", 300))
        launcher_pod = self._wait_for_launcher_pod(job_name, namespace, timeout=startup_timeout)
        if not launcher_pod:
            self._dump_debug_info(job_name, namespace)
            self.set_failed(f"Launcher pod for MPIJob {job_name} not found within {startup_timeout}s")
            return

        self.log.info(f"Launcher pod: {launcher_pod}, waiting for completion (timeout: {timeout}s)...")

        completed, phase = self._wait_for_mpijob_completion(launcher_pod, job_name, namespace, timeout)

        # Collect logs before cleanup - pods still exist with cleanPodPolicy: Running
        logs = get_pod_logs(launcher_pod, namespace, container="launcher", timeout=60)

        if not completed:
            self._dump_debug_info(job_name, namespace)
            self.set_failed(
                f"MPIJob {job_name} timed out after {timeout}s (launcher phase: {phase})",
                output=logs,
            )
            return

        if phase != "Succeeded":
            self._dump_debug_info(job_name, namespace)
            self.set_failed(f"MPIJob {job_name} failed (launcher phase: {phase})", output=logs)
            return

        self._check_and_report(logs, min_bus_bw, node_count, total_gpus, job_name)

    def _wait_for_launcher_pod(self, job_name: str, namespace: str, timeout: int = 120) -> str | None:
        """Wait for the MPIJob launcher pod to appear and return its name.

        Searches by job-name label, then identifies the launcher pod by name
        suffix (works across MPI Operator versions regardless of label casing).
        """
        label_selector = f"{_MPIJOB_LABEL_JOB_NAME}={job_name}"
        launcher_prefix = f"{job_name}-launcher-"
        start = time.time()
        while time.time() - start < timeout:
            result = run_kubectl(
                [
                    "get",
                    "pods",
                    "-n",
                    namespace,
                    "-l",
                    label_selector,
                    "-o",
                    "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
                ]
            )
            if result.returncode == 0 and result.stdout.strip():
                for pod_name in result.stdout.strip().split("\n"):
                    if pod_name.startswith(launcher_prefix):
                        return pod_name
            time.sleep(5)
        return None

    def _wait_for_mpijob_completion(
        self,
        launcher_pod: str,
        job_name: str,
        namespace: str,
        timeout: int,
    ) -> tuple[bool, str]:
        """Wait for MPIJob completion, checking launcher pod and MPIJob conditions.

        Checks three signals each iteration:
        1. Launcher pod phase (Succeeded/Failed)
        2. MPIJob status conditions (catches BackoffLimitExceeded, etc.)
        3. Worker pod health (catches StartError, ImagePullBackOff)

        Returns:
            (completed, phase) tuple matching wait_for_pod_completion semantics.
        """
        label_selector = f"{_MPIJOB_LABEL_JOB_NAME}={job_name}"
        worker_prefix = f"{job_name}-worker-"
        start = time.time()
        last_phase = "Unknown"

        while time.time() - start < timeout:
            # 1. Check launcher pod phase and container state
            result = run_kubectl(
                [
                    "get",
                    "pod",
                    launcher_pod,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.status.phase}{'\\t'}"
                    "{.status.containerStatuses[0].state}{'\\t'}"
                    "{.status.containerStatuses[0].restartCount}",
                ]
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split("\t")
                last_phase = parts[0] if parts else "Unknown"
                container_state = parts[1] if len(parts) > 1 else ""
                restart_count = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

                if last_phase in ("Succeeded", "Failed"):
                    return True, last_phase

                if "CrashLoopBackOff" in container_state or restart_count >= 3:
                    self.log.error(f"Launcher pod crash-looping (restarts: {restart_count})")
                    return True, "Failed"
            elif result.returncode != 0:
                # Pod may have been deleted - check MPIJob directly
                pass

            # 2. Check MPIJob status conditions directly
            mpijob_failed = self._check_mpijob_conditions(job_name, namespace)
            if mpijob_failed:
                self.log.error(f"MPIJob failed: {mpijob_failed}")
                return True, "Failed"

            # 3. Check worker pods for unrecoverable errors
            worker_error = self._check_worker_health(label_selector, worker_prefix, namespace)
            if worker_error:
                self.log.error(f"Worker failure detected: {worker_error}")
                self._dump_debug_info(job_name, namespace)
                return True, "Failed"

            time.sleep(5)

        return False, last_phase

    def _check_mpijob_conditions(self, job_name: str, namespace: str) -> str | None:
        """Check MPIJob status conditions for terminal failure.

        Returns:
            Error message if MPIJob has failed, None otherwise.
        """
        result = run_kubectl(
            [
                "get",
                "mpijob",
                job_name,
                "-n",
                namespace,
                "-o",
                "jsonpath={range .status.conditions[*]}"
                "{.type}{'\\t'}{.status}{'\\t'}{.reason}{'\\t'}{.message}{'\\n'}{end}",
            ]
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        for line in result.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            cond_type, status, reason = parts[0], parts[1], parts[2]
            message = parts[3] if len(parts) > 3 else ""
            if cond_type == "Failed" and status == "True":
                return f"{reason}: {message}"

        return None

    def _check_worker_health(self, label_selector: str, worker_prefix: str, namespace: str) -> str | None:
        """Check if any worker pods are in an unrecoverable error state.

        Returns:
            Error description if workers have failed, None if healthy or pending.
        """
        result = run_kubectl(
            [
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                label_selector,
                "-o",
                "jsonpath={range .items[*]}"
                "{.metadata.name}{'\\t'}{.status.phase}{'\\t'}"
                "{.status.containerStatuses[0].state}{'\\n'}{end}",
            ]
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        failed_workers: list[str] = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            pod_name, phase = parts[0], parts[1]
            if not pod_name.startswith(worker_prefix):
                continue
            state_info = parts[2] if len(parts) > 2 else ""

            # StartError and ImagePullBackOff are unrecoverable without intervention
            if phase == "Failed" or "StartError" in state_info or "ImagePullBackOff" in state_info:
                failed_workers.append(f"{pod_name}: phase={phase} {state_info}")

        if failed_workers:
            return "Worker pods failed:\n  " + "\n  ".join(failed_workers)
        return None

    def _dump_debug_info(self, job_name: str, namespace: str) -> None:
        """Log debug info on failure to help troubleshoot."""
        label_selector = f"{_MPIJOB_LABEL_JOB_NAME}={job_name}"
        result = run_kubectl(["get", "pods", "-n", namespace, "-l", label_selector, "-o", "wide"])
        if result.returncode == 0:
            self.log.error(f"MPIJob pods:\n{result.stdout}")

        # Get worker pod events/describe for StartError diagnosis
        result = run_kubectl(
            [
                "describe",
                "pods",
                "-n",
                namespace,
                "-l",
                label_selector,
            ]
        )
        if result.returncode == 0:
            self.log.error(f"Pod details:\n{result.stdout}")

        result = run_kubectl(["describe", "mpijob", job_name, "-n", namespace])
        if result.returncode == 0:
            self.log.error(f"MPIJob description:\n{result.stdout}")

    def _check_and_report(
        self,
        logs: str,
        min_bus_bw: float,
        node_count: int,
        total_gpus: int,
        job_name: str,
    ) -> None:
        """Parse NCCL output, check thresholds, and report results."""
        nccl = parse_nccl_output(logs)

        if not nccl.success:
            self.set_failed(nccl.error, output=nccl.output)
            return

        if min_bus_bw > 0 and nccl.avg_bus_bw_gbps < min_bus_bw:
            self.set_failed(
                f"Bus bandwidth {nccl.avg_bus_bw_gbps:.2f} GB/s below minimum threshold {min_bus_bw} GB/s",
                output=logs,
            )
            return

        oob_str = str(nccl.out_of_bounds) if nccl.out_of_bounds >= 0 else "N/A"
        msg = (
            f"NCCL multi-node test passed (MPIJob {job_name})\n"
            f"  Nodes: {node_count}\n"
            f"  Total GPUs: {total_gpus}\n"
            f"  Average Bus Bandwidth: {nccl.avg_bus_bw_gbps:.2f} GB/s\n"
            f"  Max Bus Bandwidth: {nccl.max_bus_bw_gbps:.2f} GB/s\n"
            f"  Out of Bounds: {oob_str}"
        )
        if min_bus_bw > 0:
            msg += f"\n  Minimum Required: {min_bus_bw} GB/s"

        self.set_passed(msg)
