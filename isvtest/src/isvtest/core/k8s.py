# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Kubernetes utility functions for validation tests."""

import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from isvtest.core.logger import setup_logger
from isvtest.core.runners import CommandResult

if TYPE_CHECKING:
    from isvtest.core.validation import BaseValidation

logger = setup_logger(__name__)

# Container waiting reasons that kubelet only reports after it has already
# given up retrying - callers can fast-fail instead of waiting out a timeout.
TERMINAL_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "ImagePullBackOff",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
        "CrashLoopBackOff",
    }
)

# Container waiting reasons that can appear transiently on the first pull
# attempt before kubelet transitions to ImagePullBackOff; callers should fail
# only if they persist across consecutive polls.
TRANSIENT_WAITING_REASONS: frozenset[str] = frozenset({"ErrImagePull"})


@functools.lru_cache(maxsize=1)
def get_k8s_provider() -> str:
    """Get the K8s provider, auto-detecting if not explicitly set.

    Detection order:
    1. Use K8S_PROVIDER environment variable if set
    2. Check if 'kubectl' command exists -> use kubectl
    3. Check if 'microk8s kubectl' command exists -> use microk8s
    4. Check if 'k3s kubectl' command exists -> use k3s
    5. Check if 'minikube kubectl' command exists -> use minikube
    6. Default to kubectl

    This function caches the result to avoid repeated detection.
    """
    # Check for explicit environment variable first
    explicit_provider = os.getenv("K8S_PROVIDER")
    if explicit_provider:
        provider = explicit_provider.lower()
        logger.info(f"Using K8S_PROVIDER from environment: {provider}")
        return provider

    # Auto-detect: check if kubectl exists and is executable
    try:
        result = subprocess.run(
            ["kubectl", "version", "--client"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("Auto-detected K8S_PROVIDER: kubectl")
            return "kubectl"
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        pass

    # Check if microk8s exists and is executable
    try:
        result = subprocess.run(
            ["microk8s", "kubectl", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("Auto-detected K8S_PROVIDER: microk8s")
            return "microk8s"
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        pass

    # Check if k3s exists and is executable
    try:
        result = subprocess.run(
            ["k3s", "kubectl", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("Auto-detected K8S_PROVIDER: k3s")
            return "k3s"
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        pass

    # Check if minikube exists and is executable
    try:
        result = subprocess.run(
            ["minikube", "kubectl", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("Auto-detected K8S_PROVIDER: minikube")
            return "minikube"
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        pass

    # Default to kubectl
    logger.info("Using K8S_PROVIDER: kubectl (default)")
    return "kubectl"


def get_kubectl_command() -> list[str]:
    """Get the kubectl command based on environment configuration.

    This function is intentionally not cached (unlike ``get_k8s_provider()``)
    so that the ``KUBECTL`` override is re-read on each call, which is
    consistent with its current behaviour and allows mid-process override in
    tests.

    Returns:
        List of command parts for kubectl execution.
        For microk8s: ["microk8s", "kubectl"]
        For standard k8s: ["kubectl"]
        When ``KUBECTL`` is set: tokens from ``shlex.split`` (e.g.
        ``["oc"]``, ``["/path/to/oc"]``, ``["microk8s", "kubectl"]``).

    Environment Variables:
        KUBECTL: Optional kubectl-compatible CLI prefix (parsed with POSIX
            shlex; takes precedence over ``K8S_PROVIDER``).
        K8S_PROVIDER: Set to "microk8s" for local microk8s development,
                     leave unset or set to "kubectl" for standard kubectl.

    Raises:
        FileNotFoundError: If ``KUBECTL`` is set but the binary is not on PATH.
        ValueError: If ``KUBECTL`` contains malformed shell syntax
            (e.g. unterminated quotes).
    """
    raw = os.environ.get("KUBECTL")
    if raw is not None:
        trimmed = raw.strip()
        if trimmed:
            try:
                parts = shlex.split(trimmed, posix=True)
            except ValueError as exc:
                msg = f"KUBECTL has invalid shell syntax: {trimmed!r}"
                raise ValueError(msg) from exc
            if parts and all(token for token in parts):
                if shutil.which(parts[0]) is None:
                    msg = f"KUBECTL is set to '{parts[0]}' but it was not found on PATH"
                    logger.error(msg)
                    raise FileNotFoundError(msg)
                logger.info("Using kubectl-compatible CLI from KUBECTL: %s", parts)
                return parts
            logger.warning("KUBECTL did not yield usable command tokens; falling through to K8S_PROVIDER detection")
        else:
            logger.warning("KUBECTL is set but empty after stripping; falling through to K8S_PROVIDER detection")

    k8s_provider = get_k8s_provider()

    if k8s_provider == "microk8s":
        return ["microk8s", "kubectl"]
    if k8s_provider == "k3s":
        return ["k3s", "kubectl"]
    # minikube, kubectl, and other providers use standard kubectl
    return ["kubectl"]


def get_kubectl_base_shell(*args: str) -> str:
    """Return the kubectl invocation (plus optional args) as a shell-quoted string.

    Use this when interpolating kubectl into a shell command string (e.g.
    passing to ``run_command`` or composing pipes). For argv-style calls
    (``subprocess.run``), use ``get_kubectl_command`` instead.

    With no args, returns just the provider-aware kubectl prefix. With args,
    returns the fully composed, shell-quoted command - useful for callers
    that would otherwise re-implement the quoting inline.
    """
    return " ".join(shlex.quote(part) for part in (*get_kubectl_command(), *args))


class KubectlParseError(Exception):
    """Raised when ``kubectl ... -o json`` output cannot be parsed."""


def parse_kubectl_json(
    result: CommandResult,
    resource: str = "kubectl JSON output",
) -> dict[str, Any]:
    """Parse ``kubectl ... -o json`` stdout into a JSON object.

    Raises ``KubectlParseError`` on malformed output. Command exit-code
    handling stays with callers so they can keep resource-specific
    failure messages.
    """
    stdout = result.stdout.strip()
    if not stdout:
        raise KubectlParseError(f"Failed to parse {resource}: empty stdout")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise KubectlParseError(f"Failed to parse {resource}: {exc}") from exc
    if not isinstance(payload, dict):
        raise KubectlParseError(f"Failed to parse {resource}: expected JSON object, got {type(payload).__name__}")
    return payload


def parse_kubectl_json_items(
    result: CommandResult,
    resource: str = "kubectl JSON list output",
) -> list[dict[str, Any]]:
    """Extract the ``items`` list from a ``kubectl get ... -o json`` result.

    Raises ``KubectlParseError`` if the payload is malformed or ``items`` is
    missing/wrongly typed.
    """
    payload = parse_kubectl_json(result, resource)
    items = payload.get("items")
    if not isinstance(items, list):
        raise KubectlParseError(f"Failed to parse {resource}: expected 'items' list")
    if any(not isinstance(item, dict) for item in items):
        raise KubectlParseError(f"Failed to parse {resource}: expected object entries in 'items'")
    return items


def kubectl_items_or_fail(
    validation: "BaseValidation",
    result: CommandResult,
    resource: str,
    *,
    exec_label: str | None = None,
) -> list[dict[str, Any]] | None:
    """Parse ``kubectl get ... -o json`` items, routing failures to ``validation.set_failed``.

    Returns the items list on success, or ``None`` after marking the validation
    failed when the command exited non-zero or returned unparseable JSON.

    ``exec_label`` overrides the noun used in the exec-failure message
    (``"Failed to get {label}: ..."``) when callers want wording other than the
    parse-error ``resource`` (e.g. ``"node count"`` vs ``"node list"``).
    """
    if result.exit_code != 0:
        label = exec_label or resource
        validation.set_failed(f"Failed to get {label}: {result.stderr}")
        return None
    try:
        return parse_kubectl_json_items(result, resource)
    except KubectlParseError as exc:
        validation.set_failed(str(exc))
        return None


def pod_status_reason(pod: dict[str, Any]) -> str:
    """Return a kubectl-like pod status reason from structured pod JSON."""
    status = pod.get("status") or {}
    phase = status.get("phase") or "Unknown"
    for key in ("initContainerStatuses", "containerStatuses"):
        for container_status in status.get(key) or []:
            state = container_status.get("state") or {}
            waiting_reason = (state.get("waiting") or {}).get("reason")
            if waiting_reason:
                return waiting_reason
            terminated_reason = (state.get("terminated") or {}).get("reason")
            if terminated_reason and terminated_reason != "Completed":
                return terminated_reason
    # Fall back to ``.status.reason`` (e.g. "Evicted", "NodeLost", "Shutdown")
    # before the bare phase so the kubectl STATUS column wording is preserved
    # for pods without informative container state.
    return str(status.get("reason") or phase)


def render_k8s_manifest(
    path: Path,
    mutate: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> str:
    """Load a multi-doc YAML manifest, apply ``mutate`` to each doc, and serialize it back.

    The manifest file must contain valid, parseable YAML with sensible default
    values - callers mutate the parsed objects rather than templating strings.
    This keeps manifests readable in isolation and avoids quoting / escaping
    pitfalls of ``str.replace``-style substitution.

    Args:
        path: Path to a ``.yaml`` file with one or more documents.
        mutate: Callable invoked once per non-empty document with the parsed
            dict; must return the (possibly mutated) dict. ``None`` leaves each
            document untouched.

    Returns:
        A YAML string suitable for ``kubectl apply -f -``.
    """
    raw = path.read_text()
    docs = [doc for doc in yaml.safe_load_all(raw) if doc is not None]
    if mutate is not None:
        docs = [mutate(doc) for doc in docs]
    return yaml.safe_dump_all(docs, sort_keys=False)


def parse_pod_state(stdout: str, stderr: str) -> tuple[str, str, str]:
    """Parse ``kubectl get pod -o json`` output into ``(phase, waiting_reason, waiting_message)``.

    ``stdout`` is the JSON output when the command succeeds; ``stderr`` is
    inspected only when ``stdout`` is empty/invalid so NotFound errors can be
    distinguished from generic failures.

    Phase values:

    - Pod phase (``"Running"``, ``"Pending"``, ``"Succeeded"``, ``"Failed"``) on success.
    - ``"NotFound"`` when kubectl reports the pod is gone (evicted, deleted).
    - ``"Unknown"`` on any other query failure.

    Waiting reason/message describe the first container's waiting state
    (both empty when the container is not waiting).
    """
    if not stdout:
        lowered = (stderr or "").lower()
        if "notfound" in lowered.replace(" ", "") or "not found" in lowered:
            return "NotFound", "", ""
        return "Unknown", "", ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "Unknown", "", ""
    status = data.get("status") or {}
    phase = status.get("phase") or "Unknown"
    statuses = status.get("containerStatuses") or []
    waiting = ((statuses[0].get("state") if statuses else {}) or {}).get("waiting") or {}
    return phase, waiting.get("reason") or "", waiting.get("message") or ""


def pod_state_from_result(
    result: "CommandResult | subprocess.CompletedProcess[Any]",
) -> tuple[str, str, str]:
    """Parse pod state from a ``kubectl get pod -o json`` result.

    Accepts either a ``CommandResult`` (validations) or a
    ``subprocess.CompletedProcess`` (core helpers). Returns the same
    ``(phase, waiting_reason, waiting_message)`` tuple as
    ``parse_pod_state``: on failure only stderr is inspected (for NotFound
    detection); on success only stdout is parsed.
    """
    exit_code = getattr(result, "exit_code", None)
    if exit_code is None:
        exit_code = result.returncode  # subprocess.CompletedProcess
    if exit_code == 0:
        return parse_pod_state(result.stdout, "")
    return parse_pod_state("", result.stderr or "")


def parse_server_version(stdout: str) -> str | None:
    """Extract the ``vX.Y.Z`` server version from ``kubectl version -o json`` output.

    Returns ``None`` if the JSON is malformed, ``serverVersion`` is missing, or
    ``gitVersion`` does not match the expected pattern. Build metadata after
    the patch number (e.g. ``v1.30.2+abc``) is stripped.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    git_version = ((data.get("serverVersion") or {}).get("gitVersion")) or ""
    match = re.match(r"^(v\d+\.\d+\.\d+)", git_version)
    return match.group(1) if match else None


def run_kubectl(
    args: list[str],
    timeout: int = 30,
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Run kubectl command with appropriate provider.

    Args:
        args: kubectl arguments (e.g., ["get", "nodes"])
        timeout: Command timeout in seconds
        capture_output: Whether to capture stdout/stderr
        text: Whether to return output as text
        check: Whether to raise exception on non-zero exit

    Returns:
        CompletedProcess instance with command results

    Example:
        >>> result = run_kubectl(["get", "nodes"])
        >>> if result.returncode == 0:
        ...     print(result.stdout)
    """
    kubectl_cmd = get_kubectl_command()
    full_cmd = kubectl_cmd + args

    try:
        return subprocess.run(
            full_cmd,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired as e:
        logger.warning(f"kubectl command timed out after {timeout}s: {' '.join(full_cmd)}")
        # Return a CompletedProcess-like object indicating timeout
        # Exit code 124 is the standard timeout exit code (used by GNU timeout)
        return subprocess.CompletedProcess(
            args=full_cmd,
            returncode=124,
            stdout=e.stdout if hasattr(e, "stdout") and e.stdout else ("" if text else b""),
            stderr=e.stderr if hasattr(e, "stderr") and e.stderr else ("" if text else b""),
        )


def is_k8s_available() -> bool:
    """Check if Kubernetes cluster is accessible.

    Returns:
        True if kubectl can connect to a cluster, False otherwise.
    """
    try:
        result = run_kubectl(["cluster-info"], timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_gpu_nodes() -> list[str]:
    """Get list of GPU-enabled nodes in the cluster.

    Returns:
        List of node names that have GPUs available.
    """
    result = run_kubectl(
        ["get", "nodes", "-l", "nvidia.com/gpu.present=true", "-o", "jsonpath={.items[*].metadata.name}"]
    )
    if result.returncode != 0:
        return []

    nodes = result.stdout.strip().split()
    return [node for node in nodes if node]


def get_node_gpu_count(node_name: str) -> int:
    """Get the number of GPUs on a specific node.

    Args:
        node_name: Name of the Kubernetes node.

    Returns:
        Number of GPUs available on the node, or 0 if none or error.
    """
    result = run_kubectl(["get", "node", node_name, "-o", "jsonpath={.status.capacity.nvidia\\.com/gpu}"])
    if result.returncode != 0 or not result.stdout.strip():
        return 0

    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def wait_for_pod_status(
    pod_name: str,
    namespace: str,
    desired_phase: str,
    timeout: int = 300,
) -> bool:
    """Wait for a pod to reach a desired phase.

    This function waits for a pod to reach the exact desired phase.
    It does not treat other terminal states as equivalent to the desired phase.

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace.
        desired_phase: Desired pod phase (e.g., 'Running', 'Succeeded', 'Failed').
        timeout: Maximum time to wait in seconds.

    Returns:
        True if pod reached the exact desired phase, False if timeout or error.

    Note:
        For waiting for job completion regardless of success/failure,
        use wait_for_pod_completion() instead.
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        result = run_kubectl(["get", "pod", pod_name, "-n", namespace, "-o", "jsonpath={.status.phase}"])

        if result.returncode == 0:
            current_phase = result.stdout.strip()
            if current_phase == desired_phase:
                return True

        time.sleep(0.5)

    return False


def wait_for_pod_completion(
    pod_name: str,
    namespace: str,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Wait for a pod to complete (reach either Succeeded or Failed state).

    This function waits for a pod to reach a terminal state and returns
    both whether it completed and what the final state was.

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace.
        timeout: Maximum time to wait in seconds.

    Returns:
        A tuple of (completed, phase) where:
        - completed: True if pod reached a terminal state (Succeeded or Failed), False if timeout
        - phase: The final phase of the pod ('Succeeded', 'Failed', or last known phase if timeout)

    Example:
        >>> completed, phase = wait_for_pod_completion("my-pod", "default", timeout=300)
        >>> if completed:
        ...     if phase == "Succeeded":
        ...         print("Pod completed successfully")
        ...     elif phase == "Failed":
        ...         print("Pod failed")
    """
    start_time = time.time()
    last_phase = "Unknown"

    while time.time() - start_time < timeout:
        result = run_kubectl(["get", "pod", pod_name, "-n", namespace, "-o", "jsonpath={.status.phase}"])

        if result.returncode == 0:
            current_phase = result.stdout.strip()
            last_phase = current_phase

            # Check if pod reached a terminal state
            if current_phase in ["Succeeded", "Failed"]:
                return True, current_phase

        time.sleep(0.5)

    # Timeout reached
    return False, last_phase


def get_pod_logs(pod_name: str, namespace: str, container: str | None = None, timeout: int = 30) -> str:
    """Get logs from a pod.

    Args:
        pod_name: Name of the pod.
        namespace: Kubernetes namespace.
        container: Optional container name (for multi-container pods).
        timeout: Timeout for fetching logs.

    Returns:
        Pod logs as string, or empty string if error.
    """
    # Build kubectl logs command
    log_cmd = ["logs", pod_name, "-n", namespace]
    if container:
        log_cmd.extend(["-c", container])

    # Try with insecure flag first (needed for microk8s with cert issues)
    result = run_kubectl(log_cmd + ["--insecure-skip-tls-verify-backend=true"], timeout=timeout)

    if result.returncode == 0:
        return result.stdout

    # Fallback to standard logs command
    result = run_kubectl(log_cmd, timeout=timeout)

    if result.returncode == 0:
        return result.stdout
    return ""


def delete_pod(pod_name: str, namespace: str, wait: bool = True) -> bool:
    """Delete a pod.

    Args:
        pod_name: Name of the pod to delete.
        namespace: Kubernetes namespace.
        wait: Whether to wait for pod to be fully deleted.

    Returns:
        True if deletion succeeded, False otherwise.
    """
    result = run_kubectl(["delete", "pod", pod_name, "-n", namespace, "--ignore-not-found=true"])

    if result.returncode != 0:
        return False

    if wait:
        # Wait up to 30 seconds for pod to be deleted
        timeout = 30
        start_time = time.time()

        while time.time() - start_time < timeout:
            check = run_kubectl(["get", "pod", pod_name, "-n", namespace])
            if check.returncode != 0:  # Pod no longer exists
                return True
            time.sleep(0.5)

    return True


def create_configmap_from_string(
    name: str,
    namespace: str,
    filename: str,
    content: str,
) -> bool:
    """Create a ConfigMap from string content.

    Args:
        name: ConfigMap name.
        namespace: Kubernetes namespace.
        filename: Filename for the data key.
        content: Content to store in ConfigMap.

    Returns:
        True if creation succeeded, False otherwise.
    """
    # Use kubectl create configmap with --from-literal or stdin
    result = run_kubectl(
        [
            "create",
            "configmap",
            name,
            "-n",
            namespace,
            f"--from-literal={filename}={content}",
            "--dry-run=client",
            "-o",
            "yaml",
        ]
    )

    if result.returncode != 0:
        return False

    # Apply the configmap
    try:
        apply_result = subprocess.run(
            [*get_kubectl_command(), "apply", "-f", "-"],
            input=result.stdout,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return apply_result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning(f"kubectl apply command timed out after 30s for ConfigMap '{name}' in namespace '{namespace}'")
        return False


def delete_configmap(name: str, namespace: str) -> bool:
    """Delete a ConfigMap.

    Args:
        name: ConfigMap name.
        namespace: Kubernetes namespace.

    Returns:
        True if deletion succeeded, False otherwise.
    """
    result = run_kubectl(["delete", "configmap", name, "-n", namespace, "--ignore-not-found=true"])

    return result.returncode == 0


def wait_for_job_completion(
    job_name: str,
    namespace: str,
    timeout: int = 600,
) -> tuple[bool, str]:
    """Wait for a job to complete (reach either Complete or Failed status).

    This function waits for a job to reach a terminal state and returns
    both whether it completed and what the final state was.

    Args:
        job_name: Name of the job.
        namespace: Kubernetes namespace.
        timeout: Maximum time to wait in seconds.

    Returns:
        A tuple of (completed, status) where:
        - completed: True if job reached a terminal state (Complete or Failed), False if timeout
        - status: The final status condition ('Complete', 'Failed', or last known status if timeout)

    Example:
        >>> completed, status = wait_for_job_completion("my-job", "default", timeout=600)
        >>> if completed:
        ...     if status == "Complete":
        ...         print("Job completed successfully")
        ...     elif status == "Failed":
        ...         print("Job failed")
    """
    start_time = time.time()
    last_status = "Unknown"
    last_print_time = start_time

    while time.time() - start_time < timeout:
        # Check job conditions for Complete or Failed status
        result = run_kubectl(
            [
                "get",
                "job",
                job_name,
                "-n",
                namespace,
                "-o",
                "jsonpath={.status.conditions[*].type}",
            ]
        )

        if result.returncode == 0 and result.stdout.strip():
            conditions = result.stdout.strip().split()

            # Check if job has Complete or Failed condition
            if "Complete" in conditions:
                return True, "Complete"
            if "Failed" in conditions:
                return True, "Failed"

            last_status = "Running" if conditions else "Pending"

        # Get pod status for better visibility
        pod_status_result = run_kubectl(
            [
                "get",
                "pods",
                "-l",
                f"job-name={job_name}",
                "-n",
                namespace,
                "-o",
                "jsonpath={.items[0].status.phase} {.items[0].status.containerStatuses[*].state}",
            ]
        )

        pod_info = "No pods"
        if pod_status_result.returncode == 0 and pod_status_result.stdout.strip():
            parts = pod_status_result.stdout.strip().split(maxsplit=1)
            pod_phase = parts[0] if parts else "Unknown"

            # Count running containers if we have container status
            if len(parts) > 1:
                container_states = parts[1]
                running_count = container_states.count("map[running:")
                waiting_count = container_states.count("map[waiting:")
                terminated_count = container_states.count("map[terminated:")
                total = running_count + waiting_count + terminated_count

                if running_count > 0:
                    pod_info = f"Pod: {pod_phase}, Containers: {running_count}/{total} running"
                elif waiting_count > 0:
                    pod_info = f"Pod: {pod_phase}, Containers: {waiting_count}/{total} waiting"
                else:
                    pod_info = f"Pod: {pod_phase}"
            else:
                pod_info = f"Pod: {pod_phase}"

            # Update last_status based on pod phase if we don't have conditions
            if not result.stdout.strip():
                last_status = pod_phase

        # Log status every 30 seconds
        current_time = time.time()
        if current_time - last_print_time >= 30:
            elapsed = int(current_time - start_time)
            logger.info(f"Still waiting for job {job_name}... elapsed={elapsed}s, {pod_info}")
            last_print_time = current_time

        time.sleep(0.5)

    # Timeout reached
    return False, last_status


def get_job_pods(job_name: str, namespace: str) -> list[str]:
    """Get list of pod names for a specific job.

    Args:
        job_name: Name of the job.
        namespace: Kubernetes namespace.

    Returns:
        List of pod names belonging to the job.
    """
    result = run_kubectl(
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"job-name={job_name}",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ]
    )

    if result.returncode != 0:
        return []

    pods = result.stdout.strip().split()
    return [pod for pod in pods if pod]


def delete_job(job_name: str, namespace: str, wait: bool = True) -> bool:
    """Delete a job.

    Args:
        job_name: Name of the job to delete.
        namespace: Kubernetes namespace.
        wait: Whether to wait for job to be fully deleted.

    Returns:
        True if deletion succeeded, False otherwise.
    """
    result = run_kubectl(["delete", "job", job_name, "-n", namespace, "--ignore-not-found=true"])

    if result.returncode != 0:
        return False

    if wait:
        # Wait up to 30 seconds for job to be deleted
        timeout = 30
        start_time = time.time()

        while time.time() - start_time < timeout:
            check = run_kubectl(["get", "job", job_name, "-n", namespace])
            if check.returncode != 0:  # Job no longer exists
                return True
            time.sleep(0.5)

    return True


def wait_for_multiple_pods_completion(
    pod_names: list[str],
    namespace: str,
    timeout: int = 300,
) -> dict[str, tuple[bool, str]]:
    """Wait for multiple pods to complete, checking all at once.

    This is much faster than calling wait_for_pod_completion() for each pod
    individually, as it checks all pods in a single kubectl call per iteration.

    Args:
        pod_names: List of pod names to wait for.
        namespace: Kubernetes namespace.
        timeout: Maximum time to wait in seconds.

    Returns:
        Dictionary mapping pod names to (completed, phase) tuples.
    """
    start_time = time.time()
    results = {pod_name: (False, "Unknown") for pod_name in pod_names}
    completed_pods = set()

    while time.time() - start_time < timeout:
        # Check all pods at once using label selector or field selector
        # Get status of all pods in one call
        result = run_kubectl(
            [
                "get",
                "pods",
                "-n",
                namespace,
                "-o",
                'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.status.phase}{"\\n"}{end}',
            ]
        )

        if result.returncode == 0:
            # Parse output: "pod-name\tPhase\n"
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) == 2:
                    pod_name, phase = parts
                    if pod_name in pod_names:
                        if phase in ["Succeeded", "Failed"]:
                            results[pod_name] = (True, phase)
                            completed_pods.add(pod_name)

        # Check if all pods completed
        if len(completed_pods) == len(pod_names):
            return results

        time.sleep(0.5)

    # Timeout - return current status
    return results


def get_all_nodes() -> list[str]:
    """Get list of all nodes in the cluster.

    Returns:
        List of all node names in the cluster.
    """
    result = run_kubectl(["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"])
    if result.returncode != 0:
        return []

    nodes = result.stdout.strip().split()
    return [node for node in nodes if node]


def get_node_status(node_name: str) -> str:
    """Get the status of a specific node.

    Args:
        node_name: Name of the node.

    Returns:
        Node status string (e.g., 'Ready', 'NotReady', 'Unknown').
        Returns 'Unknown' if unable to determine status.
    """
    result = run_kubectl(["get", "node", node_name, "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}"])
    if result.returncode != 0 or not result.stdout.strip():
        return "Unknown"

    # Status will be "True" or "False" - convert to Ready/NotReady
    status = result.stdout.strip()
    return "Ready" if status == "True" else "NotReady"


def get_nodes_with_status() -> dict[str, str]:
    """Get all nodes with their Ready status.

    Returns:
        Dictionary mapping node names to their status ('Ready', 'NotReady', 'Unknown').
    """
    nodes = get_all_nodes()
    return {node: get_node_status(node) for node in nodes}
