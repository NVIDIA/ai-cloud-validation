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

"""CSI storage validations.

Implements:

* :class:`K8sCsiStorageTypesCheck` - verify the cluster exposes a
  StorageClass for each of block / shared filesystem / NFS storage and that
  a PVC binds against each configured class.
* :class:`K8sCsiStorageQuotaApiCheck` - verify Kubernetes-native
  APIs expose storage quota and per-PVC/PV usage.
* :class:`K8sCsiTenantScopedCredentialsCheck` - verify CSI
  credentials are tenant-scoped "by construction" by inspecting in-cluster
  observable state (Secret namespaces, labels, ServiceAccount RBAC, and
  node-plugin volume topology). Does not prove cross-cluster isolation from
  the outside.
* :class:`K8sCsiProvisioningModesCheck` - verify CSI supports
  both dynamic and static provisioning, driving each through a real mount
  + canary-file write/read inside a BusyBox pod.
* :class:`K8sCsiDriverHealthCheck` - verify each configured StorageClass
  resolves to a registered ``CSIDriver`` and that the driver's controller
  Deployment and node DaemonSet are healthy.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

from kubernetes.utils import parse_quantity

from isvtest.config.settings import (
    get_k8s_csi_block_storage_class,
    get_k8s_csi_nfs_storage_class,
    get_k8s_csi_shared_fs_storage_class,
)
from isvtest.core.k8s import (
    KubectlParseError,
    get_kubectl_base_shell,
    get_kubectl_command,
    parse_kubectl_json,
    render_k8s_manifest,
)
from isvtest.core.validation import BaseValidation

_MANIFEST_DIR = Path(__file__).parent / "manifests" / "k8s"
_PVC_MANIFEST = _MANIFEST_DIR / "storage_pvc.yaml"
_RESOURCEQUOTA_MANIFEST = _MANIFEST_DIR / "storage_resourcequota.yaml"
_PV_MANIFEST = _MANIFEST_DIR / "storage_pv.yaml"
_MOUNT_POD_MANIFEST = _MANIFEST_DIR / "storage_mount_pod.yaml"

# BusyBox provides flock/stat/ls/mkdir/awk/seq; override via the ``image``
# config key for air-gapped clusters that mirror to a private registry.
_DEFAULT_IMAGE = "busybox:1.36"

_STORAGE_TYPES: tuple[tuple[str, str], ...] = (
    ("block", "ReadWriteOnce"),
    ("shared-fs", "ReadWriteMany"),
    ("nfs", "ReadWriteMany"),
)

_QUOTA_REJECTION_TOKENS: tuple[str, ...] = ("exceeded quota", "forbidden")
_DIAGNOSTIC_SECTION_LIMIT = 1024
_DIAGNOSTIC_TOTAL_LIMIT = 6144
_SENSITIVE_DIAGNOSTIC_LINE = re.compile(r"(?i)(authorization|credential|password|private[-_ ]?key|secret|token)\s*[:=]")


def _apply_manifest(kubectl_parts: list[str], manifest: str, timeout: float) -> tuple[int, str]:
    """Invoke ``kubectl apply -f -`` with ``manifest``; return ``(returncode, stderr-or-stdout)``.

    Timeouts and unexpected exceptions are normalised to returncode ``-1`` so
    callers can treat them uniformly as apply failures.
    """
    try:
        proc = subprocess.run(
            kubectl_parts + ["apply", "-f", "-"],
            input=manifest,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "kubectl apply timed out"
    except Exception as exc:
        return -1, f"kubectl apply raised: {exc}"
    return proc.returncode, proc.stderr or proc.stdout


def _get_pvc_json(run_command, kubectl_base: str, namespace: str, pvc_name: str) -> tuple[dict[str, Any] | None, str]:
    """Fetch and parse ``kubectl get pvc -o json``; return ``(payload, error)``.

    ``error`` is empty on success and otherwise contains the kubectl stderr or
    a ``KubectlParseError`` message - callers can surface it verbatim.
    """
    cmd = f"{kubectl_base} get pvc {shlex.quote(pvc_name)} -n {shlex.quote(namespace)} -o json"
    result = run_command(cmd)
    if result.exit_code != 0:
        return None, result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
    try:
        return parse_kubectl_json(result, f"PVC {pvc_name!r}"), ""
    except KubectlParseError as exc:
        return None, str(exc)


def _poll_pvc_bound(
    run_command, kubectl_base: str, namespace: str, pvc_name: str, timeout_s: int, poll_interval_s: float = 2.0
) -> bool:
    """Poll ``kubectl get pvc`` until ``status.phase == "Bound"`` or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        payload, _error = _get_pvc_json(run_command, kubectl_base, namespace, pvc_name)
        if payload is not None and (payload.get("status") or {}).get("phase") == "Bound":
            return True
        time.sleep(poll_interval_s)
    return False


def _apply_mount_pod_manifest(
    kubectl_parts: list[str],
    namespace: str,
    pod_name: str,
    pvc_name: str,
    timeout: float,
) -> tuple[int, str]:
    """Render the BusyBox mount-pod manifest for ``pvc_name`` and ``kubectl apply`` it.

    Storage classes with ``volumeBindingMode: WaitForFirstConsumer`` (the
    default for AWS EBS, GCE PD, Azure Disk, etc.) keep the PVC ``Pending``
    until a pod referencing it is scheduled, so every check that needs the
    PVC to reach ``Bound`` must land a consumer alongside it.
    """

    def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
        return _set_fs_pod_fields(doc, namespace=namespace, name=pod_name, pvc_name=pvc_name)

    return _apply_manifest(kubectl_parts, render_k8s_manifest(_MOUNT_POD_MANIFEST, _mutate), timeout)


def _wait_pod_ready(run_command, kubectl_base: str, namespace: str, pod_name: str, timeout_s: int) -> tuple[bool, str]:
    """``kubectl wait --for=condition=Ready`` for ``pod_name``; return (ok, stderr_or_stdout)."""
    cmd = (
        f"{kubectl_base} wait --for=condition=Ready "
        f"--timeout={timeout_s}s -n {shlex.quote(namespace)} pod/{shlex.quote(pod_name)}"
    )
    result = run_command(cmd, timeout=timeout_s + 30)
    if result.exit_code == 0:
        return True, ""
    return False, (result.stderr.strip() or result.stdout.strip())


def _bounded_diagnostic_section(label: str, text: str) -> str:
    """Redact sensitive lines and cap one diagnostic section."""
    redacted = "\n".join(
        "[REDACTED]" if _SENSITIVE_DIAGNOSTIC_LINE.search(line) else line for line in text.splitlines()
    ).strip()
    if len(redacted) > _DIAGNOSTIC_SECTION_LIMIT:
        marker = "\n...[section truncated]"
        redacted = f"{redacted[: _DIAGNOSTIC_SECTION_LIMIT - len(marker)]}{marker}"
    return f"== {label} ==\n{redacted or '[no output]'}"


def _collect_storage_diagnostics(
    run_command,
    kubectl_base: str,
    namespace: str,
    *,
    pod_name: str,
    pvc_name: str,
) -> str:
    """Collect bounded pod/PVC state and events without changing test outcome."""
    namespace_quoted = shlex.quote(namespace)
    resources = (
        ("Pod", "pod", "Pod", pod_name),
        ("PVC", "pvc", "PersistentVolumeClaim", pvc_name),
    )
    sections: list[str] = []
    for label, resource, event_kind, name in resources:
        name_quoted = shlex.quote(name)
        commands = (
            (f"{label} state", f"{kubectl_base} get {resource} {name_quoted} -n {namespace_quoted} -o wide"),
            (f"{label} description", f"{kubectl_base} describe {resource} {name_quoted} -n {namespace_quoted}"),
            (
                f"{label} events",
                f"{kubectl_base} get events -n {namespace_quoted} "
                f"--field-selector involvedObject.kind={event_kind},involvedObject.name={name_quoted} "
                "--sort-by=.lastTimestamp",
            ),
        )
        for section_label, command in commands:
            try:
                result = run_command(command, timeout=30)
                output = (
                    result.stdout
                    if result.exit_code == 0
                    else (result.stderr or result.stdout or f"command exited {result.exit_code}")
                )
            except Exception as exc:
                output = f"diagnostic command failed: {type(exc).__name__}: {exc}"
            sections.append(_bounded_diagnostic_section(section_label, output))

    diagnostics = "\n".join(sections)
    if len(diagnostics) > _DIAGNOSTIC_TOTAL_LIMIT:
        marker = "\n...[diagnostics truncated]"
        diagnostics = f"{diagnostics[: _DIAGNOSTIC_TOTAL_LIMIT - len(marker)]}{marker}"
    return diagnostics


def _log_storage_diagnostics(
    validation: BaseValidation,
    *,
    pod_name: str,
    pvc_name: str,
) -> None:
    """Emit failure evidence before namespace cleanup; collection is best-effort."""
    diagnostics = _collect_storage_diagnostics(
        validation.run_command,
        validation._kubectl_base,
        validation._namespace,
        pod_name=pod_name,
        pvc_name=pvc_name,
    )
    validation.log.error(
        "CSI pod/PVC diagnostics before cleanup (pod=%s pvc=%s):\n%s",
        pod_name,
        pvc_name,
        diagnostics,
    )


def _storage_quantities_equal(left: Any, right: Any) -> bool:
    """Return True when two Kubernetes storage quantities represent the same byte count.

    Returns False when either side cannot be parsed as a Kubernetes quantity.
    """
    try:
        return parse_quantity(str(left).strip()) == parse_quantity(str(right).strip())
    except (ValueError, TypeError):
        return False


def _capacity_meets_request(capacity: Any, request: Any) -> bool:
    """Return True when ``capacity`` >= ``request`` as Kubernetes storage quantities.

    Returns False when either side is empty or cannot be parsed as a quantity.
    """
    try:
        return parse_quantity(str(capacity).strip()) >= parse_quantity(str(request).strip())
    except (ValueError, TypeError):
        return False


class K8sCsiStorageTypesCheck(BaseValidation):
    """Verify CSI supports block, shared filesystem, and NFS storage.

    For each configured storage type, two subtests run:

    * ``sc-exists[<type>]`` - the named StorageClass is visible to kubectl.
    * ``pvc-binds[<type>]`` - a fresh PVC against that StorageClass reaches
      ``Bound`` within ``bind_timeout_s``. A BusyBox consumer pod is
      scheduled alongside the PVC so this also works for StorageClasses
      with ``volumeBindingMode: WaitForFirstConsumer`` (the default for
      most cloud block CSIs).

    Types with no configured StorageClass are reported as skipped, so the
    check is safe to enable on every provider; pass if every configured type
    passes both subtests.

    Config keys (with defaults):
        block_storage_class: StorageClass for block (RWO) storage
            (default: from :func:`get_k8s_csi_block_storage_class`).
        shared_fs_storage_class: StorageClass for shared filesystem (RWX)
            (default: from :func:`get_k8s_csi_shared_fs_storage_class`).
        nfs_storage_class: StorageClass for NFS (RWX)
            (default: from :func:`get_k8s_csi_nfs_storage_class`).
        pvc_size: Requested capacity for each probe PVC (default: ``1Gi``).
        bind_timeout_s: Max wait for each PVC to reach ``Bound`` (default: 120).
        namespace_prefix: Prefix for the ephemeral namespace
            (default: ``isvtest-csi-types``).
        timeout: Overall class-level timeout for each ``run_command`` call
            (default: 300).
    """

    description: ClassVar[str] = "Verify CSI supports block, shared filesystem, and NFS storage classes."
    timeout: ClassVar[int] = 300

    def run(self) -> None:
        """Create an ephemeral namespace, probe each configured storage type, and record subtests."""
        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()
        bind_timeout = int(self.config.get("bind_timeout_s", 120))
        namespace_prefix = self.config.get("namespace_prefix", "isvtest-csi-types")
        pvc_size = str(self.config.get("pvc_size", "1Gi"))

        configured: dict[str, str] = {}
        for type_name, _ in _STORAGE_TYPES:
            sc_name = self.config.get(f"{type_name.replace('-', '_')}_storage_class") or _env_fallback(type_name)
            if sc_name:
                configured[type_name] = sc_name

        if not configured:
            self.set_passed("Skipped: no StorageClass configured for block/shared-fs/nfs")
            return

        self._namespace = f"{namespace_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)
        ns_created = False
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            any_failed = False
            covered: list[str] = []

            for type_name, access_mode in _STORAGE_TYPES:
                sc_name = configured.get(type_name)
                if not sc_name:
                    self.report_subtest(
                        f"sc-exists[{type_name}]",
                        passed=True,
                        message=f"{type_name} StorageClass not configured",
                        skipped=True,
                    )
                    self.report_subtest(
                        f"pvc-binds[{type_name}]",
                        passed=True,
                        message=f"{type_name} StorageClass not configured",
                        skipped=True,
                    )
                    continue

                sc_result = self.run_command(f"{self._kubectl_base} get storageclass {shlex.quote(sc_name)} -o json")
                sc_error = ""
                if sc_result.exit_code != 0:
                    sc_error = sc_result.stderr.strip() or sc_result.stdout.strip()
                else:
                    try:
                        parse_kubectl_json(sc_result, f"StorageClass {sc_name!r}")
                    except KubectlParseError as exc:
                        sc_error = str(exc)
                sc_ok = not sc_error
                self.report_subtest(
                    f"sc-exists[{type_name}]",
                    passed=sc_ok,
                    message=(
                        f"StorageClass {sc_name!r} found" if sc_ok else f"StorageClass {sc_name!r} missing: {sc_error}"
                    ),
                )
                if not sc_ok:
                    any_failed = True
                    # Skip the paired pvc-binds subtest so the overall ratio
                    # still reflects one failure per broken storage type.
                    self.report_subtest(
                        f"pvc-binds[{type_name}]",
                        passed=True,
                        message=f"StorageClass {sc_name!r} missing; PVC probe skipped",
                        skipped=True,
                    )
                    continue

                pvc_name = f"csi-types-{type_name}-{uuid.uuid4().hex[:6]}"
                pod_name = f"csi-types-{type_name}-{uuid.uuid4().hex[:6]}"
                if not self._apply_pvc(pvc_name, sc_name, access_mode, pvc_size):
                    any_failed = True
                    self.report_subtest(
                        f"pvc-binds[{type_name}]",
                        passed=False,
                        message=f"kubectl apply failed for PVC {pvc_name!r} ({sc_name}, {access_mode})",
                    )
                    continue

                pod_rc, pod_err = _apply_mount_pod_manifest(
                    self._kubectl_parts, self._namespace, pod_name, pvc_name, self.timeout
                )
                if pod_rc != 0:
                    any_failed = True
                    self.report_subtest(
                        f"pvc-binds[{type_name}]",
                        passed=False,
                        message=(
                            f"kubectl apply failed for consumer pod {pod_name!r} "
                            f"({sc_name}, {access_mode}): {pod_err.strip()[:200]}"
                        ),
                    )
                    continue

                pod_ready, wait_err = _wait_pod_ready(
                    self.run_command, self._kubectl_base, self._namespace, pod_name, bind_timeout
                )
                bound = pod_ready and self._wait_pvc_bound(pvc_name, 5)
                if not bound:
                    _log_storage_diagnostics(
                        self,
                        pod_name=pod_name,
                        pvc_name=pvc_name,
                    )
                self.report_subtest(
                    f"pvc-binds[{type_name}]",
                    passed=bound,
                    message=(
                        f"PVC {pvc_name!r} bound against {sc_name} ({access_mode}) via consumer pod {pod_name!r}"
                        if bound
                        else (
                            f"PVC {pvc_name!r} did not reach Bound via consumer pod {pod_name!r} "
                            f"within {bind_timeout}s ({sc_name}, {access_mode}): {wait_err[:200]}"
                        )
                    ),
                )
                if bound:
                    covered.append(type_name)
                else:
                    any_failed = True

            if any_failed:
                self.set_failed("One or more CSI storage-type subtests failed; see subtest details")
            else:
                self.set_passed(f"CSI supports storage types: {', '.join(sorted(covered))}")
        finally:
            # Cleanup must never overwrite pass/fail outcome set above.
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _apply_pvc(self, name: str, sc_name: str, access_mode: str, size: str) -> bool:
        """Render the PVC manifest and apply it; return True on success."""
        returncode, error = _apply_pvc_manifest(
            self._kubectl_parts, self._namespace, name, sc_name, access_mode, size, self.timeout
        )
        if returncode != 0:
            self.log.error("kubectl apply failed for PVC %s: %s", name, error.strip())
            return False
        return True

    def _wait_pvc_bound(self, pvc_name: str, timeout_s: int) -> bool:
        return _poll_pvc_bound(self.run_command, self._kubectl_base, self._namespace, pvc_name, timeout_s)


def _set_pvc_fields(
    doc: dict[str, Any],
    *,
    namespace: str,
    name: str,
    sc: str,
    mode: str,
    size: str,
) -> dict[str, Any]:
    """Mutate a parsed PVC manifest in place with the requested fields."""
    metadata = doc.setdefault("metadata", {})
    metadata["name"] = name
    metadata["namespace"] = namespace

    spec = doc.setdefault("spec", {})
    spec["storageClassName"] = sc
    spec["accessModes"] = [mode]
    resources = spec.setdefault("resources", {})
    requests = resources.setdefault("requests", {})
    requests["storage"] = size
    return doc


def _env_fallback(type_name: str) -> str:
    """Return the env-backed StorageClass default for a given storage type."""
    if type_name == "block":
        return get_k8s_csi_block_storage_class()
    if type_name == "shared-fs":
        return get_k8s_csi_shared_fs_storage_class()
    if type_name == "nfs":
        return get_k8s_csi_nfs_storage_class()
    return ""


class K8sCsiStorageQuotaApiCheck(BaseValidation):
    """Verify Kubernetes-native APIs expose storage quota and per-PVC/PV usage.

    Four subtests run against a single ephemeral namespace:

    * ``resourcequota-storage-api`` - a ResourceQuota carrying both
      ``requests.storage`` and
      ``<sc>.storageclass.storage.k8s.io/requests.storage`` lands and its
      ``.status.hard`` publishes both keys.
    * ``per-pvc-usage`` - a PVC against the configured StorageClass binds
      (via a BusyBox consumer pod so this works under
      ``WaitForFirstConsumer``), exposes ``.status.capacity.storage``, and
      its usage is reflected in the ResourceQuota's ``.status.used``.
    * ``quota-enforcement`` - an over-quota PVC is rejected at admission
      with an ``exceeded quota`` / ``forbidden`` message.
    * ``pv-usage-api`` - the bound PV exposes ``.spec.capacity.storage``,
      ``.spec.claimRef.name`` matching the usage PVC, and
      ``.spec.csi.driver``.

    When ``resourcequota-storage-api`` fails, the three downstream subtests
    are reported as skipped because they all require the quota to be in
    place; the overall validation still fails.

    Config keys (with defaults):
        storage_class: StorageClass to use for the probe PVCs; defaults to
            :func:`get_k8s_csi_block_storage_class`. When unset the whole
            check is skipped so it is safe to enable everywhere.
        total_quota: Namespace-wide ``requests.storage`` cap
            (default: ``10Gi``).
        per_sc_quota: Per-StorageClass cap (default: ``5Gi``). Must exceed
            ``pvc_request`` so the usage PVC fits.
        pvc_request: Size requested by the usage PVC (default: ``1Gi``).
        over_quota_request: Size requested by the enforcement PVC; must
            exceed ``per_sc_quota`` so admission rejects it
            (default: ``100Gi``).
        bind_timeout_s: Max wait for the usage PVC to Bind (default: 120).
        quota_settle_s: Max wait for each ``ResourceQuota.status`` section
            (``hard`` and ``used``) to populate (default: 30).
        namespace_prefix: Prefix for the ephemeral namespace
            (default: ``isvtest-csi-quota``).
        timeout: Overall class-level timeout for each ``run_command``
            (default: 300).
    """

    description: ClassVar[str] = "Verify Kubernetes-native APIs expose storage quota and per-PVC/PV usage."
    timeout: ClassVar[int] = 300

    def run(self) -> None:
        """Drive the four-subtest quota-API probe against a single ephemeral namespace."""
        storage_class = self.config.get("storage_class") or get_k8s_csi_block_storage_class()
        if not storage_class:
            self.set_passed("Skipped: no storage_class configured")
            return

        total_quota = str(self.config.get("total_quota", "10Gi"))
        per_sc_quota = str(self.config.get("per_sc_quota", "5Gi"))
        pvc_request = str(self.config.get("pvc_request", "1Gi"))
        over_quota_request = str(self.config.get("over_quota_request", "100Gi"))
        bind_timeout = int(self.config.get("bind_timeout_s", 120))
        quota_settle = int(self.config.get("quota_settle_s", 30))
        ns_prefix = self.config.get("namespace_prefix", "isvtest-csi-quota")

        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()
        self._namespace = f"{ns_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)

        sc_quota_key = f"{storage_class}.storageclass.storage.k8s.io/requests.storage"
        rq_name = f"storage-quota-{uuid.uuid4().hex[:6]}"
        usage_pvc = f"quota-usage-{uuid.uuid4().hex[:6]}"
        over_pvc = f"quota-over-{uuid.uuid4().hex[:6]}"

        ns_created = False
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            any_failed = False

            quota_ok = self._apply_resourcequota(rq_name, total_quota, sc_quota_key, per_sc_quota)
            hard: dict[str, Any] | None = None
            if quota_ok:
                hard = self._wait_quota_section(rq_name, "hard", [sc_quota_key, "requests.storage"], quota_settle)
                quota_ok = hard is not None

            if quota_ok:
                self.report_subtest(
                    "resourcequota-storage-api",
                    passed=True,
                    message=(
                        f"ResourceQuota {rq_name!r} hard={{requests.storage: {hard.get('requests.storage')!r}, "
                        f"{sc_quota_key}: {hard.get(sc_quota_key)!r}}}"
                        if hard
                        else f"ResourceQuota {rq_name!r} hard populated"
                    ),
                )
            else:
                self.report_subtest(
                    "resourcequota-storage-api",
                    passed=False,
                    message=(
                        f"ResourceQuota {rq_name!r} did not publish hard limits "
                        f"(both {'requests.storage'!r} and {sc_quota_key!r}) within {quota_settle}s"
                    ),
                )
                for dependent in ("per-pvc-usage", "quota-enforcement", "pv-usage-api"):
                    self.report_subtest(
                        dependent,
                        passed=True,
                        message="resourcequota-storage-api failed; dependent probe skipped",
                        skipped=True,
                    )
                self.set_failed("ResourceQuota did not land; downstream quota probes skipped")
                return

            usage_ok = self._run_per_pvc_usage(
                pvc_name=usage_pvc,
                storage_class=storage_class,
                pvc_request=pvc_request,
                rq_name=rq_name,
                sc_quota_key=sc_quota_key,
                bind_timeout=bind_timeout,
                quota_settle=quota_settle,
            )
            if not usage_ok:
                any_failed = True

            if not self._run_quota_enforcement(over_pvc, storage_class, over_quota_request):
                any_failed = True

            # pv-usage-api requires per-pvc-usage to have produced a Bound PVC.
            if usage_ok:
                if not self._run_pv_usage_api(usage_pvc):
                    any_failed = True
            else:
                self.report_subtest(
                    "pv-usage-api",
                    passed=True,
                    message="per-pvc-usage did not produce a bound PV; pv-usage-api skipped",
                    skipped=True,
                )

            if any_failed:
                self.set_failed("One or more storage quota API subtests failed; see subtest details")
            else:
                self.set_passed(f"Storage quota APIs verified against StorageClass {storage_class!r}")
        finally:
            # Cleanup must never overwrite pass/fail outcome set above.
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _apply_resourcequota(
        self,
        name: str,
        total_quota: str,
        sc_quota_key: str,
        per_sc_quota: str,
    ) -> bool:
        """Render and apply the ResourceQuota manifest; return True on success."""

        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            return _set_resourcequota_fields(
                doc,
                namespace=self._namespace,
                name=name,
                total_quota=total_quota,
                sc_quota_key=sc_quota_key,
                per_sc_quota=per_sc_quota,
            )

        manifest = render_k8s_manifest(_RESOURCEQUOTA_MANIFEST, _mutate)
        returncode, stderr = self._run_kubectl_apply(manifest)
        if returncode != 0:
            self.log.error("kubectl apply failed for ResourceQuota %s: %s", name, stderr)
            return False
        return True

    def _run_per_pvc_usage(
        self,
        *,
        pvc_name: str,
        storage_class: str,
        pvc_request: str,
        rq_name: str,
        sc_quota_key: str,
        bind_timeout: int,
        quota_settle: int,
    ) -> bool:
        """Apply the usage PVC + consumer pod, wait for Bound, and assert per-PVC / quota-used visibility."""
        returncode, stderr = self._apply_pvc(pvc_name, storage_class, "ReadWriteOnce", pvc_request)
        if returncode != 0:
            self.report_subtest(
                "per-pvc-usage",
                passed=False,
                message=f"kubectl apply failed for PVC {pvc_name!r}: {stderr.strip()}",
            )
            return False

        pod_name = f"quota-usage-{uuid.uuid4().hex[:6]}"
        pod_rc, pod_err = _apply_mount_pod_manifest(
            self._kubectl_parts, self._namespace, pod_name, pvc_name, self.timeout
        )
        if pod_rc != 0:
            self.report_subtest(
                "per-pvc-usage",
                passed=False,
                message=f"kubectl apply failed for consumer pod {pod_name!r}: {pod_err.strip()[:200]}",
            )
            return False

        pod_ready, wait_err = _wait_pod_ready(
            self.run_command, self._kubectl_base, self._namespace, pod_name, bind_timeout
        )
        if not pod_ready or not self._wait_pvc_bound(pvc_name, 5):
            _log_storage_diagnostics(
                self,
                pod_name=pod_name,
                pvc_name=pvc_name,
            )
            self.report_subtest(
                "per-pvc-usage",
                passed=False,
                message=(
                    f"PVC {pvc_name!r} did not reach Bound via consumer pod {pod_name!r} "
                    f"within {bind_timeout}s: {wait_err[:200]}"
                ),
            )
            return False

        capacity = self._get_pvc_capacity(pvc_name)
        if not capacity:
            self.report_subtest(
                "per-pvc-usage",
                passed=False,
                message=f"PVC {pvc_name!r} bound but status.capacity.storage is empty",
            )
            return False

        used = self._wait_quota_section(rq_name, "used", [sc_quota_key, "requests.storage"], quota_settle)
        if used is None:
            self.report_subtest(
                "per-pvc-usage",
                passed=False,
                message=(
                    f"PVC {pvc_name!r} capacity={capacity!r} but ResourceQuota.status.used "
                    f"did not reflect both keys within {quota_settle}s"
                ),
            )
            return False

        mismatches = [
            f"{key}={value!r}"
            for key, value in ((k, used[k]) for k in ("requests.storage", sc_quota_key))
            if not _storage_quantities_equal(value, pvc_request)
        ]
        if mismatches:
            self.report_subtest(
                "per-pvc-usage",
                passed=False,
                message=(
                    f"PVC {pvc_name!r} request={pvc_request!r}, capacity={capacity!r} "
                    f"but ResourceQuota.status.used did not match requested storage: {', '.join(mismatches)}"
                ),
            )
            return False

        self.report_subtest(
            "per-pvc-usage",
            passed=True,
            message=(
                f"PVC {pvc_name!r} request={pvc_request!r}, capacity={capacity!r}; "
                f"quota.used[requests.storage]={used['requests.storage']!r}, "
                f"quota.used[{sc_quota_key}]={used[sc_quota_key]!r}"
            ),
        )
        return True

    def _run_quota_enforcement(self, pvc_name: str, storage_class: str, request_size: str) -> bool:
        """Attempt to apply an over-quota PVC; pass if admission rejects with a quota message."""
        returncode, stderr = self._apply_pvc(pvc_name, storage_class, "ReadWriteOnce", request_size)
        if returncode == 0:
            self.report_subtest(
                "quota-enforcement",
                passed=False,
                message=(
                    f"Over-quota PVC {pvc_name!r} (request={request_size}) was admitted; "
                    f"ResourceQuota did not enforce the per-StorageClass cap"
                ),
            )
            return False

        lowered = stderr.lower()
        if any(token in lowered for token in _QUOTA_REJECTION_TOKENS):
            self.report_subtest(
                "quota-enforcement",
                passed=True,
                message=f"Over-quota PVC {pvc_name!r} rejected at admission: {stderr.strip()[:200]}",
            )
            return True

        self.report_subtest(
            "quota-enforcement",
            passed=False,
            message=(
                f"Over-quota PVC {pvc_name!r} apply failed but stderr did not mention quota enforcement: "
                f"{stderr.strip()[:200]}"
            ),
        )
        return False

    def _run_pv_usage_api(self, pvc_name: str) -> bool:
        """Resolve the PVC's bound PV and assert ``spec.capacity/claimRef/csi.driver`` are populated."""
        pvc_payload, error = _get_pvc_json(self.run_command, self._kubectl_base, self._namespace, pvc_name)
        if pvc_payload is None:
            self.report_subtest(
                "pv-usage-api",
                passed=False,
                message=f"Could not resolve volumeName for PVC {pvc_name!r}: {error}",
            )
            return False
        pv_name = str((pvc_payload.get("spec") or {}).get("volumeName") or "")
        if not pv_name:
            self.report_subtest(
                "pv-usage-api",
                passed=False,
                message=f"Could not resolve volumeName for PVC {pvc_name!r}: spec.volumeName is empty",
            )
            return False

        pv_json = self.run_command(f"{self._kubectl_base} get pv {shlex.quote(pv_name)} -o json")
        if pv_json.exit_code != 0:
            self.report_subtest(
                "pv-usage-api",
                passed=False,
                message=f"kubectl get pv {pv_name!r} failed: {pv_json.stderr.strip()}",
            )
            return False

        try:
            payload = json.loads(pv_json.stdout)
        except json.JSONDecodeError as exc:
            self.report_subtest(
                "pv-usage-api",
                passed=False,
                message=f"Failed to parse PV {pv_name!r} JSON: {exc}",
            )
            return False

        spec = payload.get("spec") or {}
        capacity = (spec.get("capacity") or {}).get("storage")
        claim_ref_name = (spec.get("claimRef") or {}).get("name")
        csi_driver = (spec.get("csi") or {}).get("driver")

        missing: list[str] = []
        if not capacity:
            missing.append("spec.capacity.storage")
        if claim_ref_name != pvc_name:
            missing.append(f"spec.claimRef.name (expected {pvc_name!r}, got {claim_ref_name!r})")
        if not csi_driver:
            missing.append("spec.csi.driver")

        if missing:
            self.report_subtest(
                "pv-usage-api",
                passed=False,
                message=f"PV {pv_name!r} missing required fields: {', '.join(missing)}",
            )
            return False

        self.report_subtest(
            "pv-usage-api",
            passed=True,
            message=(
                f"PV {pv_name!r} spec.capacity.storage={capacity!r}, "
                f"spec.claimRef.name={claim_ref_name!r}, spec.csi.driver={csi_driver!r}"
            ),
        )
        return True

    def _apply_pvc(self, name: str, storage_class: str, access_mode: str, size: str) -> tuple[int, str]:
        """Render the PVC manifest and apply it; return (returncode, stderr)."""
        return _apply_pvc_manifest(
            self._kubectl_parts, self._namespace, name, storage_class, access_mode, size, self.timeout
        )

    def _run_kubectl_apply(self, manifest: str) -> tuple[int, str]:
        return _apply_manifest(self._kubectl_parts, manifest, self.timeout)

    def _wait_pvc_bound(self, pvc_name: str, timeout_s: int) -> bool:
        return _poll_pvc_bound(self.run_command, self._kubectl_base, self._namespace, pvc_name, timeout_s)

    def _get_pvc_capacity(self, pvc_name: str) -> str:
        """Return ``.status.capacity.storage`` for ``pvc_name`` (empty string if unset)."""
        payload, _error = _get_pvc_json(self.run_command, self._kubectl_base, self._namespace, pvc_name)
        if payload is None:
            return ""
        return str(((payload.get("status") or {}).get("capacity") or {}).get("storage") or "")

    def _wait_quota_section(
        self,
        rq_name: str,
        section: str,
        required_keys: list[str],
        timeout_s: int,
    ) -> dict[str, Any] | None:
        """Poll ``ResourceQuota.status.<section>`` until every ``required_keys`` entry appears.

        Returns the section dict once fully populated, or ``None`` on timeout
        / parse failure. Keys are compared verbatim - the caller is
        responsible for passing the concrete per-StorageClass key name.
        """
        deadline = time.time() + timeout_s
        cmd = f"{self._kubectl_base} get resourcequota {shlex.quote(rq_name)} -n {shlex.quote(self._namespace)} -o json"
        while time.time() < deadline:
            result = self.run_command(cmd)
            if result.exit_code == 0 and result.stdout:
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError:
                    payload = None
                if payload:
                    section_dict = (payload.get("status") or {}).get(section) or {}
                    if all(key in section_dict for key in required_keys):
                        return section_dict
            time.sleep(2.0)
        return None


def _set_resourcequota_fields(
    doc: dict[str, Any],
    *,
    namespace: str,
    name: str,
    total_quota: str,
    sc_quota_key: str,
    per_sc_quota: str,
) -> dict[str, Any]:
    """Mutate a parsed ResourceQuota manifest in place with the requested fields.

    ``spec.hard`` is rebuilt rather than merged so the per-StorageClass key
    (whose name is only known at runtime) fully replaces any placeholder
    value carried by the template.
    """
    metadata = doc.setdefault("metadata", {})
    metadata["name"] = name
    metadata["namespace"] = namespace

    spec = doc.setdefault("spec", {})
    spec["hard"] = {
        "requests.storage": total_quota,
        sc_quota_key: per_sc_quota,
    }
    return doc


_CSI_CONTROLLER_IMAGE_TOKENS: tuple[str, ...] = (
    "csi-provisioner",
    "csi-attacher",
    "csi-resizer",
    "csi-snapshotter",
    "external-provisioner",
    "external-attacher",
    "external-resizer",
    "external-snapshotter",
)

_CSI_NODE_IMAGE_TOKENS: tuple[str, ...] = (
    "csi-node-driver-registrar",
    "node-driver-registrar",
)

_CSI_IMAGE_TOKENS: tuple[str, ...] = _CSI_CONTROLLER_IMAGE_TOKENS + _CSI_NODE_IMAGE_TOKENS

_SECRET_VERBS: frozenset[str] = frozenset(
    {"*", "get", "list", "watch", "create", "update", "patch", "delete", "deletecollection"}
)

_NODE_PLUGIN_ALLOWED_VOLUME_KEYS: frozenset[str] = frozenset(
    {
        "hostPath",
        "emptyDir",
        "secret",
        "configMap",
        "projected",
        "downwardAPI",
        "serviceAccountToken",
        "csi",
    }
)

_SHARED_CLUSTER_ANNOTATIONS: tuple[str, ...] = (
    "csi.nvidia.com/shared",
    "csi.nvidia.com/shared-across-clusters",
)


class K8sCsiTenantScopedCredentialsCheck(BaseValidation):
    """Verify CSI credentials are tenant-scoped by construction.

    This check inspects in-cluster observable state only; it does **not**
    attempt any cross-cluster exfiltration, and so cannot prove isolation
    between tenants from the outside - that is tracked separately.

    Five subtests run (all must pass):

    * ``csi-secrets-discovered`` - enumerate CSI drivers and discover the
      Secrets they reference via ``PersistentVolume.spec.csi.*SecretRef``
      and via CSI controller/node pod specs (``envFrom``, ``env.valueFrom``,
      Secret volume mounts). Skipped when no ``CSIDriver`` objects exist.
    * ``secrets-not-cross-namespace`` - every discovered Secret lives in
      ``csi_driver_namespaces`` (or ``allowed_workload_namespaces``), never
      in ``default`` or an unlisted workload namespace.
    * ``no-shared-cluster-markers`` - no discovered Secret carries any of
      ``forbidden_labels`` or an annotation like
      ``csi.nvidia.com/shared=true``.
    * ``serviceaccount-rbac-scoped`` - CSI controller ServiceAccounts hold
      no ``ClusterRoleBinding`` that grants ``secrets`` verbs cluster-wide
      without a ``resourceNames`` restriction. Namespace-scoped
      ``RoleBinding`` grants are permitted.
    * ``node-plugin-uses-hostpath-not-shared-mount`` - node-plugin pods
      mount only local/pod-scoped volume types (hostPath, emptyDir, secret,
      configMap, projected, downwardAPI, serviceAccountToken, csi). Any
      ``nfs``/``iscsi``/``persistentVolumeClaim`` volume fails this subtest.

    Config keys (with defaults):
        csi_driver_namespaces: Namespaces where CSI controller/node pods
            live (default: ``["kube-system"]``).
        allowed_workload_namespaces: Extra namespaces where CSI Secrets are
            permitted (default: ``[]``).
        forbidden_labels: ``key=value`` label pairs whose presence on a CSI
            Secret marks it as shared across clusters
            (default: ``["shared-across-clusters=true"]``).
        timeout: Overall class-level timeout for each ``run_command``
            (default: 300).
    """

    description: ClassVar[str] = "Verify CSI credentials are tenant-scoped by construction."
    timeout: ClassVar[int] = 300

    def run(self) -> None:
        """Drive the five read-only subtests over CSI drivers, Secrets, RBAC, and pods."""
        self._kubectl_base = get_kubectl_base_shell()

        driver_namespaces = [str(ns) for ns in (self.config.get("csi_driver_namespaces") or ["kube-system"])]
        allowed_workload_namespaces = [str(ns) for ns in (self.config.get("allowed_workload_namespaces") or [])]
        forbidden_labels_raw = self.config.get("forbidden_labels") or ["shared-across-clusters=true"]
        forbidden_labels = _parse_label_pairs(forbidden_labels_raw)

        permitted_namespaces = set(driver_namespaces) | set(allowed_workload_namespaces)

        # Discover CSIDriver objects up front. If none exist we have nothing
        # to validate; the check is skipped so it is safe to enable on
        # clusters without any CSI driver installed.
        csi_drivers = self._list_csi_drivers()
        if csi_drivers is None:
            self.set_failed("Failed to list CSIDriver objects")
            return
        if not csi_drivers:
            for name in (
                "csi-secrets-discovered",
                "secrets-not-cross-namespace",
                "no-shared-cluster-markers",
                "serviceaccount-rbac-scoped",
                "node-plugin-uses-hostpath-not-shared-mount",
            ):
                self.report_subtest(name, passed=True, message="No CSIDriver objects present", skipped=True)
            self.set_passed("Skipped: no CSIDriver objects found")
            return

        pods_by_ns: dict[str, list[dict[str, Any]]] = {}
        for ns in driver_namespaces:
            pods = self._list_pods(ns)
            if pods is None:
                self.set_failed(f"Failed to list pods in namespace {ns!r}")
                return
            pods_by_ns[ns] = pods

        pvs = self._list_pvs()
        if pvs is None:
            self.set_failed("Failed to list PersistentVolumes")
            return

        pv_secret_refs = _collect_pv_secret_refs(pvs)
        pod_secret_refs = _collect_pod_secret_refs(pods_by_ns)
        discovered_secrets = sorted({(ns, name) for (ns, name) in (pv_secret_refs | pod_secret_refs)})

        if not discovered_secrets:
            self.report_subtest(
                "csi-secrets-discovered",
                passed=True,
                message=(
                    f"No CSI Secret references found across {len(csi_drivers)} driver(s); "
                    "CSI likely uses IRSA / workload-identity or node-level IAM"
                ),
            )
        else:
            self.report_subtest(
                "csi-secrets-discovered",
                passed=True,
                message=(
                    f"Discovered {len(discovered_secrets)} CSI Secret reference(s): "
                    f"{', '.join(f'{ns}/{name}' for ns, name in discovered_secrets[:10])}"
                    + ("..." if len(discovered_secrets) > 10 else "")
                ),
            )

        any_failed = False

        cross_ns = [(ns, name) for (ns, name) in discovered_secrets if ns not in permitted_namespaces]
        if cross_ns:
            any_failed = True
            self.report_subtest(
                "secrets-not-cross-namespace",
                passed=False,
                message=(
                    f"Discovered Secret(s) outside {sorted(permitted_namespaces)}: "
                    f"{', '.join(f'{ns}/{name}' for ns, name in cross_ns)}"
                ),
            )
        else:
            self.report_subtest(
                "secrets-not-cross-namespace",
                passed=True,
                message=(
                    f"All {len(discovered_secrets)} discovered Secret(s) live in permitted namespaces "
                    f"{sorted(permitted_namespaces)}"
                ),
            )

        marker_failures: list[str] = []
        missing_secrets: list[str] = []
        for ns, name in discovered_secrets:
            secret, status = self._get_secret(ns, name)
            if status == "missing":
                # Dangling Secret reference - the CSI pod declares it
                # (e.g. `envFrom.secretRef`) but the Secret was never
                # created, which is the EKS / IRSA default for EFS and
                # EBS CSI drivers. Nothing to carry a marker, so this
                # does not fail the check.
                missing_secrets.append(f"{ns}/{name}")
                continue
            if secret is None:
                # Reading the Secret failed for some other reason
                # (typically Forbidden). We can't prove the marker is
                # absent, so surface this as a failure for investigation.
                marker_failures.append(f"{ns}/{name} (unreadable)")
                continue
            marker = _find_shared_cluster_marker(secret, forbidden_labels)
            if marker:
                marker_failures.append(f"{ns}/{name} ({marker})")

        if marker_failures:
            any_failed = True
            self.report_subtest(
                "no-shared-cluster-markers",
                passed=False,
                message=f"Secret(s) carry shared-cluster markers: {', '.join(marker_failures)}",
            )
        else:
            if missing_secrets:
                msg = (
                    f"No shared-cluster labels/annotations on "
                    f"{len(discovered_secrets) - len(missing_secrets)} discovered Secret(s); "
                    f"{len(missing_secrets)} declared Secret ref(s) do not exist "
                    f"(likely IRSA / workload-identity): "
                    f"{', '.join(missing_secrets[:5])}" + ("..." if len(missing_secrets) > 5 else "")
                )
            else:
                msg = f"No shared-cluster labels/annotations on {len(discovered_secrets)} discovered Secret(s)"
            self.report_subtest("no-shared-cluster-markers", passed=True, message=msg)

        controller_sa_refs = _collect_controller_service_accounts(pods_by_ns)
        if not controller_sa_refs:
            self.report_subtest(
                "serviceaccount-rbac-scoped",
                passed=True,
                message="No CSI controller pods identified; RBAC check not applicable",
                skipped=True,
            )
        else:
            rbac_failures = self._check_controller_rbac(controller_sa_refs)
            if rbac_failures is None:
                any_failed = True
                self.report_subtest(
                    "serviceaccount-rbac-scoped",
                    passed=False,
                    message="Failed to enumerate ClusterRoleBindings / ClusterRoles",
                )
            elif rbac_failures:
                any_failed = True
                self.report_subtest(
                    "serviceaccount-rbac-scoped",
                    passed=False,
                    message=(
                        "CSI controller ServiceAccount(s) hold cluster-scoped Secret privileges: "
                        + "; ".join(rbac_failures)
                    ),
                )
            else:
                self.report_subtest(
                    "serviceaccount-rbac-scoped",
                    passed=True,
                    message=(
                        f"No ClusterRoleBinding grants cluster-scoped Secret verbs to "
                        f"{len(controller_sa_refs)} CSI controller ServiceAccount(s)"
                    ),
                )

        node_volume_failures = _check_node_plugin_volumes(pods_by_ns)
        if node_volume_failures == "no-node-pods":
            self.report_subtest(
                "node-plugin-uses-hostpath-not-shared-mount",
                passed=True,
                message="No CSI node-plugin pods identified; mount-topology check not applicable",
                skipped=True,
            )
        elif node_volume_failures:
            any_failed = True
            self.report_subtest(
                "node-plugin-uses-hostpath-not-shared-mount",
                passed=False,
                message=(
                    "CSI node-plugin pod(s) mount cross-namespace / non-local volume types: "
                    + "; ".join(node_volume_failures)
                ),
            )
        else:
            self.report_subtest(
                "node-plugin-uses-hostpath-not-shared-mount",
                passed=True,
                message="CSI node-plugin pods mount only local/pod-scoped volume types",
            )

        if any_failed:
            self.set_failed("One or more CSI tenant-scoping subtests failed; see subtest details")
        else:
            self.set_passed(
                f"CSI credentials tenant-scoped by construction "
                f"({len(csi_drivers)} driver(s), {len(discovered_secrets)} Secret ref(s))"
            )

    def _list_csi_drivers(self) -> list[dict[str, Any]] | None:
        result = self.run_command(f"{self._kubectl_base} get csidriver -o json")
        if result.exit_code != 0:
            self.log.error("kubectl get csidriver failed: %s", result.stderr.strip())
            return None
        return _load_items(result.stdout)

    def _list_pods(self, namespace: str) -> list[dict[str, Any]] | None:
        result = self.run_command(f"{self._kubectl_base} get pods -n {shlex.quote(namespace)} -o json")
        if result.exit_code != 0:
            self.log.error("kubectl get pods -n %s failed: %s", namespace, result.stderr.strip())
            return None
        return _load_items(result.stdout)

    def _list_pvs(self) -> list[dict[str, Any]] | None:
        result = self.run_command(f"{self._kubectl_base} get pv -o json")
        if result.exit_code != 0:
            self.log.error("kubectl get pv failed: %s", result.stderr.strip())
            return None
        return _load_items(result.stdout)

    def _get_secret(self, namespace: str, name: str) -> tuple[dict[str, Any] | None, str]:
        """Fetch Secret ``namespace/name``; return ``(secret, status)``.

        ``status`` is one of:
          * ``"found"`` - Secret exists and parsed (``secret`` is the dict).
          * ``"missing"`` - Secret does not exist (``--ignore-not-found``
            returned empty stdout). Common when a CSI pod declares an
            ``envFrom.secretRef`` but the Secret is never created - e.g.
            EKS CSI drivers running under IRSA.
          * ``"error"`` - kubectl failed (Forbidden, RBAC, transient, ...)
            or JSON parsing failed.
        """
        result = self.run_command(
            f"{self._kubectl_base} get secret {shlex.quote(name)} -n {shlex.quote(namespace)} "
            f"--ignore-not-found=true -o json"
        )
        if result.exit_code != 0:
            self.log.warning("kubectl get secret %s/%s failed: %s", namespace, name, result.stderr.strip())
            return None, "error"
        stdout = result.stdout.strip()
        if not stdout:
            self.log.info(
                "Secret %s/%s referenced by CSI pod but not present in cluster "
                "(likely optional envFrom with driver using IRSA / workload identity)",
                namespace,
                name,
            )
            return None, "missing"
        try:
            return json.loads(stdout), "found"
        except json.JSONDecodeError as exc:
            self.log.warning("Failed to parse Secret %s/%s JSON: %s", namespace, name, exc)
            return None, "error"

    def _check_controller_rbac(self, service_accounts: set[tuple[str, str]]) -> list[str] | None:
        """Return a list of violation descriptions (empty if none) or None on listing error."""
        crb_result = self.run_command(f"{self._kubectl_base} get clusterrolebinding -o json")
        if crb_result.exit_code != 0:
            self.log.error("kubectl get clusterrolebinding failed: %s", crb_result.stderr.strip())
            return None
        cr_result = self.run_command(f"{self._kubectl_base} get clusterrole -o json")
        if cr_result.exit_code != 0:
            self.log.error("kubectl get clusterrole failed: %s", cr_result.stderr.strip())
            return None

        bindings = _load_items(crb_result.stdout)
        roles = {(item.get("metadata") or {}).get("name", ""): item for item in _load_items(cr_result.stdout)}

        return _find_cluster_secret_grants(bindings, roles, service_accounts)


def _parse_label_pairs(raw: list[Any]) -> list[tuple[str, str]]:
    """Parse ``['k=v', 'k2=v2']`` config entries into ``[(k, v), ...]``.

    Entries without ``=`` are treated as ``(key, "")`` so callers can match
    a key regardless of value when that is the intent.
    """
    pairs: list[tuple[str, str]] = []
    for entry in raw:
        text = str(entry)
        if "=" in text:
            key, _, value = text.partition("=")
            pairs.append((key.strip(), value.strip()))
        else:
            pairs.append((text.strip(), ""))
    return pairs


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is a dict, else an empty dict (defensive config access)."""
    return value if isinstance(value, dict) else {}


def _load_items(stdout: str) -> list[dict[str, Any]]:
    """Parse a ``kubectl get ... -o json`` list payload into its ``items`` list."""
    if not stdout:
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _collect_pv_secret_refs(pvs: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Return ``{(namespace, name), ...}`` for Secret refs under every PV's ``spec.csi``."""
    refs: set[tuple[str, str]] = set()
    secret_ref_keys = (
        "controllerPublishSecretRef",
        "nodePublishSecretRef",
        "nodeStageSecretRef",
        "controllerExpandSecretRef",
        "nodeExpandSecretRef",
    )
    for pv in pvs:
        csi = ((pv.get("spec") or {}).get("csi")) or {}
        for key in secret_ref_keys:
            ref = csi.get(key) or {}
            name = ref.get("name")
            namespace = ref.get("namespace")
            if name and namespace:
                refs.add((str(namespace), str(name)))
    return refs


def _collect_pod_secret_refs(pods_by_ns: dict[str, list[dict[str, Any]]]) -> set[tuple[str, str]]:
    """Return Secret refs mounted / projected by CSI pods in each driver namespace.

    Pods whose containers do not carry a CSI-related image token are
    ignored - this keeps noisy side-car / operator Secret references from
    leaking into the check's scope.
    """
    refs: set[tuple[str, str]] = set()
    for namespace, pods in pods_by_ns.items():
        for pod in pods:
            if not _pod_has_csi_image(pod):
                continue
            spec = pod.get("spec") or {}
            for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
                for env_from in container.get("envFrom") or []:
                    secret_ref = (env_from.get("secretRef") or {}).get("name")
                    if secret_ref:
                        refs.add((namespace, str(secret_ref)))
                for env in container.get("env") or []:
                    secret_key_ref = ((env.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
                    if secret_key_ref:
                        refs.add((namespace, str(secret_key_ref)))
            for volume in spec.get("volumes") or []:
                secret_volume = volume.get("secret") or {}
                name = secret_volume.get("secretName")
                if name:
                    refs.add((namespace, str(name)))
                for source in (volume.get("projected") or {}).get("sources") or []:
                    name = (source.get("secret") or {}).get("name")
                    if name:
                        refs.add((namespace, str(name)))
    return refs


def _pod_has_csi_image(pod: dict[str, Any]) -> bool:
    """Return True when any container in the pod uses a CSI sidecar/node-registrar image."""
    spec = pod.get("spec") or {}
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        image = str(container.get("image") or "")
        if any(token in image for token in _CSI_IMAGE_TOKENS):
            return True
    return False


def _pod_is_controller(pod: dict[str, Any]) -> bool:
    """Return True when the pod looks like a CSI controller (has a provisioner/attacher sidecar)."""
    spec = pod.get("spec") or {}
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        image = str(container.get("image") or "")
        if any(token in image for token in _CSI_CONTROLLER_IMAGE_TOKENS):
            return True
    return False


def _pod_is_node_plugin(pod: dict[str, Any]) -> bool:
    """Return True when the pod looks like a CSI node plugin (has node-driver-registrar)."""
    spec = pod.get("spec") or {}
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        image = str(container.get("image") or "")
        if any(token in image for token in _CSI_NODE_IMAGE_TOKENS):
            return True
    return False


def _find_shared_cluster_marker(
    secret: dict[str, Any],
    forbidden_labels: list[tuple[str, str]],
) -> str:
    """Return a short human description of the first shared-cluster marker found, or ``""``.

    An empty string means no marker was found.
    """
    metadata = secret.get("metadata") or {}
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}

    for key, value in forbidden_labels:
        actual = labels.get(key)
        if actual is None:
            continue
        if not value or str(actual) == value:
            return f"label {key}={actual}"

    for annotation_key in _SHARED_CLUSTER_ANNOTATIONS:
        annotation_value = annotations.get(annotation_key)
        if annotation_value and str(annotation_value).lower() == "true":
            return f"annotation {annotation_key}={annotation_value}"

    return ""


def _collect_controller_service_accounts(
    pods_by_ns: dict[str, list[dict[str, Any]]],
) -> set[tuple[str, str]]:
    """Return ``{(namespace, serviceAccountName), ...}`` for CSI controller pods."""
    refs: set[tuple[str, str]] = set()
    for namespace, pods in pods_by_ns.items():
        for pod in pods:
            if not _pod_is_controller(pod):
                continue
            spec = pod.get("spec") or {}
            sa = spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"
            refs.add((namespace, str(sa)))
    return refs


def _find_cluster_secret_grants(
    bindings: list[dict[str, Any]],
    roles: dict[str, dict[str, Any]],
    service_accounts: set[tuple[str, str]],
) -> list[str]:
    """Return a list of violation descriptions for cluster-scoped Secret grants to CSI SAs.

    A violation is any ``ClusterRoleBinding`` whose ``subjects`` include one
    of ``service_accounts`` and whose referenced ``ClusterRole`` contains a
    rule covering ``secrets`` without a ``resourceNames`` restriction.
    """
    violations: list[str] = []
    for binding in bindings:
        subjects = binding.get("subjects") or []
        matched = [
            (subj.get("namespace", ""), subj.get("name", ""))
            for subj in subjects
            if subj.get("kind") == "ServiceAccount"
            and (subj.get("namespace", ""), subj.get("name", "")) in service_accounts
        ]
        if not matched:
            continue
        role_ref = binding.get("roleRef") or {}
        if role_ref.get("kind") != "ClusterRole":
            continue
        role_name = role_ref.get("name", "")
        role = roles.get(role_name)
        if role is None:
            continue
        for rule in role.get("rules") or []:
            if _rule_grants_unrestricted_secrets(rule):
                subject_str = ", ".join(f"{ns}/{name}" for ns, name in sorted(matched))
                binding_name = (binding.get("metadata") or {}).get("name", "")
                violations.append(
                    f"ClusterRoleBinding {binding_name!r} → ClusterRole {role_name!r} (subjects: {subject_str})"
                )
                break
    return violations


def _rule_grants_unrestricted_secrets(rule: dict[str, Any]) -> bool:
    """Return True when an RBAC rule covers the ``secrets`` resource without resourceNames."""
    resources = {str(r) for r in (rule.get("resources") or [])}
    if "secrets" not in resources and "*" not in resources:
        return False
    api_groups = {str(g) for g in (rule.get("apiGroups") or [])}
    if api_groups and not (api_groups & {"", "*"}):
        return False
    verbs = {str(v) for v in (rule.get("verbs") or [])}
    if not (verbs & _SECRET_VERBS):
        return False
    resource_names = rule.get("resourceNames") or []
    if resource_names:
        return False
    return True


def _check_node_plugin_volumes(
    pods_by_ns: dict[str, list[dict[str, Any]]],
) -> list[str] | str:
    """Return a list of node-plugin volume-topology violations.

    Returns the sentinel ``"no-node-pods"`` when no node plugin pods are
    found at all (so the caller can surface a skipped subtest rather than
    a false pass).
    """
    violations: list[str] = []
    node_pod_seen = False
    for namespace, pods in pods_by_ns.items():
        for pod in pods:
            if not _pod_is_node_plugin(pod):
                continue
            node_pod_seen = True
            spec = pod.get("spec") or {}
            pod_name = (pod.get("metadata") or {}).get("name", "")
            for volume in spec.get("volumes") or []:
                volume_name = volume.get("name", "")
                keys = {k for k in volume.keys() if k != "name"}
                bad_keys = keys - _NODE_PLUGIN_ALLOWED_VOLUME_KEYS
                if bad_keys:
                    violations.append(f"{namespace}/{pod_name} volume {volume_name!r} uses {sorted(bad_keys)}")
    if not node_pod_seen:
        return "no-node-pods"
    return violations


class K8sCsiProvisioningModesCheck(BaseValidation):
    """Verify CSI supports both dynamic and static provisioning.

    Three subtests run against a single ephemeral namespace:

    * ``dynamic`` - a PVC against ``dynamic_storage_class`` reaches ``Bound``,
      the backing PV exposes ``spec.csi.driver``, and a BusyBox pod mounts
      the PVC and round-trips a canary file (write then read back).
    * ``capacity`` - the bound PV's ``spec.capacity.storage`` is at least the
      PVC request, i.e. ``pv.capacity >= pvc.requests.storage``.
    * ``static`` - a PersistentVolume with the configured ``volume_handle``
      + ``csi_driver`` is pre-created, a PVC with ``storageClassName: ""``
      and a matching ``volumeName`` pre-binds to that PV, and a BusyBox pod
      round-trips a canary file. Skipped when ``static_pv.volume_handle`` or
      ``static_pv.csi_driver`` is unset, so the check is safe to enable on
      providers that do not supply an out-of-band volume.

    Pass requires ``dynamic`` and ``capacity`` to pass; ``static`` is reported
    as skipped (not failed) when its inputs are unset. When
    ``dynamic_storage_class`` itself is unset the whole check is skipped.

    Config keys (with defaults):
        dynamic_storage_class: StorageClass for the dynamic probe; defaults
            to :func:`get_k8s_csi_block_storage_class`. When unset the whole
            check is skipped.
        dynamic_pvc_size: PVC request size for the dynamic probe, also the
            floor the ``capacity`` subtest checks the bound PV against
            (default: ``1Gi``).
        static_pv.volume_handle: CSI ``volumeHandle`` of the pre-provisioned
            backing volume. Unset disables the static subtest.
        static_pv.csi_driver: CSI driver name matching the backing volume
            (e.g. ``ebs.csi.aws.com``). Unset disables the static subtest.
        static_pv.fs_type: Filesystem type written to ``spec.csi.fsType``
            (default: ``ext4``).
        static_pv.capacity: PV ``spec.capacity.storage`` and the matching
            PVC request size (default: ``1Gi``).
        static_pv.access_mode: PV / PVC access mode (default: ``ReadWriteOnce``).
        static_pv.zone: ``topology.kubernetes.io/zone`` the backing volume
            lives in. Set for zonal block backends (AWS EBS, GCE PD, Azure
            Disk) so the consumer pod is scheduled in the volume's zone and
            the attach does not hang cross-zone. Unset for zone-agnostic
            backends (e.g. EFS).
        bind_timeout_s: Max wait for PVC Bind and mount-pod Ready
            (default: 180).
        namespace_prefix: Prefix for the ephemeral namespace
            (default: ``isvtest-csi-prov``).
        timeout: Overall class-level timeout for each ``run_command``
            (default: 600).
    """

    description: ClassVar[str] = "Verify CSI supports dynamic and static provisioning."
    timeout: ClassVar[int] = 600

    def run(self) -> None:
        """Drive the dynamic + static provisioning subtests against an ephemeral namespace."""
        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()

        dynamic_sc = str(self.config.get("dynamic_storage_class") or get_k8s_csi_block_storage_class() or "")
        static_pv_cfg = dict(self.config.get("static_pv") or {})
        bind_timeout = int(self.config.get("bind_timeout_s", 180))
        dynamic_pvc_size = str(self.config.get("dynamic_pvc_size") or "1Gi")
        ns_prefix = self.config.get("namespace_prefix", "isvtest-csi-prov")

        if not dynamic_sc:
            self.set_passed("Skipped: no dynamic_storage_class configured")
            return

        self._namespace = f"{ns_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)
        ns_created = False
        static_pv_name = ""
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            any_failed = False
            dyn_summary = self._run_dynamic(dynamic_sc, bind_timeout, dynamic_pvc_size)
            if dyn_summary is None:
                any_failed = True

            static_volume_handle = str(static_pv_cfg.get("volume_handle") or "")
            static_driver = str(static_pv_cfg.get("csi_driver") or "")
            if not static_volume_handle or not static_driver:
                self.report_subtest(
                    "static",
                    passed=True,
                    message="static_pv.volume_handle or static_pv.csi_driver unset; static probe skipped",
                    skipped=True,
                )
            else:
                static_pv_name = f"csi-prov-static-{uuid.uuid4().hex[:6]}"
                static_ok = self._run_static(
                    pv_name=static_pv_name,
                    driver=static_driver,
                    volume_handle=static_volume_handle,
                    fs_type=str(static_pv_cfg.get("fs_type") or "ext4"),
                    capacity=str(static_pv_cfg.get("capacity") or "1Gi"),
                    access_mode=str(static_pv_cfg.get("access_mode") or "ReadWriteOnce"),
                    bind_timeout=bind_timeout,
                    zone=str(static_pv_cfg.get("zone") or ""),
                )
                if not static_ok:
                    any_failed = True

            if any_failed:
                self.set_failed("One or more CSI provisioning-mode subtests failed; see subtest details")
            else:
                self.set_passed(
                    f"CSI dynamic provisioning verified against StorageClass {dynamic_sc!r}"
                    + (
                        f"; static provisioning verified against driver {static_driver!r}"
                        if static_pv_name
                        else "; static provisioning skipped"
                    )
                )
        finally:
            # Cluster-scoped PV must be cleaned up separately from the
            # namespace: the PVC is gone with the namespace, but the PV
            # keeps a Released status (reclaimPolicy=Retain) because the
            # underlying volume is managed out-of-band by the provider.
            if static_pv_name:
                pv_cleanup = self.run_command(
                    f"{self._kubectl_base} delete pv {shlex.quote(static_pv_name)} --wait=false --ignore-not-found=true"
                )
                if pv_cleanup.exit_code != 0:
                    self.log.warning("PV cleanup failed for %s: %s", static_pv_name, pv_cleanup.stderr)
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _run_dynamic(self, storage_class: str, bind_timeout: int, requested_size: str) -> str | None:
        """Run the ``dynamic`` subtest: PVC + consumer pod → Ready → Bound → PV has csi.driver → canary.

        The mount pod is applied before waiting for ``Bound`` so this works
        under ``volumeBindingMode: WaitForFirstConsumer``, where the PVC
        stays ``Pending`` until a consumer is scheduled. Returns a short
        summary string on pass, or ``None`` on failure; the subtest itself
        is always reported before returning.
        """
        pvc_name = f"csi-prov-dyn-{uuid.uuid4().hex[:6]}"
        pod_name = f"csi-prov-dyn-{uuid.uuid4().hex[:6]}"

        returncode, stderr = self._apply_pvc(pvc_name, storage_class, "ReadWriteOnce", requested_size)
        if returncode != 0:
            self.report_subtest(
                "dynamic",
                passed=False,
                message=f"kubectl apply failed for dynamic PVC {pvc_name!r}: {stderr.strip()}",
            )
            return None

        pod_rc, pod_err = _apply_mount_pod_manifest(
            self._kubectl_parts, self._namespace, pod_name, pvc_name, self.timeout
        )
        if pod_rc != 0:
            self.report_subtest(
                "dynamic",
                passed=False,
                message=f"kubectl apply failed for mount pod {pod_name!r}: {pod_err.strip()[:200]}",
            )
            return None

        pod_ready, wait_err = _wait_pod_ready(
            self.run_command, self._kubectl_base, self._namespace, pod_name, bind_timeout
        )
        if not pod_ready:
            _log_storage_diagnostics(
                self,
                pod_name=pod_name,
                pvc_name=pvc_name,
            )
            self.report_subtest(
                "dynamic",
                passed=False,
                message=(
                    f"Mount pod {pod_name!r} for PVC {pvc_name!r} did not become Ready "
                    f"within {bind_timeout}s: {wait_err[:200]}"
                ),
            )
            return None

        if not self._wait_pvc_bound(pvc_name, 5):
            _log_storage_diagnostics(
                self,
                pod_name=pod_name,
                pvc_name=pvc_name,
            )
            self.report_subtest(
                "dynamic",
                passed=False,
                message=f"Dynamic PVC {pvc_name!r} did not reach Bound even after consumer pod became Ready",
            )
            return None

        pv_name, csi_driver, pv_capacity, err = self._resolve_bound_pv_driver(pvc_name)
        if err:
            self.report_subtest("dynamic", passed=False, message=err)
            return None
        if not csi_driver:
            self.report_subtest(
                "dynamic",
                passed=False,
                message=f"Dynamic PV {pv_name!r} missing spec.csi.driver",
            )
            return None

        capacity_ok = _capacity_meets_request(pv_capacity, requested_size)
        self.report_subtest(
            "capacity",
            passed=capacity_ok,
            message=(
                f"PV {pv_name!r} capacity {pv_capacity!r} >= requested {requested_size!r}"
                if capacity_ok
                else (
                    f"PV {pv_name!r} capacity {pv_capacity or '<empty>'!r} is less than "
                    f"requested {requested_size!r} (or could not be parsed)"
                )
            ),
        )

        if not self._canary_write_read(pod_name):
            self.report_subtest(
                "dynamic",
                passed=False,
                message=(
                    f"Dynamic PVC {pvc_name!r} bound to PV {pv_name!r} (driver={csi_driver!r}) "
                    "but canary write/read failed"
                ),
            )
            return None

        summary = f"Dynamic PVC {pvc_name!r} bound to PV {pv_name!r} (driver={csi_driver!r}); canary roundtrip OK"
        self.report_subtest("dynamic", passed=True, message=summary)
        return summary if capacity_ok else None

    def _run_static(
        self,
        *,
        pv_name: str,
        driver: str,
        volume_handle: str,
        fs_type: str,
        capacity: str,
        access_mode: str,
        bind_timeout: int,
        zone: str = "",
    ) -> bool:
        """Run the ``static`` subtest: pre-create PV + PVC → Bound → mount + canary."""
        pvc_name = f"csi-prov-static-{uuid.uuid4().hex[:6]}"
        pod_name = f"csi-prov-static-{uuid.uuid4().hex[:6]}"

        returncode, stderr = self._apply_pv(
            pv_name=pv_name,
            driver=driver,
            volume_handle=volume_handle,
            fs_type=fs_type,
            capacity=capacity,
            access_mode=access_mode,
            claim_name=pvc_name,
            zone=zone,
        )
        if returncode != 0:
            self.report_subtest(
                "static",
                passed=False,
                message=f"kubectl apply failed for PV {pv_name!r}: {stderr.strip()}",
            )
            return False

        returncode, stderr = self._apply_static_pvc(pvc_name, pv_name, access_mode, capacity)
        if returncode != 0:
            self.report_subtest(
                "static",
                passed=False,
                message=f"kubectl apply failed for static PVC {pvc_name!r}: {stderr.strip()}",
            )
            return False

        if not self._wait_pvc_bound(pvc_name, bind_timeout):
            self.report_subtest(
                "static",
                passed=False,
                message=(
                    f"Static PVC {pvc_name!r} did not bind to PV {pv_name!r} (volume_handle={volume_handle!r}) "
                    f"within {bind_timeout}s"
                ),
            )
            return False

        if not self._canary_roundtrip(pvc_name, pod_name, bind_timeout):
            self.report_subtest(
                "static",
                passed=False,
                message=(
                    f"Static PV {pv_name!r} (volume_handle={volume_handle!r}, driver={driver!r}) "
                    "bound but mount + canary roundtrip failed"
                ),
            )
            return False

        self.report_subtest(
            "static",
            passed=True,
            message=(
                f"Static PV {pv_name!r} (driver={driver!r}, volume_handle={volume_handle!r}) "
                f"bound via PVC {pvc_name!r}; canary roundtrip OK"
            ),
        )
        return True

    def _apply_pvc(self, name: str, storage_class: str, access_mode: str, size: str) -> tuple[int, str]:
        """Render the PVC manifest for dynamic provisioning and apply it."""
        return _apply_pvc_manifest(
            self._kubectl_parts, self._namespace, name, storage_class, access_mode, size, self.timeout
        )

    def _apply_static_pvc(self, name: str, pv_name: str, access_mode: str, size: str) -> tuple[int, str]:
        """Render a PVC that pre-binds to the given PV via ``volumeName`` + empty StorageClass."""

        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            _set_pvc_fields(doc, namespace=self._namespace, name=name, sc="", mode=access_mode, size=size)
            doc.setdefault("spec", {})["volumeName"] = pv_name
            return doc

        return self._run_kubectl_apply(render_k8s_manifest(_PVC_MANIFEST, _mutate))

    def _apply_pv(
        self,
        *,
        pv_name: str,
        driver: str,
        volume_handle: str,
        fs_type: str,
        capacity: str,
        access_mode: str,
        claim_name: str,
        zone: str = "",
    ) -> tuple[int, str]:
        """Render the static PV manifest and apply it."""

        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            return _set_pv_fields(
                doc,
                name=pv_name,
                driver=driver,
                volume_handle=volume_handle,
                fs_type=fs_type,
                capacity=capacity,
                access_mode=access_mode,
                claim_namespace=self._namespace,
                claim_name=claim_name,
                zone=zone,
            )

        return self._run_kubectl_apply(render_k8s_manifest(_PV_MANIFEST, _mutate))

    def _apply_mount_pod(self, pod_name: str, pvc_name: str) -> tuple[int, str]:
        """Render the BusyBox mount pod and apply it."""

        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            return _set_fs_pod_fields(doc, namespace=self._namespace, name=pod_name, pvc_name=pvc_name)

        return self._run_kubectl_apply(render_k8s_manifest(_MOUNT_POD_MANIFEST, _mutate))

    def _run_kubectl_apply(self, manifest: str) -> tuple[int, str]:
        return _apply_manifest(self._kubectl_parts, manifest, self.timeout)

    def _wait_pvc_bound(self, pvc_name: str, timeout_s: int) -> bool:
        return _poll_pvc_bound(self.run_command, self._kubectl_base, self._namespace, pvc_name, timeout_s)

    def _resolve_bound_pv_driver(self, pvc_name: str) -> tuple[str, str, str, str]:
        """Return ``(pv_name, csi_driver, capacity, error)`` - ``error`` is empty on success."""
        pvc_payload, error = _get_pvc_json(self.run_command, self._kubectl_base, self._namespace, pvc_name)
        if pvc_payload is None:
            return "", "", "", f"Failed to resolve volumeName for PVC {pvc_name!r}: {error}"
        pv_name = str((pvc_payload.get("spec") or {}).get("volumeName") or "")
        if not pv_name:
            return "", "", "", f"PVC {pvc_name!r} bound but spec.volumeName is empty"

        pv_json = self.run_command(f"{self._kubectl_base} get pv {shlex.quote(pv_name)} -o json")
        if pv_json.exit_code != 0:
            return pv_name, "", "", f"kubectl get pv {pv_name!r} failed: {pv_json.stderr.strip()}"
        try:
            payload = json.loads(pv_json.stdout)
        except json.JSONDecodeError as exc:
            return pv_name, "", "", f"Failed to parse PV {pv_name!r} JSON: {exc}"
        spec = payload.get("spec") or {}
        driver = (spec.get("csi") or {}).get("driver")
        capacity = (spec.get("capacity") or {}).get("storage")
        return pv_name, str(driver) if driver else "", str(capacity) if capacity else "", ""

    def _canary_roundtrip(self, pvc_name: str, pod_name: str, timeout_s: int) -> bool:
        """Apply a BusyBox mount pod for ``pvc_name``, wait for Ready, and run the canary round-trip."""
        returncode, stderr = self._apply_mount_pod(pod_name, pvc_name)
        if returncode != 0:
            self.log.error("kubectl apply failed for mount pod %s: %s", pod_name, stderr.strip())
            return False

        ready, err = _wait_pod_ready(self.run_command, self._kubectl_base, self._namespace, pod_name, timeout_s)
        if not ready:
            _log_storage_diagnostics(
                self,
                pod_name=pod_name,
                pvc_name=pvc_name,
            )
            self.log.error("Mount pod %s did not become Ready: %s", pod_name, err)
            return False

        return self._canary_write_read(pod_name)

    def _canary_write_read(self, pod_name: str) -> bool:
        """Write a canary file to ``/data`` inside ``pod_name`` and read it back verbatim."""
        canary = f"csi-prov-{uuid.uuid4().hex[:16]}"
        write_inner = f"echo {shlex.quote(canary)} > /data/canary.txt"
        write_cmd = (
            f"{self._kubectl_base} exec -n {shlex.quote(self._namespace)} "
            f"{shlex.quote(pod_name)} -- sh -c {shlex.quote(write_inner)}"
        )
        write_result = self.run_command(write_cmd)
        if write_result.exit_code != 0:
            self.log.error(
                "Canary write failed in pod %s: %s",
                pod_name,
                write_result.stderr.strip() or write_result.stdout.strip(),
            )
            return False

        read_cmd = (
            f"{self._kubectl_base} exec -n {shlex.quote(self._namespace)} "
            f"{shlex.quote(pod_name)} -- sh -c {shlex.quote('cat /data/canary.txt')}"
        )
        read_result = self.run_command(read_cmd)
        if read_result.exit_code != 0:
            self.log.error(
                "Canary read failed in pod %s: %s",
                pod_name,
                read_result.stderr.strip() or read_result.stdout.strip(),
            )
            return False
        if read_result.stdout.strip() != canary:
            self.log.error(
                "Canary mismatch in pod %s: expected %r, got %r",
                pod_name,
                canary,
                read_result.stdout.strip(),
            )
            return False
        return True


def _set_pv_fields(
    doc: dict[str, Any],
    *,
    name: str,
    driver: str,
    volume_handle: str,
    fs_type: str,
    capacity: str,
    access_mode: str,
    claim_namespace: str,
    claim_name: str,
    zone: str = "",
) -> dict[str, Any]:
    """Mutate a parsed PersistentVolume manifest in place with the requested fields.

    ``claimRef`` pre-reserves the PV for the matching PVC so the binding is
    deterministic and cannot race against another claim landing in the same
    cluster while the static probe runs.

    When ``zone`` is set, a ``spec.nodeAffinity`` on
    ``topology.kubernetes.io/zone`` pins the volume to that zone. Zonal block
    backends (AWS EBS, GCE PD, Azure Disk) can only attach to a node in the
    volume's own zone; without this the scheduler may place the consumer pod
    in a different zone and the attach hangs until the mount timeout.
    """
    metadata = doc.setdefault("metadata", {})
    metadata["name"] = name

    spec = doc.setdefault("spec", {})
    spec["capacity"] = {"storage": capacity}
    spec["accessModes"] = [access_mode]
    spec["persistentVolumeReclaimPolicy"] = "Retain"
    spec["storageClassName"] = ""
    spec["csi"] = {
        "driver": driver,
        "volumeHandle": volume_handle,
        "fsType": fs_type,
    }
    spec["claimRef"] = {
        "namespace": claim_namespace,
        "name": claim_name,
    }
    if zone:
        spec["nodeAffinity"] = {
            "required": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "topology.kubernetes.io/zone",
                                "operator": "In",
                                "values": [zone],
                            }
                        ]
                    }
                ]
            }
        }
    else:
        spec.pop("nodeAffinity", None)
    return doc


class K8sCsiConcurrentPvcCheck(BaseValidation):
    """Verify the CSI driver can provision multiple PVCs in parallel against the same StorageClass.

    Two subtests run against a single ephemeral namespace:

    * ``pvc-bound[0]`` … ``pvc-bound[N-1]`` - each PVC reaches ``Bound``
      within ``bind_timeout_s``. Consumer pods are deployed alongside the
      PVCs so this works for StorageClasses with
      ``volumeBindingMode: WaitForFirstConsumer``.
    * ``distinct-pvs`` - every bound PVC resolves to a different
      ``spec.volumeName``. Fails if any PVC is unbound or two PVCs share
      a backing PV.

    Pass requires all ``pvc-bound`` subtests and ``distinct-pvs`` to pass.
    When ``storage_class`` is unset the whole check is skipped.

    Config keys (with defaults):
        storage_class: StorageClass to probe; defaults to
            :func:`get_k8s_csi_block_storage_class` (env: ``K8S_CSI_BLOCK_SC``).
            Whole check skipped when unset.
        pvc_count: Number of PVCs to create (default: ``2``).
        pvc_size: Capacity per PVC (default: ``1Gi``).
        bind_timeout_s: Per-pod / per-PVC bind wait (default: ``120``).
        namespace_prefix: Prefix for the ephemeral namespace
            (default: ``isvtest-csi-concurrent``).
        timeout: Overall class-level timeout for each ``run_command``
            (default: 300).
    """

    description: ClassVar[str] = "Verify CSI provisions multiple concurrent PVCs backed by distinct PVs."
    timeout: ClassVar[int] = 300
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        """Create N PVCs + consumer pods concurrently, assert all bind to distinct PVs."""
        storage_class = str(self.config.get("storage_class") or get_k8s_csi_block_storage_class() or "")
        if not storage_class:
            self.set_passed("Skipped: no storage_class configured")
            return

        pvc_count = int(self.config.get("pvc_count", 2))
        if pvc_count < 2:
            self.set_failed("Invalid config: pvc_count must be >= 2 for concurrent provisioning validation")
            return
        pvc_size = str(self.config.get("pvc_size", "1Gi"))
        bind_timeout = int(self.config.get("bind_timeout_s", 120))
        ns_prefix = self.config.get("namespace_prefix", "isvtest-csi-concurrent")

        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()
        self._namespace = f"{ns_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)

        pvc_names = [f"csi-conc-{i}-{uuid.uuid4().hex[:6]}" for i in range(pvc_count)]
        pod_names = [f"csi-conc-{i}-{uuid.uuid4().hex[:6]}" for i in range(pvc_count)]

        ns_created = False
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            # Apply PVC manifests then wait for all to be Bound.
            apply_errors: list[str] = []
            for pvc_name in pvc_names:
                rc, err = _apply_pvc_manifest(
                    self._kubectl_parts,
                    self._namespace,
                    pvc_name,
                    storage_class,
                    "ReadWriteOnce",
                    pvc_size,
                    self.timeout,
                )
                if rc != 0:
                    apply_errors.append(f"{pvc_name}: {err.strip()[:150]}")

            if apply_errors:
                self.set_failed(f"kubectl apply failed for PVC(s): {'; '.join(apply_errors)}")
                return

            # Submit all consumer pods before waiting on any.
            pod_apply_errors: list[str] = []
            for pvc_name, pod_name in zip(pvc_names, pod_names):
                rc, err = _apply_mount_pod_manifest(
                    self._kubectl_parts, self._namespace, pod_name, pvc_name, self.timeout
                )
                if rc != 0:
                    pod_apply_errors.append(f"{pod_name}: {err.strip()[:150]}")

            if pod_apply_errors:
                self.set_failed(f"kubectl apply failed for consumer pod(s): {'; '.join(pod_apply_errors)}")
                return

            # Wait for each pod to become Ready, then confirm PVC is Bound.
            any_failed = False
            for i, (pvc_name, pod_name) in enumerate(zip(pvc_names, pod_names)):
                subtest = f"pvc-bound[{i}]"
                pod_ready, wait_err = _wait_pod_ready(
                    self.run_command, self._kubectl_base, self._namespace, pod_name, bind_timeout
                )
                bound = pod_ready and _poll_pvc_bound(
                    self.run_command, self._kubectl_base, self._namespace, pvc_name, 5
                )
                self.report_subtest(
                    subtest,
                    passed=bound,
                    message=(
                        f"PVC {pvc_name!r} bound against {storage_class!r} via consumer pod {pod_name!r}"
                        if bound
                        else (
                            f"PVC {pvc_name!r} did not reach Bound via consumer pod {pod_name!r} "
                            f"within {bind_timeout}s ({storage_class!r}): {wait_err[:200]}"
                        )
                    ),
                )
                if not bound:
                    _log_storage_diagnostics(
                        self,
                        pod_name=pod_name,
                        pvc_name=pvc_name,
                    )
                    any_failed = True

            # Collect PV names from all bound PVCs and assert they are distinct.
            pv_names: list[str] = []
            for pvc_name in pvc_names:
                payload, _err = _get_pvc_json(self.run_command, self._kubectl_base, self._namespace, pvc_name)
                if payload is not None:
                    pv_name = str((payload.get("spec") or {}).get("volumeName") or "")
                    if pv_name:
                        pv_names.append(pv_name)

            if len(pv_names) == pvc_count and len(set(pv_names)) == pvc_count:
                self.report_subtest(
                    "distinct-pvs",
                    passed=True,
                    message=f"All {pvc_count} PVCs backed by distinct PVs: {', '.join(sorted(pv_names))}",
                )
            else:
                any_failed = True
                if len(pv_names) < pvc_count:
                    self.report_subtest(
                        "distinct-pvs",
                        passed=False,
                        message=(
                            f"Only {len(pv_names)}/{pvc_count} PVCs resolved a volumeName "
                            f"(some PVCs may not have reached Bound)"
                        ),
                    )
                else:
                    duplicates = [pv for pv in pv_names if pv_names.count(pv) > 1]
                    self.report_subtest(
                        "distinct-pvs",
                        passed=False,
                        message=f"PVCs share backing PV(s): {', '.join(sorted(set(duplicates)))}",
                    )

            if any_failed:
                self.set_failed("One or more concurrent-PVC subtests failed; see subtest details")
            else:
                self.set_passed(f"{pvc_count} concurrent PVCs provisioned with distinct PVs against {storage_class!r}")
        finally:
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)


class K8sCsiPvcExpandCheck(BaseValidation):
    """Verify the CSI driver can expand a bound PVC and the mount reflects the new capacity.

    The PVC/PV capacity subtests pass when capacity reaches >= the
    requested ``expanded_size`` (not an exact match, as a CSI driver may
    provision more than requested due to rounding up to the next valid storage increment).

    The test is skipped when the StorageClass does not set allowVolumeExpansion=true
    or when ``storage_class`` is unset.

    Config keys (with defaults):
        storage_class: StorageClass to probe; defaults to
            :func:`get_k8s_csi_block_storage_class` (env: ``K8S_CSI_BLOCK_SC``).
        initial_size: Starting PVC capacity (default: ``1Gi``).
        expanded_size: Target capacity after resize (default: ``2Gi``).
        bind_timeout_s: Wait for initial PVC Bind + pod Ready (default: ``120``).
        expand_timeout_s: Wait for PVC / PV capacity to reflect the new
            size (default: ``180``).
        namespace_prefix: Prefix for the ephemeral namespace
            (default: ``isvtest-csi-expand``).
        timeout: Overall class-level timeout for each ``run_command``
            (default: 600).
    """

    description: ClassVar[str] = "Verify CSI PVC expansion updates PV capacity and the mounted filesystem size."
    timeout: ClassVar[int] = 600
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        """Provision a PVC, resize it, and assert PV + df reflect the new capacity."""
        storage_class = str(self.config.get("storage_class") or get_k8s_csi_block_storage_class() or "")
        if not storage_class:
            self.set_passed("Skipped: no storage_class configured")
            return

        initial_size = str(self.config.get("initial_size", "1Gi"))
        expanded_size = str(self.config.get("expanded_size", "2Gi"))
        bind_timeout = int(self.config.get("bind_timeout_s", 120))
        expand_timeout = int(self.config.get("expand_timeout_s", 180))
        ns_prefix = self.config.get("namespace_prefix", "isvtest-csi-expand")

        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()

        # Check whether the StorageClass advertises allowVolumeExpansion.
        sc_result = self.run_command(f"{self._kubectl_base} get storageclass {shlex.quote(storage_class)} -o json")
        if sc_result.exit_code != 0:
            self.set_failed(
                f"Could not fetch StorageClass {storage_class!r}: {sc_result.stderr.strip() or sc_result.stdout.strip()}"
            )
            return
        try:
            sc_payload = json.loads(sc_result.stdout)
        except json.JSONDecodeError as exc:
            self.set_failed(f"Failed to parse StorageClass {storage_class!r} JSON: {exc}")
            return

        allows_expansion = bool(sc_payload.get("allowVolumeExpansion"))
        if not allows_expansion:
            skip_msg = f"StorageClass {storage_class!r} does not set allowVolumeExpansion=true"
            for name in ("sc-allows-expansion", "pvc-patch-accepted", "pv-capacity-updated", "df-shows-new-size"):
                self.report_subtest(name, passed=True, message=skip_msg, skipped=True)
            self.set_passed(f"Skipped: {skip_msg}")
            return

        self.report_subtest(
            "sc-allows-expansion",
            passed=True,
            message=f"StorageClass {storage_class!r} has allowVolumeExpansion=true",
        )

        self._namespace = f"{ns_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)

        pvc_name = f"csi-expand-{uuid.uuid4().hex[:6]}"
        pod_name = f"csi-expand-{uuid.uuid4().hex[:6]}"

        ns_created = False
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            rc, err = _apply_pvc_manifest(
                self._kubectl_parts,
                self._namespace,
                pvc_name,
                storage_class,
                "ReadWriteOnce",
                initial_size,
                self.timeout,
            )
            if rc != 0:
                self.set_failed(f"kubectl apply failed for PVC {pvc_name!r}: {err.strip()}")
                return

            rc, err = _apply_mount_pod_manifest(self._kubectl_parts, self._namespace, pod_name, pvc_name, self.timeout)
            if rc != 0:
                self.set_failed(f"kubectl apply failed for consumer pod {pod_name!r}: {err.strip()[:200]}")
                return

            pod_ready, wait_err = _wait_pod_ready(
                self.run_command, self._kubectl_base, self._namespace, pod_name, bind_timeout
            )
            if not pod_ready or not _poll_pvc_bound(self.run_command, self._kubectl_base, self._namespace, pvc_name, 5):
                _log_storage_diagnostics(
                    self,
                    pod_name=pod_name,
                    pvc_name=pvc_name,
                )
                self.set_failed(
                    f"PVC {pvc_name!r} did not reach Bound before resize within {bind_timeout}s: {wait_err[:200]}"
                )
                return

            any_failed = False

            # Patch the PVC to the larger size.
            patch_json = json.dumps({"spec": {"resources": {"requests": {"storage": expanded_size}}}})
            patch_cmd = (
                f"{self._kubectl_base} patch pvc {shlex.quote(pvc_name)} "
                f"-n {shlex.quote(self._namespace)} "
                f"--type=merge -p {shlex.quote(patch_json)}"
            )
            patch_result = self.run_command(patch_cmd)
            if patch_result.exit_code != 0:
                self.report_subtest(
                    "pvc-patch-accepted",
                    passed=False,
                    message=(
                        f"kubectl patch failed for PVC {pvc_name!r}: "
                        f"{patch_result.stderr.strip() or patch_result.stdout.strip()}"
                    ),
                )
                for name in ("pv-capacity-updated", "df-shows-new-size"):
                    self.report_subtest(
                        name, passed=True, message="pvc-patch-accepted failed; probe skipped", skipped=True
                    )
                self.set_failed("PVC patch rejected; downstream expansion probes skipped")
                return

            self.report_subtest(
                "pvc-patch-accepted",
                passed=True,
                message=f"PVC {pvc_name!r} patched to {expanded_size!r}",
            )

            # Poll until PVC status.capacity.storage reflects the new size.
            pv_name = self._poll_pvc_capacity_updated(pvc_name, expanded_size, expand_timeout)
            if pv_name is None:
                any_failed = True
                self.report_subtest(
                    "pv-capacity-updated",
                    passed=False,
                    message=(
                        f"PVC {pvc_name!r} status.capacity.storage did not reach >= {expanded_size!r} "
                        f"within {expand_timeout}s"
                    ),
                )
                self.report_subtest(
                    "df-shows-new-size",
                    passed=True,
                    message="pv-capacity-updated failed; df probe skipped",
                    skipped=True,
                )
            else:
                # Also check the PV's spec.capacity.storage.
                pv_ok = self._check_pv_capacity(pv_name, expanded_size)
                self.report_subtest(
                    "pv-capacity-updated",
                    passed=pv_ok,
                    message=(
                        f"PV {pv_name!r} spec.capacity.storage is >= {expanded_size!r}"
                        if pv_ok
                        else f"PV {pv_name!r} spec.capacity.storage did not reach >= {expanded_size!r}"
                    ),
                )
                if not pv_ok:
                    any_failed = True

                df_ok = self._poll_df_size(pod_name, expanded_size, expand_timeout)
                self.report_subtest(
                    "df-shows-new-size",
                    passed=df_ok,
                    message=(
                        f"df /data inside pod {pod_name!r} reflects ≥90% of {expanded_size!r}"
                        if df_ok
                        else (
                            f"df /data inside pod {pod_name!r} did not reflect ≥90% of {expanded_size!r} "
                            f"within {expand_timeout}s"
                        )
                    ),
                )
                if not df_ok:
                    any_failed = True

            if any_failed:
                self.set_failed("One or more PVC-expand subtests failed; see subtest details")
            else:
                self.set_passed(
                    f"PVC {pvc_name!r} expanded from {initial_size!r} to {expanded_size!r}; "
                    f"PV and mount reflect new capacity"
                )
        finally:
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _poll_pvc_capacity_updated(self, pvc_name: str, expected_size: str, timeout_s: int) -> str | None:
        """Poll until ``PVC.status.capacity.storage`` is at least ``expected_size``.

        Returns the PVC's ``spec.volumeName`` (used to check the PV) on
        success, or ``None`` on timeout / parse failure.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            payload, _err = _get_pvc_json(self.run_command, self._kubectl_base, self._namespace, pvc_name)
            if payload is not None:
                capacity = str(((payload.get("status") or {}).get("capacity") or {}).get("storage") or "")
                if capacity and _capacity_meets_request(capacity, expected_size):
                    return str((payload.get("spec") or {}).get("volumeName") or "")
            time.sleep(3.0)
        return None

    def _check_pv_capacity(self, pv_name: str, expected_size: str) -> bool:
        """Return True when the PV's ``spec.capacity.storage`` is at least ``expected_size``."""
        if not pv_name:
            return False
        result = self.run_command(f"{self._kubectl_base} get pv {shlex.quote(pv_name)} -o json")
        if result.exit_code != 0:
            self.log.warning("kubectl get pv %s failed: %s", pv_name, result.stderr.strip())
            return False
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        capacity = str(((payload.get("spec") or {}).get("capacity") or {}).get("storage") or "")
        return bool(capacity) and _capacity_meets_request(capacity, expected_size)

    def _poll_df_size(self, pod_name: str, expected_size: str, timeout_s: int) -> bool:
        """Poll ``df -k /data`` inside ``pod_name`` until it reports ≥ 90 % of ``expected_size``.

        Retries every 5 s up to ``timeout_s``. Some CSI drivers (especially
        NFS/shared-FS) update the Kubernetes API objects before the
        mount-side quota or export size propagates to the node.
        """
        try:
            expected_bytes = int(parse_quantity(expected_size))
        except (ValueError, TypeError):
            self.log.warning("Could not parse expected_size %r as a Kubernetes quantity", expected_size)
            return False

        cmd = f"{self._kubectl_base} exec -n {shlex.quote(self._namespace)} {shlex.quote(pod_name)} -- df -k /data"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            result = self.run_command(cmd)
            if result.exit_code != 0:
                self.log.warning(
                    "df exec failed in pod %s: %s",
                    pod_name,
                    result.stderr.strip() or result.stdout.strip(),
                )
                time.sleep(5.0)
                continue
            kb_blocks = _parse_df_size_kb(result.stdout)
            if kb_blocks is None:
                self.log.warning("df output not parseable in pod %s:\n%s", pod_name, result.stdout.strip())
                time.sleep(5.0)
                continue
            actual_bytes = kb_blocks * 1024
            if actual_bytes >= int(expected_bytes * 0.90):
                return True
            self.log.info(
                "df /data in pod %s: %d KiB (%d bytes); waiting for ≥90%% of %s (%d bytes)\n%s",
                pod_name,
                kb_blocks,
                actual_bytes,
                expected_size,
                int(expected_bytes * 0.90),
                result.stdout.strip(),
            )
            time.sleep(5.0)
        return False


def _parse_df_size_kb(df_output: str) -> int | None:
    """Extract the total-size column (1K-blocks) from ``df -k`` output.

    Handles the two common output shapes:

    * Single data line (most filesystems)::

        Filesystem   1K-blocks  Used  Available  Use%  Mounted on
        /dev/sda1    2097152    512   2096640    0%    /data

    * Wrapped data line (long NFS/CIFS paths)::

        Filesystem
                     1K-blocks  Used  Available  Use%  Mounted on
        server:/very/long/path/name
                     2097152    512   2096640    0%    /data

    Returns the integer value of the first ``1K-blocks`` field found on a
    data line, or ``None`` when the output cannot be parsed.
    """
    lines = df_output.strip().splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("Filesystem"):
            continue
        parts = stripped.split()
        # A data line has ≥5 fields (filesystem, size, used, avail, use%, mount).
        # A wrapped-path continuation line has 5 fields (no leading filesystem token).
        if len(parts) >= 5:
            # If the first field looks numeric it's a wrapped continuation line
            # and parts[0] is the 1K-blocks value; otherwise parts[1] is.
            if parts[0].isdigit():
                try:
                    return int(parts[0])
                except ValueError:
                    continue
            if len(parts) >= 6:
                try:
                    return int(parts[1])
                except ValueError:
                    continue
    return None


def _apply_pvc_manifest(
    kubectl_parts: list[str],
    namespace: str,
    name: str,
    sc: str,
    mode: str,
    size: str,
    timeout: float,
) -> tuple[int, str]:
    """Render the PVC manifest and apply it; return ``(returncode, stderr-or-stdout)``."""

    def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
        return _set_pvc_fields(doc, namespace=namespace, name=name, sc=sc, mode=mode, size=size)

    return _apply_manifest(kubectl_parts, render_k8s_manifest(_PVC_MANIFEST, _mutate), timeout)


def _set_fs_pod_fields(
    doc: dict[str, Any],
    *,
    namespace: str,
    name: str,
    pvc_name: str,
    image: str = _DEFAULT_IMAGE,
    node_name: str | None = None,
    node_selector: dict[str, str] | None = None,
    tolerations: list[dict[str, Any]] | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Mutate a parsed probe-pod manifest in place, binding its single PVC volume.

    Sets the container ``image``, optionally pins the pod to ``node_name`` or
    restricts scheduling via ``node_selector`` (``spec.nodeSelector``), and
    overrides the container command. ``command`` must be a full argv list
    (e.g. ``["sh", "-c", "sleep 3600"]``). ``node_name`` and ``node_selector``
    may both be set (nodeName takes precedence in the scheduler, nodeSelector
    is still recorded). ``tolerations`` is appended to any existing
    ``spec.tolerations``. The CSI provisioning checks call this with only
    ``namespace``/``name``/``pvc_name`` (the manifest defaults cover image and
    command); the shared-filesystem checks additionally supply
    image/command/node placement.

    The pod is also kept off transient test-provisioning node pools (nodes
    carrying the ``isv.ncp.validation/pool`` marker). Those pools are created/
    scaled/deleted within the same run by node-pool CRUD checks, so a freshly
    joined node may not yet have the CSI node-plugin DaemonSet pod running - a
    probe pod landing there hangs at mount until the bind timeout. Baseline
    cluster nodes never carry the marker, so this is a no-op for providers that
    do not provision test pools (single-node k3s/minikube/microk8s included).
    """
    metadata = doc.setdefault("metadata", {})
    metadata["name"] = name
    metadata["namespace"] = namespace

    spec = doc.setdefault("spec", {})
    spec["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "isv.ncp.validation/pool",
                                "operator": "DoesNotExist",
                            }
                        ]
                    }
                ]
            }
        }
    }
    if node_name:
        spec["nodeName"] = node_name
    if node_selector:
        spec["nodeSelector"] = node_selector
    if tolerations:
        existing = spec.setdefault("tolerations", [])
        existing.extend(tolerations)
    # Force the kubelet to always apply fsGroup ownership to the mount point.
    # Without this, kubelets configured with FSGroupChangePolicy=OnRootMismatch
    # may skip the chown on a fresh volume whose root is owned by root:root,
    # leaving /data unwritable by the non-root container user.
    sc = spec.setdefault("securityContext", {})
    sc.setdefault("fsGroupChangePolicy", "Always")

    volumes = spec.setdefault("volumes", [])
    if not volumes:
        volumes.append({"name": "data"})
    volumes[0]["name"] = "data"
    volumes[0].pop("emptyDir", None)
    volumes[0]["persistentVolumeClaim"] = {"claimName": pvc_name}

    containers = spec.setdefault("containers", [])
    if not containers:
        containers.append({"name": "probe"})
    containers[0]["image"] = image
    if command is not None:
        containers[0]["command"] = command
    return doc


class K8sCsiDriverHealthCheck(BaseValidation):
    """Verify CSI drivers are installed and healthy for the configured StorageClasses.

    Resolves each StorageClass to its ``spec.provisioner`` and, per unique
    provisioner, checks the StorageClass exists, the ``CSIDriver`` is registered,
    and (when ``workloads`` are given) the controller Deployment and node
    DaemonSet are Ready.

    Config (``drivers`` is a list of specs; the whole check skips when no
    StorageClasses resolve)::

        drivers:
          - storage_classes: [<sc-name>, ...]
            workloads:                 # optional; omit to skip controller/node checks
              namespace: kube-system
              controller: {deployment: <name>}
              node: {daemonset: <name>}

    ``min_controller_replicas`` (default 1) sets the minimum Ready controller
    replicas.
    """

    description: ClassVar[str] = "Verify the CSI driver is installed and healthy."
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        """Resolve each driver spec's StorageClasses to provisioners and run health subtests."""
        sc_to_workloads = self._collect_driver_specs()
        if not sc_to_workloads:
            self.set_passed("Skipped: no storage_classes configured")
            return

        min_replicas = self._parse_positive_int("min_controller_replicas", default=1)
        if min_replicas is None:
            return
        kubectl_base = get_kubectl_base_shell()

        # Single kubectl call to resolve all SC names → provisioners.
        sc_result = self.run_command(f"{kubectl_base} get storageclass -o json")
        if sc_result.exit_code != 0:
            self.set_failed(f"kubectl get storageclass failed: {sc_result.stderr.strip()}")
            return
        all_sc_items = _load_items(sc_result.stdout)
        sc_to_provisioner: dict[str, str] = {
            (item.get("metadata") or {}).get("name", ""): str(item.get("provisioner") or "") for item in all_sc_items
        }

        any_failed = False
        provisioner_to_scs: dict[str, list[str]] = {}
        # Multiple StorageClasses can share a provisioner; track each distinct
        # workloads block to detect conflicting configs.
        provisioner_to_workloads: dict[str, list[dict[str, Any]]] = {}
        for sc_name, workloads in sc_to_workloads.items():
            provisioner = sc_to_provisioner.get(sc_name)
            if not provisioner:
                self.report_subtest(
                    f"storageclass-found[{sc_name}]",
                    passed=False,
                    message=(
                        f"StorageClass {sc_name!r} not found in the cluster "
                        f"({len(all_sc_items)} StorageClass(es) present)"
                    ),
                )
                any_failed = True
                continue
            provisioner_to_scs.setdefault(provisioner, []).append(sc_name)
            if workloads:
                distinct = provisioner_to_workloads.setdefault(provisioner, [])
                if workloads not in distinct:
                    distinct.append(workloads)

        for provisioner in sorted(provisioner_to_scs):
            if self._report_provisioner_health(
                kubectl_base,
                provisioner,
                provisioner_to_scs[provisioner],
                provisioner_to_workloads.get(provisioner) or [],
                min_replicas,
            ):
                any_failed = True

        if any_failed:
            self.set_failed("One or more CSI driver health subtests failed; see subtest details")
        else:
            self.set_passed(f"CSI driver health verified for: {', '.join(sorted(provisioner_to_scs))}")

    def _collect_driver_specs(self) -> dict[str, dict[str, Any]]:
        """Map each configured StorageClass name to its (optional) ``workloads``"""
        specs = self.config.get("drivers")
        sc_to_workloads: dict[str, dict[str, Any]] = {}
        for spec in specs if isinstance(specs, list) else []:
            if not isinstance(spec, dict):
                continue
            workloads = _as_dict(spec.get("workloads"))
            for raw_sc in spec.get("storage_classes") or []:
                sc = str(raw_sc).strip()
                if sc and sc not in sc_to_workloads:
                    sc_to_workloads[sc] = workloads
        return sc_to_workloads

    def _report_provisioner_health(
        self,
        kubectl_base: str,
        provisioner: str,
        storage_classes: list[str],
        workloads_list: list[dict[str, Any]],
        min_replicas: int,
    ) -> bool:
        """Report the registration + controller/node subtests for one provisioner.

        Returns ``True`` if any non-skipped subtest failed.
        """
        tag = f"[{provisioner}]"

        ok, msg = self._check_csidriver_registered(kubectl_base, provisioner)
        self.report_subtest(f"csidriver-registered{tag}", passed=ok, message=msg)
        failed = not ok

        if len(workloads_list) > 1:
            conflict_msg = (
                f"Conflicting `workloads` blocks for provisioner {provisioner!r} "
                f"across StorageClasses {storage_classes} ({len(workloads_list)} distinct); "
                "use a single workloads block per provisioner"
            )
            self.report_subtest(f"controller-deployment-healthy{tag}", passed=False, message=conflict_msg)
            self.report_subtest(f"node-daemonset-healthy{tag}", passed=False, message=conflict_msg)
            return True

        if not workloads_list:
            skip_msg = (
                f"No workloads configured for provisioner {provisioner!r}; add a "
                "`workloads` block (controller.deployment and node.daemonset) to its "
                "drivers entry to health-check the controller Deployment and node DaemonSet"
            )
            self.report_subtest(f"controller-deployment-healthy{tag}", passed=True, message=skip_msg, skipped=True)
            self.report_subtest(f"node-daemonset-healthy{tag}", passed=True, message=skip_msg, skipped=True)
            return failed

        workloads = workloads_list[0]
        namespace = str(workloads.get("namespace") or "kube-system")
        controller = _as_dict(workloads.get("controller"))
        node = _as_dict(workloads.get("node"))

        ok, msg = self._check_controller_deployment(
            kubectl_base, namespace, str(controller.get("deployment") or ""), min_replicas
        )
        self.report_subtest(f"controller-deployment-healthy{tag}", passed=ok, message=msg)
        failed = failed or not ok

        ok, msg = self._check_node_daemonset(kubectl_base, namespace, str(node.get("daemonset") or ""))
        self.report_subtest(f"node-daemonset-healthy{tag}", passed=ok, message=msg)
        return failed or not ok

    def _check_csidriver_registered(self, kubectl_base: str, provisioner: str) -> tuple[bool, str]:
        """Return ``(ok, message)`` for the ``csidriver-registered`` subtest."""
        cmd = f"{kubectl_base} get csidriver {shlex.quote(provisioner)} --ignore-not-found=true -o json"
        result = self.run_command(cmd)
        if result.exit_code != 0:
            return False, f"kubectl get csidriver {provisioner!r} failed: {result.stderr.strip()}"
        if not result.stdout.strip():
            return False, f"CSIDriver/{provisioner} is not registered in the cluster"
        try:
            payload = parse_kubectl_json(result, f"CSIDriver {provisioner!r}")
        except KubectlParseError as exc:
            return False, str(exc)
        name = (payload.get("metadata") or {}).get("name", "")
        return True, f"CSIDriver/{name} is registered"

    def _check_controller_deployment(
        self,
        kubectl_base: str,
        namespace: str,
        deployment_name: str,
        min_replicas: int,
    ) -> tuple[bool, str]:
        """Return ``(ok, message)`` for the ``controller-deployment-healthy`` subtest."""
        deployment, error = self._get_workload(
            kubectl_base, "deployment", namespace, deployment_name, "workloads.controller.deployment"
        )
        if error:
            return False, error

        meta = deployment.get("metadata") or {}
        spec = deployment.get("spec") or {}
        status = deployment.get("status") or {}
        name = meta.get("name", "")
        desired = int(spec.get("replicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        if desired < min_replicas:
            return False, (
                f"Deployment {namespace}/{name} has spec.replicas={desired} < min_controller_replicas={min_replicas}"
            )
        if ready != desired:
            return False, (f"Deployment {namespace}/{name}: readyReplicas={ready} != spec.replicas={desired}")
        return True, f"Deployment {namespace}/{name}: {ready}/{desired} replicas Ready"

    def _check_node_daemonset(
        self,
        kubectl_base: str,
        namespace: str,
        daemonset_name: str,
    ) -> tuple[bool, str]:
        """Return ``(ok, message)`` for the ``node-daemonset-healthy`` subtest."""
        daemonset, error = self._get_workload(
            kubectl_base, "daemonset", namespace, daemonset_name, "workloads.node.daemonset"
        )
        if error:
            return False, error

        meta = daemonset.get("metadata") or {}
        status = daemonset.get("status") or {}
        name = meta.get("name", "")
        desired = int(status.get("desiredNumberScheduled") or 0)
        available = int(status.get("numberAvailable") or 0)
        ready = int(status.get("numberReady") or 0)
        if desired == 0:
            # An empty DaemonSet usually means its nodeSelector / tolerations
            # don't match any node.
            return False, (
                f"DaemonSet {namespace}/{name}: desiredNumberScheduled=0 (no nodes match the DaemonSet's node selector)"
            )
        if available != desired or ready != desired:
            return False, (f"DaemonSet {namespace}/{name}: desired={desired}, available={available}, ready={ready}")
        return True, f"DaemonSet {namespace}/{name}: {ready}/{desired} pods Ready and Available"

    def _get_workload(
        self,
        kubectl_base: str,
        kind: str,
        namespace: str,
        name: str,
        name_config_key: str,
    ) -> tuple[dict[str, Any], str]:
        """Fetch a Deployment or DaemonSet by name.

        Returns ``(workload, "")`` on success or ``({}, error)`` on failure.
        """
        if not name:
            return {}, f"{name_config_key} must be set for the {kind} subtest"
        cmd = (
            f"{kubectl_base} get {kind} {shlex.quote(name)} -n {shlex.quote(namespace)} --ignore-not-found=true -o json"
        )
        result = self.run_command(cmd)
        if result.exit_code != 0:
            return {}, f"kubectl get {kind} failed: {result.stderr.strip()}"
        if not result.stdout.strip():
            return {}, f"{kind.capitalize()} {namespace}/{name} not found"
        try:
            return parse_kubectl_json(result, f"{kind.capitalize()} {name!r}"), ""
        except KubectlParseError as exc:
            return {}, str(exc)
