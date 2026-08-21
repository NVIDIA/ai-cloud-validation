#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Inspect NVIDIA node-health agents with read-only systemd or Kubernetes queries.

BFX04-01 accepts either NVIDIA Fleet Intelligence Agent (GPUd) or the
NVSentinel GPU Health Monitor. A GPU node is covered only when a supported
agent pod is Running, Ready, and controlled by a matching DaemonSet.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any

AGENT_LABEL = "app.kubernetes.io/name"
SUPPORTED_AGENTS = frozenset({"fleet-intelligence-agent", "gpu-health-monitor"})
GPU_LABEL = "nvidia.com/gpu.present"
GPU_RESOURCE = "nvidia.com/gpu"
SYSTEMD_AGENTS = ("fleetintd", "gpud", "nvsentinel", "gpu-health-monitor")
NODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class KubernetesQueryError(RuntimeError):
    """Raised when a read-only Kubernetes inventory query fails."""


class NodeQueryError(RuntimeError):
    """Raised when a read-only bare-metal node query fails."""


class CommandOverrideError(RuntimeError):
    """Raised when a configured command prefix cannot be parsed."""


def _command_override(env_name: str, default: str) -> list[str]:
    """Return a safe configured command prefix or locate its default binary."""
    override = os.environ.get(env_name, "").strip()
    if override:
        try:
            command = shlex.split(override)
        except ValueError as exc:
            raise CommandOverrideError(f"Invalid {env_name} command override") from exc
        if command:
            return command
    executable = shutil.which(default)
    if executable:
        return [executable]
    raise NodeQueryError(f"No {default}-compatible command is available")


def _kubectl_command() -> list[str]:
    """Return the configured kubectl-compatible command prefix."""
    try:
        return _command_override("KUBECTL", "kubectl")
    except NodeQueryError:
        pass
    microk8s = shutil.which("microk8s")
    if microk8s:
        return [microk8s, "kubectl"]
    raise KubernetesQueryError("No kubectl-compatible command is available")


def _ssh_command() -> list[str]:
    """Return the configured SSH-compatible command prefix."""
    return _command_override("SSH", "ssh")


def _parse_nodes(value: str) -> list[str]:
    """Parse and validate comma/whitespace-separated SSH node names."""
    nodes = list(dict.fromkeys(part for part in re.split(r"[\s,]+", value.strip()) if part))
    invalid = [node for node in nodes if not NODE_PATTERN.fullmatch(node)]
    if invalid:
        raise NodeQueryError("Invalid bare-metal node name")
    return nodes


def _query_systemd_node(node: str) -> dict[str, Any]:
    """Return supported systemd-agent status for one SSH-reachable node."""
    remote_command = "systemctl is-active " + " ".join(SYSTEMD_AGENTS) + " 2>/dev/null || true"
    command = [
        *_ssh_command(),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        node,
        remote_command,
    ]
    try:
        response = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NodeQueryError(f"Node health agent query failed for {node}") from exc
    if response.returncode != 0:
        raise NodeQueryError(f"Node health agent query failed for {node}")
    states = response.stdout.splitlines()
    if len(states) != len(SYSTEMD_AGENTS):
        raise NodeQueryError(f"Node health agent query returned invalid status for {node}")
    active = [service for service, state in zip(SYSTEMD_AGENTS, states, strict=True) if state.strip() == "active"]
    return {
        "node_id": node,
        "agent_name": active[0] if active else "GPUd/Sentinel",
        "running": bool(active),
    }


def _evaluate_systemd(nodes: list[str]) -> dict[str, Any]:
    """Build provider-neutral BFX04-01 evidence from systemd service states."""
    return {
        "success": True,
        "platform": "bare_metal",
        "test_name": "query_node_health_agents",
        "agents_observable": True,
        "agents": [_query_systemd_node(node) for node in nodes],
    }


def _get_collection(resource: str, *, all_namespaces: bool = False) -> dict[str, Any]:
    """Read one Kubernetes resource collection and decode its JSON document."""
    command = [*_kubectl_command(), "get", resource]
    if all_namespaces:
        command.append("--all-namespaces")
    command.extend(["--output=json", "--request-timeout=30s"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KubernetesQueryError(f"Kubernetes API query failed for {resource}") from exc
    if result.returncode != 0:
        raise KubernetesQueryError(f"Kubernetes API query failed for {resource}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise KubernetesQueryError(f"Kubernetes API returned invalid JSON for {resource}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise KubernetesQueryError(f"Kubernetes API returned an invalid collection for {resource}")
    return payload


def _positive_gpu_count(node: dict[str, Any]) -> bool:
    """Return whether node capacity or allocatable data proves a GPU exists."""
    status = node.get("status") if isinstance(node.get("status"), dict) else {}
    for field in ("capacity", "allocatable"):
        resources = status.get(field) if isinstance(status.get(field), dict) else {}
        value = resources.get(GPU_RESOURCE)
        try:
            if value is not None and int(value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _gpu_nodes(nodes: dict[str, Any]) -> list[str]:
    """Return sorted node names identified as GPU-bearing by label or capacity."""
    names: set[str] = set()
    for node in nodes.get("items", []):
        if not isinstance(node, dict):
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        name = metadata.get("name")
        if (
            isinstance(name, str)
            and name
            and (str(labels.get(GPU_LABEL, "")).lower() == "true" or _positive_gpu_count(node))
        ):
            names.add(name)
    return sorted(names)


def _supported_daemonsets(daemonsets: dict[str, Any]) -> list[dict[str, str]]:
    """Return identity records for exact-label supported agent DaemonSets."""
    records: list[dict[str, str]] = []
    for daemonset in daemonsets.get("items", []):
        if not isinstance(daemonset, dict):
            continue
        metadata = daemonset.get("metadata") if isinstance(daemonset.get("metadata"), dict) else {}
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        agent_name = labels.get(AGENT_LABEL)
        name = metadata.get("name")
        namespace = metadata.get("namespace") or "default"
        if agent_name not in SUPPORTED_AGENTS or not isinstance(name, str) or not name:
            continue
        if not isinstance(namespace, str) or not namespace:
            continue
        uid = metadata.get("uid")
        records.append(
            {
                "namespace": namespace,
                "name": name,
                "uid": uid if isinstance(uid, str) else "",
                "agent_name": agent_name,
            }
        )
    return records


def _owned_by(pod: dict[str, Any], daemonset: dict[str, str]) -> bool:
    """Return whether a pod is controlled by the given DaemonSet."""
    metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
    references = metadata.get("ownerReferences")
    if not isinstance(references, list):
        return False
    for reference in references:
        if not isinstance(reference, dict):
            continue
        if reference.get("kind") != "DaemonSet" or reference.get("controller") is not True:
            continue
        if reference.get("name") != daemonset["name"]:
            continue
        if daemonset["uid"] and reference.get("uid") != daemonset["uid"]:
            continue
        return True
    return False


def _pod_ready(pod: dict[str, Any]) -> bool:
    """Return whether the pod Ready condition is explicitly true."""
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


def _running_agents(pods: dict[str, Any], daemonsets: list[dict[str, str]]) -> dict[str, str]:
    """Map GPU node names to supported Ready/Running DaemonSet-owned agents."""
    records: dict[str, str] = {}
    for pod in pods.get("items", []):
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
        if metadata.get("deletionTimestamp"):
            continue
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        agent_name = labels.get(AGENT_LABEL)
        namespace = metadata.get("namespace") or "default"
        spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
        status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
        node_name = spec.get("nodeName")
        if agent_name not in SUPPORTED_AGENTS or not isinstance(node_name, str) or not node_name:
            continue
        if status.get("phase") != "Running" or not _pod_ready(pod):
            continue
        owner = next(
            (
                daemonset
                for daemonset in daemonsets
                if daemonset["namespace"] == namespace
                and daemonset["agent_name"] == agent_name
                and _owned_by(pod, daemonset)
            ),
            None,
        )
        if owner is not None:
            records[node_name] = agent_name
    return records


def _evaluate(nodes: dict[str, Any], daemonsets: dict[str, Any], pods: dict[str, Any]) -> dict[str, Any]:
    """Build minimal provider-neutral BFX04-01 evidence from Kubernetes inventory."""
    gpu_nodes = _gpu_nodes(nodes)
    result: dict[str, Any] = {
        "success": True,
        "platform": "kubernetes",
        "test_name": "query_node_health_agents",
        "agents_observable": True,
        "agents": [],
    }
    if not gpu_nodes:
        result.update(
            {
                "skipped": True,
                "skip_reason": "No GPU nodes detected; BFX04-01 is not applicable",
            }
        )
        return result

    daemonset_records = _supported_daemonsets(daemonsets)
    running = _running_agents(pods, daemonset_records)
    result["agents"] = [
        {
            "node_id": node_name,
            "agent_name": running.get(node_name, "GPUd/Sentinel"),
            "running": node_name in running,
        }
        for node_name in gpu_nodes
    ]
    return result


def main() -> int:
    """Query the cluster and emit one structured BFX04-01 result."""
    parser = argparse.ArgumentParser(description="Inspect NVIDIA node-health agents")
    parser.add_argument(
        "--nodes",
        default=os.environ.get("BFX04_NODES", ""),
        help="Comma-separated SSH node names; omit to use Kubernetes",
    )
    args = parser.parse_args()
    try:
        nodes = _parse_nodes(args.nodes)
        if nodes:
            result = _evaluate_systemd(nodes)
        else:
            result = _evaluate(
                _get_collection("nodes"),
                _get_collection("daemonsets.apps", all_namespaces=True),
                _get_collection("pods", all_namespaces=True),
            )
    except (CommandOverrideError, KubernetesQueryError, NodeQueryError) as exc:
        result = {
            "success": False,
            "platform": "bare_metal" if args.nodes.strip() else "kubernetes",
            "test_name": "query_node_health_agents",
            "error_type": "node_health_query_failed",
            "error": str(exc),
        }
        print(json.dumps(result))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
