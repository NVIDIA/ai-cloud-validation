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

"""NFS-focused Kubernetes storage validations.

These checks exercise NFS/shared-filesystem behavior by mounting an RWX PVC
(or a no-PVC probe pod) from BusyBox pods and driving real commands through
``kubectl exec``:

* :class:`K8sNodeKernelModulesCheck` - required DKMS / vendor kernel modules
  (e.g. ``lustre``, ``lnet``) are loaded on all matching worker nodes.
* :class:`K8sNfsMountOptionsCheck` - the NFS mount backing an RWX PVC uses the
  expected version / nconnect / proto and (optionally) readahead.

The shared harness (:class:`_K8sSharedFsCheck`) provisions the ephemeral
namespace, PVC and probe pods and is reused across both checks.
"""

from __future__ import annotations

import shlex
import uuid
from typing import Any, ClassVar

from isvtest.config.settings import (
    get_k8s_csi_nfs_storage_class,
    get_k8s_csi_shared_fs_storage_class,
)
from isvtest.core.k8s import (
    get_kubectl_base_shell,
    get_kubectl_command,
    kubectl_items_or_empty,
    render_k8s_manifest,
    run_kubectl,
)
from isvtest.core.runners import CommandResult
from isvtest.core.validation import BaseValidation

# Reuse the stable PVC manifest + apply/poll helpers from the CSI checks so
# the storage modules stay consistent on manifest shape.
from isvtest.validations.k8s_storage import (
    _MOUNT_POD_MANIFEST,
    _PVC_MANIFEST,
    _apply_manifest,
    _poll_pvc_bound,
    _set_pvc_fields,
    _wait_pod_ready,
)

_DATA_DIR = "/data"

# BusyBox provides flock/stat/ls/mkdir/awk/seq; override via the ``image``
# config key for air-gapped clusters that mirror to a private registry.
_DEFAULT_IMAGE = "busybox:1.36"

# Exclude nodes with these taints from scheduling
# unless the user supplies matching tolerations via the ``tolerations`` config key.
_DEFAULT_NOEXECUTE_TAINT_KEYS: frozenset[str] = frozenset(
    {
        "node.kubernetes.io/not-ready",
        "node.kubernetes.io/unreachable",
    }
)


def _taint_is_tolerated(taint: dict[str, Any], tolerations: list[dict[str, Any]]) -> bool:
    """Return True when any entry in ``tolerations`` covers ``taint``."""
    taint_key = taint.get("key", "")
    taint_value = taint.get("value", "")
    taint_effect = taint.get("effect", "")
    for t in tolerations:
        if not isinstance(t, dict):
            continue
        t_effect = t.get("effect", "")
        if t_effect and t_effect != taint_effect:
            continue
        t_op = t.get("operator", "Equal")
        t_key = t.get("key")
        if t_op == "Exists":
            if t_key is None or t_key == "" or t_key == taint_key:
                return True
        else:  # Equal
            if (t_key is None or t_key == taint_key) and t.get("value", "") == taint_value:
                return True
    return False


def _fmt_err(text: str, max_len: int = 200) -> str:
    """Strip whitespace and truncate ``text`` to ``max_len`` characters."""
    return text.strip()[:max_len]


# --------------------------------------------------------------------------
# Manifest mutators.
# --------------------------------------------------------------------------


def _set_fs_pod_fields(
    doc: dict[str, Any],
    *,
    namespace: str,
    name: str,
    pvc_name: str,
    image: str,
    node_name: str | None = None,
    node_selector: dict[str, str] | None = None,
    tolerations: list[dict[str, Any]] | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Mutate a parsed fs-test-pod manifest in place.

    Binds the single PVC volume, sets the container ``image``, optionally pins
    the pod to ``node_name`` or restricts scheduling via ``node_selector``
    (``spec.nodeSelector``), and overrides the container command. ``command``
    must be a full argv list (e.g. ``["sh", "-c", "sleep 3600"]``).

    ``node_name`` and ``node_selector`` may both be set (nodeName takes
    precedence in the scheduler, nodeSelector is still recorded).
    ``tolerations`` is appended to any existing ``spec.tolerations``.
    """
    metadata = doc.setdefault("metadata", {})
    metadata["name"] = name
    metadata["namespace"] = namespace

    spec = doc.setdefault("spec", {})
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


# --------------------------------------------------------------------------
# Shared harness.
# --------------------------------------------------------------------------


class _K8sSharedFsCheck(BaseValidation):
    """Common harness for shared-filesystem checks: namespace + RWX PVC + pods.

    Concrete subclasses implement :meth:`run`. This base is abstract (no
    ``run``) so it is skipped by validation discovery.

    Common config keys (in addition to each subclass's own):
        image: Container image for the probe pods (default: ``busybox:1.36``).
            Override to point at a mirror on air-gapped clusters; it must
            provide ``sh``/``flock``/``stat``/``ls``/``mkdir``/``awk``/``seq``.
        node_selector: Optional dict of label key/value pairs added to
            ``spec.nodeSelector`` on every probe pod. Use this to restrict
            scheduling to nodes where the CSI driver is installed, e.g.
            ``{scd.vastdata.com/node: "true"}`` or ``{kubernetes.io/os: linux}``.
            For cross-node checks the selector also filters which
            nodes are considered when picking two distinct Ready nodes.
        tolerations: Optional list of Kubernetes toleration objects appended to
            ``spec.tolerations`` on every probe pod.
    """

    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    # Subclasses may override these config-key defaults.
    _DEFAULT_NS_PREFIX: ClassVar[str] = "isvtest-fs"
    _DEFAULT_PVC_SIZE: ClassVar[str] = "1Gi"
    _DEFAULT_BIND_TIMEOUT_S: ClassVar[int] = 180

    def _setup_kubectl(self) -> None:
        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()

    def _resolve_shared_sc(self) -> str:
        """Resolve the RWX StorageClass: shared-fs, then NFS, then env fallbacks."""
        return str(
            self.config.get("shared_fs_storage_class")
            or self.config.get("nfs_storage_class")
            or get_k8s_csi_shared_fs_storage_class()
            or get_k8s_csi_nfs_storage_class()
            or ""
        )

    def _create_namespace(self) -> bool:
        prefix = self.config.get("namespace_prefix", self._DEFAULT_NS_PREFIX)
        self._namespace = f"{prefix}-{uuid.uuid4().hex[:8]}"
        self._ns_quoted = shlex.quote(self._namespace)
        result = self.run_command(f"{self._kubectl_base} create namespace {self._ns_quoted}")
        if result.exit_code != 0:
            self.set_failed(f"Failed to create namespace {self._namespace}: {result.stderr}")
            return False
        return True

    def _cleanup_namespace(self, created: bool) -> None:
        if not created:
            return
        cleanup = self.run_command(
            f"{self._kubectl_base} delete namespace {self._ns_quoted} --wait=false --ignore-not-found=true"
        )
        if cleanup.exit_code != 0:
            self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _apply_pvc(self, name: str, sc: str, size: str) -> tuple[int, str]:
        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            return _set_pvc_fields(doc, namespace=self._namespace, name=name, sc=sc, mode="ReadWriteMany", size=size)

        return _apply_manifest(self._kubectl_parts, render_k8s_manifest(_PVC_MANIFEST, _mutate), self.timeout)

    def _image(self) -> str:
        return str(self.config.get("image") or _DEFAULT_IMAGE)

    def _node_selector(self) -> dict[str, str]:
        """Return ``node_selector`` from config as a ``{label: value}`` dict."""
        raw = self.config.get("node_selector") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def _tolerations(self) -> list[dict[str, Any]]:
        """Return ``tolerations`` from config as a list of toleration dicts."""
        raw = self.config.get("tolerations") or []
        if not isinstance(raw, list):
            return []
        return [t for t in raw if isinstance(t, dict)]

    def _apply_pod(
        self,
        name: str,
        pvc_name: str,
        *,
        node_name: str | None = None,
        command: list[str] | None = None,
    ) -> tuple[int, str]:
        image = self._image()
        node_selector = self._node_selector() or None
        tolerations = self._tolerations() or None

        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            return _set_fs_pod_fields(
                doc,
                namespace=self._namespace,
                name=name,
                pvc_name=pvc_name,
                image=image,
                node_name=node_name,
                node_selector=node_selector,
                tolerations=tolerations,
                command=command,
            )

        return _apply_manifest(self._kubectl_parts, render_k8s_manifest(_MOUNT_POD_MANIFEST, _mutate), self.timeout)

    def _wait_ready(self, pod_name: str, timeout_s: int) -> tuple[bool, str]:
        ok, err = _wait_pod_ready(self.run_command, self._kubectl_base, self._namespace, pod_name, timeout_s)
        if not ok:
            # Append container logs so the failure message contains the actual
            # error (e.g. "flock: Operation not supported") rather than just
            # "did not become Ready".
            logs = self.run_command(
                f"{self._kubectl_base} logs -n {self._ns_quoted} {shlex.quote(pod_name)} --tail=20 2>&1"
            )
            if logs.stdout.strip():
                err = f"{err}\nContainer logs:\n{logs.stdout.strip()}"
        return ok, err

    def _wait_pvc_bound(self, pvc_name: str, timeout_s: int) -> bool:
        return _poll_pvc_bound(self.run_command, self._kubectl_base, self._namespace, pvc_name, timeout_s)

    def _exec(self, pod_name: str, inner: str, timeout: int | None = None) -> CommandResult:
        cmd = f"{self._kubectl_base} exec -n {self._ns_quoted} {shlex.quote(pod_name)} -- sh -c {shlex.quote(inner)}"
        # Log the un-double-quoted form; shlex.quote's nested '"'"' escaping of
        # the inner snippet is correct but unreadable in logs.
        display_cmd = f"{self._kubectl_base} exec -n {self._namespace} {pod_name} -- sh -c {inner!r}"
        return self.run_command(cmd, timeout=timeout, display_cmd=display_cmd)

    def _delete_pod(self, pod_name: str, *, wait: bool) -> None:
        wait_flag = "true" if wait else "false"
        self.run_command(
            f"{self._kubectl_base} delete pod {shlex.quote(pod_name)} -n {self._ns_quoted} "
            f"--wait={wait_flag} --ignore-not-found=true"
        )

    @staticmethod
    def _item_is_ready(node_item: dict[str, Any]) -> bool:
        """Return True when a node item's ``Ready`` condition has ``status == "True"``."""
        for condition in (node_item.get("status") or {}).get("conditions") or []:
            if isinstance(condition, dict) and condition.get("type") == "Ready":
                return condition.get("status") == "True"
        return False

    @staticmethod
    def _has_untolerated_noexecute_taint(
        node_item: dict[str, Any],
        user_tolerations: list[dict[str, Any]],
    ) -> bool:
        """Return True when the node has a NoExecute taint our pods won't tolerate.

        Checks against the two default Kubernetes tolerations (``not-ready`` /
        ``unreachable``) plus any user-supplied ``tolerations`` from config.
        """
        for taint in (node_item.get("spec") or {}).get("taints") or []:
            if not isinstance(taint, dict) or taint.get("effect") != "NoExecute":
                continue
            if taint.get("key") in _DEFAULT_NOEXECUTE_TAINT_KEYS:
                continue
            if _taint_is_tolerated(taint, user_tolerations):
                continue
            return True
        return False

    def _ready_nodes(self) -> list[str]:
        """Return schedulable node matching node_selector (if set), sorted for determinism."""
        selector = self._node_selector()
        extra_args = ["-l", ",".join(f"{k}={v}" for k, v in sorted(selector.items()))] if selector else []
        result = run_kubectl(["get", "nodes", *extra_args, "-o", "json"])
        if result.returncode != 0:
            self.log.warning(
                "kubectl get nodes%s failed (rc=%s): %s",
                f" -l {extra_args[1]}" if extra_args else "",
                result.returncode,
                _fmt_err(result.stderr or ""),
            )
            return []
        user_tolerations = self._tolerations()
        return sorted(
            str(name)
            for item in kubectl_items_or_empty(result)
            if self._item_is_ready(item)
            and not self._has_untolerated_noexecute_taint(item, user_tolerations)
            and (name := (item.get("metadata") or {}).get("name"))
        )


# ---------------------------------------------------------------------------
# Kernel module checks
# ---------------------------------------------------------------------------


class K8sNodeKernelModulesCheck(_K8sSharedFsCheck):
    """Verify required kernel modules are loaded on all GPU worker nodes.

    Creates a lightweight no-PVC probe pod (pinned via ``nodeName``) on each
    node matching ``node_selector`` and checks ``/proc/modules`` for every
    module in ``kernel_modules``.

    A subtest passes only when the module is found on **all** matching nodes,
    the failure message lists the nodes where it is absent.

    Config keys (with defaults):
        kernel_modules: List of module names to verify (e.g.
            ``["lustre", "lnet"]``). Whole check skipped when empty.
        node_selector: Label selector dict restricting which nodes to probe
            (e.g. ``{nvidia.com/gpu.present: "true"}``). Empty = all nodes.
        image: Container image for probe pods (default: ``busybox:1.36``).
        tolerations: List of toleration dicts applied to probe pods.
        bind_timeout_s: Max seconds to wait for each probe pod to become
            Ready (default: 60).
        namespace_prefix: Ephemeral namespace prefix
            (default: ``isvtest-kmod``).
        timeout: Per-command timeout in seconds (default: 60).
    """

    description: ClassVar[str] = "Verify required DKMS / vendor kernel modules are loaded on all GPU worker nodes."
    timeout: ClassVar[int] = 60
    _DEFAULT_NS_PREFIX = "isvtest-kmod"
    _DEFAULT_BIND_TIMEOUT_S: ClassVar[int] = 60

    def _apply_probe_pod(self, pod_name: str, node_name: str) -> tuple[int, str]:
        """Apply a no-PVC BusyBox pod pinned to ``node_name``."""
        import yaml as _yaml  # local import; pyyaml is a workspace dep

        image = self._image()
        tolerations = self._tolerations() or None

        spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "nodeName": node_name,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "runAsGroup": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "command": ["sh", "-c", "while true; do sleep 3600; done"],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
        }
        if tolerations:
            spec["tolerations"] = tolerations

        manifest = _yaml.dump(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": pod_name, "namespace": self._namespace},
                "spec": spec,
            }
        )
        return _apply_manifest(self._kubectl_parts, manifest, self.timeout)

    def run(self) -> None:
        raw_modules = self.config.get("kernel_modules") or []
        if not isinstance(raw_modules, list):
            raw_modules = [raw_modules]
        modules = [str(m) for m in raw_modules if m]
        if not modules:
            self.set_passed("Skipped: kernel_modules not configured")
            return

        self._setup_kubectl()
        bind_timeout = int(self.config.get("bind_timeout_s", self._DEFAULT_BIND_TIMEOUT_S))
        nodes = self._ready_nodes()
        if not nodes:
            self.set_failed("No Ready nodes found matching node_selector")
            return

        created = False
        try:
            created = self._create_namespace()
            if not created:
                return

            # Collect per-node results: {module: [missing_node, ...]}
            missing: dict[str, list[str]] = {m: [] for m in modules}

            for node in nodes:
                suffix = uuid.uuid4().hex[:6]
                pod_name = f"kmod-{suffix}"

                rc, err = self._apply_probe_pod(pod_name, node_name=node)
                if rc != 0:
                    self.set_failed(f"kubectl apply failed for probe pod on node {node!r}: {_fmt_err(err)}")
                    return
                ready, wait_err = self._wait_ready(pod_name, bind_timeout)
                if not ready:
                    self.set_failed(
                        f"Probe pod on node {node!r} did not become Ready within {bind_timeout}s: {_fmt_err(wait_err)}"
                    )
                    return

                for module in modules:
                    result = self._exec(
                        pod_name,
                        f"grep -E {shlex.quote('^' + module + ' ')} /proc/modules",
                    )
                    if result.exit_code != 0:
                        missing[module].append(node)

                self._delete_pod(pod_name, wait=False)

            any_failed = False
            for module in modules:
                absent_nodes = missing[module]
                if not absent_nodes:
                    self.report_subtest(
                        module,
                        passed=True,
                        message=f"Module {module!r} found on all {len(nodes)} node(s)",
                    )
                else:
                    any_failed = True
                    self.report_subtest(
                        module,
                        passed=False,
                        message=(
                            f"Module {module!r} absent on {len(absent_nodes)}/{len(nodes)} node(s): "
                            + ", ".join(absent_nodes)
                        ),
                    )

            if any_failed:
                self.set_failed("One or more required kernel modules are missing; see subtest details")
            else:
                self.set_passed(f"All {len(modules)} module(s) present on all {len(nodes)} node(s)")
        finally:
            self._cleanup_namespace(created)


# ---------------------------------------------------------------------------
# NFS mount-option checks
# ---------------------------------------------------------------------------


def _parse_nfs_mount_options(mountinfo: str, mount_path: str) -> tuple[str, dict[str, str]] | None:
    """Return ``(fstype, options_dict)`` for ``mount_path`` from ``/proc/self/mountinfo``."""
    for raw_line in mountinfo.splitlines():
        parts = raw_line.split()
        if len(parts) < 10:
            continue
        # mountinfo field layout:
        # 0:mountid  1:parentid  2:major:minor  3:root  4:mountpoint
        # 5:mountoptions  [optional fields]  -  fstype  source  superoptions
        if parts[4] != mount_path:
            continue
        try:
            sep_idx = parts.index("-")
        except ValueError:
            continue
        if sep_idx + 3 >= len(parts):
            continue
        fstype = parts[sep_idx + 1]
        major_minor = parts[2]
        superoptions = parts[sep_idx + 3]
        opts: dict[str, str] = {}
        for token in superoptions.split(","):
            if "=" in token:
                k, _, v = token.partition("=")
                opts[k] = v
            else:
                opts[token] = ""
        # Attach major:minor as a synthetic key so callers can find the BDI.
        opts["__major_minor__"] = major_minor
        return fstype, opts
    return None


class K8sNfsMountOptionsCheck(_K8sSharedFsCheck):
    """Verify NFS mount options and optional kernel module / readahead.

    Provisions a single RWX PVC and BusyBox pod, reads
    ``/proc/self/mountinfo`` once, then asserts each configured option.
    Subtests whose expected value is left empty are skipped rather than
    failed, so providers only need to set the options they care about.

    Subtests:
        nfs-version    asserts ``vers == expected_version``
        nfs-nconnect   asserts ``int(nconnect) == expected_nconnect``
        nfs-proto      asserts ``proto == expected_proto``
        read-ahead-kb  asserts BDI read_ahead_kb equals
                       ``expected_read_ahead_kb`` (skipped if unset)

    Config keys (with defaults):
        shared_fs_storage_class / nfs_storage_class: RWX StorageClass
            (env: ``K8S_CSI_SHARED_FS_SC`` / ``K8S_CSI_NFS_SC``).
            Whole check skipped when unset.
        expected_version: NFS version string (e.g. ``"4.1"``); empty = skip.
        expected_nconnect: nconnect multiplier (int, e.g. ``4``); empty = skip.
        expected_proto: Transport protocol (e.g. ``"rdma"`` or ``"tcp"``);
            empty = skip.
        expected_read_ahead_kb: Expected readahead in KiB (int); empty = skip.
        pvc_size: PVC request size (default: ``1Gi``).
        bind_timeout_s: Max wait for PVC bind / pod Ready (default: 180).
        namespace_prefix: Ephemeral namespace prefix
            (default: ``isvtest-fs-nfs``).
        timeout: Per-command timeout (default: 300).
    """

    description: ClassVar[str] = (
        "Verify NFS mount options (version, nconnect, proto) and optional kernel module / readahead."
    )
    timeout: ClassVar[int] = 300
    _DEFAULT_NS_PREFIX = "isvtest-fs-nfs"

    def _check_read_ahead(self, pod_name: str, options: dict[str, str]) -> tuple[bool, str]:
        """Return ``(passed, message)`` for the read-ahead-kb subtest."""
        major_minor = options.get("__major_minor__", "")
        if not major_minor:
            return False, "Could not extract major:minor from mountinfo for /data"

        bdi_path = f"/sys/class/bdi/{major_minor}/read_ahead_kb"
        bdi_result = self._exec(pod_name, f"cat {shlex.quote(bdi_path)}")
        if bdi_result.exit_code != 0:
            return False, (
                f"cat {bdi_path} failed (sysfs may not be visible from container): "
                f"{_fmt_err(bdi_result.stderr or bdi_result.stdout)}"
            )

        raw_expected = self.config.get("expected_read_ahead_kb")
        if raw_expected is None:
            return False, "expected_read_ahead_kb not configured"

        try:
            expected_kb = int(raw_expected)
            actual_kb = int(bdi_result.stdout.strip())
        except ValueError:
            return False, f"Could not parse read_ahead_kb value: {bdi_result.stdout.strip()!r}"

        if actual_kb == expected_kb:
            return True, f"{bdi_path} = {actual_kb} KiB as expected"
        return False, f"Expected read_ahead_kb={expected_kb} but {bdi_path} = {actual_kb}"

    def run(self) -> None:
        sc = self._resolve_shared_sc()
        if not sc:
            self.set_passed("Skipped: no shared_fs_storage_class / nfs_storage_class configured")
            return

        self._setup_kubectl()
        bind_timeout = int(self.config.get("bind_timeout_s", self._DEFAULT_BIND_TIMEOUT_S))
        pvc_size = str(self.config.get("pvc_size", self._DEFAULT_PVC_SIZE))
        suffix = uuid.uuid4().hex[:6]
        pvc_name = f"fs-nfs-{suffix}"
        pod_name = f"fs-nfs-{suffix}"

        created = False
        try:
            created = self._create_namespace()
            if not created:
                return

            rc, err = self._apply_pvc(pvc_name, sc, pvc_size)
            if rc != 0:
                self.set_failed(f"kubectl apply failed for PVC {pvc_name!r}: {_fmt_err(err)}")
                return
            if not self._wait_pvc_bound(pvc_name, bind_timeout):
                self.set_failed(f"PVC {pvc_name!r} did not reach Bound within {bind_timeout}s")
                return

            rc, err = self._apply_pod(pod_name, pvc_name)
            if rc != 0:
                self.set_failed(f"kubectl apply failed for pod {pod_name!r}: {_fmt_err(err)}")
                return
            ready, wait_err = self._wait_ready(pod_name, bind_timeout)
            if not ready:
                self.set_failed(f"Pod {pod_name!r} did not become Ready within {bind_timeout}s: {_fmt_err(wait_err)}")
                return

            mi_result = self._exec(pod_name, "cat /proc/self/mountinfo")
            if mi_result.exit_code != 0:
                self.set_failed(f"cat /proc/self/mountinfo failed: {_fmt_err(mi_result.stderr or mi_result.stdout)}")
                return

            parsed = _parse_nfs_mount_options(mi_result.stdout, _DATA_DIR)
            if parsed is None:
                self.set_failed(
                    f"Could not find mount entry for {_DATA_DIR!r} in /proc/self/mountinfo\n{mi_result.stdout.strip()}"
                )
                return

            fstype, options = parsed
            not_nfs = fstype not in ("nfs", "nfs4")

            any_failed = False

            # --- nfs-version ---
            expected_version = str(self.config.get("expected_version") or "")
            if not expected_version:
                self.report_subtest(
                    "nfs-version", passed=True, message="expected_version not configured; skipped", skipped=True
                )
            elif not_nfs:
                self.report_subtest("nfs-version", passed=True, message=f"fstype={fstype!r}; not NFS", skipped=True)
            else:
                actual = options.get("vers", "")
                passed = actual == expected_version
                self.report_subtest(
                    "nfs-version",
                    passed=passed,
                    message=(
                        f"NFS mount uses vers={actual!r} as expected"
                        if passed
                        else f"Expected vers={expected_version!r} but mount shows vers={actual!r}"
                    ),
                )
                any_failed = any_failed or not passed

            # --- nfs-nconnect ---
            raw_nconnect = self.config.get("expected_nconnect")
            if not raw_nconnect and raw_nconnect != 0:
                self.report_subtest(
                    "nfs-nconnect", passed=True, message="expected_nconnect not configured; skipped", skipped=True
                )
            elif not_nfs:
                self.report_subtest("nfs-nconnect", passed=True, message=f"fstype={fstype!r}; not NFS", skipped=True)
            else:
                try:
                    expected_nconnect = int(raw_nconnect)
                except (ValueError, TypeError):
                    self.report_subtest(
                        "nfs-nconnect", passed=False, message=f"expected_nconnect is not an integer: {raw_nconnect!r}"
                    )
                    any_failed = True
                else:
                    if "nconnect" not in options:
                        self.report_subtest(
                            "nfs-nconnect",
                            passed=False,
                            message=f"nconnect option absent from NFS mount (expected {expected_nconnect}); driver may not support it",
                        )
                        any_failed = True
                    else:
                        try:
                            actual_nconnect = int(options["nconnect"])
                        except ValueError:
                            self.report_subtest(
                                "nfs-nconnect",
                                passed=False,
                                message=f"nconnect={options['nconnect']!r} is not parseable as int",
                            )
                            any_failed = True
                        else:
                            passed = actual_nconnect == expected_nconnect
                            self.report_subtest(
                                "nfs-nconnect",
                                passed=passed,
                                message=(
                                    f"NFS mount uses nconnect={actual_nconnect} as expected"
                                    if passed
                                    else f"Expected nconnect={expected_nconnect} but mount shows nconnect={actual_nconnect}"
                                ),
                            )
                            any_failed = any_failed or not passed

            # --- nfs-proto ---
            expected_proto = str(self.config.get("expected_proto") or "")
            if not expected_proto:
                self.report_subtest(
                    "nfs-proto", passed=True, message="expected_proto not configured; skipped", skipped=True
                )
            elif not_nfs:
                self.report_subtest("nfs-proto", passed=True, message=f"fstype={fstype!r}; not NFS", skipped=True)
            else:
                actual_proto = options.get("proto", "")
                passed = actual_proto == expected_proto
                self.report_subtest(
                    "nfs-proto",
                    passed=passed,
                    message=(
                        f"NFS mount uses proto={actual_proto!r} as expected"
                        if passed
                        else f"Expected proto={expected_proto!r} but mount shows proto={actual_proto!r}"
                    ),
                )
                any_failed = any_failed or not passed

            # --- read-ahead-kb (optional) ---
            raw_read_ahead = self.config.get("expected_read_ahead_kb")
            if not raw_read_ahead and raw_read_ahead != 0:
                self.report_subtest(
                    "read-ahead-kb", passed=True, message="expected_read_ahead_kb not configured; skipped", skipped=True
                )
            elif not_nfs:
                self.report_subtest("read-ahead-kb", passed=True, message=f"fstype={fstype!r}; not NFS", skipped=True)
            else:
                ra_passed, ra_message = self._check_read_ahead(pod_name, options)
                self.report_subtest("read-ahead-kb", passed=ra_passed, message=ra_message)
                any_failed = any_failed or not ra_passed

            if any_failed:
                self.set_failed("One or more NFS mount-option subtests failed; see subtest details")
            else:
                self.set_passed(f"NFS mount options verified for storage class {sc!r}")
        finally:
            self._cleanup_namespace(created)
