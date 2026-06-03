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

"""NIM Helm Chart Deployment Workload with GenAI-Perf KPIs.

This workload will:
- Deploy NIM using Helm chart to Kubernetes cluster
- Run inference validation and collect performance KPIs
- Uses GenAI-Perf for comprehensive performance testing

Reference: https://docs.nvidia.com/nim/large-language-models/latest/deploy-helm.html
"""

import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from isvtest.config.settings import get_k8s_namespace
from isvtest.core.k8s import get_gpu_nodes, get_kubectl_base_shell, get_kubectl_command
from isvtest.core.ngc import get_ngc_api_key, validate_nim_inference
from isvtest.core.workload import BaseWorkloadCheck


@dataclass
class NimPerfMetrics:
    """Performance metrics collected from GenAI-Perf."""

    request_throughput: float | None = None  # requests/sec
    output_token_throughput: float | None = None  # tokens/sec
    time_to_first_token_avg: float | None = None  # ms
    time_to_first_token_p99: float | None = None  # ms
    inter_token_latency_avg: float | None = None  # ms
    inter_token_latency_p99: float | None = None  # ms
    request_latency_avg: float | None = None  # ms
    request_latency_p99: float | None = None  # ms

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary for reporting."""
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def summary(self) -> str:
        """Return human-readable summary of metrics."""
        lines = ["=== NIM Performance Metrics ==="]
        if self.request_throughput:
            lines.append(f"Request Throughput: {self.request_throughput:.2f} req/sec")
        if self.output_token_throughput:
            lines.append(f"Output Token Throughput: {self.output_token_throughput:.2f} tokens/sec")
        if self.time_to_first_token_avg:
            lines.append(f"Time to First Token (avg): {self.time_to_first_token_avg:.2f} ms")
        if self.time_to_first_token_p99:
            lines.append(f"Time to First Token (p99): {self.time_to_first_token_p99:.2f} ms")
        if self.inter_token_latency_avg:
            lines.append(f"Inter-Token Latency (avg): {self.inter_token_latency_avg:.2f} ms")
        if self.request_latency_avg:
            lines.append(f"Request Latency (avg): {self.request_latency_avg:.2f} ms")
        if self.request_latency_p99:
            lines.append(f"Request Latency (p99): {self.request_latency_p99:.2f} ms")
        return "\n".join(lines)


class K8sNimHelmWorkload(BaseWorkloadCheck):
    """Deploy NIM via Helm chart and validate with GenAI-Perf.

    This workload:
    1. Adds NVIDIA NIM Helm repository
    2. Deploys NIM using helm install
    3. Waits for NIM service to be ready
    4. Runs GenAI-Perf to collect performance metrics
    5. Validates inference correctness
    6. Reports KPIs (latency, throughput, tokens/sec)
    7. Cleans up resources

    Configuration options (via YAML or environment):
        model: NIM model to deploy (default: meta/llama-3.2-3b-instruct)
        timeout: Total timeout in seconds (default: 1800 = 30 min)
        gpu_count: Number of GPUs for NIM (default: 1)
        genai_perf_requests: Number of requests for performance test (default: 100)
        genai_perf_concurrency: Concurrent requests (default: 1)
    """

    description = "Deploy NIM using Helm chart and validate with GenAI-Perf."
    # NIM Helm chart configuration
    # Reference: https://docs.nvidia.com/nim/large-language-models/latest/deploy-helm.html
    # The chart is downloaded directly from NGC, not from a Helm repository
    HELM_CHART_BASE_URL = "https://helm.ngc.nvidia.com/nim/charts"
    HELM_CHART_NAME = "nim-llm"
    HELM_CHART_VERSION = "1.15.1"  # Update as needed

    # Default model - Llama 3.2 3B is fast and requires ~8GB VRAM
    DEFAULT_MODEL = "meta/llama-3.2-3b-instruct"
    DEFAULT_MODEL_TAG = "latest"

    # GenAI-Perf container (Triton SDK includes genai-perf)
    GENAI_PERF_IMAGE = "nvcr.io/nvidia/tritonserver:24.08-py3-sdk"

    # Local path for downloaded chart
    _chart_path: str | None = None

    def run(self) -> None:
        """Execute the NIM Helm deployment and validation.

        Config options:
            reuse_deployment: Service name of existing NIM deployment to reuse (skips deploy/cleanup)
            skip_cleanup: If True, don't delete the NIM deployment after test (for dev iteration)
        """
        namespace = get_k8s_namespace()
        timeout = self.config.get("timeout", 1800)
        model = self.config.get("model", self.DEFAULT_MODEL)
        model_tag = self.config.get("model_tag", self.DEFAULT_MODEL_TAG)
        gpu_count = self.config.get("gpu_count", 1)

        # GenAI-Perf settings
        perf_requests = self.config.get("genai_perf_requests", 100)
        perf_concurrency = self.config.get("genai_perf_concurrency", 1)

        # Dev options
        reuse_deployment = self.config.get("reuse_deployment")  # e.g., "nim-dev-nim-llm"
        skip_cleanup = self.config.get("skip_cleanup", False)

        # Check if reusing existing deployment
        if reuse_deployment:
            self.log.info(f"Reusing existing NIM deployment: {reuse_deployment}")
            self.log.info("  Skipping deployment and cleanup steps")
            service_name = reuse_deployment
            release_name = None  # No cleanup needed

            # Just verify the service exists
            kubectl_base = get_kubectl_base_shell()
            result = self.run_command(f"{kubectl_base} get svc {service_name} -n {namespace}")
            if result.exit_code != 0:
                self.set_failed(f"Service {service_name} not found in namespace {namespace}")
                return

            nim_url = f"http://{service_name}:8000"  # Short name works in same namespace

            # Run inference validation (using shared utility)
            success, message = validate_nim_inference(nim_url, namespace, model)
            if not success:
                self.set_failed(f"Inference validation failed: {message}")
                return
            self.log.info(message)

            # Run GenAI-Perf benchmark
            metrics = self._run_genai_perf(
                nim_url=nim_url,
                namespace=namespace,
                model=model,
                num_requests=perf_requests,
                concurrency=perf_concurrency,
            )

            # Report results
            if metrics:
                self.log.info(metrics.summary())
                self.set_passed(f"NIM Helm workload passed!\n{metrics.summary()}")
            else:
                self.set_passed("NIM inference validated (GenAI-Perf metrics unavailable)")
            return

        # Full deployment flow
        # Early check: Skip if Helm is not installed
        if not self._is_helm_available():
            self.log.warning("Skipping: Helm is not installed or not in PATH")
            pytest.skip("Helm is not installed or not in PATH")

        # Verify prerequisites (NGC credentials, secrets)
        if not self._check_prerequisites(namespace):
            return

        # Verify GPU nodes available
        nodes = get_gpu_nodes()
        if not nodes:
            self.log.warning("Skipping: No GPU nodes found in cluster")
            pytest.skip("No GPU nodes found in cluster")

        # Generate unique release name
        release_name = f"nim-bench-{uuid.uuid4().hex[:8]}"
        service_name = f"{release_name}-nim-llm"

        self.log.info("Starting NIM Helm workload")
        self.log.info(f"  Model: {model}:{model_tag}")
        self.log.info(f"  GPUs: {gpu_count}")
        self.log.info(f"  Timeout: {timeout}s")
        if skip_cleanup:
            self.log.info(f"  Skip cleanup: enabled (service will remain: {service_name})")

        try:
            # Step 1: Add Helm repo
            if not self._setup_helm_repo():
                return

            # Step 2: Deploy NIM via Helm
            if not self._deploy_nim_helm(release_name, namespace, model, model_tag, gpu_count):
                return

            # Step 3: Wait for NIM to be ready
            if not self._wait_for_nim_ready(service_name, namespace, timeout):
                self._dump_helm_status(release_name, namespace)
                return

            # Step 4: Run basic inference validation (using shared utility)
            nim_url = f"http://{service_name}:8000"  # Short name works in same namespace
            success, message = validate_nim_inference(nim_url, namespace, model)
            if not success:
                self.set_failed(f"Inference validation failed: {message}")
                return
            self.log.info(message)

            # Step 5: Run GenAI-Perf benchmark
            metrics = self._run_genai_perf(
                nim_url=nim_url,
                namespace=namespace,
                model=model,
                num_requests=perf_requests,
                concurrency=perf_concurrency,
            )

            # Step 6: Report results
            if metrics:
                self.log.info(metrics.summary())
                self.set_passed(f"NIM Helm workload passed!\n{metrics.summary()}")
            else:
                self.set_passed("NIM Helm deployment and inference validated (GenAI-Perf metrics unavailable)")

        finally:
            # Step 7: Cleanup (unless skip_cleanup is set)
            if not skip_cleanup:
                self._cleanup_helm(release_name, namespace)
            else:
                self.log.info(f"Skipping cleanup - NIM deployment remains running as: {service_name}")
                self.log.info(f'  To reuse: reuse_deployment: "{service_name}"')
                self.log.info(f"  To cleanup manually: helm uninstall {release_name} -n {namespace}")

    def _is_helm_available(self) -> bool:
        """Check if Helm CLI is available."""
        result = self.run_command("helm version --short")
        return result.exit_code == 0

    def _check_prerequisites(self, namespace: str) -> bool:
        """Check that required secrets and tools are available.

        Creates two secrets as per NVIDIA NIM Helm docs:
        1. ngc-secret: docker-registry secret for pulling NIM images from nvcr.io
        2. ngc-api: generic secret with NGC_API_KEY key (value from NGC_API_KEY env var)

        Reference: https://docs.nvidia.com/nim/large-language-models/latest/deploy-helm.html
        """
        ngc_api_key = get_ngc_api_key()

        if not ngc_api_key:
            self.log.warning("Skipping: NGC_API_KEY not set - NGC credentials required")
            pytest.skip("NGC_API_KEY not set - NGC credentials required")

        kubectl_parts = get_kubectl_command()
        kubectl_base = get_kubectl_base_shell()

        # Create namespace if it doesn't exist
        self.run_command(
            f"{kubectl_base} create namespace {namespace} --dry-run=client -o yaml | {kubectl_base} apply -f -"
        )

        # Create docker-registry secret for image pulls (ngc-secret)
        # Always recreate to ensure credentials are fresh
        res = self.run_command(f"{kubectl_base} get secret ngc-secret -n {namespace}")
        if res.exit_code == 0:
            self.log.info("Refreshing NGC image pull secret (ngc-secret)...")
            self.run_command(f"{kubectl_base} delete secret ngc-secret -n {namespace}")
        else:
            self.log.info("Creating NGC image pull secret (ngc-secret)...")

        try:
            # Use list form (no shell) to pass credentials directly and securely
            cmd = kubectl_parts + [
                "create",
                "secret",
                "docker-registry",
                "ngc-secret",
                "--docker-server=nvcr.io",
                "--docker-username=$oauthtoken",  # Literal string - NGC special username
                f"--docker-password={ngc_api_key}",
                "-n",
                namespace,
            ]
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except Exception as e:
            self.set_failed(f"Failed to create NGC image pull secret: {e}")
            return False

        # Create generic secret for NGC API key (ngc-api)
        # Always recreate to ensure credentials are fresh
        res = self.run_command(f"{kubectl_base} get secret ngc-api -n {namespace}")
        if res.exit_code == 0:
            self.log.info("Refreshing NGC API secret (ngc-api)...")
            self.run_command(f"{kubectl_base} delete secret ngc-api -n {namespace}")
        else:
            self.log.info("Creating NGC API secret (ngc-api)...")

        try:
            # Use list form (no shell) to pass credentials directly and securely
            cmd = kubectl_parts + [
                "create",
                "secret",
                "generic",
                "ngc-api",
                f"--from-literal=NGC_API_KEY={ngc_api_key}",  # Key name required by NIM Helm chart
                "-n",
                namespace,
            ]
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except Exception as e:
            self.set_failed(f"Failed to create NGC API secret: {e}")
            return False

        return True

    def _setup_helm_repo(self) -> bool:
        """Download NIM Helm chart from NGC.

        The NIM Helm chart requires NGC authentication and is downloaded directly
        as a .tgz file, not from a public Helm repository.
        Reference: https://docs.nvidia.com/nim/large-language-models/latest/deploy-helm.html
        """
        ngc_api_key = get_ngc_api_key()
        if not ngc_api_key:
            self.set_failed("NGC_API_KEY is required to download NIM Helm chart")
            return False

        chart_version = self.config.get("helm_chart_version", self.HELM_CHART_VERSION)
        chart_url = f"{self.HELM_CHART_BASE_URL}/{self.HELM_CHART_NAME}-{chart_version}.tgz"
        chart_filename = f"{self.HELM_CHART_NAME}-{chart_version}.tgz"

        self.log.info(f"Downloading NIM Helm chart: {chart_url}")

        # Use helm pull to download the chart with authentication
        # helm fetch/pull supports --username and --password for authenticated downloads
        try:
            result = subprocess.run(
                [
                    "helm",
                    "pull",
                    chart_url,
                    "--username",
                    "$oauthtoken",
                    "--password",
                    ngc_api_key,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                self.set_failed(f"Failed to download Helm chart: {result.stderr}")
                return False

            # Store the chart path for later use
            self._chart_path = chart_filename
            self.log.info(f"Helm chart downloaded: {chart_filename}")
            return True

        except subprocess.TimeoutExpired:
            self.set_failed("Helm chart download timed out")
            return False
        except Exception as e:
            self.set_failed(f"Failed to download Helm chart: {e}")
            return False

    def _deploy_nim_helm(self, release_name: str, namespace: str, model: str, model_tag: str, gpu_count: int) -> bool:
        """Deploy NIM using downloaded Helm chart.

        Reference: https://docs.nvidia.com/nim/large-language-models/latest/deploy-helm.html
        """
        self.log.info(f"Deploying NIM release: {release_name}")

        if not self._chart_path:
            self.set_failed("Helm chart not downloaded")
            return False

        # Build helm install command with values
        # Reference: https://docs.nvidia.com/nim/large-language-models/latest/deploy-helm.html
        helm_cmd = [
            "helm",
            "install",
            release_name,
            self._chart_path,  # Use local chart file
            "--namespace",
            namespace,
            "--create-namespace",
            "--set",
            f"image.repository=nvcr.io/nim/{model}",
            "--set",
            f"image.tag={model_tag}",
            "--set",
            f"resources.limits.nvidia\\.com/gpu={gpu_count}",
            "--set",
            "model.ngcAPISecret=ngc-api",  # Secret name from _check_prerequisites
            "--set",
            "imagePullSecrets[0].name=ngc-secret",  # Image pull secret
            "--set",
            "persistence.enabled=true",
            # Target GPU nodes using node selector (use --set-string to avoid bool conversion)
            "--set-string",
            "nodeSelector.nvidia\\.com/gpu\\.present=true",
            # Note: Don't use --wait here, _wait_for_nim_ready() handles waiting with progress logging
        ]

        try:
            result = subprocess.run(
                helm_cmd,
                capture_output=True,
                text=True,
                timeout=120,  # 2 min for helm install (actual waiting done in _wait_for_nim_ready)
            )

            if result.returncode != 0:
                self.set_failed(f"Helm install failed: {result.stderr}")
                return False

            self.log.info("NIM Helm deployment initiated successfully")
            return True

        except subprocess.TimeoutExpired:
            self.set_failed("Helm install timed out after 2 minutes")
            return False
        except Exception as e:
            self.set_failed(f"Helm install exception: {e}")
            return False

    def _wait_for_nim_ready(self, service_name: str, namespace: str, timeout: int) -> bool:
        """Wait for NIM deployment to be ready and serving."""
        self.log.info(f"Waiting for NIM service {service_name} to be ready...")

        kubectl_base = get_kubectl_base_shell()
        release_name = service_name.replace("-nim-llm", "")

        start_time = time.time()
        check_interval = 30

        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)

            # Check for fatal errors (ImagePullBackOff, ErrImagePull, etc.) - fail fast
            fatal_error = self._check_for_fatal_pod_errors(release_name, namespace, kubectl_base)
            if fatal_error:
                self.set_failed(f"NIM deployment failed: {fatal_error}")
                return False

            # Check pod status
            cmd = f"{kubectl_base} get pods -n {namespace} -l app.kubernetes.io/instance={release_name} -o jsonpath='{{.items[*].status.phase}}'"
            result = self.run_command(cmd)

            if result.exit_code == 0:
                phase = result.stdout.strip()

                if phase == "Running":
                    # Check readiness via service endpoint
                    health_cmd = f"{kubectl_base} run nim-health-check-{uuid.uuid4().hex[:6]} --rm -i --restart=Never --image=curlimages/curl:latest -n {namespace} -- curl -s -o /dev/null -w '%{{http_code}}' http://{service_name}:8000/v1/health/ready"
                    health_result = self.run_command(health_cmd)

                    if health_result.exit_code == 0 and "200" in health_result.stdout:
                        self.log.info(f"NIM is ready after {elapsed}s")
                        return True

                self.log.info(f"  NIM status: {phase} ({elapsed}s elapsed)")

            time.sleep(check_interval)

        self.set_failed(f"NIM failed to become ready within {timeout}s")
        return False

    def _check_for_fatal_pod_errors(self, release_name: str, namespace: str, kubectl_base: str) -> str | None:
        """Check for fatal pod errors that should cause immediate failure.

        Returns error message if fatal error found, None otherwise.
        """
        # Check container statuses for fatal waiting reasons
        cmd = f"{kubectl_base} get pods -n {namespace} -l app.kubernetes.io/instance={release_name} -o jsonpath='{{.items[*].status.containerStatuses[*].state.waiting.reason}}'"
        result = self.run_command(cmd)

        if result.exit_code == 0 and result.stdout.strip():
            waiting_reasons = result.stdout.strip().split()
            fatal_reasons = {
                "ImagePullBackOff",
                "ErrImagePull",
                "InvalidImageName",
                "CreateContainerConfigError",
                "CrashLoopBackOff",
            }

            for reason in waiting_reasons:
                if reason in fatal_reasons:
                    error_msg = f"{reason}"

                    # For CrashLoopBackOff, get logs to understand why
                    if reason == "CrashLoopBackOff":
                        logs_cmd = f"{kubectl_base} logs -n {namespace} {release_name}-nim-llm-0 --tail=20"
                        logs_result = self.run_command(logs_cmd)
                        if logs_result.exit_code == 0 and logs_result.stdout.strip():
                            # Look for common GPU issues in logs
                            logs = logs_result.stdout
                            if "NVIDIA Driver was not detected" in logs:
                                error_msg += " - NVIDIA Driver not detected on node (check GPU node labels and drivers)"
                            elif "Failed to dlopen libcuda.so" in logs:
                                error_msg += " - CUDA library not found (node may not have working GPU drivers)"
                            else:
                                # Include last few lines of logs
                                last_lines = logs.strip().split("\n")[-3:]
                                error_msg += f" - {' | '.join(last_lines)[:200]}"
                    else:
                        # Get more details from events for other errors
                        events_cmd = f"{kubectl_base} get events -n {namespace} --field-selector involvedObject.name={release_name}-nim-llm-0 --sort-by='.lastTimestamp' -o jsonpath='{{.items[-1].message}}'"
                        events_result = self.run_command(events_cmd)
                        event_msg = events_result.stdout.strip() if events_result.exit_code == 0 else ""
                        if event_msg:
                            error_msg += f" - {event_msg[:200]}"

                    self.log.error(f"Fatal pod error detected: {error_msg}")
                    return error_msg

        return None

    def _run_genai_perf(
        self,
        nim_url: str,
        namespace: str,
        model: str,
        num_requests: int = 100,
        concurrency: int = 1,
    ) -> NimPerfMetrics | None:
        """Run GenAI-Perf benchmark and collect metrics."""
        self.log.info(f"Running GenAI-Perf benchmark ({num_requests} requests, concurrency={concurrency})")

        job_name = f"genai-perf-{uuid.uuid4().hex[:8]}"
        kubectl_parts = get_kubectl_command()
        kubectl_base = get_kubectl_base_shell()

        # Create GenAI-Perf job manifest
        job_yaml = self._create_genai_perf_job_yaml(
            job_name=job_name,
            nim_url=nim_url,
            model=model,
            num_requests=num_requests,
            concurrency=concurrency,
        )

        try:
            # Apply job
            result = subprocess.run(
                kubectl_parts + ["apply", "-f", "-", "-n", namespace],
                input=job_yaml,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                self.log.error(f"Failed to create GenAI-Perf job: {result.stderr}")
                return None

            # Wait for job completion (allow 10 min for benchmarking)
            self.log.info("Waiting for GenAI-Perf benchmark to complete...")
            start_time = time.time()
            timeout = 600
            job_completed = False
            artifacts_copied = False

            while time.time() - start_time < timeout:
                time.sleep(15)
                elapsed = int(time.time() - start_time)

                # Check job status
                cmd = f"{kubectl_base} get job {job_name} -n {namespace} -o jsonpath='{{.status.succeeded}},{{.status.failed}},{{.status.active}}'"
                result = self.run_command(cmd)

                if result.exit_code == 0:
                    status_parts = result.stdout.strip().split(",")
                    succeeded = status_parts[0] if len(status_parts) > 0 else ""
                    failed = status_parts[1] if len(status_parts) > 1 else ""
                    active = status_parts[2] if len(status_parts) > 2 else ""

                    self.log.info(
                        f"  GenAI-Perf status: succeeded={succeeded}, failed={failed}, active={active} ({elapsed}s elapsed)"
                    )

                    if succeeded == "1":
                        job_completed = True
                        self.log.info(f"GenAI-Perf job completed successfully after {elapsed}s")
                        break
                    # Check if benchmark is done but pod still sleeping - copy artifacts now
                    if active == "1" and not artifacts_copied:
                        log_cmd = f"{kubectl_base} logs job/{job_name} -n {namespace}"
                        log_result = self.run_command(log_cmd)
                        if log_result.exit_code == 0 and "=== Benchmark Complete ===" in log_result.stdout:
                            self.log.info("Benchmark complete, copying artifacts while pod is still alive...")
                            self._copy_genai_perf_artifacts(job_name, namespace, kubectl_base)
                            artifacts_copied = True
                    if failed == "1":
                        self.log.error("GenAI-Perf job failed")
                        # Get logs for debugging before returning
                        cmd = f"{kubectl_base} logs job/{job_name} -n {namespace}"
                        log_result = self.run_command(cmd)
                        if log_result.exit_code == 0:
                            self.log.error(f"GenAI-Perf failure logs:\n{log_result.stdout[:2000]}")
                        return None

            if not job_completed:
                self.log.error(f"GenAI-Perf job timed out after {timeout}s")
                return None

            # Get logs and parse metrics
            cmd = f"{kubectl_base} logs job/{job_name} -n {namespace}"
            result = self.run_command(cmd)

            if result.exit_code != 0:
                self.log.error(f"Failed to get GenAI-Perf logs: {result.stderr}")
                return None

            # Log raw output for debugging
            self.log.info(f"GenAI-Perf raw output (first 3000 chars):\n{result.stdout[:3000]}")

            metrics = self._parse_genai_perf_output(result.stdout)
            if not metrics:
                self.log.warning(f"Full GenAI-Perf output for debugging:\n{result.stdout}")

            return metrics

        finally:
            # Cleanup job
            self.run_command(f"{kubectl_base} delete job {job_name} -n {namespace} --wait=false")

    def _copy_genai_perf_artifacts(self, job_name: str, namespace: str, kubectl_base: str) -> None:
        """Copy GenAI-Perf artifacts from pod to local _output directory."""
        try:
            # Get the pod name for the job
            cmd = f"{kubectl_base} get pods -l job-name={job_name} -n {namespace} -o jsonpath='{{.items[0].metadata.name}}'"
            result = self.run_command(cmd)
            if result.exit_code != 0 or not result.stdout.strip():
                self.log.warning("Could not find GenAI-Perf pod to copy artifacts")
                return

            pod_name = result.stdout.strip()
            output_dir = "_output/genai-perf"
            os.makedirs(output_dir, exist_ok=True)

            # Copy artifacts directory from pod
            self.log.info(f"Copying GenAI-Perf artifacts from pod {pod_name}...")
            cmd = f"{kubectl_base} cp {namespace}/{pod_name}:/artifacts {output_dir} --retries=3"
            result = self.run_command(cmd)

            if result.exit_code == 0:
                self.log.info(f"GenAI-Perf artifacts saved to {output_dir}/")
            else:
                self.log.warning(f"Could not copy artifacts: {result.stderr}")

        except Exception as e:
            self.log.warning(f"Failed to copy GenAI-Perf artifacts: {e}")

    def _create_genai_perf_job_yaml(
        self,
        job_name: str,
        nim_url: str,
        model: str,
        num_requests: int,
        concurrency: int,
    ) -> str:
        """Create GenAI-Perf Kubernetes Job manifest."""
        return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: genai-perf
          image: {self.GENAI_PERF_IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["/bin/bash", "-c"]
          args:
            - |
              set -e
              echo "=== GenAI-Perf Benchmark ==="
              echo "Target: {nim_url}"
              echo "Model: {model}"
              echo "Requests: {num_requests}"
              echo "Concurrency: {concurrency}"
              echo ""

              # Run genai-perf benchmark
              # Note: --backend openai-chat is deprecated, use --service-kind and --endpoint-type
              genai-perf profile \\
                --model {model} \\
                --service-kind openai \\
                --endpoint-type chat \\
                --url {nim_url} \\
                --num-prompts {num_requests} \\
                --concurrency {concurrency} \\
                --streaming \\
                --random-seed 42 \\
                --synthetic-input-tokens-mean 128 \\
                --synthetic-input-tokens-stddev 0 \\
                --output-tokens-mean 128 \\
                --output-tokens-stddev 0 \\
                --artifact-dir /artifacts \\
                2>&1 || echo "GenAI-Perf completed with warnings"

              echo ""
              echo "=== Benchmark Complete ==="

              # Keep pod alive so artifacts can be copied (copy happens during wait loop)
              sleep 30
          resources:
            requests:
              cpu: "1"
              memory: "2Gi"
            limits:
              cpu: "2"
              memory: "4Gi"
          volumeMounts:
            - name: artifacts
              mountPath: /artifacts
      volumes:
        - name: artifacts
          emptyDir: {{}}
"""

    def _parse_genai_perf_output(self, output: str) -> NimPerfMetrics | None:
        """Parse GenAI-Perf output and extract metrics."""
        metrics = NimPerfMetrics()

        try:
            # Parse request throughput
            if match := re.search(r"Request throughput.*?:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.request_throughput = float(match.group(1))

            # Parse output token throughput
            if match := re.search(r"Output token throughput.*?:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.output_token_throughput = float(match.group(1))

            # Parse time to first token
            if match := re.search(r"Time to first token.*?avg:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.time_to_first_token_avg = float(match.group(1))
            if match := re.search(r"Time to first token.*?p99:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.time_to_first_token_p99 = float(match.group(1))

            # Parse inter-token latency
            if match := re.search(r"Inter token latency.*?avg:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.inter_token_latency_avg = float(match.group(1))
            if match := re.search(r"Inter token latency.*?p99:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.inter_token_latency_p99 = float(match.group(1))

            # Parse request latency
            if match := re.search(r"Request latency.*?avg:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.request_latency_avg = float(match.group(1))
            if match := re.search(r"Request latency.*?p99:\s*([\d.]+)", output, re.IGNORECASE):
                metrics.request_latency_p99 = float(match.group(1))

            # Check if we got any metrics
            if any(v is not None for v in metrics.__dict__.values()):
                return metrics

            self.log.warning("Could not parse any metrics from GenAI-Perf output")
            self.log.debug(f"GenAI-Perf output:\n{output}")
            return None

        except Exception as e:
            self.log.error(f"Error parsing GenAI-Perf output: {e}")
            return None

    def _dump_helm_status(self, release_name: str, namespace: str) -> None:
        """Dump Helm release status for debugging."""
        self.log.error("Dumping Helm release status for debugging...")

        kubectl_base = get_kubectl_base_shell()

        self.run_command(f"helm status {release_name} -n {namespace}")
        self.run_command(f"{kubectl_base} describe pods -n {namespace} -l app.kubernetes.io/instance={release_name}")
        self.run_command(f"{kubectl_base} logs -n {namespace} -l app.kubernetes.io/instance={release_name} --tail=100")

    def _cleanup_helm(self, release_name: str, namespace: str) -> None:
        """Clean up Helm release and downloaded chart file."""
        self.log.info(f"Cleaning up Helm release: {release_name}")
        self.run_command(f"helm uninstall {release_name} -n {namespace} --wait=false")

        # Clean up downloaded chart file
        if self._chart_path:
            try:
                if os.path.exists(self._chart_path):
                    os.remove(self._chart_path)
                    self.log.info(f"Removed chart file: {self._chart_path}")
            except Exception as e:
                self.log.warning(f"Failed to remove chart file: {e}")
