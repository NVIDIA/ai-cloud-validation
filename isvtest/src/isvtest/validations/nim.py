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

"""NIM inference validations (platform-agnostic).

SSH-based validations for NIM containers running on any host.
These work identically on VMaaS, BMaaS, or any SSH-accessible machine
with a NIM container running.

They consume connection details from step outputs:
    host: "{{steps.deploy_nim.host}}"
    key_file: "{{steps.deploy_nim.key_file}}"
    user: "{{steps.deploy_nim.ssh_user}}"
    port: "{{steps.deploy_nim.port}}"

Requires paramiko: pip install paramiko
"""

from __future__ import annotations

import json
import shlex
from typing import ClassVar

import pytest

from isvtest.core.ssh import (
    get_failed_subtests,
    get_ssh_client,
    get_ssh_config,
    run_ssh_command,
)
from isvtest.core.validation import BaseValidation


def _get_nim_port(config: dict) -> int:
    """Extract NIM port from config or step output."""
    return config.get("port") or config.get("step_output", {}).get("port") or 8000


def _is_nim_skipped(config: dict) -> str | None:
    """Return skip reason if NIM deployment was skipped or missing, else None."""
    step_output = config.get("step_output", {})
    if step_output.get("skipped"):
        return step_output.get("skip_reason", "NIM deployment was skipped")
    if not step_output or not step_output.get("success"):
        return "NIM deployment did not succeed"
    return None


class NimHealthCheck(BaseValidation):
    """Check NIM container health endpoint via SSH.

    Connects via SSH and curls the NIM /v1/health/ready endpoint.

    Config:
        host, key_file, user: SSH connection details (or from step_output)
        port: NIM port on the host (default: 8000)
    """

    description: ClassVar[str] = "Validates NIM health endpoint"
    timeout: ClassVar[int] = 120
    labels: ClassVar[tuple[str, ...]] = ("ssh", "gpu", "bare_metal", "vm")

    def run(self) -> None:
        skip_reason = _is_nim_skipped(self.config)
        if skip_reason:
            pytest.skip(skip_reason)

        try:
            import paramiko  # noqa: F401
        except ImportError:
            self.set_failed("paramiko not installed")
            return

        ssh_cfg = get_ssh_config(self.config, self.config.get("inventory", {}))
        host = ssh_cfg["ssh_host"]
        user = ssh_cfg["ssh_user"]
        key_path = ssh_cfg["ssh_key_path"]
        port = _get_nim_port(self.config)

        if not host or not key_path:
            self.set_failed("Missing host or key_file")
            return

        try:
            ssh = get_ssh_client(host, user, key_path)

            exit_code, stdout, _ = run_ssh_command(
                ssh,
                f"curl -sf http://localhost:{port}/v1/health/ready 2>/dev/null; echo $?",
            )
            healthy = exit_code == 0 and "0" in stdout.strip().split("\n")[-1]
            self.report_subtest("health_ready", healthy, "NIM healthy" if healthy else "Health endpoint not ready")

            ssh.close()

            if healthy:
                self.set_passed(f"NIM health check passed on {host}:{port}")
            else:
                self.set_failed(f"NIM health endpoint not ready on {host}:{port}")

        except Exception as e:
            self.set_failed(f"NIM health check failed: {e}")


class NimInferenceCheck(BaseValidation):
    """Run NIM inference test via SSH.

    Sends a chat completion request to the NIM container and validates
    the response contains generated content.

    Config:
        host, key_file, user: SSH connection details (or from step_output)
        port: NIM port on the host (default: 8000)
        prompt: Test prompt (default: "What is CUDA?")
        model: Model name for the request (optional, auto-detected from /v1/models)
        max_tokens: Max tokens in response (default: 50)
    """

    description: ClassVar[str] = "Validates NIM inference via chat completions"
    timeout: ClassVar[int] = 300
    labels: ClassVar[tuple[str, ...]] = ("ssh", "gpu", "workload", "slow", "bare_metal", "vm")

    def run(self) -> None:
        skip_reason = _is_nim_skipped(self.config)
        if skip_reason:
            pytest.skip(skip_reason)

        try:
            import paramiko  # noqa: F401
        except ImportError:
            self.set_failed("paramiko not installed")
            return

        ssh_cfg = get_ssh_config(self.config, self.config.get("inventory", {}))
        host = ssh_cfg["ssh_host"]
        user = ssh_cfg["ssh_user"]
        key_path = ssh_cfg["ssh_key_path"]
        port = _get_nim_port(self.config)
        prompt = self.config.get("prompt", "What is CUDA?")
        max_tokens = self.config.get("max_tokens", 50)
        model = self.config.get("model") or self.config.get("step_output", {}).get("model")

        if not host or not key_path:
            self.set_failed("Missing host or key_file")
            return

        try:
            ssh = get_ssh_client(host, user, key_path)

            # Auto-detect model name if not provided
            if not model:
                exit_code, stdout, _ = run_ssh_command(ssh, f"curl -sf http://localhost:{port}/v1/models 2>/dev/null")
                if exit_code == 0 and stdout.strip():
                    try:
                        models_data = json.loads(stdout)
                        model_list = models_data.get("data", [])
                        if model_list:
                            model = model_list[0].get("id", "")
                    except json.JSONDecodeError:
                        pass

            if not model:
                self.set_failed("Could not determine model name")
                ssh.close()
                return

            self.report_subtest("model_detected", True, f"Model: {model}")

            # Send chat completion request
            payload = json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                }
            )
            curl_cmd = (
                f"curl -sf -X POST http://localhost:{port}/v1/chat/completions"
                f" -H 'Content-Type: application/json'"
                f" -d {shlex.quote(payload)}"
            )
            exit_code, stdout, stderr = run_ssh_command(ssh, curl_cmd)

            if exit_code != 0 or not stdout.strip():
                self.set_failed(f"Inference request failed: {stderr}")
                ssh.close()
                return

            try:
                response = json.loads(stdout)
            except json.JSONDecodeError:
                self.set_failed(f"Invalid JSON response: {stdout[:200]}")
                ssh.close()
                return

            # Validate response structure
            choices = response.get("choices", [])
            has_choices = len(choices) > 0
            self.report_subtest("has_choices", has_choices, f"{len(choices)} choice(s)")

            if not has_choices:
                self.set_failed("No choices in response")
                ssh.close()
                return

            content = choices[0].get("message", {}).get("content", "")
            has_content = len(content) > 0
            self.report_subtest("has_content", has_content, f"Response: {content[:80]}...")

            finish_reason = choices[0].get("finish_reason", "")
            valid_finish = finish_reason in ("stop", "length")
            self.report_subtest("finish_reason", valid_finish, f"finish_reason={finish_reason}")

            # Check usage stats if present
            usage = response.get("usage", {})
            if usage:
                tokens = usage.get("completion_tokens", 0)
                self.report_subtest("tokens_generated", tokens > 0, f"{tokens} tokens")

            ssh.close()

            failed = get_failed_subtests(self._subtest_results)
            if failed:
                self.set_failed(f"Inference subtests failed: {', '.join(failed)}")
            else:
                self.set_passed(f"NIM inference OK on {host}:{port} (model={model})")

        except Exception as e:
            self.set_failed(f"NIM inference check failed: {e}")


class NimModelCheck(BaseValidation):
    """Check NIM /v1/models endpoint via SSH.

    Validates the models endpoint returns at least one model and
    optionally verifies a specific model name is present.

    Config:
        host, key_file, user: SSH connection details (or from step_output)
        port: NIM port on the host (default: 8000)
        expected_model: Model name substring to look for (optional)
    """

    description: ClassVar[str] = "Validates NIM model listing"
    timeout: ClassVar[int] = 120
    labels: ClassVar[tuple[str, ...]] = ("ssh", "gpu", "bare_metal", "vm")

    def run(self) -> None:
        skip_reason = _is_nim_skipped(self.config)
        if skip_reason:
            pytest.skip(skip_reason)

        try:
            import paramiko  # noqa: F401
        except ImportError:
            self.set_failed("paramiko not installed")
            return

        ssh_cfg = get_ssh_config(self.config, self.config.get("inventory", {}))
        host = ssh_cfg["ssh_host"]
        user = ssh_cfg["ssh_user"]
        key_path = ssh_cfg["ssh_key_path"]
        port = _get_nim_port(self.config)
        expected_model = self.config.get("expected_model")

        if not host or not key_path:
            self.set_failed("Missing host or key_file")
            return

        try:
            ssh = get_ssh_client(host, user, key_path)

            exit_code, stdout, stderr = run_ssh_command(ssh, f"curl -sf http://localhost:{port}/v1/models 2>/dev/null")
            if exit_code != 0 or not stdout.strip():
                self.set_failed(f"/v1/models request failed: {stderr}")
                ssh.close()
                return

            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                self.set_failed(f"Invalid JSON from /v1/models: {stdout[:200]}")
                ssh.close()
                return

            models = data.get("data", [])
            has_models = len(models) > 0
            self.report_subtest("has_models", has_models, f"{len(models)} model(s)")

            if not has_models:
                self.set_failed("No models returned")
                ssh.close()
                return

            model_ids = [m.get("id", "") for m in models]
            self.report_subtest("model_list", True, ", ".join(model_ids))

            if expected_model:
                found = any(expected_model in mid for mid in model_ids)
                self.report_subtest(
                    "expected_model",
                    found,
                    f"'{expected_model}' {'found' if found else 'not found'} in model list",
                )

            ssh.close()

            failed = get_failed_subtests(self._subtest_results)
            if failed:
                self.set_failed(f"Model check subtests failed: {', '.join(failed)}")
            else:
                self.set_passed(f"NIM model check passed ({', '.join(model_ids)})")

        except Exception as e:
            self.set_failed(f"NIM model check failed: {e}")
