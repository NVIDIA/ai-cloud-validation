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

import json
import shlex
from typing import Any, NamedTuple

import pytest

from isvtest.config.settings import get_k8s_gpu_operator_namespace
from isvtest.core.k8s import (
    KubectlParseError,
    command_detail,
    get_kubectl_base_shell,
    is_resource_absent,
    kubectl_items_or_fail,
    names_from_items,
    parse_kubectl_json,
    parse_kubectl_json_items,
    pod_status_reason,
)
from isvtest.core.validation import BaseValidation


class K8sGpuOperatorNamespaceCheck(BaseValidation):
    description = "Verify GPU Operator namespace exists."

    def run(self) -> None:
        # Prefer config value, fall back to global setting
        namespace = self.config.get("namespace") or get_k8s_gpu_operator_namespace()

        kubectl_base = get_kubectl_base_shell()

        result = self.run_command(f"{kubectl_base} get namespace {shlex.quote(namespace)}")

        if result.exit_code != 0:
            self.set_failed(f"GPU Operator namespace '{namespace}' not found: {result.stderr}")
            return

        self.set_passed(f"GPU Operator namespace '{namespace}' exists")


class K8sGpuOperatorPodsCheck(BaseValidation):
    description = "Check if NVIDIA GPU Operator pods are running."

    def run(self) -> None:
        # Prefer config value, fall back to global setting
        namespace = self.config.get("namespace") or get_k8s_gpu_operator_namespace()

        kubectl_base = get_kubectl_base_shell()

        result = self.run_command(f"{kubectl_base} get pods -n {shlex.quote(namespace)} -o json")
        pods = kubectl_items_or_fail(self, result, "GPU Operator pod list")
        if pods is None:
            return

        running_pods = []
        for pod in pods:
            if pod_status_reason(pod) == "Running":
                running_pods.append((pod.get("metadata") or {}).get("name", "unknown"))

        if not running_pods:
            self.set_failed(f"No GPU Operator pods are running in namespace '{namespace}'")
            return

        self.set_passed(f"Found {len(running_pods)} running pods in '{namespace}'")


class _DriverConfigKind(NamedTuple):
    """A GPU Operator resource carrying the driver version a tenant sets."""

    resource: str
    version_path: tuple[str, ...]


# Both spellings of the operator's driver contract: ClusterPolicy is the
# long-standing single object, NVIDIADriver the per-node-pool CR that GPU
# Operator 24.6+ adds alongside it.
DRIVER_CONFIG_KINDS: tuple[_DriverConfigKind, ...] = (
    _DriverConfigKind("clusterpolicies.nvidia.com", ("spec", "driver", "version")),
    _DriverConfigKind("nvidiadrivers.nvidia.com", ("spec", "version")),
)

# Overriding the driver version in place is patch/update; swapping the whole
# configuration out is delete/create.
CONFIG_VERBS: tuple[str, ...] = ("patch", "update", "delete", "create")

# Installing a different operator version rewrites its own workloads, and the
# driver runs as a DaemonSet the tenant has to be able to replace with it.
WORKLOAD_RESOURCES: tuple[str, ...] = ("deployments.apps", "daemonsets.apps")
WORKLOAD_VERBS: tuple[str, ...] = ("patch", "update")


class K8sGpuOperatorOverrideCheck(BaseValidation):
    """Verify a tenant can override the provider-default GPU Operator and driver.

    A provider may ship the GPU Operator as a managed add-on; the requirement is
    that a tenant can still install the operator and driver versions its
    workloads need. Three things have to hold, none of which mutate the cluster:

    * The operator's driver configuration exists, so there is something to
      override. A cluster with neither ``ClusterPolicy`` nor ``NVIDIADriver``
      exposes no operator-managed driver version and fails rather than passing
      on the absence of evidence.
    * The tenant is authorized to rewrite that configuration and the operator's
      own workloads, asked server-side via ``kubectl auth can-i``.
    * A write of the tenant-required driver version survives admission. The
      patch runs with ``--dry-run=server``, so RBAC and every mutating and
      validating webhook evaluate it but nothing is persisted. Reading the
      version back off the returned object is what catches a provider webhook
      that accepts the write and pins the version anyway. When the cluster
      already runs the tenant-required version the patch would change nothing,
      so a neighbouring version is requested to keep admission under test.

    Config:
        driver_version: Tenant-required driver version to attempt. Skips when
            empty - there is no override to prove.
        namespace: Namespace holding the GPU Operator workloads. Falls back to
            the GPU Operator namespace setting.
    """

    description = "Verify the provider-default GPU Operator driver version can be overridden by the tenant."

    def run(self) -> None:
        """Probe driver configuration, tenant authorization, and admission."""
        driver_version = str(self.config.get("driver_version") or "").strip()
        if not driver_version:
            pytest.skip("driver_version is not configured; no tenant-required version to override to")

        namespace = self.config.get("namespace") or get_k8s_gpu_operator_namespace()
        kubectl_base = get_kubectl_base_shell()

        discovered = self._discover_driver_config(kubectl_base)
        if discovered is None:
            return
        kind, name, current_version = discovered

        if not self._tenant_can_replace_operator(kubectl_base, kind, namespace):
            return

        self._verify_override_admitted(kubectl_base, kind, name, driver_version, current_version)

    def _discover_driver_config(self, kubectl_base: str) -> tuple[_DriverConfigKind, str, str] | None:
        """Return the kind, object name, and configured version of the driver config.

        Returns ``None`` after marking the check failed when no kind is present
        or a query failed for a reason other than the kind being absent.
        """
        query_errors: list[str] = []

        for kind in DRIVER_CONFIG_KINDS:
            result = self.run_command(f"{kubectl_base} get {shlex.quote(kind.resource)} -o json")
            if result.exit_code != 0:
                if not is_resource_absent(result.stderr):
                    query_errors.append(f"{kind.resource}: {command_detail(result)}")
                continue
            try:
                items = parse_kubectl_json_items(result, kind.resource)
            except KubectlParseError as exc:
                self.set_failed(str(exc))
                return None
            if items:
                return kind, names_from_items(items)[0], _dig(items[0], kind.version_path)

        if query_errors:
            self.set_failed("Unable to query the GPU Operator driver configuration: " + "; ".join(query_errors))
            return None

        resources = ", ".join(kind.resource for kind in DRIVER_CONFIG_KINDS)
        self.set_failed(
            f"No GPU Operator driver configuration found ({resources}); the driver version is not "
            "operator-managed, so a tenant override cannot be proven"
        )
        return None

    def _tenant_can_replace_operator(
        self,
        kubectl_base: str,
        kind: _DriverConfigKind,
        namespace: str,
    ) -> bool:
        """Return True when every override route is authorized, marking failures itself."""
        probes = [(verb, kind.resource, "") for verb in CONFIG_VERBS]
        probes += [(verb, resource, namespace) for resource in WORKLOAD_RESOURCES for verb in WORKLOAD_VERBS]

        denials: list[str] = []
        for verb, resource, scope in probes:
            command = f"{kubectl_base} auth can-i {shlex.quote(verb)} {shlex.quote(resource)}"
            if scope:
                command += f" -n {shlex.quote(scope)}"
            result = self.run_command(command)

            # `auth can-i` answers "no" with a non-zero exit, so the exit code
            # alone cannot separate a denial from a broken probe.
            answer = result.stdout.strip().lower()
            if answer.startswith("yes"):
                continue
            if answer.startswith("no"):
                denials.append(f"cannot {verb} {resource}" + (f" in {scope}" if scope else ""))
                continue

            self.set_failed(f"Authorization probe for '{verb} {resource}' was inconclusive: {command_detail(result)}")
            return False

        if denials:
            self.set_failed(
                f"Tenant is not authorized to replace the provider-default GPU Operator: {'; '.join(denials)}"
            )
            return False
        return True

    def _verify_override_admitted(
        self,
        kubectl_base: str,
        kind: _DriverConfigKind,
        name: str,
        driver_version: str,
        current_version: str,
    ) -> None:
        """Dry-run the version override server-side and confirm admission keeps it."""
        # Requesting the version already configured writes nothing, so admission
        # would return it untouched and the check would pass without a version
        # ever having been overridden. Probe with a neighbouring version instead.
        requested = driver_version if driver_version != current_version else _next_version(current_version)

        patch = json.dumps(_nested(kind.version_path, requested))
        result = self.run_command(
            f"{kubectl_base} patch {shlex.quote(kind.resource)} {shlex.quote(name)} "
            f"--type=merge --patch {shlex.quote(patch)} --dry-run=server -o json"
        )
        if result.exit_code != 0:
            self.set_failed(
                f"Admission rejected driver version '{requested}' on {kind.resource}/{name}: {command_detail(result)}"
            )
            return

        try:
            admitted = parse_kubectl_json(result, f"{kind.resource}/{name} dry-run response")
        except KubectlParseError as exc:
            self.set_failed(str(exc))
            return

        version_path = ".".join(kind.version_path)
        admitted_version = _dig(admitted, kind.version_path)
        if admitted_version != requested:
            self.set_failed(
                f"Admission kept the provider-default driver version: requested '{requested}' at "
                f"{version_path} on {kind.resource}/{name}, admitted object reports "
                f"'{admitted_version or 'unset'}'"
            )
            return

        # A probed neighbouring version is one nobody asked for, so name the
        # tenant-required version rather than leave it out of the report.
        probed = "" if requested == driver_version else f"; tenant-required '{driver_version}' is already installed"
        self.set_passed(
            f"Tenant can override the provider-default driver: {kind.resource}/{name} {version_path} "
            f"accepts a write of '{requested}' (currently '{current_version or 'unset'}'){probed}"
        )


def _dig(obj: dict[str, Any], path: tuple[str, ...]) -> str:
    """Return the string at ``path`` in a nested mapping, or ``""`` when absent."""
    current: Any = obj
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _next_version(version: str) -> str:
    """Return ``version`` with its trailing number incremented."""
    head, separator, tail = version.rpartition(".")
    if tail.isdigit():
        return f"{head}{separator}{int(tail) + 1}"
    return f"{version}.1"


def _nested(path: tuple[str, ...], value: str) -> dict[str, Any]:
    """Build the nested mapping that places ``value`` at ``path``."""
    body: Any = value
    for key in reversed(path):
        body = {key: body}
    return body
