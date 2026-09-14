#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read-only reference implementation of the SDN17-02 compute-domain probe.

Reports whether a Kubernetes cluster offering multi-node NVLink through the
resource-allocation driver carries the compute-domain capability without the
tenant installing anything: the compute-domain device classes registered
cluster-wide, and compute-domain resources published for every GPU node in the
NVLink clique by the driver's per-node plugin.

Everything here is read from the cluster API. Nothing shells into a node and
nothing invokes IMEX tooling - both facts are observable as cluster objects,
and a tooling probe would need the IMEX command service enabled, which is not
the default, so it would fail on a correctly configured cluster.

Three decisions are load-bearing:

* **Publication is read from the published resource, not from a pod.** A
  ``ResourceSlice`` for the compute-domain driver exists only once that node's
  plugin registered with the node agent and published successfully. A pod in a
  Running state establishes neither.
* **Scope is every GPU node the cluster accounts for**, and is deliberately not
  selectable. A node list on the command line would hand the decision of what
  gets tested back to the provider, and a GPU node that carries no clique label
  stays in the reported set with ``clique_labelled: false`` - which the check
  fails - rather than dropping itself out of scope.
* **Nothing is asserted about a host IMEX daemon**, here or in the check. The
  driver supports two ownership modes and an active host daemon is a defect
  under one and a requirement under the other, so the observed mode is reported
  as evidence instead.

The device classes and the per-node driver are matched by role - a
``compute-domain*`` name under the vendor's suffix - rather than by exact name,
because those names are versioned with the driver while the role they play is
not.

A cluster that advertises no multi-node NVLink capability through the driver
(the ComputeDomain CRD is not registered) is out of scope for SDN17-02 rather
than failing it, and emits a structured skip. That gate is deliberately a
different object from the device classes the check asserts on, so the
assertion cannot certify itself.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any

TEST_NAME = "imex_compute_domain"

# The driver's own CRD. Its presence is what makes a cluster's multi-node NVLink
# capability "advertised" for scoping purposes.
COMPUTE_DOMAIN_CRD = "computedomains.resource.nvidia.com"
VENDOR_API_GROUP = "resource.nvidia.com"

# Device classes and per-node drivers are matched by role: a compute-domain name
# under the vendor's suffix. The exact names move with the driver version, the
# role they play does not.
COMPUTE_DOMAIN_ROLE = re.compile(r"^compute-domain[a-z0-9.-]*\.nvidia\.com$")

# Set by GPU feature discovery on nodes that belong to an NVLink clique; its
# value names the clique.
CLIQUE_LABEL = "nvidia.com/gpu.clique"
# GPU accounting. A DRA-only cluster need not expose the extended resource at
# all, so the feature-discovery label counts as well.
GPU_RESOURCE = "nvidia.com/gpu"
GPU_PRESENT_LABEL = "nvidia.com/gpu.present"

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0


class ComputeDomainProbeError(RuntimeError):
    """Raised when the compute-domain capability cannot be read from the cluster."""


def _kubectl_command() -> list[str]:
    """Return the configured kubectl-compatible command prefix."""
    configured = os.environ.get("KUBECTL", "kubectl")
    try:
        command = shlex.split(configured)
    except ValueError as exc:
        raise ComputeDomainProbeError(f"Invalid KUBECTL value: {exc}") from exc
    if not command:
        raise ComputeDomainProbeError("KUBECTL must not be blank")
    return command


def _command_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """Return bounded stderr/stdout detail for a failed command."""
    detail = (completed.stderr or completed.stdout).strip()
    return detail[-500:] if detail else "command failed without output"


def _run(kubectl: list[str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run one bounded kubectl command and translate process failures."""
    command = [*kubectl, *args, f"--request-timeout={DEFAULT_REQUEST_TIMEOUT_SECONDS:g}s"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ComputeDomainProbeError(
            f"kubectl {' '.join(args)} timed out after {DEFAULT_COMMAND_TIMEOUT_SECONDS:g} seconds"
        ) from exc
    except OSError as exc:
        raise ComputeDomainProbeError(f"Unable to run {' '.join(kubectl)}: {exc}") from exc
    if completed.returncode != 0:
        raise ComputeDomainProbeError(f"kubectl {' '.join(args)} failed: {_command_detail(completed)}")
    return completed


def _items(kubectl: list[str], resource: str) -> list[dict[str, Any]]:
    """Return the object list for one cluster-scoped resource kind."""
    completed = _run(kubectl, "get", resource, "-o", "json")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ComputeDomainProbeError(f"kubectl returned invalid JSON for {resource}") from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ComputeDomainProbeError(f"kubectl list for {resource} is missing items")
    return [item for item in items if isinstance(item, dict)]


def _multi_node_nvlink_advertised(kubectl: list[str]) -> bool:
    """Return whether the cluster advertises multi-node NVLink through the driver.

    An unserved API group is an empty successful listing, so a failure here is a
    cluster the probe could not read - which must not be mistaken for a cluster
    that offers no multi-node NVLink.
    """
    completed = _run(kubectl, "api-resources", f"--api-group={VENDOR_API_GROUP}", "-o", "name")
    return COMPUTE_DOMAIN_CRD in completed.stdout.split()


def _compute_domain_device_classes(kubectl: list[str]) -> list[str]:
    """Return the names of the registered compute-domain device classes."""
    names = [item.get("metadata", {}).get("name") for item in _items(kubectl, "deviceclasses.resource.k8s.io")]
    return [name for name in names if isinstance(name, str) and COMPUTE_DOMAIN_ROLE.fullmatch(name)]


def _ownership_mode(device_classes: list[str]) -> str:
    """Return the observed IMEX daemon ownership mode.

    The driver runs the daemons itself only where it also offers a device class
    for them; a cluster whose daemons are managed on the host registers the
    channel class alone. Evidence only - no assertion rests on this.
    """
    return "driver" if any("daemon" in name for name in device_classes) else "host"


def _publishing_nodes(kubectl: list[str]) -> set[str]:
    """Return the nodes the compute-domain per-node plugin has published for."""
    published: set[str] = set()
    for slice_ in _items(kubectl, "resourceslices.resource.k8s.io"):
        spec = slice_.get("spec")
        if not isinstance(spec, dict):
            continue
        driver = spec.get("driver")
        node_name = spec.get("nodeName")
        if isinstance(driver, str) and COMPUTE_DOMAIN_ROLE.fullmatch(driver) and isinstance(node_name, str):
            published.add(node_name)
    return published


def _is_gpu_node(node: dict[str, Any]) -> bool:
    """Return whether the cluster accounts for GPUs on this node."""
    labels = node.get("metadata", {}).get("labels") or {}
    if isinstance(labels, dict) and labels.get(GPU_PRESENT_LABEL) == "true":
        return True
    allocatable = node.get("status", {}).get("allocatable") or {}
    if not isinstance(allocatable, dict):
        return False
    try:
        return int(allocatable.get(GPU_RESOURCE, 0)) > 0
    except (TypeError, ValueError):
        return False


def _node_name(node: dict[str, Any]) -> str:
    """Return the node's name, or an empty string when it carries none."""
    name = node.get("metadata", {}).get("name")
    return name if isinstance(name, str) else ""


def _scoped_nodes(kubectl: list[str]) -> list[dict[str, Any]]:
    """Return every GPU node the cluster accounts for, in name order.

    Scope comes from the cluster's own GPU accounting rather than from a node's
    view of its NVLink support, so a clique-less GPU node is still examined.
    """
    scoped = [node for node in _items(kubectl, "nodes") if _node_name(node) and _is_gpu_node(node)]
    return sorted(scoped, key=_node_name)


def _node_report(node: dict[str, Any], published: set[str]) -> dict[str, Any]:
    """Return one per-node entry of the SDN17-02 contract."""
    name = _node_name(node)
    labels = node.get("metadata", {}).get("labels") or {}
    clique = labels.get(CLIQUE_LABEL) if isinstance(labels, dict) else None
    return {
        "node_id": name,
        "clique_labelled": bool(isinstance(clique, str) and clique.strip()),
        "compute_domain_resources_published": name in published,
    }


def _probe(kubectl: list[str]) -> dict[str, Any]:
    """Return provider-neutral SDN17-02 evidence read from the cluster API."""
    if not _multi_node_nvlink_advertised(kubectl):
        return {
            "success": True,
            "platform": "kubernetes",
            "test_name": TEST_NAME,
            "skipped": True,
            "skip_reason": (
                f"Cluster advertises no multi-node NVLink capability through the driver ({COMPUTE_DOMAIN_CRD} "
                "is not registered)"
            ),
            "nodes": [],
        }

    device_classes = _compute_domain_device_classes(kubectl)
    published = _publishing_nodes(kubectl)
    reports = [_node_report(node, published) for node in _scoped_nodes(kubectl)]
    return {
        "success": True,
        "platform": "kubernetes",
        "test_name": TEST_NAME,
        "device_classes_registered": bool(device_classes),
        "daemon_ownership_mode": _ownership_mode(device_classes),
        "nodes_checked": len(reports),
        "nodes_validated": sum(
            1 for report in reports if report["clique_labelled"] and report["compute_domain_resources_published"]
        ),
        "nodes": reports,
    }


def main() -> int:
    """Emit one structured SDN17-02 result for the cluster under test."""
    try:
        result = _probe(_kubectl_command())
    except ComputeDomainProbeError as exc:
        result = {
            "success": False,
            "platform": "kubernetes",
            "test_name": TEST_NAME,
            "error_type": "compute_domain_probe_failed",
            "error": str(exc),
            "nodes": [],
        }
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
