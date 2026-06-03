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
from typing import Any, ClassVar

from isvtest.core.k8s import (
    KubectlParseError,
    get_kubectl_base_shell,
    get_kubectl_command,
    job_terminal_status,
    parse_kubectl_json,
    parse_kubectl_json_items,
)
from isvtest.core.runners import CommandResult, Runner
from isvtest.core.validation import BaseValidation


class BaseWorkloadCheck(BaseValidation):
    """Base class for all ISV workload validations.

    Workload validations are longer running tests that deploy workloads,
    validate functionality, or stress test the system.
    """

    # Workloads usually have longer timeouts
    timeout: ClassVar[int] = 600

    def __init__(self, runner: Runner | None = None, config: dict[str, Any] | None = None):
        super().__init__(runner, config)
        # Add workload-specific initialization here if needed

    def run_k8s_job(
        self, job_name: str, namespace: str, yaml_content: str, timeout: int = 600, wait_for_completion: bool = True
    ) -> CommandResult:
        """Helper to deploy a K8s Job, wait for it, and return logs.

        This simplifies the common pattern of:
        1. Apply Job YAML
        2. Wait for completion
        3. Get Logs
        4. Delete Job
        """
        kubectl_parts = get_kubectl_command()
        kubectl_base = get_kubectl_base_shell()

        # 1. Apply Job
        self.log.info(f"Deploying job {job_name} in namespace {namespace}...")

        # Note: We use subprocess directly here for 'apply -f -' because it involves stdin
        # Our Runner interface doesn't support stdin yet. This is an area for future improvement.
        try:
            result = subprocess.run(
                kubectl_parts + ["apply", "-f", "-", "-n", namespace],
                input=yaml_content,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return CommandResult(
                    exit_code=result.returncode,
                    stdout=result.stdout,
                    stderr=f"Failed to create job: {result.stderr}",
                    duration=0.0,
                )
        except Exception as e:
            return CommandResult(exit_code=-1, stdout="", stderr=f"Exception creating job: {e}", duration=0.0)

        if not wait_for_completion:
            return CommandResult(exit_code=0, stdout="Job created", stderr="", duration=0.0)

        # 2. Wait for completion
        self.log.info(f"Waiting for job {job_name} to complete (timeout: {timeout}s)...")
        start_time = time.time()
        end_time = start_time + timeout

        job_status = "Unknown"

        while time.time() < end_time:
            time.sleep(5)

            res = self.runner.run(f"{kubectl_base} get job {job_name} -n {namespace} -o json")
            if res.exit_code != 0:
                continue
            try:
                payload = parse_kubectl_json(res, f"job {job_name!r}")
            except KubectlParseError:
                continue
            terminal = job_terminal_status(payload)
            if terminal is not None:
                job_status = terminal
                break

        duration = time.time() - start_time

        if job_status not in ["Complete", "Failed"]:
            # Timeout
            self.log.error(f"Job {job_name} timed out after {timeout}s")
            # Try to get debug info
            res_desc = self.run_command(f"{kubectl_base} describe job {job_name} -n {namespace}")
            self.log.error(f"Job Description:\n{res_desc.stdout}")

            res_pods = self.run_command(f"{kubectl_base} get pods -n {namespace} -l job-name={job_name}")
            self.log.error(f"Job Pods:\n{res_pods.stdout}")

            # Cleanup and return failure
            self.run_command(f"{kubectl_base} delete job {job_name} -n {namespace} --wait=false")
            return CommandResult(
                exit_code=-1, stdout="", stderr=f"Job timed out in status {job_status}", duration=duration
            )

        # 3. Get Logs (from the first pod of the job)
        self.log.info(f"Collecting logs for job {job_name}...")

        # Find pod
        pod_res = self.runner.run(f"{kubectl_base} get pods -n {namespace} -l job-name={job_name} -o json")

        logs_stdout = ""
        logs_stderr = ""
        pod_name = ""
        if pod_res.exit_code == 0:
            try:
                pods = parse_kubectl_json_items(pod_res, f"pods for job {job_name!r}")
            except KubectlParseError:
                pods = []
            if pods:
                pod_name = str((pods[0].get("metadata") or {}).get("name") or "")

        if pod_name:
            logs_res = self.runner.run(f"{kubectl_base} logs {pod_name} -n {namespace}")
            logs_stdout = logs_res.stdout
            logs_stderr = logs_res.stderr
        else:
            logs_stderr = "Could not find pod for job to get logs"

        # 4. Cleanup
        self.log.info(f"Cleaning up job {job_name}...")
        self.run_command(f"{kubectl_base} delete job {job_name} -n {namespace} --wait=false")

        # Return result based on Job status
        exit_code = 0 if job_status == "Complete" else 1
        if exit_code != 0:
            logs_stderr = f"Job Failed. {logs_stderr}"

        return CommandResult(exit_code=exit_code, stdout=logs_stdout, stderr=logs_stderr, duration=duration)
