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

"""Cluster Autoscaler integration check."""

from __future__ import annotations

import shlex
from typing import Any, ClassVar

import pytest

from isvtest.core.k8s import (
    KubectlParseError,
    get_kubectl_base_shell,
    parse_kubectl_json,
    parse_kubectl_json_items,
    pod_status_reason,
)
from isvtest.core.runners import CommandResult
from isvtest.core.validation import BaseValidation

DEFAULT_DEPLOYMENT_NAMES: tuple[str, ...] = ("cluster-autoscaler",)
DEFAULT_LABEL_SELECTORS: tuple[str, ...] = (
    "app.kubernetes.io/name=cluster-autoscaler",
    "app=cluster-autoscaler",
    "k8s-app=cluster-autoscaler",
)


class K8sClusterAutoscalerCheck(BaseValidation):
    """Verify an upstream Cluster Autoscaler deployment is installed and running.

    Config keys:

    * ``namespaces`` - namespaces to probe by deployment name. Defaults to
      ``["kube-system"]``. ``namespace`` is accepted as a single-value alias.
    * ``deployment_names`` - deployment names to probe. Defaults to
      ``["cluster-autoscaler"]``.
    * ``label_selectors`` - label selectors used to discover deployments across
      all namespaces. Defaults cover common upstream manifests and Helm charts.
    * ``require_autoscaler`` - when ``False`` (default), absent deployments
      cause the check to skip rather than fail. Set to ``True`` to require a
      Cluster Autoscaler integration on the cluster.
    """

    description: ClassVar[str] = "Verify upstream Cluster Autoscaler integration is installed and running."
    markers: ClassVar[list[str]] = ["kubernetes"]

    def run(self) -> None:
        """Find Cluster Autoscaler deployments and verify their replicas and pods are healthy."""
        try:
            namespaces = _coerce_str_list(
                self.config.get("namespaces", self.config.get("namespace", ["kube-system"])),
                "namespaces",
            )
            deployment_names = _coerce_str_list(
                self.config.get("deployment_names", list(DEFAULT_DEPLOYMENT_NAMES)),
                "deployment_names",
            )
            label_selectors = _coerce_str_list(
                self.config.get("label_selectors", list(DEFAULT_LABEL_SELECTORS)),
                "label_selectors",
            )
        except ValueError as exc:
            self.set_failed(f"Invalid config: {exc}")
            return

        if not deployment_names and not label_selectors:
            self.set_failed("Invalid config: deployment_names and label_selectors cannot both be empty")
            return

        require_autoscaler = bool(self.config.get("require_autoscaler", False))

        kubectl_base = get_kubectl_base_shell()
        deployments = self._discover_deployments(kubectl_base, namespaces, deployment_names, label_selectors)
        if deployments is None:
            return
        if not deployments:
            msg = (
                "No Cluster Autoscaler deployment found using "
                f"names={deployment_names or '[]'} namespaces={namespaces or '[]'} "
                f"selectors={label_selectors or '[]'}"
            )
            if require_autoscaler:
                self.set_failed(msg)
            else:
                pytest.skip(f"{msg} (require_autoscaler is false)")
            return

        failures: list[str] = []
        running_total = 0
        for deployment in deployments:
            namespace, name = _object_ref(deployment)
            status = deployment.get("status") or {}
            spec = deployment.get("spec") or {}
            desired = _replica_count(spec.get("replicas"), default=1)
            available = _replica_count(status.get("availableReplicas"), default=0)

            if not _has_autoscaler_container(deployment):
                failures.append(f"{namespace}/{name}: no container looks like Cluster Autoscaler")
            if desired < 1:
                failures.append(f"{namespace}/{name}: deployment is scaled to {desired} replicas")
            if available < desired:
                failures.append(f"{namespace}/{name}: {available}/{desired} replicas available")

            pod_selector = _selector_from_deployment(deployment)
            if not pod_selector:
                failures.append(f"{namespace}/{name}: deployment selector has no matchLabels for pod verification")
                continue
            pods = self._pods_for_deployment(kubectl_base, namespace, pod_selector)
            if pods is None:
                return
            running_pods = _running_pod_names(pods)
            running_total += len(running_pods)
            if len(running_pods) < desired:
                failures.append(f"{namespace}/{name}: {len(running_pods)}/{desired} matching pods Running")

        if failures:
            self.set_failed("Cluster Autoscaler integration is not healthy: " + "; ".join(failures))
            return

        refs = ", ".join(f"{_object_ref(deployment)[0]}/{_object_ref(deployment)[1]}" for deployment in deployments)
        self.set_passed(
            f"Found {len(deployments)} healthy Cluster Autoscaler deployment(s): {refs}; "
            f"{running_total} matching pod(s) Running"
        )

    def _discover_deployments(
        self,
        kubectl_base: str,
        namespaces: list[str],
        deployment_names: list[str],
        label_selectors: list[str],
    ) -> list[dict[str, Any]] | None:
        """Discover deployments by upstream labels and configured names."""
        deployments_by_ref: dict[tuple[str, str], dict[str, Any]] = {}
        query_errors: list[str] = []
        successful_queries = 0

        for selector in label_selectors:
            result = self.run_command(f"{kubectl_base} get deployments -A -l {shlex.quote(selector)} -o json")
            if result.exit_code != 0:
                query_errors.append(_format_error(f"selector {selector!r}", result))
                continue
            successful_queries += 1
            try:
                for deployment in parse_kubectl_json_items(result, f"deployments for selector {selector!r}"):
                    deployments_by_ref[_object_ref(deployment)] = deployment
            except KubectlParseError as exc:
                self.set_failed(str(exc))
                return None

        for namespace in namespaces:
            for name in deployment_names:
                result = self.run_command(
                    f"{kubectl_base} get deployment -n {shlex.quote(namespace)} {shlex.quote(name)} -o json"
                )
                if result.exit_code != 0:
                    if not _is_not_found(result.stderr):
                        query_errors.append(_format_error(f"deployment {namespace}/{name}", result))
                    continue
                successful_queries += 1
                try:
                    deployment = parse_kubectl_json(result, f"deployment {namespace}/{name}")
                except KubectlParseError as exc:
                    self.set_failed(str(exc))
                    return None
                deployments_by_ref[_object_ref(deployment)] = deployment

        if successful_queries == 0 and query_errors:
            self.set_failed("Unable to query Cluster Autoscaler deployments: " + "; ".join(query_errors))
            return None
        return list(deployments_by_ref.values())

    def _pods_for_deployment(
        self,
        kubectl_base: str,
        namespace: str,
        pod_selector: str,
    ) -> list[dict[str, Any]] | None:
        """Return pods matching a deployment selector, or mark the validation failed."""
        result = self.run_command(
            f"{kubectl_base} get pods -n {shlex.quote(namespace)} -l {shlex.quote(pod_selector)} -o json"
        )
        if result.exit_code != 0:
            self.set_failed(_format_error(f"pods for selector {pod_selector!r} in {namespace}", result))
            return None
        try:
            return parse_kubectl_json_items(result, f"pods for selector {pod_selector!r} in {namespace}")
        except KubectlParseError as exc:
            self.set_failed(str(exc))
            return None


def _coerce_str_list(value: Any, field: str) -> list[str]:
    """Coerce a scalar or list into a stripped ``list[str]``."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a string or list of strings")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings, got {item!r}")
        stripped = item.strip()
        if stripped:
            values.append(stripped)
    return values


def _object_ref(obj: dict[str, Any]) -> tuple[str, str]:
    """Return ``(namespace, name)`` for a Kubernetes object."""
    metadata = obj.get("metadata") or {}
    return str(metadata.get("namespace") or "default"), str(metadata.get("name") or "unknown")


def _replica_count(value: Any, default: int) -> int:
    """Return a non-negative replica count from Kubernetes status/spec values."""
    try:
        replicas = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, replicas)


def _has_autoscaler_container(deployment: dict[str, Any]) -> bool:
    """Return True when a pod template container identifies as Cluster Autoscaler."""
    pod_spec = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    for container in pod_spec.get("containers") or []:
        if not isinstance(container, dict):
            continue
        haystack = " ".join(
            str(value)
            for value in (
                container.get("name", ""),
                container.get("image", ""),
                " ".join(container.get("command") or []),
                " ".join(container.get("args") or []),
            )
        )
        if "cluster-autoscaler" in haystack:
            return True
    return False


def _selector_from_deployment(deployment: dict[str, Any]) -> str:
    """Build a Kubernetes label selector from ``spec.selector.matchLabels``."""
    match_labels = ((deployment.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not isinstance(match_labels, dict):
        return ""
    parts = []
    for key, value in sorted(match_labels.items()):
        if isinstance(key, str) and isinstance(value, str) and key and value:
            parts.append(f"{key}={value}")
    return ",".join(parts)


def _running_pod_names(pods: list[dict[str, Any]]) -> list[str]:
    """Return names of pods whose kubectl-like status is Running."""
    names: list[str] = []
    for pod in pods:
        if pod_status_reason(pod) == "Running":
            names.append(_object_ref(pod)[1])
    return names


def _is_not_found(stderr: str) -> bool:
    """Return True when kubectl stderr is a normal NotFound response."""
    lowered = stderr.lower().replace(" ", "")
    return "notfound" in lowered or "notfound" in lowered.replace("(", "").replace(")", "")


def _format_error(scope: str, result: CommandResult) -> str:
    """Format a concise kubectl error."""
    detail = (result.stderr or result.stdout or "").strip()
    return f"Failed to get {scope}: {detail or f'exit {result.exit_code}'}"
