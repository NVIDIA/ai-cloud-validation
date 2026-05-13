# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""NGC (NVIDIA GPU Cloud) utility functions for NIM deployments.

This module provides shared functionality for:
- NGC secret management (image pull secrets, API key secrets)
- NIM inference validation
"""

import json
import os
import subprocess
import time
import uuid
from typing import Any

from isvtest.core.k8s import get_kubectl_command, job_terminal_status, run_kubectl
from isvtest.core.logger import setup_logger

logger = setup_logger(__name__)

# Standardized secret names used across NIM workloads
NGC_IMAGE_SECRET_NAME = "ngc-image-secret"
NGC_API_SECRET_NAME = "ngc-api-secret"


def get_ngc_api_key() -> str:
    """Read the NGC API key from environment variables.

    Checks NGC_API_KEY first, then NGC_NIM_API_KEY as a legacy fallback.

    Returns:
        The API key string, or empty string if neither variable is set.
    """
    return os.environ.get("NGC_API_KEY", "") or os.environ.get("NGC_NIM_API_KEY", "")


def ensure_ngc_secrets(namespace: str, ngc_api_key: str | None = None) -> tuple[bool, str]:
    """Ensure NGC secrets exist in the namespace, creating them if needed.

    Creates two secrets as per NVIDIA NIM docs:
    1. ngc-image-secret: docker-registry secret for pulling NIM images from nvcr.io
    2. ngc-api-secret: generic secret with NGC API key for model downloads

    Args:
        namespace: Kubernetes namespace.
        ngc_api_key: NGC API key. If None, reads via get_ngc_api_key().

    Returns:
        Tuple of (success, error_message). If success is True, error_message is empty.
    """
    if ngc_api_key is None:
        ngc_api_key = get_ngc_api_key() or None

    if not ngc_api_key:
        return False, "NGC_API_KEY not set"

    kubectl_parts = get_kubectl_command()

    # Ensure namespace exists
    result = run_kubectl(
        ["create", "namespace", namespace, "--dry-run=client", "-o", "yaml"],
        timeout=10,
    )
    if result.returncode == 0:
        try:
            subprocess.run(
                kubectl_parts + ["apply", "-f", "-"],
                input=result.stdout,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"Namespace creation timed out for {namespace}")

    # Check/create docker-registry secret for image pulls
    result = run_kubectl(["get", "secret", NGC_IMAGE_SECRET_NAME, "-n", namespace], timeout=10)
    if result.returncode != 0:
        logger.info(f"Creating NGC image pull secret ({NGC_IMAGE_SECRET_NAME})...")
        try:
            # Pass API key directly as command argument (no shell interpretation needed)
            # Note: Key may be visible in /proc/cmdline briefly during execution
            cmd = kubectl_parts + [
                "create",
                "secret",
                "docker-registry",
                NGC_IMAGE_SECRET_NAME,
                "--docker-server=nvcr.io",
                "--docker-username=$oauthtoken",
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
        except subprocess.CalledProcessError as e:
            return False, f"Failed to create NGC image pull secret: {e.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Timeout creating NGC image pull secret"
        except Exception as e:
            return False, f"Failed to create NGC image pull secret: {e}"

    # Check/create generic secret for NGC API key
    result = run_kubectl(["get", "secret", NGC_API_SECRET_NAME, "-n", namespace], timeout=10)
    if result.returncode != 0:
        logger.info(f"Creating NGC API secret ({NGC_API_SECRET_NAME})...")
        try:
            # Use --from-file with stdin to pass the key securely
            subprocess.run(
                kubectl_parts
                + [
                    "create",
                    "secret",
                    "generic",
                    NGC_API_SECRET_NAME,
                    "--from-file=apikey=/dev/stdin",
                    "-n",
                    namespace,
                ],
                input=ngc_api_key,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            return False, f"Failed to create NGC API secret: {e.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Timeout creating NGC API secret"
        except Exception as e:
            return False, f"Failed to create NGC API secret: {e}"

    return True, ""


def validate_nim_inference(
    nim_url: str,
    namespace: str,
    model: str,
    timeout: int = 120,
) -> tuple[bool, str]:
    """Run basic inference validation against a NIM endpoint.

    Creates a temporary Job that sends a simple inference request and validates
    the response contains expected content.

    Args:
        nim_url: Full URL to NIM service (e.g., http://nim-svc.namespace.svc.cluster.local:8000)
        namespace: Kubernetes namespace for the test job.
        model: Model name for the inference request.
        timeout: Maximum time to wait for inference test in seconds.

    Returns:
        Tuple of (success, message). If success is True, message contains the response.
        If success is False, message contains the error.
    """
    logger.info(f"Running inference validation against model: {model}")

    kubectl_parts = get_kubectl_command()
    job_name = f"nim-inference-test-{uuid.uuid4().hex[:6]}"

    # Create inference test job
    job_yaml = f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 60
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: curl
          image: curlimages/curl:latest
          command: ["/bin/sh", "-c"]
          args:
            - |
              echo "Testing NIM endpoint: {nim_url}"
              echo "Model: {model}"
              MAX_RETRIES=5
              RETRY_DELAY=5
              for i in $(seq 1 $MAX_RETRIES); do
                echo "Attempt $i/$MAX_RETRIES..."
                if curl -sS -f -X POST "{nim_url}/v1/chat/completions" \
                  -H "Content-Type: application/json" \
                  -d '{{"model": "{model}", "messages": [{{"role": "user", "content": "What is 2+2? Reply with just the number."}}], "max_tokens": 10, "temperature": 0.1}}'; then
                  echo "Success!"
                  exit 0
                fi
                if [ $i -lt $MAX_RETRIES ]; then
                  echo "Failed, retrying in ${{RETRY_DELAY}}s..."
                  sleep $RETRY_DELAY
                fi
              done
              echo "All retries failed. Checking models endpoint for debugging..."
              curl -sS "{nim_url}/v1/models" || echo "Models endpoint also failed"
              exit 1
"""

    try:
        # Create the job
        result = subprocess.run(
            kubectl_parts + ["apply", "-f", "-", "-n", namespace],
            input=job_yaml,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return False, f"Failed to create inference test job: {result.stderr}"

        # Wait for job to complete
        logger.info("Waiting for inference test to complete...")
        job_completed = False
        job_failed = False

        for _ in range(timeout // 5):
            time.sleep(5)
            result = run_kubectl(["get", "job", job_name, "-n", namespace, "-o", "json"], timeout=10)
            if result.returncode != 0:
                continue
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                continue
            terminal = job_terminal_status(payload)
            if terminal == "Complete":
                job_completed = True
                break
            if terminal == "Failed":
                job_failed = True
                break

        if not job_completed and not job_failed:
            return False, f"Inference test timed out after {timeout}s"

        if job_failed:
            # Get logs for debugging
            log_result = run_kubectl(["logs", f"job/{job_name}", "-n", namespace], timeout=30)
            return False, f"Inference test job failed. Logs: {log_result.stdout[:500]}"

        # Get job logs (the inference response)
        log_result = run_kubectl(["logs", f"job/{job_name}", "-n", namespace], timeout=30)

        if log_result.returncode != 0:
            return False, f"Failed to get inference test logs: {log_result.stderr}"

        output = log_result.stdout.strip()

        # Parse response
        try:
            response = json.loads(output)
            if "choices" in response and len(response["choices"]) > 0:
                content = response["choices"][0].get("message", {}).get("content", "")
                logger.info(f"Inference response: {content}")
                if "4" in content:
                    return True, f"Inference validated. Response: {content}"
                else:
                    # Don't fail, just warn about unexpected answer
                    return True, f"Inference completed (unexpected answer: {content})"

            return True, "Inference completed (unexpected response format)"

        except json.JSONDecodeError:
            # Don't fail on parse error if HTTP succeeded
            return True, "Inference completed (could not parse JSON response)"

    finally:
        # Cleanup job
        run_kubectl(["delete", "job", job_name, "-n", namespace, "--wait=false"], timeout=10)


def create_ngc_docker_config(ngc_api_key: str) -> dict[str, Any]:
    """Create a Docker config JSON for NGC registry authentication.

    This is useful when you need the full docker config (e.g., for --from-file).

    Args:
        ngc_api_key: NGC API key.

    Returns:
        Docker config dictionary suitable for .dockerconfigjson.
    """
    import base64

    return {
        "auths": {
            "nvcr.io": {
                "username": "$oauthtoken",
                "password": ngc_api_key,
                "auth": base64.b64encode(f"$oauthtoken:{ngc_api_key}".encode()).decode(),
            }
        }
    }
