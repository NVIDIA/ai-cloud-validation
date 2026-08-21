#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise a reversible Kubernetes NodeMaintenance request for BFX01-02."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any
from urllib.parse import quote

DEFAULT_IMAGE = "registry.k8s.io/pause:3.10"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
MUTATION_OPT_IN_ENV = "ISVTEST_BREAKFIX_ALLOW_MUTATION"
NODE_MAINTENANCE_RESOURCE = "nodemaintenances.maintenance.nvidia.com"
NODE_MAINTENANCE_CRD = f"{NODE_MAINTENANCE_RESOURCE}"
RUN_LABEL = "isvtest.nvidia.com/bfx01-02-run"
REQUESTOR_ID = "bfx01-02.isvtest.nvidia.com"


class MaintenanceTestError(RuntimeError):
    """Raised when the maintenance workflow cannot prove the requirement."""


class KubectlTimeoutError(MaintenanceTestError):
    """Raised when a bounded kubectl process exceeds its deadline."""


def _kubectl_command() -> list[str]:
    """Return the configured kubectl-compatible command prefix."""
    configured = os.environ.get("KUBECTL", "kubectl")
    try:
        command = shlex.split(configured)
    except ValueError as exc:
        raise MaintenanceTestError(f"Invalid KUBECTL value: {exc}") from exc
    if not command:
        raise MaintenanceTestError("KUBECTL must not be blank")
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
        raise MaintenanceTestError("Kubectl command and request timeouts must be greater than zero")
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
        raise MaintenanceTestError(f"Unable to run {' '.join(kubectl)}: {exc}") from exc
    if check and completed.returncode != 0:
        raise MaintenanceTestError(f"kubectl {' '.join(args)} failed: {_command_detail(completed)}")
    return completed


def _json_output(completed: subprocess.CompletedProcess[str], resource: str) -> dict[str, Any]:
    """Parse one kubectl JSON object."""
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MaintenanceTestError(f"kubectl returned invalid JSON for {resource}") from exc
    if not isinstance(payload, dict):
        raise MaintenanceTestError(f"kubectl returned a non-object for {resource}")
    return payload


def _get_json(kubectl: list[str], *args: str, resource: str) -> dict[str, Any]:
    """Get one Kubernetes object or list as JSON."""
    return _json_output(_run(kubectl, *args, "-o", "json"), resource)


def _condition_status(payload: dict[str, Any], condition_type: str) -> tuple[str, str, int | None]:
    """Return status, reason, and observed generation for one condition."""
    conditions = payload.get("status", {}).get("conditions", [])
    if not isinstance(conditions, list):
        return "", "", None
    for condition in conditions:
        if not isinstance(condition, dict) or condition.get("type") != condition_type:
            continue
        observed = condition.get("observedGeneration")
        return (
            str(condition.get("status") or ""),
            str(condition.get("reason") or ""),
            observed if isinstance(observed, int) else None,
        )
    return "", "", None


def _node_ready_and_schedulable(node: dict[str, Any]) -> bool:
    """Return whether a node is Ready and not already under maintenance."""
    conditions = node.get("status", {}).get("conditions", [])
    ready = any(
        isinstance(item, dict) and item.get("type") == "Ready" and item.get("status") == "True" for item in conditions
    )
    return ready and node.get("spec", {}).get("unschedulable", False) is not True


def _require_permission(
    kubectl: list[str],
    verb: str,
    resource: str,
    *,
    namespace: str | None = None,
    all_namespaces: bool = False,
) -> None:
    """Require one Kubernetes permission before creating test resources."""
    args = ["auth", "can-i", verb, resource]
    if namespace:
        args.extend(["-n", namespace])
    if all_namespaces:
        args.append("--all-namespaces")
    completed = _run(kubectl, *args, check=False)
    output = completed.stdout.strip().lower()
    verdict = output.split(maxsplit=1)[0] if output else ""
    if completed.returncode == 0 and verdict == "yes":
        return
    scope = f" in {namespace}" if namespace else " cluster-wide"
    if verdict == "no":
        raise MaintenanceTestError(f"Kubernetes RBAC does not allow {verb} on {resource}{scope}")
    if completed.returncode != 0:
        raise MaintenanceTestError(f"kubectl {' '.join(args)} failed: {_command_detail(completed)}")
    raise MaintenanceTestError(f"Kubernetes RBAC returned an unexpected response for {verb} on {resource}{scope}")


def _maintenance_requests(kubectl: list[str], node_name: str) -> list[dict[str, Any]]:
    """Return NodeMaintenance objects targeting the explicit node."""
    existing = _get_json(
        kubectl,
        "get",
        NODE_MAINTENANCE_RESOURCE,
        "-A",
        resource="NodeMaintenance list",
    )
    items = existing.get("items")
    if not isinstance(items, list):
        raise MaintenanceTestError("NodeMaintenance list is missing items")
    return [item for item in items if isinstance(item, dict) and item.get("spec", {}).get("nodeName") == node_name]


def _maintenance_owners(kubectl: list[str], node_name: str) -> list[str]:
    """Return namespaced names of maintenance requests targeting a node."""
    return [
        f"{item.get('metadata', {}).get('namespace', '')}/{item.get('metadata', {}).get('name', '')}"
        for item in _maintenance_requests(kubectl, node_name)
    ]


def _require_unclaimed_node(kubectl: list[str], node_name: str) -> dict[str, Any]:
    """Require a Ready, schedulable node without another maintenance request."""
    node = _get_json(kubectl, "get", "node", node_name, resource=f"node {node_name}")
    if node.get("metadata", {}).get("name") != node_name:
        raise MaintenanceTestError("Kubernetes returned a different node than the explicit target")
    if not _node_ready_and_schedulable(node):
        raise MaintenanceTestError(f"Target node {node_name!r} must be Ready and schedulable")
    owners = _maintenance_owners(kubectl, node_name)
    if owners:
        raise MaintenanceTestError(
            f"Target node {node_name!r} already has a NodeMaintenance request: {', '.join(owners)}"
        )
    return node


def _preflight(kubectl: list[str], node_name: str, namespace: str) -> dict[str, Any]:
    """Validate the API, permissions, target node, and exclusive ownership."""
    _run(kubectl, "get", "crd", NODE_MAINTENANCE_CRD)
    for verb in ("create", "get", "delete"):
        _require_permission(kubectl, verb, NODE_MAINTENANCE_RESOURCE, namespace=namespace)
    _require_permission(kubectl, "list", NODE_MAINTENANCE_RESOURCE, all_namespaces=True)
    _require_permission(kubectl, "get", "nodes")
    for verb in ("create", "get", "delete"):
        _require_permission(kubectl, verb, "deployments.apps", namespace=namespace)
    _require_permission(kubectl, "list", "pods", namespace=namespace)

    return _require_unclaimed_node(kubectl, node_name)


def _node_tolerations(node: dict[str, Any]) -> list[dict[str, str]]:
    """Mirror existing node taints without tolerating the maintenance cordon."""
    tolerations: list[dict[str, str]] = []
    for taint in node.get("spec", {}).get("taints", []):
        if not isinstance(taint, dict):
            continue
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
                "value": str(taint.get("value") or ""),
                "effect": effect,
            }
        )
    return tolerations


def _deployment_manifest(
    name: str,
    namespace: str,
    hostname: str,
    run_id: str,
    image: str,
    tolerations: list[dict[str, str]],
) -> str:
    """Build a uniquely labelled probe Deployment pinned to the target node."""
    labels = {
        "app.kubernetes.io/managed-by": "isvtest",
        "isvtest.nvidia.com/purpose": "bfx01-02",
        RUN_LABEL: run_id,
    }
    return json.dumps(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {RUN_LABEL: run_id}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "nodeSelector": {"kubernetes.io/hostname": hostname},
                        "tolerations": tolerations,
                        "containers": [{"name": "probe", "image": image}],
                    },
                },
            },
        },
        separators=(",", ":"),
    )


def _maintenance_manifest(
    name: str,
    namespace: str,
    node_name: str,
    run_id: str,
    timeout_seconds: int,
) -> str:
    """Build a NodeMaintenance request that drains only the owned probe."""
    return json.dumps(
        {
            "apiVersion": "maintenance.nvidia.com/v1alpha1",
            "kind": "NodeMaintenance",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "isvtest",
                    "isvtest.nvidia.com/purpose": "bfx01-02",
                    RUN_LABEL: run_id,
                },
            },
            "spec": {
                "requestorID": REQUESTOR_ID,
                "nodeName": node_name,
                "cordon": True,
                "drainSpec": {
                    "force": False,
                    "deleteEmptyDir": False,
                    "podSelector": f"{RUN_LABEL}={run_id}",
                    "timeoutSeconds": timeout_seconds,
                },
            },
        },
        separators=(",", ":"),
    )


def _list_probe_pods(kubectl: list[str], namespace: str, run_id: str) -> list[dict[str, Any]]:
    """Return probe pods created by this run."""
    payload = _get_json(
        kubectl,
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        f"{RUN_LABEL}={run_id}",
        resource="probe pod list",
    )
    items = payload.get("items")
    if not isinstance(items, list):
        raise MaintenanceTestError("Probe pod list is missing items")
    return [item for item in items if isinstance(item, dict)]


def _validate_owned_resource(
    payload: dict[str, Any],
    *,
    kind: str,
    name: str,
    namespace: str,
    run_id: str,
    node_name: str | None = None,
) -> dict[str, Any]:
    """Require exact identity and ownership on a resource created by this run."""
    metadata = payload.get("metadata", {})
    if metadata.get("name") != name or metadata.get("namespace") != namespace:
        raise MaintenanceTestError(f"Kubernetes returned a different {kind} than the owned resource")
    if metadata.get("labels", {}).get(RUN_LABEL) != run_id:
        raise MaintenanceTestError(f"Refusing to use {kind} {namespace}/{name} without this run's ownership label")
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise MaintenanceTestError(f"Owned {kind} {namespace}/{name} has no Kubernetes UID")
    if node_name is not None:
        spec = payload.get("spec", {})
        if spec.get("nodeName") != node_name or spec.get("requestorID") != REQUESTOR_ID:
            raise MaintenanceTestError("NodeMaintenance ownership fields do not match this run")
    return payload


def _read_owned_resource(
    kubectl: list[str],
    kind: str,
    name: str,
    namespace: str,
    run_id: str,
    *,
    node_name: str | None = None,
) -> dict[str, Any] | None:
    """Read and validate an owned resource, returning None only when absent."""
    completed = _run(
        kubectl,
        "get",
        kind,
        name,
        "-n",
        namespace,
        "-o",
        "json",
        check=False,
    )
    if completed.returncode != 0:
        detail = _command_detail(completed)
        if "notfound" in detail.lower() or "not found" in detail.lower():
            return None
        raise MaintenanceTestError(f"get {kind} {namespace}/{name} failed: {detail}")
    payload = _json_output(completed, f"{kind} {namespace}/{name}")
    return _validate_owned_resource(
        payload,
        kind=kind,
        name=name,
        namespace=namespace,
        run_id=run_id,
        node_name=node_name,
    )


def _create_owned_resource(
    kubectl: list[str],
    kind: str,
    name: str,
    namespace: str,
    run_id: str,
    manifest: str,
    *,
    node_name: str | None = None,
) -> dict[str, Any]:
    """Create a unique owned resource and recover safely from a lost response."""
    try:
        completed = _run(
            kubectl,
            "create",
            "-f",
            "-",
            "-o",
            "json",
            input_text=manifest,
            check=False,
        )
    except KubectlTimeoutError as exc:
        observed = _read_owned_resource(
            kubectl,
            kind,
            name,
            namespace,
            run_id,
            node_name=node_name,
        )
        if observed is None:
            raise exc
        return observed
    if completed.returncode == 0:
        payload = _json_output(completed, f"created {kind} {namespace}/{name}")
        return _validate_owned_resource(
            payload,
            kind=kind,
            name=name,
            namespace=namespace,
            run_id=run_id,
            node_name=node_name,
        )
    observed = _read_owned_resource(
        kubectl,
        kind,
        name,
        namespace,
        run_id,
        node_name=node_name,
    )
    if observed is None:
        raise MaintenanceTestError(f"Could not create {kind}: {_command_detail(completed)}")
    return observed


def _pod_ready_on_node(pod: dict[str, Any], node_name: str) -> bool:
    """Return whether one probe is Ready on the target node."""
    if pod.get("spec", {}).get("nodeName") != node_name or pod.get("status", {}).get("phase") != "Running":
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(
        isinstance(item, dict) and item.get("type") == "Ready" and item.get("status") == "True" for item in conditions
    )


def _pod_unschedulable(pod: dict[str, Any]) -> bool:
    """Return whether a replacement probe is blocked by node maintenance."""
    if pod.get("spec", {}).get("nodeName"):
        return False
    conditions = pod.get("status", {}).get("conditions", [])
    return any(
        isinstance(item, dict)
        and item.get("type") == "PodScheduled"
        and item.get("status") == "False"
        and item.get("reason") == "Unschedulable"
        for item in conditions
    )


def _wait_for_initial_probe(
    kubectl: list[str],
    namespace: str,
    run_id: str,
    node_name: str,
    deadline: float,
    poll_interval_seconds: float,
) -> str:
    """Wait for the original probe and return its Kubernetes UID."""
    while True:
        for pod in _list_probe_pods(kubectl, namespace, run_id):
            if _pod_ready_on_node(pod, node_name):
                uid = pod.get("metadata", {}).get("uid")
                if isinstance(uid, str) and uid:
                    return uid
        if time.monotonic() >= deadline:
            raise MaintenanceTestError("Owned probe did not become Ready on the target node")
        time.sleep(poll_interval_seconds)


def _wait_for_maintenance_ready(
    kubectl: list[str],
    namespace: str,
    name: str,
    deadline: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    """Wait until the operator reports Ready or RequestorFailed."""
    while True:
        payload = _get_json(
            kubectl,
            "get",
            NODE_MAINTENANCE_RESOURCE,
            name,
            "-n",
            namespace,
            resource=f"NodeMaintenance {namespace}/{name}",
        )
        generation = payload.get("metadata", {}).get("generation")
        failed, failed_reason, failed_generation = _condition_status(payload, "RequestorFailed")
        if failed == "True" and failed_generation == generation:
            raise MaintenanceTestError(f"NodeMaintenance failed: {failed_reason or 'operator reported failure'}")
        ready, ready_reason, ready_generation = _condition_status(payload, "Ready")
        if ready_reason == "MaintenanceFailed" and ready_generation == generation:
            raise MaintenanceTestError("NodeMaintenance entered MaintenanceFailed state")
        if ready == "True" and ready_reason == "Ready" and ready_generation == generation:
            return payload
        if time.monotonic() >= deadline:
            raise MaintenanceTestError("Timed out waiting for NodeMaintenance Ready=True")
        time.sleep(poll_interval_seconds)


def _wait_for_replacement_blocked(
    kubectl: list[str],
    namespace: str,
    run_id: str,
    original_uid: str,
    deadline: float,
    poll_interval_seconds: float,
) -> tuple[bool, bool]:
    """Wait for original eviction and a different unschedulable replacement."""
    evacuated = False
    while True:
        pods = _list_probe_pods(kubectl, namespace, run_id)
        uids = {str(pod.get("metadata", {}).get("uid")) for pod in pods if pod.get("metadata", {}).get("uid")}
        evacuated = original_uid not in uids
        replacement_blocked = any(
            str(pod.get("metadata", {}).get("uid") or "") != original_uid and _pod_unschedulable(pod) for pod in pods
        )
        if evacuated and replacement_blocked:
            return True, True
        if time.monotonic() >= deadline:
            return evacuated, replacement_blocked
        time.sleep(poll_interval_seconds)


def _wait_for_recovery(
    kubectl: list[str],
    namespace: str,
    run_id: str,
    node_name: str,
    deadline: float,
    poll_interval_seconds: float,
) -> bool:
    """Wait for a replacement probe to become Ready after maintenance."""
    while True:
        if any(_pod_ready_on_node(pod, node_name) for pod in _list_probe_pods(kubectl, namespace, run_id)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_seconds)


def _delete_resource(
    kubectl: list[str],
    kind: str,
    name: str,
    namespace: str,
    *,
    uid: str,
) -> None:
    """Delete one resource atomically using its Kubernetes UID."""
    escaped_namespace = quote(namespace, safe="")
    escaped_name = quote(name, safe="")
    if kind == NODE_MAINTENANCE_RESOURCE:
        uri = f"/apis/maintenance.nvidia.com/v1alpha1/namespaces/{escaped_namespace}/nodemaintenances/{escaped_name}"
    elif kind == "deployment":
        uri = f"/apis/apps/v1/namespaces/{escaped_namespace}/deployments/{escaped_name}"
    else:
        raise MaintenanceTestError(f"No atomic-delete API path is defined for {kind}")
    delete_options = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": uid},
            "propagationPolicy": "Background",
        },
        separators=(",", ":"),
    )
    completed = _run(
        kubectl,
        "delete",
        f"--raw={uri}",
        "-f",
        "-",
        input_text=delete_options,
        check=False,
    )
    if completed.returncode != 0:
        detail = _command_detail(completed)
        if "notfound" in detail.lower() or "not found" in detail.lower():
            return
        raise MaintenanceTestError(f"delete {kind} {namespace}/{name} failed: {detail}")


def _delete_owned_resource(
    kubectl: list[str],
    kind: str,
    name: str,
    namespace: str,
    run_id: str,
    *,
    expected_uid: str,
    timeout_seconds: float,
    node_name: str | None = None,
) -> None:
    """Delete and wait for only the exact resource UID created by this run."""
    payload = _read_owned_resource(
        kubectl,
        kind,
        name,
        namespace,
        run_id,
        node_name=node_name,
    )
    if payload is None:
        return
    actual_uid = str(payload.get("metadata", {}).get("uid") or "")
    if expected_uid and actual_uid != expected_uid:
        raise MaintenanceTestError(f"Refusing to delete replaced {kind} {namespace}/{name}")
    additional = payload.get("spec", {}).get("additionalRequestors") if node_name else None
    try:
        _delete_resource(
            kubectl,
            kind,
            name,
            namespace,
            uid=actual_uid,
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            observed = _read_owned_resource(
                kubectl,
                kind,
                name,
                namespace,
                run_id,
                node_name=node_name,
            )
            if observed is None:
                return
            observed_uid = str(observed.get("metadata", {}).get("uid") or "")
            if observed_uid != actual_uid:
                raise MaintenanceTestError(f"A replacement {kind} appeared while waiting for deletion")
            if time.monotonic() >= deadline:
                raise MaintenanceTestError(f"Timed out waiting for {kind} {namespace}/{name} deletion")
            time.sleep(1)
    except MaintenanceTestError as exc:
        suffix = ""
        if isinstance(additional, list) and additional:
            suffix = f"; {len(additional)} additional requestor(s) still hold maintenance"
        raise MaintenanceTestError(f"{exc}{suffix}") from exc


def _wait_for_probe_absent(
    kubectl: list[str],
    namespace: str,
    run_id: str,
    deadline: float,
    poll_interval_seconds: float,
) -> bool:
    """Wait for cascading deletion of every pod owned by the probe run."""
    while True:
        if not _list_probe_pods(kubectl, namespace, run_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_seconds)


def _wait_for_node_restored(
    kubectl: list[str],
    node_name: str,
    deadline: float,
    poll_interval_seconds: float,
) -> bool:
    """Wait for the operator to return the target node to its initial state."""
    while True:
        node = _get_json(kubectl, "get", "node", node_name, resource=f"node {node_name}")
        if _node_ready_and_schedulable(node):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_seconds)


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Request and restore Kubernetes node maintenance")
    parser.add_argument("--node", default="", help="Explicit Ready node dedicated to this validation")
    parser.add_argument("--namespace", default="default", help="Namespace for owned validation resources")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="Container image for the owned probe")
    parser.add_argument("--timeout-seconds", type=float, default=300, help="Timeout for each state transition")
    parser.add_argument("--poll-interval-seconds", type=float, default=2, help="State polling interval")
    return parser


def main() -> int:
    """Run one reversible NodeMaintenance request and emit provider-neutral JSON."""
    args = _parser().parse_args()
    operation: dict[str, Any] = {
        "requested": False,
        "accepted": False,
        "maintenance_mode": "",
        "workload_evacuated": False,
        "replacement_blocked": False,
        "workload_recovered": False,
        "restored": False,
    }
    result: dict[str, Any] = {
        "success": False,
        "platform": "kubernetes",
        "test_name": "return_node_maintenance",
    }
    kubectl: list[str] = []
    deployment_create_attempted = False
    maintenance_create_attempted = False
    deployment_uid = ""
    maintenance_uid = ""
    run_id = uuid.uuid4().hex[:16]
    deployment_name = f"isvtest-bfx01-02-probe-{run_id}"
    maintenance_name = f"isvtest-bfx01-02-{run_id}"

    try:
        if args.timeout_seconds <= 0 or args.poll_interval_seconds <= 0:
            raise MaintenanceTestError("Timeout and poll interval must be greater than zero")
        if os.environ.get(MUTATION_OPT_IN_ENV) != "1":
            raise MaintenanceTestError(
                f"Refusing to mutate cluster state; explicitly set {MUTATION_OPT_IN_ENV}=1 for BFX01-02"
            )
        node_name = args.node.strip()
        if not node_name:
            raise MaintenanceTestError("BFX01-02 requires an explicit --node dedicated to the validation")
        operation["node_id"] = node_name
        kubectl = _kubectl_command()
        node = _preflight(kubectl, node_name, args.namespace)
        hostname = node.get("metadata", {}).get("labels", {}).get("kubernetes.io/hostname")
        if not isinstance(hostname, str) or not hostname:
            raise MaintenanceTestError(f"Target node {node_name!r} is missing the kubernetes.io/hostname label")

        deployment_create_attempted = True
        deployment = _create_owned_resource(
            kubectl,
            "deployment",
            deployment_name,
            args.namespace,
            run_id,
            _deployment_manifest(
                deployment_name,
                args.namespace,
                hostname,
                run_id,
                args.image,
                _node_tolerations(node),
            ),
        )
        deployment_uid = str(deployment["metadata"]["uid"])
        deadline = time.monotonic() + args.timeout_seconds
        original_uid = _wait_for_initial_probe(
            kubectl,
            args.namespace,
            run_id,
            node_name,
            deadline,
            args.poll_interval_seconds,
        )

        # Close the preflight/create window immediately before asking the
        # operator to mutate the node. The operator itself schedules at most
        # one NodeMaintenance per node if another request races this check.
        _require_unclaimed_node(kubectl, node_name)
        maintenance_create_attempted = True
        operation["requested"] = True
        maintenance_created = _create_owned_resource(
            kubectl,
            NODE_MAINTENANCE_RESOURCE,
            maintenance_name,
            args.namespace,
            run_id,
            _maintenance_manifest(
                maintenance_name,
                args.namespace,
                node_name,
                run_id,
                max(1, int(args.timeout_seconds)),
            ),
            node_name=node_name,
        )
        maintenance_uid = str(maintenance_created["metadata"]["uid"])
        node_requests = _maintenance_requests(kubectl, node_name)
        request_uids = {item.get("metadata", {}).get("uid") for item in node_requests}
        if len(node_requests) != 1 or request_uids != {maintenance_uid}:
            raise MaintenanceTestError("A concurrent NodeMaintenance request claimed the target node")

        deadline = time.monotonic() + args.timeout_seconds
        maintenance = _wait_for_maintenance_ready(
            kubectl,
            args.namespace,
            maintenance_name,
            deadline,
            args.poll_interval_seconds,
        )
        node = _get_json(kubectl, "get", "node", node_name, resource=f"node {node_name}")
        if node.get("spec", {}).get("unschedulable") is not True:
            raise MaintenanceTestError("NodeMaintenance reported Ready but the node is not cordoned")

        drain = maintenance.get("status", {}).get("drain") or {}
        eviction_pods = drain.get("evictionPods")
        if eviction_pods != 1:
            raise MaintenanceTestError("NodeMaintenance did not report exactly one owned probe for eviction")
        if drain.get("drainProgress") != 100:
            raise MaintenanceTestError("NodeMaintenance reported Ready without completing its drain")
        if drain.get("waitForEviction") not in (None, []):
            raise MaintenanceTestError("NodeMaintenance reported Ready with pending pod evictions")

        transition_deadline = time.monotonic() + args.timeout_seconds
        evacuated, replacement_blocked = _wait_for_replacement_blocked(
            kubectl,
            args.namespace,
            run_id,
            original_uid,
            transition_deadline,
            args.poll_interval_seconds,
        )
        operation["workload_evacuated"] = evacuated
        operation["replacement_blocked"] = replacement_blocked
        if not evacuated or not replacement_blocked:
            raise MaintenanceTestError(
                "NodeMaintenance did not prove owned workload evacuation and replacement blocking"
            )
        operation["maintenance_mode"] = "Maintenance"
        operation["accepted"] = True
    except MaintenanceTestError as exc:
        result["error"] = str(exc)
    finally:
        cleanup_errors: list[str] = []
        if kubectl and maintenance_create_attempted:
            try:
                _delete_owned_resource(
                    kubectl,
                    NODE_MAINTENANCE_RESOURCE,
                    maintenance_name,
                    args.namespace,
                    run_id,
                    expected_uid=maintenance_uid,
                    timeout_seconds=args.timeout_seconds,
                    node_name=str(operation.get("node_id") or ""),
                )
            except MaintenanceTestError as exc:
                cleanup_errors.append(f"delete NodeMaintenance: {exc}")
        if kubectl and operation.get("node_id") and maintenance_create_attempted:
            try:
                deadline = time.monotonic() + args.timeout_seconds
                node_restored = _wait_for_node_restored(
                    kubectl,
                    str(operation["node_id"]),
                    deadline,
                    args.poll_interval_seconds,
                )
                if not node_restored:
                    cleanup_errors.append("NodeMaintenance deletion did not restore node schedulability")
                elif deployment_create_attempted:
                    recovery_deadline = time.monotonic() + args.timeout_seconds
                    operation["workload_recovered"] = _wait_for_recovery(
                        kubectl,
                        args.namespace,
                        run_id,
                        str(operation["node_id"]),
                        recovery_deadline,
                        args.poll_interval_seconds,
                    )
                    if not operation["workload_recovered"]:
                        cleanup_errors.append("Owned workload did not recover after maintenance")
                operation["restored"] = bool(node_restored and operation["workload_recovered"])
            except MaintenanceTestError as exc:
                cleanup_errors.append(f"verify restoration: {exc}")
        if kubectl and deployment_create_attempted:
            try:
                cleanup_timeout = min(args.timeout_seconds, 120)
                _delete_owned_resource(
                    kubectl,
                    "deployment",
                    deployment_name,
                    args.namespace,
                    run_id,
                    expected_uid=deployment_uid,
                    timeout_seconds=cleanup_timeout,
                )
                cleanup_deadline = time.monotonic() + cleanup_timeout
                if not _wait_for_probe_absent(
                    kubectl,
                    args.namespace,
                    run_id,
                    cleanup_deadline,
                    args.poll_interval_seconds,
                ):
                    cleanup_errors.append("Probe pods remained after Deployment cleanup")
            except MaintenanceTestError as exc:
                cleanup_errors.append(f"delete probe Deployment: {exc}")
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            result.setdefault("error", "Node maintenance cleanup failed")

    result["success"] = bool(
        operation["requested"]
        and operation["accepted"]
        and operation["workload_evacuated"]
        and operation["replacement_blocked"]
        and operation["workload_recovered"]
        and operation["restored"]
        and not result.get("cleanup_errors")
    )
    if not result["success"]:
        result.setdefault("error", "Node maintenance validation did not complete")
    result["operation"] = operation
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
