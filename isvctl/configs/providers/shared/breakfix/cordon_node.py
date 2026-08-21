#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise Kubernetes node cordoning semantics for BFX01-04."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

DEFAULT_IMAGE = "registry.k8s.io/pause:3.10"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
PROBE_ACTIVE_DEADLINE_SECONDS = 3600
OWNER_ANNOTATION = "isvtest.nvidia.com/bfx01-04-owner"
OWNER_ANNOTATION_PATH = "/metadata/annotations/isvtest.nvidia.com~1bfx01-04-owner"
MUTATION_OPT_IN_ENV = "ISVTEST_BREAKFIX_ALLOW_MUTATION"
UNCORDON_ATTEMPTS = 3
UNCORDON_RETRY_DELAY_SECONDS = 1.0


class CordonTestError(RuntimeError):
    """Raised when the cordon workflow cannot prove the required behavior."""


class KubectlTimeoutError(CordonTestError):
    """Raised when a kubectl process exceeds its finite deadline."""


@dataclass(frozen=True)
class NodeSelection:
    """A Ready node snapshot used for the atomic cordon claim."""

    name: str
    hostname: str
    resource_version: str
    spec: dict[str, Any]
    annotations_present: bool
    tolerations: list[dict[str, str]]


@dataclass(frozen=True)
class CordonOwnership:
    """The unique marker authorizing this run to restore a node."""

    node_name: str
    token: str


def _kubectl_command() -> list[str]:
    """Return the configured kubectl-compatible command prefix."""
    configured = os.environ.get("KUBECTL", "kubectl")
    try:
        command = shlex.split(configured)
    except ValueError as exc:
        raise CordonTestError(f"Invalid KUBECTL value: {exc}") from exc
    if not command:
        raise CordonTestError("KUBECTL must not be blank")
    return command


def _command_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """Return bounded stderr/stdout detail for a failed command."""
    detail = (completed.stderr or completed.stdout).strip()
    return detail[-500:] if detail else "command failed without output"


def _run(
    kubectl: list[str],
    *args: str,
    input_text: str | None = None,
    check: bool = True,
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded kubectl command and translate process failures."""
    if command_timeout_seconds <= 0 or request_timeout_seconds <= 0:
        raise CordonTestError("Kubectl command and request timeouts must be greater than zero")
    command = [*kubectl, *args, f"--request-timeout={request_timeout_seconds:g}s"]
    try:
        completed = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=command_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise KubectlTimeoutError(
            f"kubectl {' '.join(args)} timed out after {command_timeout_seconds:g} seconds"
        ) from exc
    except OSError as exc:
        raise CordonTestError(f"Unable to run {' '.join(kubectl)}: {exc}") from exc
    if check and completed.returncode != 0:
        raise CordonTestError(f"kubectl {' '.join(args)} failed: {_command_detail(completed)}")
    return completed


def _json_output(completed: subprocess.CompletedProcess[str], resource: str) -> dict[str, Any]:
    """Parse one kubectl JSON object."""
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CordonTestError(f"kubectl returned invalid JSON for {resource}") from exc
    if not isinstance(payload, dict):
        raise CordonTestError(f"kubectl returned a non-object for {resource}")
    return payload


def _node_is_available(node: dict[str, Any]) -> bool:
    """Return whether a node is Ready, schedulable, and unclaimed."""
    conditions = node.get("status", {}).get("conditions", [])
    ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions)
    metadata = node.get("metadata", {})
    annotations = metadata.get("annotations") or {}
    unclaimed = isinstance(annotations, dict) and OWNER_ANNOTATION not in annotations
    return ready and not node.get("spec", {}).get("unschedulable", False) and unclaimed


def _select_node(kubectl: list[str], requested_node: str | None) -> NodeSelection:
    """Select a schedulable Ready node and retain its concurrency snapshot."""
    payload = _json_output(_run(kubectl, "get", "nodes", "-o", "json"), "node list")
    items = payload.get("items")
    if not isinstance(items, list):
        raise CordonTestError("kubectl node list is missing items")

    candidates = [node for node in items if isinstance(node, dict) and _node_is_available(node)]
    if requested_node:
        candidates = [node for node in candidates if node.get("metadata", {}).get("name") == requested_node]
        if not candidates:
            raise CordonTestError(f"Requested node {requested_node!r} is not Ready, schedulable, and unclaimed")
    if not candidates:
        raise CordonTestError("No Ready, schedulable, unclaimed node is available for the cordon test")
    if not requested_node and len(candidates) != 1:
        raise CordonTestError("Multiple Ready, schedulable nodes are available; pass --node with a dedicated test node")

    node = candidates[0]
    metadata = node.get("metadata", {})
    spec = node.get("spec", {})
    name = metadata.get("name")
    hostname = metadata.get("labels", {}).get("kubernetes.io/hostname")
    resource_version = metadata.get("resourceVersion")
    if not isinstance(name, str) or not name:
        raise CordonTestError("Selected node is missing metadata.name")
    if not isinstance(hostname, str) or not hostname:
        raise CordonTestError(f"Node {name!r} is missing the kubernetes.io/hostname label")
    if not isinstance(resource_version, str) or not resource_version:
        raise CordonTestError(f"Node {name!r} is missing metadata.resourceVersion")
    if not isinstance(spec, dict):
        raise CordonTestError(f"Node {name!r} has invalid spec data")
    annotations = metadata.get("annotations")
    if annotations is not None and not isinstance(annotations, dict):
        raise CordonTestError(f"Node {name!r} has invalid metadata.annotations data")

    tolerations: list[dict[str, str]] = []
    for taint in spec.get("taints", []):
        key = taint.get("key")
        effect = taint.get("effect")
        if not isinstance(key, str) or effect not in {"NoSchedule", "NoExecute"}:
            continue
        if key == "node.kubernetes.io/unschedulable":
            continue
        tolerations.append(
            {
                "key": key,
                "operator": "Equal",
                "value": str(taint.get("value", "")),
                "effect": effect,
            }
        )
    return NodeSelection(
        name=name,
        hostname=hostname,
        resource_version=resource_version,
        spec=spec,
        annotations_present=annotations is not None,
        tolerations=tolerations,
    )


def _claim_node(kubectl: list[str], selection: NodeSelection, token: str) -> CordonOwnership:
    """Atomically mark a selected node unschedulable and record ownership."""
    ownership = CordonOwnership(node_name=selection.name, token=token)
    patch: list[dict[str, Any]] = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": selection.resource_version,
        }
    ]
    if "unschedulable" in selection.spec:
        patch.append({"op": "test", "path": "/spec/unschedulable", "value": False})
    else:
        # The field is optional; testing the full snapshot proves it is still
        # absent (and therefore false) when the conditional patch is applied.
        patch.append({"op": "test", "path": "/spec", "value": selection.spec})
    if selection.annotations_present:
        patch.append({"op": "add", "path": OWNER_ANNOTATION_PATH, "value": token})
    else:
        patch.append({"op": "add", "path": "/metadata/annotations", "value": {OWNER_ANNOTATION: token}})
    patch.append({"op": "add", "path": "/spec/unschedulable", "value": True})

    try:
        completed = _run(
            kubectl,
            "patch",
            "node",
            selection.name,
            "--type=json",
            "-p",
            json.dumps(patch, separators=(",", ":")),
            check=False,
        )
    except KubectlTimeoutError:
        # The API may have committed the atomic patch before the client lost
        # its response. Only claim cleanup ownership after observing our unique
        # marker and the expected state.
        if _claim_is_observed(kubectl, ownership):
            return ownership
        raise
    if completed.returncode != 0:
        # kubectl's own --request-timeout exits nonzero instead of raising a
        # process timeout. Its request can still have reached the API server,
        # so apply the same unique-marker verification before giving up cleanup
        # ownership.
        if _claim_is_observed(kubectl, ownership):
            return ownership
        raise CordonTestError(f"Could not atomically claim node {selection.name!r}: {_command_detail(completed)}")
    return ownership


def _get_node(kubectl: list[str], node_name: str) -> dict[str, Any]:
    """Return one node as a JSON object."""
    completed = _run(kubectl, "get", "node", node_name, "-o", "json")
    return _json_output(completed, f"node {node_name}")


def _owned_by(node: dict[str, Any], token: str) -> bool:
    """Return whether a node still carries this run's ownership marker."""
    annotations = node.get("metadata", {}).get("annotations") or {}
    return isinstance(annotations, dict) and annotations.get(OWNER_ANNOTATION) == token


def _claim_is_observed(kubectl: list[str], ownership: CordonOwnership) -> bool:
    """Confirm an ambiguous claim from its unique marker and cordoned state."""
    try:
        node = _get_node(kubectl, ownership.node_name)
    except CordonTestError:
        return False
    return _owned_by(node, ownership.token) and node.get("spec", {}).get("unschedulable") is True


def _release_node(kubectl: list[str], ownership: CordonOwnership) -> None:
    """Conditionally uncordon a node only while this run still owns it."""
    last_error = "conditional patch did not succeed"
    for attempt in range(UNCORDON_ATTEMPTS):
        try:
            node = _get_node(kubectl, ownership.node_name)
        except CordonTestError as exc:
            last_error = str(exc)
            if attempt + 1 < UNCORDON_ATTEMPTS:
                time.sleep(UNCORDON_RETRY_DELAY_SECONDS)
            continue
        metadata = node.get("metadata", {})
        spec = node.get("spec", {})
        annotations = metadata.get("annotations") or {}
        owner = annotations.get(OWNER_ANNOTATION) if isinstance(annotations, dict) else None
        unschedulable = spec.get("unschedulable", False) if isinstance(spec, dict) else False
        if owner is None and not unschedulable:
            return
        if owner != ownership.token:
            raise CordonTestError(f"Node {ownership.node_name!r} ownership changed; leaving schedulability unchanged")
        resource_version = metadata.get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            raise CordonTestError(f"Node {ownership.node_name!r} is missing metadata.resourceVersion")

        patch: list[dict[str, Any]] = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
            {"op": "test", "path": OWNER_ANNOTATION_PATH, "value": ownership.token},
        ]
        if unschedulable:
            patch.extend(
                [
                    {"op": "test", "path": "/spec/unschedulable", "value": True},
                    {"op": "replace", "path": "/spec/unschedulable", "value": False},
                ]
            )
        patch.append({"op": "remove", "path": OWNER_ANNOTATION_PATH})
        try:
            completed = _run(
                kubectl,
                "patch",
                "node",
                ownership.node_name,
                "--type=json",
                "-p",
                json.dumps(patch, separators=(",", ":")),
                check=False,
            )
        except KubectlTimeoutError as exc:
            # Re-read on the next attempt. If the patch committed, the missing
            # marker plus schedulable state is recognized as successful.
            last_error = str(exc)
            if attempt + 1 < UNCORDON_ATTEMPTS:
                time.sleep(UNCORDON_RETRY_DELAY_SECONDS)
            continue
        if completed.returncode == 0:
            return
        last_error = _command_detail(completed)
        if attempt + 1 < UNCORDON_ATTEMPTS:
            time.sleep(UNCORDON_RETRY_DELAY_SECONDS)
    raise CordonTestError(f"Could not safely uncordon node {ownership.node_name!r}: {last_error}")


def _pod_manifest(
    name: str,
    namespace: str,
    hostname: str,
    image: str,
    tolerations: list[dict[str, str]],
) -> str:
    """Build a minimal long-running pod constrained to one node hostname."""
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "isvtest",
                    "isvtest.nvidia.com/purpose": "bfx01-04",
                },
            },
            "spec": {
                "activeDeadlineSeconds": PROBE_ACTIVE_DEADLINE_SECONDS,
                "restartPolicy": "Never",
                "nodeSelector": {"kubernetes.io/hostname": hostname},
                "tolerations": tolerations,
                "containers": [{"name": "probe", "image": image}],
            },
        }
    )


def _get_pod(kubectl: list[str], namespace: str, name: str) -> dict[str, Any]:
    """Return one pod as a JSON object."""
    completed = _run(kubectl, "get", "pod", name, "-n", namespace, "-o", "json")
    return _json_output(completed, f"pod {namespace}/{name}")


def _pod_is_ready_on_node(pod: dict[str, Any], node_name: str) -> bool:
    """Return whether a pod is still Ready and bound to the expected node."""
    if pod.get("spec", {}).get("nodeName") != node_name or pod.get("status", {}).get("phase") != "Running":
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions)


def _pod_is_unschedulable(pod: dict[str, Any]) -> bool:
    """Return whether the scheduler explicitly reported the pod unschedulable."""
    if pod.get("spec", {}).get("nodeName"):
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(
        item.get("type") == "PodScheduled" and item.get("status") == "False" and item.get("reason") == "Unschedulable"
        for item in conditions
    )


def _wait_for_unschedulable(
    kubectl: list[str],
    namespace: str,
    name: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> bool:
    """Poll until Kubernetes reports that the new probe cannot be scheduled."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if _pod_is_unschedulable(_get_pod(kubectl, namespace, name)):
                return True
        except CordonTestError:
            # Treat transient API reads as retryable until the assertion's
            # own deadline expires.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_seconds)


def _cleanup(
    kubectl: list[str],
    namespace: str,
    pod_names: list[str],
    ownership: CordonOwnership | None,
) -> list[str]:
    """Delete probe pods and conditionally restore the owned node."""
    errors: list[str] = []
    for pod_name in pod_names:
        try:
            completed = _run(
                kubectl,
                "delete",
                "pod",
                pod_name,
                "-n",
                namespace,
                "--ignore-not-found=true",
                "--wait=false",
                check=False,
            )
        except CordonTestError as exc:
            errors.append(f"delete pod {namespace}/{pod_name}: {exc}")
            continue
        if completed.returncode != 0:
            errors.append(f"delete pod {namespace}/{pod_name}: {_command_detail(completed)}")
    if ownership:
        try:
            _release_node(kubectl, ownership)
        except CordonTestError as exc:
            errors.append(f"uncordon node {ownership.node_name}: {exc}")
    return errors


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Cordon a node and verify Kubernetes scheduling behavior")
    parser.add_argument("--node", help="Specific Ready, schedulable node to test")
    parser.add_argument("--namespace", default="default", help="Namespace for temporary probe pods")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="Container image for temporary probe pods")
    parser.add_argument("--timeout-seconds", type=float, default=120, help="Timeout for each scheduling assertion")
    parser.add_argument("--poll-interval-seconds", type=float, default=2, help="Pending-pod polling interval")
    return parser


def main() -> int:
    """Run the reversible cordon test and emit its provider-neutral JSON result."""
    args = _parser().parse_args()
    operation: dict[str, Any] = {
        "cordoned": False,
        "new_workloads_blocked": False,
        "existing_workloads_running": False,
    }
    result: dict[str, Any] = {"success": False, "platform": "kubernetes", "test_name": "cordon_node"}
    kubectl: list[str] = []
    created_pods: list[str] = []
    ownership: CordonOwnership | None = None

    try:
        if args.timeout_seconds <= 0 or args.poll_interval_seconds <= 0:
            raise CordonTestError("Timeout and poll interval must be greater than zero")
        if os.environ.get(MUTATION_OPT_IN_ENV) != "1":
            raise CordonTestError(
                f"Refusing to mutate cluster state; explicitly set {MUTATION_OPT_IN_ENV}=1 for BFX01-04"
            )
        kubectl = _kubectl_command()
        selection = _select_node(kubectl, args.node)
        operation["node_id"] = selection.name
        run_id = uuid.uuid4().hex
        suffix = run_id
        existing_pod = f"isvtest-bfx-existing-{suffix}"
        blocked_pod = f"isvtest-bfx-blocked-{suffix}"

        created_pods.append(existing_pod)
        _run(
            kubectl,
            "create",
            "-f",
            "-",
            input_text=_pod_manifest(
                existing_pod,
                args.namespace,
                selection.hostname,
                args.image,
                selection.tolerations,
            ),
        )
        _run(
            kubectl,
            "wait",
            "--for=condition=Ready",
            f"pod/{existing_pod}",
            "-n",
            args.namespace,
            f"--timeout={args.timeout_seconds:g}s",
            command_timeout_seconds=args.timeout_seconds + DEFAULT_REQUEST_TIMEOUT_SECONDS + 5,
            request_timeout_seconds=args.timeout_seconds + 5,
        )

        # Pod startup can take long enough for kubelet status updates to advance
        # resourceVersion. Refresh immediately before the conditional claim.
        claim_selection = _select_node(kubectl, selection.name)
        # Retain a conditional cleanup candidate before issuing the PATCH. If
        # the API commits the claim but both the PATCH response and immediate
        # verification GET are lost, cleanup can later release the node only
        # after observing this exact unique annotation.
        ownership = CordonOwnership(selection.name, f"isvtest-bfx01-04-{run_id}")
        _claim_node(kubectl, claim_selection, ownership.token)
        node = _get_node(kubectl, selection.name)
        operation["cordoned"] = node.get("spec", {}).get("unschedulable") is True and _owned_by(node, ownership.token)
        if not operation["cordoned"]:
            raise CordonTestError(f"Node {selection.name!r} was not marked unschedulable by this run")

        operation["existing_workloads_running"] = _pod_is_ready_on_node(
            _get_pod(kubectl, args.namespace, existing_pod), selection.name
        )
        if not operation["existing_workloads_running"]:
            raise CordonTestError("Existing probe pod did not remain Ready on the cordoned node")

        created_pods.append(blocked_pod)
        _run(
            kubectl,
            "create",
            "-f",
            "-",
            input_text=_pod_manifest(
                blocked_pod,
                args.namespace,
                selection.hostname,
                args.image,
                selection.tolerations,
            ),
        )
        operation["new_workloads_blocked"] = _wait_for_unschedulable(
            kubectl,
            args.namespace,
            blocked_pod,
            args.timeout_seconds,
            args.poll_interval_seconds,
        )
        if not operation["new_workloads_blocked"]:
            raise CordonTestError("New probe pod was not confirmed unschedulable on the cordoned node")
        result["success"] = True
    except CordonTestError as exc:
        result["error"] = str(exc)
    except Exception as exc:
        result["error"] = f"Unexpected cordon test failure: {exc}"
    finally:
        try:
            cleanup_errors = _cleanup(kubectl, args.namespace, created_pods, ownership) if kubectl else []
        except Exception as exc:
            cleanup_errors = [f"unexpected cleanup failure: {exc}"]
        if cleanup_errors:
            result["success"] = False
            result["cleanup_errors"] = cleanup_errors
            result.setdefault("error", "Cordon test cleanup failed")

    result["operation"] = operation
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
