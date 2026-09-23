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

"""Validate Dynamic Resource Allocation (DRA) on a Kubernetes cluster."""

from __future__ import annotations

import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from isvtest.core.k8s import (
    TERMINAL_WAITING_REASONS,
    KubectlParseError,
    command_detail,
    get_kubectl_base_shell,
    kubectl_list_or_fail,
    names_from_items,
    parse_kubectl_json,
    pod_status_reason,
)
from isvtest.core.validation import BaseValidation

_MANIFEST_DIR = Path(__file__).parent / "manifests" / "k8s"
_CLAIM_POD_MANIFEST = _MANIFEST_DIR / "dra_claim_pod.yaml"
_COMPUTE_DOMAIN_MANIFEST = _MANIFEST_DIR / "imex_compute_domain.yaml"

DRA_API_GROUP = "resource.k8s.io"
# Newest first. K8S24 holds whatever DRA's upstream stage, so a served beta
# version counts as much as GA - no Kubernetes version is required.
ACCEPTED_VERSIONS = ("v1", "v1beta2", "v1beta1")
DEVICE_CLASS_RESOURCE = f"deviceclasses.{DRA_API_GROUP}"
RESOURCE_SLICE_RESOURCE = f"resourceslices.{DRA_API_GROUP}"
RESOURCE_CLAIM_RESOURCE = f"resourceclaims.{DRA_API_GROUP}"

DEFAULT_DEVICE_CLASS = "gpu.nvidia.com"
# The NVIDIA DRA driver can run its ComputeDomain half alone, with GPUs still
# handed out by the device plugin. A pod then reaches multi-node NVLink through
# an IMEX channel claim, which is DRA-scheduled all the same.
CHANNEL_DEVICE_CLASS = "compute-domain-default-channel.nvidia.com"
COMPUTE_DOMAIN_RESOURCE = "computedomains.resource.nvidia.com"
# Mirrors the NVIDIA DRA driver's own examples. Override via ``image`` for
# air-gapped clusters that mirror to a private registry.
DEFAULT_IMAGE = "ubuntu:22.04"

_NAME = "isvtest-dra"
_CHANNEL_TEMPLATE = f"{_NAME}-channel"
_CONTAINER = "dra"
_CLAIM_ENTRY = "dra"
_DELETE_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class _Workload:
    """What the claim-backed pod requests, runs, and must print to prove it."""

    kind: str
    device_class: str
    command: str
    evidence_prefix: str


def _gpu_workload(device_class: str) -> _Workload:
    """Return the workload that lists the GPU a claim for ``device_class`` delivered."""
    return _Workload("GPU", device_class, "nvidia-smi -L && sleep 3600", "GPU ")


_CHANNEL_WORKLOAD = _Workload(
    "IMEX channel", CHANNEL_DEVICE_CLASS, "ls /dev/nvidia-caps-imex-channels && sleep 3600", "channel"
)


def _load_docs(path: Path) -> list[dict[str, Any]]:
    """Return the mapping documents of a multi-document YAML manifest."""
    return [doc for doc in yaml.safe_load_all(path.read_text()) if isinstance(doc, dict)]


def _claim_template(doc: dict[str, Any], *, version: str, namespace: str, device_class: str) -> dict[str, Any]:
    """Mutate the parsed ResourceClaimTemplate in place for the served API version."""
    doc["apiVersion"] = f"{DRA_API_GROUP}/{version}"
    doc["metadata"] = {"name": _NAME, "namespace": namespace}
    request: dict[str, Any] = {"name": "device"}
    if version == "v1beta1":
        request["deviceClassName"] = device_class
    else:
        request["exactly"] = {"deviceClassName": device_class}
    doc["spec"]["spec"]["devices"]["requests"] = [request]
    return doc


def _pod(doc: dict[str, Any], *, namespace: str, image: str, command: str, template: str) -> dict[str, Any]:
    """Mutate the parsed Pod in place."""
    doc["metadata"] = {"name": _NAME, "namespace": namespace}
    container = doc["spec"]["containers"][0]
    container["image"] = image
    container["command"] = ["sh", "-c", command]
    doc["spec"]["resourceClaims"][0]["resourceClaimTemplateName"] = template
    return doc


def _compute_domain(doc: dict[str, Any], *, namespace: str) -> dict[str, Any]:
    """Mutate the parsed ComputeDomain in place."""
    doc["metadata"] = {"name": _NAME, "namespace": namespace}
    doc["spec"]["channel"]["resourceClaimTemplate"]["name"] = _CHANNEL_TEMPLATE
    return doc


def _evidence(logs: str, prefix: str) -> list[str]:
    """Return the log lines that name a device the claim delivered."""
    return [line.strip() for line in logs.splitlines() if line.strip().startswith(prefix)]


def _pending_detail(pod: dict[str, Any]) -> str:
    """Return why a pod has not started, preferring the scheduler's own message."""
    for condition in (pod.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            return str(condition.get("message") or condition.get("reason") or "unschedulable")
    return pod_status_reason(pod)


def _generated_claim(pod: dict[str, Any]) -> str | None:
    """Return the name of the ResourceClaim generated for the pod's claim entry."""
    for entry in (pod.get("status") or {}).get("resourceClaimStatuses") or []:
        if isinstance(entry, dict) and entry.get("name") == _CLAIM_ENTRY and entry.get("resourceClaimName"):
            return str(entry["resourceClaimName"])
    return None


class K8sDraCheck(BaseValidation):
    """Verify DRA is enabled and a pod is scheduled through a ResourceClaim.

    Four facts, each failing on its own:

    * The control plane serves ``resource.k8s.io`` at v1, v1beta2 or v1beta1.
      Beta counts: K8S24 requires DRA enabled whatever its upstream stage.
    * At least one DeviceClass and one ResourceSlice exist, so a DRA driver is
      installed and publishing devices.
    * A pod consuming a claim runs and shows the device it was given. With
      ``device_class`` (GPUs) registered the claim is for a GPU and the pod
      lists it with ``nvidia-smi -L``. A cluster running only the driver's
      ComputeDomain half instead gets a ComputeDomain, and the pod lists the
      IMEX channel its claim delivered - multi-node NVLink through DRA.
    * While that pod runs, its claim is allocated and reserved for it. This is
      what ties the device to DRA: an API server with DRA disabled drops a
      pod's claim fields and would run the pod as an ordinary one.

    Everything lives in an ephemeral namespace. Cleanup is mandatory, and the
    pod goes before the ComputeDomain so no channel is still held when the
    domain is removed.

    Config keys:
        device_class: GPU DeviceClass the claim requests (default ``gpu.nvidia.com``).
        image: Container image for the pod (default ``ubuntu:22.04``).
        wait_timeout: Seconds to wait for the pod to show its device (default 300).
    """

    description: ClassVar[str] = "Check the DRA API is served and a pod is scheduled through a ResourceClaim"
    _POLL_INTERVAL_SECONDS: ClassVar[int] = 5

    def run(self) -> None:
        """Assert the DRA API and driver, run a claim-backed pod, and clean up."""
        device_class = str(self.config.get("device_class") or DEFAULT_DEVICE_CLASS)
        image = str(self.config.get("image") or DEFAULT_IMAGE)
        wait_timeout = self._parse_positive_int("wait_timeout", default=300)
        if wait_timeout is None:
            return

        version = self._served_version()
        if version is None:
            return

        classes = kubectl_list_or_fail(self, DEVICE_CLASS_RESOURCE)
        if classes is None:
            return
        slices = kubectl_list_or_fail(self, RESOURCE_SLICE_RESOURCE)
        if slices is None:
            return
        if not classes or not slices:
            self.set_failed(
                f"{DRA_API_GROUP}/{version} is served but no DRA driver is installed: "
                f"{len(classes)} DeviceClass(es) and {len(slices)} ResourceSlice(s) registered"
            )
            return

        class_names = names_from_items(classes)
        if device_class in class_names:
            workload = _gpu_workload(device_class)
        elif CHANNEL_DEVICE_CLASS in class_names:
            workload = _CHANNEL_WORKLOAD
        else:
            self.set_failed(
                f"Neither DeviceClass {device_class!r} (GPUs) nor {CHANNEL_DEVICE_CLASS!r} (ComputeDomain "
                f"channels) is registered (registered: {', '.join(sorted(class_names))})"
            )
            return

        namespace = f"{_NAME}-{uuid.uuid4().hex[:8]}"
        result = self.run_command(get_kubectl_base_shell("create", "namespace", namespace))
        if result.exit_code != 0:
            self.set_failed(f"Failed to create namespace {namespace}: {command_detail(result)}")
            return
        try:
            self._run_claim_pod(version, namespace, workload, image, wait_timeout, len(classes), len(slices))
        finally:
            self._cleanup(namespace, workload)

    def _served_version(self) -> str | None:
        """Return the newest accepted ``resource.k8s.io`` version served, or None after failing."""
        result = self.run_command(get_kubectl_base_shell("api-versions"))
        if result.exit_code != 0:
            self.set_failed(f"Failed to list served API versions: {command_detail(result)}")
            return None
        served = {line.strip() for line in result.stdout.splitlines()}
        for version in ACCEPTED_VERSIONS:
            if f"{DRA_API_GROUP}/{version}" in served:
                return version
        others = sorted(v for v in served if v.startswith(f"{DRA_API_GROUP}/"))
        self.set_failed(
            f"DRA is not enabled: {DRA_API_GROUP} is not served at any of {', '.join(ACCEPTED_VERSIONS)}"
            + (f" (served: {', '.join(others)})" if others else "")
        )
        return None

    def _manifest(self, version: str, namespace: str, workload: _Workload, image: str) -> str:
        """Render the objects for this workload: a claim template or ComputeDomain, then the pod."""
        template_doc, pod_doc = _load_docs(_CLAIM_POD_MANIFEST)
        if workload is _CHANNEL_WORKLOAD:
            source = _compute_domain(_load_docs(_COMPUTE_DOMAIN_MANIFEST)[0], namespace=namespace)
            template = _CHANNEL_TEMPLATE
        else:
            source = _claim_template(
                template_doc, version=version, namespace=namespace, device_class=workload.device_class
            )
            template = _NAME
        pod = _pod(pod_doc, namespace=namespace, image=image, command=workload.command, template=template)
        return yaml.safe_dump_all([source, pod], sort_keys=False)

    def _run_claim_pod(
        self,
        version: str,
        namespace: str,
        workload: _Workload,
        image: str,
        wait_timeout: int,
        class_count: int,
        slice_count: int,
    ) -> None:
        """Create the claim source and its pod, then assert the pod got its device through the claim."""
        manifest = self._manifest(version, namespace, workload, image)
        apply = get_kubectl_base_shell("apply", "-f", "-")
        result = self.run_command(f"printf '%s' {shlex.quote(manifest)} | {apply}")
        if result.exit_code != 0:
            self.set_failed(f"Failed to create the claim-backed pod: {command_detail(result)}")
            return

        running = self._wait_for_evidence(namespace, workload, wait_timeout)
        if running is None:
            return
        pod, evidence = running

        claim_name = _generated_claim(pod)
        if claim_name is None:
            self.set_failed(
                f"The pod saw {len(evidence)} {workload.kind}(s), but carries no ResourceClaim - the API server "
                "dropped its claim fields, so the device did not come through DRA"
            )
            return
        result = self.run_command(
            get_kubectl_base_shell("get", RESOURCE_CLAIM_RESOURCE, claim_name, "-n", namespace, "-o", "json")
        )
        if result.exit_code != 0:
            self.set_failed(f"Failed to read ResourceClaim {claim_name} back: {command_detail(result)}")
            return
        try:
            claim = parse_kubectl_json(result, "ResourceClaim")
        except KubectlParseError as exc:
            self.set_failed(str(exc))
            return

        status = claim.get("status") or {}
        devices = ((status.get("allocation") or {}).get("devices") or {}).get("results") or []
        reserved = {entry.get("uid") for entry in status.get("reservedFor") or [] if isinstance(entry, dict)}
        pod_uid = (pod.get("metadata") or {}).get("uid")
        if not devices or pod_uid not in reserved:
            self.set_failed(
                f"The pod saw {len(evidence)} {workload.kind}(s), but ResourceClaim {claim_name} is not allocated "
                f"and reserved for it ({len(devices)} device(s) allocated) - the device did not come through DRA"
            )
            return

        node = (pod.get("spec") or {}).get("nodeName") or "unknown node"
        self.set_passed(
            f"{DRA_API_GROUP}/{version} is served with {class_count} DeviceClass(es) and {slice_count} "
            f"ResourceSlice(s); a pod on {node} was allocated {len(devices)} {workload.device_class} device(s) "
            f"through a ResourceClaim and saw {workload.kind}(s): {'; '.join(evidence)}"
        )

    def _wait_for_evidence(
        self, namespace: str, workload: _Workload, wait_timeout: int
    ) -> tuple[dict[str, Any], list[str]] | None:
        """Wait until the pod runs and shows its device; return the pod and evidence, or None after failing."""
        get_pod = get_kubectl_base_shell("get", "pod", _NAME, "-n", namespace, "-o", "json")
        get_logs = get_kubectl_base_shell("logs", _NAME, "-n", namespace, "-c", _CONTAINER)
        deadline = time.monotonic() + wait_timeout
        detail = "pod not observed yet"
        while True:
            result = self.run_command(get_pod)
            if result.exit_code != 0:
                detail = command_detail(result)
            else:
                try:
                    pod = parse_kubectl_json(result, "DRA pod")
                except KubectlParseError as exc:
                    detail = str(exc)
                else:
                    phase = (pod.get("status") or {}).get("phase")
                    if phase in ("Succeeded", "Failed"):
                        logs = self.run_command(get_logs)
                        self.set_failed(
                            f"The pod exited ({phase}) instead of showing the {workload.kind} its "
                            f"{workload.device_class} claim delivered",
                            output=(logs.stdout or logs.stderr).strip(),
                        )
                        return None
                    if phase == "Running":
                        logs = self.run_command(get_logs)
                        evidence = _evidence(logs.stdout, workload.evidence_prefix) if logs.exit_code == 0 else []
                        if evidence:
                            return pod, evidence
                        detail = f"running, no {workload.kind} shown yet"
                    else:
                        detail = _pending_detail(pod)
                        if detail in TERMINAL_WAITING_REASONS:
                            self.set_failed(f"The pod cannot start: {detail}")
                            return None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.set_failed(
                    f"The pod requesting a {workload.device_class} device through a ResourceClaim did not show a "
                    f"{workload.kind} within {wait_timeout}s: {detail}"
                )
                return None
            time.sleep(min(float(self._POLL_INTERVAL_SECONDS), remaining))

    def _cleanup(self, namespace: str, workload: _Workload) -> None:
        """Delete what the check created, failing a passing check on anything left behind."""
        deletions: list[tuple[str, ...]] = [("pod", _NAME, "-n", namespace, f"--timeout={_DELETE_TIMEOUT_SECONDS}s")]
        if workload is _CHANNEL_WORKLOAD:
            deletions.append((COMPUTE_DOMAIN_RESOURCE, _NAME, "-n", namespace, f"--timeout={_DELETE_TIMEOUT_SECONDS}s"))
        deletions.append(("namespace", namespace, "--wait=false"))

        leftovers: list[str] = []
        for args in deletions:
            result = self.run_command(
                get_kubectl_base_shell("delete", *args, "--ignore-not-found=true"),
                timeout=_DELETE_TIMEOUT_SECONDS + 30,
            )
            if result.exit_code != 0:
                leftovers.append(f"{args[0]}/{args[1]}: {command_detail(result)}")
        if leftovers and self.passed:
            self.set_failed(f"Cleanup left objects behind in namespace {namespace}: {'; '.join(leftovers)}")
