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

"""Shared-filesystem POSIX-semantics validations (multi-pod, RWX PVC).

These checks exercise filesystem behavior that a shared (RWX) volume must
provide for home-directory / scratch use cases, by mounting one PVC from two
BusyBox pods and driving real syscalls through ``kubectl exec``:

* :class:`K8sFileLockingCheck` - ``flock`` acquired from pod A blocks
  or EAGAINs pod B on the same PVC, and releases cleanly.
* :class:`K8sCrossNodeWriteVisibilityCheck` - a file written from a
  pod on node A is readable with correct content from a pod on node B.
* :class:`K8sCrossNodeAttrConsistencyCheck` - extending a file on
  node A is reflected in ``stat`` size + mtime from node B within the
  vendor-documented attribute-cache window.
* :class:`K8sLargeDirListingFilesCheck` - create a large number of
  files in one directory and list them without error or truncation.
* :class:`K8sLargeDirListingDirsCheck` - same for subdirectories.
* :class:`K8sPosixComplianceCheck` - build and run the upstream
  pjdfstest POSIX suite (chmod/chown/link/rename/...) in a privileged
  root pod against the mounted volume.

The per-operation logic is factored into transport-neutral shell-snippet
helpers (``write_payload_cmd``, ``flock_nonblock_cmd``, ``stat_size_mtime_cmd``,
``create_files_cmd``, ...). The k8s checks wrap them in ``kubectl exec``; a
future bare-metal sibling can run the same snippets over an SSH/local runner
against two hosts sharing the mount.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
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
# the two storage modules stay consistent on manifest shape
from isvtest.validations.k8s_storage import (
    _MOUNT_POD_MANIFEST,
    _PVC_MANIFEST,
    _apply_manifest,
    _poll_pvc_bound,
    _set_fs_pod_fields,
    _set_pvc_fields,
    _wait_pod_ready,
)

_MANIFEST_DIR = Path(__file__).parent / "manifests" / "k8s"
_PJDFSTEST_POD_MANIFEST = _MANIFEST_DIR / "pjdfstest_pod.yaml"

# Vendored pjdfstest source, if not present, run `make vendor-pjdfstest`
_PJDFSTEST_SRC_DIR = Path(__file__).resolve().parents[3] / "vendor" / "pjdfstest"
# Where the source is copied (and built) inside the probe pod.
_PJDFSTEST_DEST = "/opt/pjdfstest"

# Default probe image for the POSIX-compliance check. A stock public toolchain
# image (buildpack-deps based) that already ships cc/make/autoconf/automake,
# perl (prove), git and tar - so building pjdfstest and running prove need
# nothing from the network beyond pulling the image. Override via the ``image``
# config key to point at a registry mirror on air-gapped clusters.
_DEFAULT_BUILD_IMAGE = "gcc:12"

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
    """Return True when any entry in ``tolerations`` covers ``taint``. """
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
# Transport-neutral shell-snippet helpers.
#
# Each returns an ``sh -c``-ready command string operating on a mounted path.
# They are deliberately free of any kubectl/SSH coupling so the same snippet
# can be wrapped by whichever transport a provider uses.
# --------------------------------------------------------------------------


def write_payload_cmd(path: str, payload: str) -> str:
    """Truncate ``path`` and write ``payload`` verbatim (no trailing newline)."""
    return f"printf %s {shlex.quote(payload)} > {shlex.quote(path)}"


def append_payload_cmd(path: str, payload: str) -> str:
    """Append ``payload`` to ``path`` verbatim (no trailing newline)."""
    return f"printf %s {shlex.quote(payload)} >> {shlex.quote(path)}"


def read_file_cmd(path: str) -> str:
    """Read ``path`` to stdout."""
    return f"cat {shlex.quote(path)}"


def stat_size_mtime_cmd(path: str) -> str:
    """Print ``<size_bytes> <mtime_epoch_seconds>`` for ``path``."""
    return f"stat -c '%s %Y' {shlex.quote(path)}"


def flock_hold_command(lock_path: str) -> list[str]:
    """Container command that grabs an exclusive ``flock`` and holds it for the
    pod's lifetime (released only when the pod is deleted).
    """
    return ["flock", "-x", lock_path, "sh", "-c", "while true; do sleep 3600; done"]


def flock_nonblock_cmd(lock_path: str) -> str:
    """Try to grab an exclusive ``flock`` without blocking; non-zero on EAGAIN."""
    return f"flock -xn {shlex.quote(lock_path)} true"


def create_files_cmd(directory: str, count: int, prefix: str = "f") -> str:
    """Create ``count`` empty files ``<prefix>1..<prefix>count`` under ``directory``."""
    names = f"seq 1 {int(count)} | awk -v d={shlex.quote(directory)} -v p={shlex.quote(prefix)} '{{print d\"/\"p$0}}'"
    return f"mkdir -p {shlex.quote(directory)} && {names} | xargs touch"


def create_dirs_cmd(directory: str, count: int, prefix: str = "d") -> str:
    """Create ``count`` subdirectories ``<prefix>1..<prefix>count`` under ``directory``."""
    names = f"seq 1 {int(count)} | awk -v d={shlex.quote(directory)} -v p={shlex.quote(prefix)} '{{print d\"/\"p$0}}'"
    return f"mkdir -p {shlex.quote(directory)} && {names} | xargs mkdir"


def list_dir_quiet_cmd(directory: str) -> str:
    """List ``directory`` discarding output; non-zero exit means ``ls`` errored."""
    return f"ls -1A {shlex.quote(directory)} >/dev/null"


def count_entries_cmd(directory: str) -> str:
    """Count immediate entries under ``directory`` (excludes ``.`` and ``..``)."""
    return f"find {shlex.quote(directory)} -mindepth 1 -maxdepth 1 | wc -l"


# --------------------------------------------------------------------------
# pjdfstest (prove/TAP) output parsing.
# --------------------------------------------------------------------------

# prove footer: "Result: PASS" / "Result: FAIL".
_RE_PROVE_RESULT = re.compile(r"^Result:\s*(PASS|FAIL)\s*$", re.MULTILINE)
# prove footer: "Files=238, Tests=12345, 30 wallclock secs (...)".
_RE_PROVE_FILES_TESTS = re.compile(r"^Files=(\d+),\s*Tests=(\d+)", re.MULTILINE)
# Per-file failure lines in the "Test Summary Report" block, e.g.
# "tests/chmod/12.t (Wstat: 0 Tests: 203 Failed: 1)".
_RE_PROVE_FAILED_FILE = re.compile(
    r"^(\S+\.t)\s+\(Wstat:\s*\d+\s+Tests:\s*(\d+)\s+Failed:\s*(\d+)\)",
    re.MULTILINE,
)
# Per-file lines: progress lines ("<path>.t ..... ok") during the run and the
# "Test Summary Report" lines both start with the file path at column 0, so a
# deduped scan of these enumerates every test file prove executed.
_RE_PROVE_FILE = re.compile(r"^(\S+\.t)\b", re.MULTILINE)
_PROVE_ALL_OK = "All tests successful."


@dataclass
class PjdfstestResult:
    """Parsed result of a ``prove -r <pjdfstest>/tests`` run.

    ``success`` is True only when prove reported PASS (or "All tests
    successful." with no failing files). ``error`` is set when the output
    could not be interpreted as a prove summary at all.
    """

    success: bool
    files_total: int = 0
    tests_total: int = 0
    failed_files: list[tuple[str, int]] = field(default_factory=list)
    all_files: list[str] = field(default_factory=list)
    result: str = ""  # "PASS" | "FAIL" | ""
    error: str = ""


def parse_pjdfstest_output(output: str) -> PjdfstestResult:
    """Parse prove's summary for the pjdfstest suite.

    Extracts the PASS/FAIL verdict, file/test totals, and the per-file
    failure list from prove's "Test Summary Report". Mirrors the shape of
    :func:`isvtest.workloads.nccl_common.parse_nccl_output`.

    Args:
        output: Combined stdout/stderr from a ``prove`` invocation.

    Returns:
        A :class:`PjdfstestResult`. ``success`` is False (with ``error`` set)
        when no recognizable prove summary is present.
    """
    failed_files = [
        (name, int(failed)) for name, _tests, failed in _RE_PROVE_FAILED_FILE.findall(output) if int(failed) > 0
    ]

    # Enumerate every test file (progress + summary lines), preserving first-seen
    # (execution) order, so callers can emit a per-file subtest for all of them.
    all_files: list[str] = []
    seen: set[str] = set()
    for name in _RE_PROVE_FILE.findall(output):
        if name not in seen:
            seen.add(name)
            all_files.append(name)

    files_total = tests_total = 0
    ft = _RE_PROVE_FILES_TESTS.search(output)
    if ft:
        files_total, tests_total = int(ft.group(1)), int(ft.group(2))

    verdict_match = _RE_PROVE_RESULT.search(output)
    verdict = verdict_match.group(1) if verdict_match else ""
    all_ok = _PROVE_ALL_OK in output

    if not verdict and not all_ok:
        return PjdfstestResult(
            success=False,
            files_total=files_total,
            tests_total=tests_total,
            failed_files=failed_files,
            all_files=all_files,
            error="Could not parse prove summary from pjdfstest output",
        )

    success = (verdict == "PASS" or (all_ok and not verdict)) and not failed_files
    return PjdfstestResult(
        success=success,
        files_total=files_total,
        tests_total=tests_total,
        failed_files=failed_files,
        all_files=all_files,
        result=verdict or ("PASS" if all_ok else "FAIL"),
    )


# --------------------------------------------------------------------------
# Shared harness.
# --------------------------------------------------------------------------


def _coerce_maybe_json(value: Any) -> Any:
    """Return structured config, JSON-parsing the value when it is a string.

    Native YAML config supplies ``node_selector`` / ``kernel_modules`` as a
    dict / list. When the values are instead injected as a Jinja-rendered JSON
    string (e.g. from a setup step's output), parse those back into the
    underlying structure. Empty / blank strings become ``None`` (the caller's
    "unset" sentinel); unparseable strings pass through untouched.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return value


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
        """Return ``node_selector`` from config as a ``{label: value}`` dict.

        Accepts either a native dict or the JSON-string form emitted by the
        storage manifest's ``manifest_to_steps`` setup step.
        """
        raw = _coerce_maybe_json(self.config.get("node_selector")) or {}
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
        return _poll_pvc_bound(self.run_command, self._kubectl_base, self._namespace, pvc_name, timeout_s, poll_interval_s=60.0)

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


# --------------------------------------------------------------------------
# Cross-node base.
# --------------------------------------------------------------------------


class _K8sCrossNodeCheck(_K8sSharedFsCheck):
    """Shared setup for two pods pinned to distinct nodes on one RWX PVC."""

    def _two_nodes(self) -> list[str] | None:
        """Return two distinct Ready node names, or ``None`` (after skipping)."""
        nodes = self._ready_nodes()
        if len(nodes) < 2:
            self.set_passed(f"Skipped: cross-node test requires >= 2 Ready nodes, found {len(nodes)}")
            return None
        return nodes[:2]

    def _provision(
        self,
        pvc_name: str,
        sc: str,
        pvc_size: str,
        pod_a: str,
        pod_b: str,
        nodes: list[str],
        bind_timeout: int,
        *,
        cmd_a: list[str] | None = None,
        cmd_b: list[str] | None = None,
    ) -> bool:
        """Create the PVC and both node-pinned pods, then wait for Ready."""
        rc, err = self._apply_pvc(pvc_name, sc, pvc_size)
        if rc != 0:
            self.set_failed(f"kubectl apply failed for PVC {pvc_name!r}: {_fmt_err(err)}")
            return False
        if not self._wait_pvc_bound(pvc_name, bind_timeout):
            self.set_failed(f"PVC {pvc_name!r} did not reach Bound within {bind_timeout}s")
            return False
        still_ready = set(self._ready_nodes())
        stale = [n for n in nodes if n not in still_ready]
        if stale:
            self.set_failed(
                f"Node(s) {stale} are no longer schedulable after PVC binding; "
                "retry or investigate node health before re-running"
            )
            return False
        rc, err = self._apply_pod(pod_a, pvc_name, node_name=nodes[0], command=cmd_a)
        if rc != 0:
            self.set_failed(f"kubectl apply failed for pod {pod_a!r} on {nodes[0]!r}: {_fmt_err(err)}")
            return False
        rc, err = self._apply_pod(pod_b, pvc_name, node_name=nodes[1], command=cmd_b)
        if rc != 0:
            self.set_failed(f"kubectl apply failed for pod {pod_b!r} on {nodes[1]!r}: {_fmt_err(err)}")
            return False
        for pod, node in ((pod_a, nodes[0]), (pod_b, nodes[1])):
            ready, wait_err = self._wait_ready(pod, bind_timeout)
            if not ready:
                self.set_failed(
                    f"Pod {pod!r} on node {node!r} did not become Ready within {bind_timeout}s: {_fmt_err(wait_err)}"
                )
                return False
        return True


# --------------------------------------------------------------------------
# File locking across pods.
# --------------------------------------------------------------------------


class K8sFileLockingCheck(_K8sCrossNodeCheck):
    """Verify ``flock`` is honoured across pods on distinct nodes.

    Pod A (node A) holds an exclusive ``flock`` on a file on a shared RWX PVC
    for its lifetime. While A holds the lock, pod B (node B) on the same PVC
    must be denied by a non-blocking ``flock -xn`` (EAGAIN). After pod A is
    deleted, pod B's ``flock -xn`` must succeed. Pinning the pods to distinct
    nodes is what makes this exercise *distributed* lock arbitration rather
    than local-kernel enforcement, so the check is skipped on single-node
    clusters.

    Config keys (with defaults):
        shared_fs_storage_class / nfs_storage_class: RWX StorageClass; the
            check is skipped when neither (nor the env fallbacks) is set.
        pvc_size: PVC request size (default: ``1Gi``).
        bind_timeout_s: Max wait for PVCs to bind / pods to become Ready
            (default: 180).
        release_timeout_s: Max wait for pod B to acquire after A is deleted
            (default: 60).
        namespace_prefix: Ephemeral namespace prefix (default:
            ``isvtest-fs-lock``).
        timeout: Per-command timeout (default: 300).
    """

    description: ClassVar[str] = "Verify flock locking is honoured across pods on distinct nodes on one RWX PVC."
    timeout: ClassVar[int] = 300

    _DEFAULT_NS_PREFIX = "isvtest-fs-lock"

    def run(self) -> None:
        self._setup_kubectl()
        sc = self._resolve_shared_sc()
        if not sc:
            self.set_passed("Skipped: no shared-fs/nfs StorageClass configured")
            return

        nodes = self._two_nodes()
        if nodes is None:
            return

        bind_timeout = int(self.config.get("bind_timeout_s", self._DEFAULT_BIND_TIMEOUT_S))
        pvc_size = str(self.config.get("pvc_size", self._DEFAULT_PVC_SIZE))
        release_timeout = int(self.config.get("release_timeout_s", 60))
        lock_path = f"{_DATA_DIR}/lockfile"

        created = False
        try:
            created = self._create_namespace()
            if not created:
                return

            pvc_name = f"fs-lock-{uuid.uuid4().hex[:6]}"
            pod_a = f"fs-lock-holder-{uuid.uuid4().hex[:6]}"
            pod_b = f"fs-lock-waiter-{uuid.uuid4().hex[:6]}"

            # Pod A holds the lock for its whole lifetime (released only by the
            # explicit delete below); pod B uses the default keepalive command.
            if not self._provision(
                pvc_name,
                sc,
                pvc_size,
                pod_a,
                pod_b,
                nodes,
                bind_timeout,
                cmd_a=flock_hold_command(lock_path),
            ):
                return

            # Give pod A a moment to actually acquire the lock after its
            # container starts running.
            time.sleep(2.0)

            # Subtest 1: pod B must NOT be able to acquire while A holds.
            contend = self._exec(pod_b, flock_nonblock_cmd(lock_path))
            if contend.exit_code == 1:
                contention_ok = True
                contention_msg = "Pod B was correctly denied the lock (EAGAIN) while pod A held it"
            elif contend.exit_code == 0:
                contention_ok = False
                contention_msg = "Pod B acquired the lock while pod A still held it (locking not enforced across pods)"
            else:
                contention_ok = False
                contention_msg = (
                    f"flock on pod B errored (exit {contend.exit_code}); could not evaluate contention: "
                    f"{_fmt_err(contend.stderr or contend.stdout)}"
                )
            self.report_subtest("lock-contention", passed=contention_ok, message=contention_msg)

            # Subtest 2: after A releases (pod deleted), B must be able to acquire.
            self._delete_pod(pod_a, wait=True)
            release_ok = False
            deadline = time.time() + release_timeout
            while time.time() < deadline:
                acquired = self._exec(pod_b, flock_nonblock_cmd(lock_path))
                if acquired.exit_code == 0:
                    release_ok = True
                    break
                time.sleep(2.0)
            self.report_subtest(
                "lock-release",
                passed=release_ok,
                message=(
                    "Pod B acquired the lock after pod A was deleted"
                    if release_ok
                    else f"Pod B could not acquire the lock within {release_timeout}s after pod A was deleted"
                ),
            )

            if contention_ok and release_ok:
                self.set_passed("flock locking enforced across pods on the shared PVC")
            else:
                self.set_failed("Cross-pod flock locking did not behave correctly; see subtest details")
        finally:
            self._cleanup_namespace(created)


# --------------------------------------------------------------------------
# Cross-node write visibility.
# --------------------------------------------------------------------------


class K8sCrossNodeWriteVisibilityCheck(_K8sCrossNodeCheck):
    """Verify a file written on node A is visible+correct on node B.

    Pod A (node A) writes a unique payload to a file on the shared PVC; pod B
    (node B) reads it back. Passes when the content matches within
    ``visibility_window_s``.

    Config keys (with defaults):
        shared_fs_storage_class / nfs_storage_class: RWX StorageClass; skipped
            when neither (nor the env fallbacks) is set.
        pvc_size: PVC request size (default: ``1Gi``).
        bind_timeout_s: Max wait for PVC bind / pod Ready (default: 180).
        visibility_window_s: Max seconds for the write to become visible on
            node B. The spec target is 1s; defaults to 5.0 to absorb
            ``kubectl exec`` round-trip overhead - tighten per vendor SLA.
        namespace_prefix: Ephemeral namespace prefix (default:
            ``isvtest-fs-vis``).
        timeout: Per-command timeout (default: 300).
    """

    description: ClassVar[str] = "Verify a file written from a pod on node A is readable+correct from node B."
    timeout: ClassVar[int] = 300

    _DEFAULT_NS_PREFIX = "isvtest-fs-vis"

    def run(self) -> None:
        self._setup_kubectl()
        sc = self._resolve_shared_sc()
        if not sc:
            self.set_passed("Skipped: no shared-fs/nfs StorageClass configured")
            return

        nodes = self._two_nodes()
        if nodes is None:
            return

        bind_timeout = int(self.config.get("bind_timeout_s", self._DEFAULT_BIND_TIMEOUT_S))
        pvc_size = str(self.config.get("pvc_size", self._DEFAULT_PVC_SIZE))
        window_s = float(self.config.get("visibility_window_s", 5.0))
        file_path = f"{_DATA_DIR}/visfile"

        created = False
        try:
            created = self._create_namespace()
            if not created:
                return

            suffix = uuid.uuid4().hex[:6]
            pvc_name = f"fs-vis-{suffix}"
            pod_a = f"fs-vis-writer-{suffix}"
            pod_b = f"fs-vis-reader-{suffix}"

            ok = self._provision(pvc_name, sc, pvc_size, pod_a, pod_b, nodes, bind_timeout)
            if not ok:
                return

            payload = uuid.uuid4().hex
            write = self._exec(pod_a, write_payload_cmd(file_path, payload))
            if write.exit_code != 0:
                self.set_failed(f"Write from node {nodes[0]!r} failed: {_fmt_err(write.stderr or write.stdout)}")
                return

            start = time.time()
            elapsed = 0.0
            visible = False
            while True:
                read = self._exec(pod_b, read_file_cmd(file_path))
                elapsed = time.time() - start
                if read.exit_code == 0 and read.stdout.strip() == payload:
                    visible = True
                    break
                if elapsed >= window_s:
                    break
                time.sleep(0.1)

            if visible:
                self.set_passed(
                    f"File written on node {nodes[0]!r} was readable with correct content on node {nodes[1]!r} "
                    f"in {elapsed:.2f}s (window {window_s:.2f}s)"
                )
            else:
                self.set_failed(
                    f"File written on node {nodes[0]!r} was not visible with correct content on node {nodes[1]!r} "
                    f"within {window_s:.2f}s"
                )
        finally:
            self._cleanup_namespace(created)


# --------------------------------------------------------------------------
# Cross-node attribute consistency.
# --------------------------------------------------------------------------


class K8sCrossNodeAttrConsistencyCheck(_K8sCrossNodeCheck):
    """Verify extending a file on node A is reflected in stat on node B.

    Pod A (node A) creates then extends a file; pod B (node B) ``stat``s it and
    must observe the updated size and mtime within ``attr_cache_window_s``
    (the vendor-documented attribute-cache window).

    Config keys (with defaults):
        shared_fs_storage_class / nfs_storage_class: RWX StorageClass; skipped
            when neither (nor the env fallbacks) is set.
        pvc_size: PVC request size (default: ``1Gi``).
        bind_timeout_s: Max wait for PVC bind / pod Ready (default: 180).
        attr_cache_window_s: Max seconds for size+mtime to propagate to node B
            (default: 30.0).
        extend_bytes: Bytes appended on node A to grow the file (default: 1024).
        namespace_prefix: Ephemeral namespace prefix (default:
            ``isvtest-fs-attr``).
        timeout: Per-command timeout (default: 300).
    """

    description: ClassVar[str] = "Verify extending a file on node A is reflected in stat size+mtime on node B."
    timeout: ClassVar[int] = 300

    _DEFAULT_NS_PREFIX = "isvtest-fs-attr"

    def run(self) -> None:
        self._setup_kubectl()
        sc = self._resolve_shared_sc()
        if not sc:
            self.set_passed("Skipped: no shared-fs/nfs StorageClass configured")
            return

        nodes = self._two_nodes()
        if nodes is None:
            return

        bind_timeout = int(self.config.get("bind_timeout_s", self._DEFAULT_BIND_TIMEOUT_S))
        pvc_size = str(self.config.get("pvc_size", self._DEFAULT_PVC_SIZE))
        window_s = float(self.config.get("attr_cache_window_s", 30.0))
        extend_bytes = int(self.config.get("extend_bytes", 1024))
        file_path = f"{_DATA_DIR}/attrfile"

        created = False
        try:
            created = self._create_namespace()
            if not created:
                return

            suffix = uuid.uuid4().hex[:6]
            pvc_name = f"fs-attr-{suffix}"
            pod_a = f"fs-attr-writer-{suffix}"
            pod_b = f"fs-attr-reader-{suffix}"

            if not self._provision(pvc_name, sc, pvc_size, pod_a, pod_b, nodes, bind_timeout):
                return

            # Initial small file, then prime node B's attribute cache by
            # stat-ing it before the extend.
            init = self._exec(pod_a, write_payload_cmd(file_path, "x"))
            if init.exit_code != 0:
                self.set_failed(f"Initial write on node {nodes[0]!r} failed: {_fmt_err(init.stderr)}")
                return
            self._exec(pod_b, stat_size_mtime_cmd(file_path))

            # Ensure mtime can advance by at least a whole second (stat %Y is
            # second-granular), then extend the file on node A.
            time.sleep(1.1)
            extend = self._exec(pod_a, append_payload_cmd(file_path, "x" * extend_bytes))
            if extend.exit_code != 0:
                self.set_failed(f"Extend on node {nodes[0]!r} failed: {_fmt_err(extend.stderr)}")
                return

            src = self._exec(pod_a, stat_size_mtime_cmd(file_path))
            expected = self._parse_stat(src)
            if expected is None:
                self.set_failed(f"Could not parse stat output on node {nodes[0]!r}: {_fmt_err(src.stdout)!r}")
                return
            expected_size, expected_mtime = expected

            start = time.time()
            elapsed = 0.0
            consistent = False
            observed: tuple[int, int] | None = None
            while True:
                dst = self._exec(pod_b, stat_size_mtime_cmd(file_path))
                observed = self._parse_stat(dst)
                elapsed = time.time() - start
                if observed is not None and observed[0] == expected_size and observed[1] >= expected_mtime:
                    consistent = True
                    break
                if elapsed >= window_s:
                    break
                time.sleep(0.2)

            if consistent:
                self.set_passed(
                    f"stat on node {nodes[1]!r} reflected size={expected_size} and updated mtime "
                    f"in {elapsed:.2f}s (window {window_s:.2f}s)"
                )
            else:
                self.set_failed(
                    f"stat on node {nodes[1]!r} did not reflect size={expected_size}/mtime>={expected_mtime} "
                    f"within {window_s:.2f}s (last observed {observed!r})"
                )
        finally:
            self._cleanup_namespace(created)

    @staticmethod
    def _parse_stat(result: CommandResult) -> tuple[int, int] | None:
        """Parse ``"<size> <mtime>"`` stat output into ``(size, mtime)``."""
        if result.exit_code != 0:
            return None
        parts = result.stdout.split()
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None


# --------------------------------------------------------------------------
# Large directory listing.
# --------------------------------------------------------------------------


class _K8sLargeDirListingBase(_K8sSharedFsCheck):
    """Create many entries in one directory and list them without truncation.

    Subclasses set :attr:`_ENTRY_KIND` (``files`` / ``dirs``), the config key
    for the count, its default, and the creation snippet.
    """

    timeout: ClassVar[int] = 3600

    _DEFAULT_PVC_SIZE = "10Gi"
    _DEFAULT_BIND_TIMEOUT_S = 300
    _ENTRY_KIND: ClassVar[str] = ""
    _COUNT_KEY: ClassVar[str] = ""
    _DEFAULT_COUNT: ClassVar[int] = 0

    @abstractmethod
    def _create_cmd(self, directory: str, count: int) -> str:
        """Return the shell snippet that creates ``count`` entries under ``directory``."""

    def run(self) -> None:
        self._setup_kubectl()
        sc = self._resolve_shared_sc()
        if not sc:
            self.set_passed("Skipped: no shared-fs/nfs StorageClass configured")
            return

        count = self._parse_positive_int(self._COUNT_KEY, default=self._DEFAULT_COUNT)
        if count is None:
            return

        bind_timeout = int(self.config.get("bind_timeout_s", self._DEFAULT_BIND_TIMEOUT_S))
        pvc_size = str(self.config.get("pvc_size", self._DEFAULT_PVC_SIZE))
        target_dir = f"{_DATA_DIR}/bigdir"

        created = False
        try:
            created = self._create_namespace()
            if not created:
                return

            suffix = uuid.uuid4().hex[:6]
            pvc_name = f"fs-bigdir-{suffix}"
            pod = f"fs-bigdir-{suffix}"

            rc, err = self._apply_pvc(pvc_name, sc, pvc_size)
            if rc != 0:
                self.set_failed(f"kubectl apply failed for PVC {pvc_name!r}: {_fmt_err(err)}")
                return
            if not self._wait_pvc_bound(pvc_name, bind_timeout):
                self.set_failed(f"PVC {pvc_name!r} did not reach Bound within {bind_timeout}s")
                return
            rc, err = self._apply_pod(pod, pvc_name)
            if rc != 0:
                self.set_failed(f"kubectl apply failed for pod {pod!r}: {_fmt_err(err)}")
                return
            ready, wait_err = self._wait_ready(pod, bind_timeout)
            if not ready:
                self.set_failed(f"Pod {pod!r} did not become Ready within {bind_timeout}s: {_fmt_err(wait_err)}")
                return

            create = self._exec(pod, self._create_cmd(target_dir, count), timeout=self.timeout)
            if create.exit_code != 0:
                self.set_failed(
                    f"Creating {count} {self._ENTRY_KIND} failed: "
                    f"{_fmt_err(create.stderr or create.stdout)}"
                )
                return

            listing = self._exec(pod, list_dir_quiet_cmd(target_dir), timeout=self.timeout)
            if listing.exit_code != 0:
                self.set_failed(
                    f"ls of directory with {count} {self._ENTRY_KIND} errored: "
                    f"{_fmt_err(listing.stderr or listing.stdout)}"
                )
                return

            counted = self._exec(pod, count_entries_cmd(target_dir), timeout=self.timeout)
            if counted.exit_code != 0:
                self.set_failed(f"Counting entries failed: {_fmt_err(counted.stderr)}")
                return
            try:
                observed = int(counted.stdout.strip())
            except ValueError:
                self.set_failed(f"Could not parse entry count: {_fmt_err(counted.stdout)!r}")
                return

            if observed == count:
                self.set_passed(f"Listed all {count} {self._ENTRY_KIND} without error or truncation")
            else:
                self.set_failed(
                    f"Expected {count} {self._ENTRY_KIND} but listing found {observed} (possible truncation)"
                )
        finally:
            self._cleanup_namespace(created)


class K8sLargeDirListingFilesCheck(_K8sLargeDirListingBase):
    """List a directory holding a very large number of files.

    Config keys (with defaults):
        files_count: Number of files to create (default: 1,000,000).
        shared_fs_storage_class / nfs_storage_class: RWX StorageClass; skipped
            when neither (nor the env fallbacks) is set.
        pvc_size: PVC request size (default: ``10Gi``).
        bind_timeout_s: Max wait for PVC bind / pod Ready (default: 300).
        namespace_prefix: Ephemeral namespace prefix (default:
            ``isvtest-fs-bigdir``).
        timeout: Per-command timeout, also bounds creation (default: 3600).
    """

    description: ClassVar[str] = "Create a large number of files in one directory and list them without truncation."
    _DEFAULT_NS_PREFIX = "isvtest-fs-bigdir"
    _ENTRY_KIND = "files"
    _COUNT_KEY = "files_count"
    _DEFAULT_COUNT = 1_000_000

    def _create_cmd(self, directory: str, count: int) -> str:
        return create_files_cmd(directory, count, prefix="f")


class K8sLargeDirListingDirsCheck(_K8sLargeDirListingBase):
    """List a directory holding a very large number of subdirectories.

    Config keys (with defaults):
        dirs_count: Number of subdirectories to create (default: 500,000).
        shared_fs_storage_class / nfs_storage_class: RWX StorageClass; skipped
            when neither (nor the env fallbacks) is set.
        pvc_size: PVC request size (default: ``10Gi``).
        bind_timeout_s: Max wait for PVC bind / pod Ready (default: 300).
        namespace_prefix: Ephemeral namespace prefix (default:
            ``isvtest-fs-bigdir``).
        timeout: Per-command timeout, also bounds creation (default: 3600).
    """

    description: ClassVar[str] = (
        "Create a large number of subdirectories in one directory and list them without truncation."
    )
    _DEFAULT_NS_PREFIX = "isvtest-fs-bigdir"
    _ENTRY_KIND = "subdirectories"
    _COUNT_KEY = "dirs_count"
    _DEFAULT_COUNT = 500_000

    def _create_cmd(self, directory: str, count: int) -> str:
        return create_dirs_cmd(directory, count, prefix="d")


# --------------------------------------------------------------------------
# POSIX compliance (pjdfstest).
# --------------------------------------------------------------------------


class K8sPosixComplianceCheck(_K8sSharedFsCheck):
    """Run the upstream pjdfstest POSIX suite against the filesystem storage.

    pjdfstest (https://github.com/pjd/pjdfstest) exercises POSIX system-call
    semantics (chmod/chown/link/rename/mknod/truncate/...). This check
    provisions a PVC on the filesystem StorageClass, mounts it at ``/data`` in
    a single root pod, copies the vendored pjdfstest source into the pod,
    builds the ``pjdfstest`` helper, and runs ``prove`` against the
    mounted volume.

    pjdfstest must run as root, so the probe pod is privileged. On clusters
    that enforce a restrictive Pod Security Standard the pod is rejected and
    the check skips (passes with a skip message) rather than failing.

    Config keys (with defaults):
        shared_fs_storage_class / nfs_storage_class: filesystem StorageClass;
            the check is skipped when neither (nor the env fallbacks) is set.
        image: probe/toolchain image (default ``gcc:12``). Must provide
            cc/make/autoconf/automake, perl (``prove``) and tar.
        pvc_size: PVC request size (default ``5Gi``).
        bind_timeout_s: Max wait for PVC bind / pod Ready (default 300).
        build_timeout_s: Max wait for autoreconf+configure+make (default 600).
        tests_subdir: Optional ``tests/`` subdirectory to scope the run to a
            subset (e.g. ``chmod``); default runs the full suite.
        node_selector / tolerations: optional pod scheduling controls.
        timeout: Per-command timeout, also bounds the prove run (default 3600).
    """

    description: ClassVar[str] = (
        "Run the pjdfstest POSIX filesystem test suite against the cluster filesystem storage."
    )
    timeout: ClassVar[int] = 3600

    _DEFAULT_NS_PREFIX = "isvtest-fs-posix"
    _DEFAULT_PVC_SIZE = "5Gi"
    _DEFAULT_BIND_TIMEOUT_S = 300

    @staticmethod
    def _is_podsecurity_denial(text: str) -> bool:
        """Return True when ``text`` looks like a Pod Security admission rejection."""
        low = (text or "").lower()
        if "podsecurity" in low or "violates" in low:
            return True
        return "privileged" in low and ("forbidden" in low or "not allowed" in low or "denied" in low)

    def _apply_posix_pod(self, name: str, pvc_name: str) -> tuple[int, str]:
        image = str(self.config.get("image") or _DEFAULT_BUILD_IMAGE)
        node_selector = self._node_selector() or None
        tolerations = self._tolerations() or None

        def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
            return _set_fs_pod_fields(
                doc,
                namespace=self._namespace,
                name=name,
                pvc_name=pvc_name,
                image=image,
                node_selector=node_selector,
                tolerations=tolerations,
                # Keep the manifest's sleep-infinity keepalive command.
                command=None,
            )

        return _apply_manifest(self._kubectl_parts, render_k8s_manifest(_PJDFSTEST_POD_MANIFEST, _mutate), self.timeout)

    def _copy_source(self, pod: str) -> CommandResult:
        """``kubectl cp`` the vendored pjdfstest tree into ``pod`` at _PJDFSTEST_DEST."""
        src = shlex.quote(str(_PJDFSTEST_SRC_DIR))
        dest = shlex.quote(f"{self._namespace}/{pod}:{_PJDFSTEST_DEST}")
        return self.run_command(f"{self._kubectl_base} cp {src} {dest} -c probe", timeout=self.timeout)

    def run(self) -> None:
        self._setup_kubectl()
        sc = self._resolve_shared_sc()
        if not sc:
            self.set_passed("Skipped: no shared-fs/nfs StorageClass configured")
            return
        if not _PJDFSTEST_SRC_DIR.is_dir():
            self.set_failed(
                f"Vendored pjdfstest source not found at {_PJDFSTEST_SRC_DIR}; "
                "run `make vendor-pjdfstest`"
            )
            return

        bind_timeout = int(self.config.get("bind_timeout_s", self._DEFAULT_BIND_TIMEOUT_S))
        pvc_size = str(self.config.get("pvc_size", self._DEFAULT_PVC_SIZE))
        build_timeout = int(self.config.get("build_timeout_s", 600))
        subdir = str(self.config.get("tests_subdir", "")).strip().strip("/")
        tests_path = f"{_PJDFSTEST_DEST}/tests" + (f"/{subdir}" if subdir else "")

        created = False
        try:
            created = self._create_namespace()
            if not created:
                return

            suffix = uuid.uuid4().hex[:6]
            pvc_name = f"fs-posix-{suffix}"
            pod = f"fs-posix-{suffix}"

            rc, err = self._apply_pvc(pvc_name, sc, pvc_size)
            if rc != 0:
                self.set_failed(f"kubectl apply failed for PVC {pvc_name!r}: {_fmt_err(err)}")
                return
            if not self._wait_pvc_bound(pvc_name, bind_timeout):
                self.set_failed(f"PVC {pvc_name!r} did not reach Bound within {bind_timeout}s")
                return

            rc, err = self._apply_posix_pod(pod, pvc_name)
            if rc != 0:
                if self._is_podsecurity_denial(err):
                    self.set_passed(
                        "Skipped: cluster Pod Security admission blocked the privileged pjdfstest "
                        f"pod (pjdfstest must run as root): {_fmt_err(err)}"
                    )
                    return
                self.set_failed(f"kubectl apply failed for pod {pod!r}: {_fmt_err(err)}")
                return

            ready, wait_err = self._wait_ready(pod, bind_timeout)
            if not ready:
                if self._is_podsecurity_denial(wait_err):
                    self.set_passed(
                        "Skipped: cluster Pod Security admission blocked the privileged pjdfstest "
                        f"pod (pjdfstest must run as root): {_fmt_err(wait_err)}"
                    )
                    return
                self.set_failed(f"Pod {pod!r} did not become Ready within {bind_timeout}s: {_fmt_err(wait_err)}")
                return

            copied = self._copy_source(pod)
            if copied.exit_code != 0:
                self.set_failed(
                    f"Copying pjdfstest source into pod failed: {_fmt_err(copied.stderr or copied.stdout)}"
                )
                return

            build = self._exec(
                pod,
                f"cd {_PJDFSTEST_DEST} && autoreconf -ifs && ./configure && make",
                timeout=build_timeout,
            )
            if build.exit_code != 0:
                build_output = f"{build.stdout}\n{build.stderr}".strip()
                last_line = next((ln for ln in reversed(build_output.splitlines()) if ln.strip()), "")
                self.set_failed(
                    f"Building pjdfstest in the probe pod failed (autoreconf/configure/make, "
                    f"exit {build.exit_code}): {_fmt_err(last_line)}",
                    output=build_output[-4000:],
                )
                return

            run_cmd = f"cd {_DATA_DIR} && prove -r {shlex.quote(tests_path)}"
            proven = self._exec(pod, run_cmd, timeout=self.timeout)
            combined = f"{proven.stdout}\n{proven.stderr}"
            result = parse_pjdfstest_output(combined)

            if result.error:
                self.set_failed(
                    f"Could not parse pjdfstest results (prove exit {proven.exit_code}): {_fmt_err(result.error)}",
                    output=combined[-2000:],
                )
                return

            # Emit a subtest per file (pass and fail) so the JUnit export is a
            # complete per-file POSIX-compliance record, not just the failures.
            failed_map = dict(result.failed_files)
            for fname in result.all_files:
                nfailed = failed_map.get(fname, 0)
                if nfailed:
                    self.report_subtest(fname, passed=False, message=f"{nfailed} POSIX subtest(s) failed")
                else:
                    self.report_subtest(fname, passed=True)

            if result.success and proven.exit_code == 0:
                self.set_passed(
                    f"pjdfstest POSIX compliance passed: {result.tests_total} tests across "
                    f"{result.files_total} files, 0 failures"
                )
            else:
                self.set_failed(
                    f"pjdfstest reported failures ({len(result.failed_files)} test file(s) with failures, "
                    f"prove result {result.result or 'FAIL'}); see subtests",
                    output=combined[-2000:],
                )
        finally:
            self._cleanup_namespace(created)
