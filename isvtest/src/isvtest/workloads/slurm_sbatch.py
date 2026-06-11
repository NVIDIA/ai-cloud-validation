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

"""Slurm sbatch workload for running arbitrary batch scripts.

This module provides a workload that submits and monitors arbitrary sbatch scripts,
supporting variable substitution and job lifecycle management.
"""

import re
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from isvtest.core.slurm import (
    DEFAULT_JOB_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    MANIFESTS_DIR,
    TERMINAL_STATES,
    JobResult,
    get_job_output,
    get_job_state,
    get_partition_names,
    parse_sbatch_job_id,
)
from isvtest.core.workload import BaseWorkloadCheck


class SlurmSbatchWorkload(BaseWorkloadCheck):
    """Run an arbitrary sbatch script on a Slurm cluster.

    This workload submits a batch script via sbatch, waits for completion,
    and reports the results. Scripts can be provided inline or loaded from
    the manifests/slurm/ directory.

    Config options:
        script (str): Name of script file in manifests/slurm/ (e.g., "example_gpu_job.sbatch")
        script_content (str): Inline script content (alternative to script file)
        variables (dict): Variables to substitute in the script using {{VAR_NAME}} syntax
        timeout (int): Maximum time to wait for job completion in seconds (default: 600)
        poll_interval (int): How often to check job status in seconds (default: 10)
        cleanup (bool): Whether to delete output files after completion (default: True)

    Script Variable Substitution:
        Use {{VAR_NAME}} syntax in scripts. Variables are replaced from the
        'variables' config dict. Example:

        Config:
            variables:
              PARTITION: gpu
              NODES: 4
              RUNTIME: 30

        Script:
            #SBATCH --partition={{PARTITION}}
            #SBATCH --nodes={{NODES}}

    Example config:
        - SlurmSbatchWorkload:
            script: "example_gpu_job.sbatch"
            variables:
              PARTITION: "gpu"
              NODES: 2
              RUNTIME: 60
            timeout: 900
    """

    description: ClassVar[str] = "Run arbitrary sbatch script on Slurm cluster"
    timeout: ClassVar[int] = 1800
    labels: ClassVar[tuple[str, ...]] = ("workload", "slurm", "slow")

    def run(self) -> None:
        """Execute the sbatch workload."""
        script_name = self.config.get("script")
        script_content = self.config.get("script_content")
        variables = self.config.get("variables", {})
        job_timeout = self.config.get("timeout", DEFAULT_JOB_TIMEOUT)
        poll_interval = self.config.get("poll_interval", DEFAULT_POLL_INTERVAL)
        cleanup = self.config.get("cleanup", True)

        # Skip early if the script targets a partition that doesn't exist
        partition = variables.get("PARTITION")
        if partition:
            result = self.run_command("sinfo -o '%P %a %l %D %N'")
            if result.exit_code == 0:
                available = get_partition_names(result.stdout)
                if partition not in available:
                    pytest.skip(f"Partition '{partition}' not present on this cluster (available: {available})")

        # Load script content
        content = self._load_script(script_name, script_content)
        if not content:
            return  # Error already set

        # Substitute variables
        content = self._substitute_variables(content, variables)
        if not content:
            return  # Error already set

        # Submit and wait
        result = self._submit_and_wait(content, job_timeout, poll_interval, cleanup)
        self._report_result(result)

    def _load_script(self, script_name: str | None, script_content: str | None) -> str:
        """Load script from file or use inline content."""
        if script_content:
            self.log.info("Using inline script content")
            return script_content

        if script_name:
            script_path = MANIFESTS_DIR / script_name
            if not script_path.exists():
                self.set_failed(f"Script not found: {script_path}")
                return ""
            content = script_path.read_text()
            if not content:
                self.set_failed(f"Script is empty: {script_path}")
                return ""
            self.log.info(f"Loaded script from: {script_path}")
            return content

        self.set_failed("Must specify either 'script' or 'script_content' in config")
        return ""

    def _substitute_variables(self, content: str, variables: dict[str, Any]) -> str:
        """Replace {{VAR_NAME}} placeholders with values from variables dict."""
        for name, value in variables.items():
            content = content.replace("{{" + name + "}}", str(value))

        # Fail on unsubstituted variables
        remaining = re.findall(r"\{\{(\w+)\}\}", content)
        if remaining:
            self.set_failed(f"Unsubstituted variables in script: {remaining}")
            return ""

        return content

    def _submit_and_wait(
        self,
        script_content: str,
        timeout: int,
        poll_interval: int,
        cleanup: bool,
    ) -> JobResult:
        """Submit script via sbatch and wait for completion."""
        # Write to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="isvtest_sbatch_") as f:
            f.write(script_content)
            script_path = f.name

        self.log.debug(f"Wrote script to: {script_path}")

        try:
            # Submit job
            submit_result = self.run_command(f"sbatch {script_path}", timeout=30)

            if submit_result.exit_code != 0:
                error_msg = f"sbatch failed (exit {submit_result.exit_code})"
                if submit_result.stderr:
                    error_msg += f"\nstderr: {submit_result.stderr}"
                if submit_result.stdout:
                    error_msg += f"\nstdout: {submit_result.stdout}"
                return JobResult(
                    job_id="",
                    success=False,
                    state="SUBMIT_FAILED",
                    error=error_msg,
                )

            job_id = parse_sbatch_job_id(submit_result.stdout)
            if not job_id:
                return JobResult(
                    job_id="",
                    success=False,
                    state="PARSE_FAILED",
                    error=f"Could not parse job ID from: {submit_result.stdout}",
                )

            self.log.info(f"Submitted job {job_id}")
            return self._wait_for_job(job_id, timeout, poll_interval, cleanup)

        finally:
            try:
                Path(script_path).unlink()
            except OSError:
                pass

    def _wait_for_job(
        self,
        job_id: str,
        timeout: int,
        poll_interval: int,
        cleanup: bool,
    ) -> JobResult:
        """Wait for a submitted job to complete."""
        start_time = time.time()
        end_time = start_time + timeout
        use_sacct = True
        nodelist = ""

        while time.time() < end_time:
            state, exit_code, node_info, sacct_ok = get_job_state(self, job_id, use_sacct)
            use_sacct = sacct_ok  # Cache whether sacct works
            if node_info:
                nodelist = node_info

            self.log.debug(f"Job {job_id} state: {state}")

            if state in TERMINAL_STATES:
                duration = time.time() - start_time
                self.log.info(f"Job {job_id} terminal: state={state}, nodelist='{nodelist}'")

                output, error = get_job_output(self, job_id, nodelist, cleanup)
                success = state == "COMPLETED" and exit_code == 0

                return JobResult(
                    job_id=job_id,
                    success=success,
                    state=state,
                    exit_code=exit_code,
                    output=output,
                    error=error,
                    duration=duration,
                    nodelist=nodelist,
                )

            time.sleep(poll_interval)

        # Timeout - cancel the job
        self.log.warning(f"Job {job_id} timed out after {timeout}s, cancelling...")
        self.run_command(f"scancel {job_id}", timeout=30)

        return JobResult(
            job_id=job_id,
            success=False,
            state="TIMEOUT",
            error=f"Job did not complete within {timeout}s",
            duration=timeout,
        )

    def _report_result(self, result: JobResult) -> None:
        """Report job result as passed/failed/skipped."""
        if result.success:
            # Check for SKIPPED marker in output
            if result.output:
                skip_match = re.search(r"^SKIPPED:\s*(.+)", result.output, re.MULTILINE)
                if skip_match:
                    pytest.skip(skip_match.group(1).strip())
                    return

            msg = f"Job {result.job_id} completed successfully"
            if result.duration:
                msg += f" ({result.duration:.1f}s)"
            self.set_passed(msg)
        else:
            msg = f"Job {result.job_id or 'N/A'} failed: {result.state}"
            if result.error:
                msg += f"\n{result.error}"
            if result.exit_code != 0:
                msg += f"\nExit code: {result.exit_code}"
            if result.output:
                msg += f"\n\nOutput:\n{result.output[:2000]}"
            self.set_failed(msg, output=result.output)
