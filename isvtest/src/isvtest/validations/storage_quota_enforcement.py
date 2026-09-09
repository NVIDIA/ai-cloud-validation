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

"""``StorageDirectoryQuotaEnforcementCheck`` - end-to-end directory-quota test.

Unlike ``StorageProviderApiCheck`` (which probes quota surfaces with sentinel ids),
this check runs real CRUD against mounted storage and proves enforcement by
writing past the hard limit:

1. Acquire a volume (provision RWX PVC + BusyBox pod, reuse ``pvc_name`` /
   ``pod_name``, or create a backend-native volume and mount its advertised
   ``Volume.mount`` locally when Kubernetes is unavailable).
2. Resolve the acquired storage to the shim ``volume_id``.
3. ``directory-quota-crud``: empty subdir → set/get/update/list/delete round-trip.
4. ``directory-quota-enforcement``: below-limit write succeeds; over-limit writes
   eventually fail with a no-space / quota error (settle delay between chunks
   for backends with async usage accounting).

Skipped when no manifest provider declares full directory-quota CRUD support,
no Kubernetes/CSI or native volume-acquisition path is available, or the native
volume does not return mount instructions.

Config keys (with defaults):
    manifest_path: Provider manifest YAML (same as StorageProviderApiCheck).
        Empty -> skipped.
    storage_class: RWX StorageClass for the probe PVC. Defaults to
        ``get_k8s_csi_shared_fs_storage_class()`` (env ``K8S_CSI_SHARED_FS_SC``).
    pvc_name / pvc_namespace: Reuse a bound PVC (namespace default ``default``).
    pod_name: Reuse a Ready pod that mounts that PVC at ``/data``; requires
        ``pvc_name``. Cleanup then only removes the probe subdir + quota.
    pvc_size: Provisioned PVC size (default ``5Gi``); must exceed
        ``enforcement_hard_bytes``.
    create_hard_bytes: Hard limit for CRUD create (default 1 GiB).
    enforcement_hard_bytes: Hard limit for enforcement + CRUD update
        (default 64 MiB).
    bind_timeout_s / write_timeout_s: PVC/pod Ready and write timeouts
        (defaults 240 / 300).
    namespace_prefix / image / timeout: Ephemeral ns prefix, BusyBox image,
        per-command timeout (defaults ``isvtest-dq`` / ``busybox:1.36`` / 600).
    volume_size_bytes: Native volume size for non-Kubernetes acquisition
        (default 1 GiB, or enough to exceed the enforcement hard limit).
"""

from __future__ import annotations

import shlex
import tempfile
import time
import uuid
from collections.abc import Callable
from typing import ClassVar

from isvtest.config.settings import get_k8s_csi_shared_fs_storage_class
from isvtest.core.k8s import (
    get_kubectl_base_shell,
    get_kubectl_command,
    is_k8s_available,
)
from isvtest.core.runners import CommandResult
from isvtest.core.storage import ManifestError, Provider, load_provider_registry
from isvtest.core.storage_provider import (
    CAP_DIRECTORY_QUOTA_DELETE,
    CAP_DIRECTORY_QUOTA_GET,
    CAP_DIRECTORY_QUOTA_LIST,
    CAP_DIRECTORY_QUOTA_SET,
    CAP_VOLUME_CREATE,
    CAP_VOLUME_DELETE,
    CreateVolumeRequest,
    DeleteDirectoryQuotaRequest,
    DeleteVolumeRequest,
    DirectoryQuota,
    GetDirectoryQuotaRequest,
    GetVolumeRequest,
    ListDirectoryQuotasRequest,
    ListVolumesRequest,
    MountSpec,
    NotFoundError,
    QuotaLimits,
    SetDirectoryQuotaRequest,
    StorageApiError,
    StorageProvider,
    Volume,
)
from isvtest.core.validation import BaseValidation
from isvtest.validations.k8s_storage import (
    _apply_mount_pod_manifest,
    _apply_pvc_manifest,
    _get_pvc_json,
    _poll_pvc_bound,
    _wait_pod_ready,
)

_MIB = 1 << 20
_DEFAULT_IMAGE = "busybox:1.36"
# Substrings that indicate an over-limit write failed for quota / ENOSPC.
_ENOSPC_TOKENS: tuple[str, ...] = ("no space left", "disk quota exceeded", "quota exceeded")
# Settle between over-limit write chunks (async usage accounting, e.g. NFS).
_ENFORCE_SETTLE_S = 4
# Poll window after set_directory_quota before treating hard-limit mismatch as fail.
_READBACK_TIMEOUT_S = 30
_READBACK_POLL_S = 2


class StorageDirectoryQuotaEnforcementCheck(BaseValidation):
    """Exercise directory-quota CRUD and prove enforcement via PVC writes.

    Reports two subtests per shim provider that declares directory quotas:

    * ``directory-quota-crud[<provider>]`` - create / get / update / get / list
      / delete / get round-trip against an empty subdirectory of a real volume,
      asserting the stored hard limit matches each write and that the record
      disappears after delete.
    * ``directory-quota-enforcement[<provider>]`` - with the hard limit set,
      a below-limit write succeeds and sustained over-limit writes are
      eventually blocked with a no-space / quota-exceeded error.

    Skipped (passed) when the manifest is unset, no provider declares full
    directory-quota CRUD, or no Kubernetes/CSI or native volume-acquisition path
    is available.
    """

    description: ClassVar[str] = "Exercise directory-quota CRUD and enforcement against a live volume"
    timeout: ClassVar[int] = 600
    labels: ClassVar[tuple[str, ...]] = ("storage", "storage_provider_api", "slow")

    def run(self) -> None:
        """Load the manifest and drive each directory-quota-capable shim provider."""
        self._pod_name = str(self.config.get("pod_name") or "")
        self._pvc_name = str(self.config.get("pvc_name") or "")
        if self._pod_name and not self._pvc_name:
            self.set_failed("pod_name requires pvc_name: the supplied pod must already mount the PVC under test")
            return

        try:
            providers = load_provider_registry(self.config)
        except ManifestError as exc:
            self.set_failed(f"Failed to load provider manifest: {exc}")
            return
        except Exception as exc:  # ShimLoadError etc. - missing env, bad shim
            self.set_failed(f"Failed to load provider shim(s): {exc}")
            return

        required_caps = (
            CAP_DIRECTORY_QUOTA_SET,
            CAP_DIRECTORY_QUOTA_GET,
            CAP_DIRECTORY_QUOTA_LIST,
            CAP_DIRECTORY_QUOTA_DELETE,
        )
        candidates = [
            p for p in providers if p.has_shim and all(p.expected_capabilities.get(cap) for cap in required_caps)
        ]
        if not candidates:
            self.set_passed(
                "Skipped: no manifest provider declares full directory-quota CRUD supported "
                f"(requires {', '.join(required_caps)})"
            )
            return

        self._storage_class = str(self.config.get("storage_class") or get_k8s_csi_shared_fs_storage_class() or "")
        self._pvc_namespace = str(self.config.get("pvc_namespace") or "default")
        self._pvc_size = str(self.config.get("pvc_size") or "5Gi")
        self._create_hard = int(self.config.get("create_hard_bytes") or (1 << 30))
        self._enforce_hard = int(self.config.get("enforcement_hard_bytes") or (64 * _MIB))
        self._volume_size = int(self.config.get("volume_size_bytes") or max(1 << 30, self._enforce_hard * 2))
        self._bind_timeout = int(self.config.get("bind_timeout_s") or 240)
        self._write_timeout = int(self.config.get("write_timeout_s") or 300)
        self._image = str(self.config.get("image") or _DEFAULT_IMAGE)
        self._ns_prefix = str(self.config.get("namespace_prefix") or "isvtest-dq")
        self._k8s_available = is_k8s_available()
        self._kubectl_parts = get_kubectl_command() if self._k8s_available else []
        self._kubectl_base = get_kubectl_base_shell() if self._k8s_available else "kubectl"

        any_failed = False
        for provider in candidates:
            if not self._exercise_provider(provider):
                any_failed = True

        if any_failed:
            self.set_failed("One or more directory-quota subtests failed; see subtest details")
        else:
            self.set_passed(
                "Directory-quota CRUD + enforcement verified for " + ", ".join(sorted(p.name for p in candidates))
            )

    # ------------------------------------------------------------------
    # Per-provider orchestration
    # ------------------------------------------------------------------

    def _exercise_provider(self, provider: Provider) -> bool:
        """Choose Kubernetes/CSI or native acquisition, then run quota subtests."""
        native_volume = _native_volume_lifecycle(provider)
        if self._k8s_available and not native_volume:
            return self._exercise_provider_k8s(provider)
        if native_volume:
            return self._exercise_provider_native(provider)
        if self._k8s_available:
            return self._exercise_provider_k8s(provider)
        self._skip_both(
            provider.name,
            "no reachable Kubernetes cluster and provider does not declare native volume.create/delete",
        )
        return True

    def _exercise_provider_k8s(self, provider: Provider) -> bool:
        """Acquire a Kubernetes PVC-backed volume, then run quota subtests."""
        tag = provider.name
        if not self._storage_class and not self._pvc_name:
            self._skip_both(tag, "no storage_class or pvc_name configured to acquire a Kubernetes volume")
            return True
        api = provider.api
        assert api is not None

        run_id = uuid.uuid4().hex[:8]
        namespace = f"{self._ns_prefix}-{run_id}"
        ns_created = False
        pvc_created = False
        pod_created = False
        pod_name = self._pod_name or f"dq-probe-{run_id}"
        subdir = ""
        volume_id = ""
        tenant_id = provider.tenant_id

        if self._pvc_name:
            namespace = self._pvc_namespace
            pvc_name = self._pvc_name
        else:
            pvc_name = f"dq-probe-{run_id}"

        try:
            if not self._pvc_name:
                if not self._create_namespace(namespace):
                    self._fail_both(tag, f"failed to create namespace {namespace!r}")
                    return False
                ns_created = True
                if not self._provision_pvc(namespace, pvc_name):
                    self._fail_both(tag, f"failed to provision PVC {pvc_name!r} on {self._storage_class!r}")
                    return False
                pvc_created = True

            if self._pod_name:
                ready, werr = _wait_pod_ready(
                    self.run_command, self._kubectl_base, namespace, pod_name, self._bind_timeout
                )
                if not ready:
                    self._fail_both(tag, f"supplied pod {pod_name!r} in namespace {namespace!r} is not Ready: {werr}")
                    return False
            else:
                if not self._launch_mount_pod(namespace, pod_name, pvc_name):
                    self._fail_both(tag, f"mount pod {pod_name!r} did not become Ready")
                    return False
                pod_created = True

            volume, verr = self._resolve_volume(api, namespace, pvc_name, tenant_id)
            if volume is None:
                self._fail_both(tag, f"could not resolve shim volume for PVC {pvc_name!r}: {verr}")
                return False
            volume_id = volume.id

            subdir = f"isvtest-dq-{run_id}"
            mk = self._exec(namespace, pod_name, f"mkdir -p /data/{shlex.quote(subdir)}")
            if mk.exit_code != 0:
                self._fail_both(tag, f"mkdir /data/{subdir} failed: {mk.stderr.strip() or mk.stdout.strip()}")
                return False

            crud_ok = self._run_crud(api, tag, volume, subdir)

            def writer(relpath: str, count_mib: int) -> CommandResult:
                return self._dd(namespace, pod_name, f"/data/{relpath}", count_mib)

            enforce_ok = self._run_enforcement(api, tag, volume, subdir, writer)
            return crud_ok and enforce_ok
        finally:
            self._cleanup(
                namespace,
                pod_name,
                pvc_name,
                subdir,
                api,
                volume_id,
                tenant_id,
                ns_created=ns_created,
                pvc_created=pvc_created,
                pod_created=pod_created,
            )

    def _exercise_provider_native(self, provider: Provider) -> bool:
        """Create and mount a backend-native volume, then run quota subtests."""
        tag = provider.name
        api = provider.api
        assert api is not None

        run_id = uuid.uuid4().hex[:8]
        volume: Volume | None = None
        volume_id = ""
        mount_dir = ""
        mounted = False
        subdir = ""
        try:
            volume = api.create_volume(
                CreateVolumeRequest(
                    size_bytes=self._volume_size,
                    volume_type=provider.volume_type,
                    tenant_id=provider.tenant_id,
                    name=f"isvtest-dq-{run_id}",
                    tags={"isvtest-run-id": run_id, "provider": tag, "test-case": "directory-quota-enforcement"},
                )
            )
            volume_id = volume.id
        except Exception as exc:
            self._fail_both(tag, f"native create_volume raised {type(exc).__name__}: {exc}")
            return False

        try:
            if volume.mount is None:
                self._skip_both(tag, "native volume created but returned no mount instructions (Volume.mount is None)")
                return True

            mount_dir = tempfile.mkdtemp(prefix=f"{self._ns_prefix}-{run_id}-")
            if not self._mount_native_volume(volume.mount, mount_dir):
                self._fail_both(tag, f"failed to mount native volume {volume_id!r} at {mount_dir!r}")
                return False
            mounted = True

            subdir = f"isvtest-dq-{run_id}"
            mk = self._exec_local(f"mkdir -p {shlex.quote(f'{mount_dir}/{subdir}')}")
            if mk.exit_code != 0:
                self._fail_both(tag, f"mkdir {mount_dir}/{subdir} failed: {mk.stderr.strip() or mk.stdout.strip()}")
                return False

            crud_ok = self._run_crud(api, tag, volume, subdir)

            def writer(relpath: str, count_mib: int) -> CommandResult:
                return self._dd_local(mount_dir, relpath, count_mib)

            enforce_ok = self._run_enforcement(api, tag, volume, subdir, writer)
            return crud_ok and enforce_ok
        finally:
            self._cleanup_native(api, volume_id, provider.tenant_id, mount_dir, mounted, subdir)

    # ------------------------------------------------------------------
    # Subtests
    # ------------------------------------------------------------------

    def _await_hard(
        self, api: StorageProvider, vol_id: str, subdir: str, expected: int, tenant_id: str | None = None
    ) -> tuple[bool, int | None]:
        """Poll ``get_directory_quota`` until hard bytes equal ``expected``.

        Returns ``(matched, last_seen)``. Treats ``NotFoundError`` as not-yet-
        published. On timeout, ``last_seen`` is whatever the backend last
        returned (including ``None``).
        """
        deadline = time.monotonic() + _READBACK_TIMEOUT_S
        last: int | None = None
        while True:
            try:
                last = _hard(
                    api.get_directory_quota(
                        GetDirectoryQuotaRequest(volume_id=vol_id, tenant_id=tenant_id, path=subdir)
                    )
                )
            except NotFoundError:
                last = None
            if last == expected:
                return True, last
            if time.monotonic() >= deadline:
                return False, last
            time.sleep(_READBACK_POLL_S)

    def _run_crud(self, api: StorageProvider, tag: str, volume: Volume, subdir: str) -> bool:
        """create -> get -> update -> get -> list -> delete -> get round-trip."""
        name = f"directory-quota-crud[{tag}]"
        vol_id = volume.id
        try:
            # create
            api.set_directory_quota(
                SetDirectoryQuotaRequest(
                    DirectoryQuota(
                        tenant_id=volume.tenant_id,
                        volume_id=vol_id,
                        path=subdir,
                        hard=QuotaLimits(bytes=self._create_hard),
                    )
                )
            )
            matched, seen = self._await_hard(api, vol_id, subdir, self._create_hard, tenant_id=volume.tenant_id)
            if not matched:
                self.report_subtest(
                    name,
                    False,
                    f"after create, hard={seen} != {self._create_hard} (re-read for {_READBACK_TIMEOUT_S}s)",
                )
                return False

            # update
            api.set_directory_quota(
                SetDirectoryQuotaRequest(
                    DirectoryQuota(
                        tenant_id=volume.tenant_id,
                        volume_id=vol_id,
                        path=subdir,
                        hard=QuotaLimits(bytes=self._enforce_hard),
                    )
                )
            )
            matched, seen = self._await_hard(api, vol_id, subdir, self._enforce_hard, tenant_id=volume.tenant_id)
            if not matched:
                self.report_subtest(
                    name,
                    False,
                    f"after update, hard={seen} != {self._enforce_hard} (re-read for {_READBACK_TIMEOUT_S}s)",
                )
                return False

            # list contains our subdir quota
            listed = api.list_directory_quotas(
                ListDirectoryQuotasRequest(volume_id=vol_id, tenant_id=volume.tenant_id)
            ).directory_quotas
            if not any(_norm(q.path) == _norm(subdir) for q in listed):
                self.report_subtest(
                    name, False, f"subdir {subdir!r} not in list_directory_quotas ({[q.path for q in listed]})"
                )
                return False
        except (StorageApiError, NotFoundError) as exc:
            self.report_subtest(name, False, f"CRUD raised {type(exc).__name__}: {exc}")
            return False

        # NOTE: delete happens after enforcement so the enforcement subtest can
        # rely on the (update-set) hard limit still being in place. We verify the
        # delete here to keep the CRUD assertions together.
        try:
            api.delete_directory_quota(
                DeleteDirectoryQuotaRequest(volume_id=vol_id, tenant_id=volume.tenant_id, path=subdir)
            )
        except (StorageApiError, NotFoundError) as exc:
            self.report_subtest(name, False, f"delete raised {type(exc).__name__}: {exc}")
            return False
        try:
            api.get_directory_quota(GetDirectoryQuotaRequest(volume_id=vol_id, tenant_id=volume.tenant_id, path=subdir))
        except NotFoundError:
            pass  # expected - the record is gone
        except StorageApiError as exc:
            self.report_subtest(name, False, f"get after delete raised {type(exc).__name__}: {exc}")
            return False
        else:
            self.report_subtest(name, False, "get after delete returned a quota (delete did not remove it)")
            return False

        self.report_subtest(
            name,
            True,
            f"create({self._create_hard}) -> update({self._enforce_hard}) -> list -> delete round-trip verified",
        )
        return True

    def _run_enforcement(
        self,
        api: StorageProvider,
        tag: str,
        volume: Volume,
        subdir: str,
        writer: Callable[[str, int], CommandResult],
    ) -> bool:
        """Set a hard limit, then prove a below-limit write passes and an above-limit write is blocked."""
        name = f"directory-quota-enforcement[{tag}]"
        vol_id = volume.id
        try:
            api.set_directory_quota(
                SetDirectoryQuotaRequest(
                    DirectoryQuota(
                        tenant_id=volume.tenant_id,
                        volume_id=vol_id,
                        path=subdir,
                        hard=QuotaLimits(bytes=self._enforce_hard),
                    )
                )
            )
        except (StorageApiError, NotFoundError) as exc:
            self.report_subtest(name, False, f"set_directory_quota raised {type(exc).__name__}: {exc}")
            return False

        hard_mib = max(1, self._enforce_hard // _MIB)
        under_mib = max(1, hard_mib // 4)

        under = writer(f"{subdir}/under", under_mib)
        if under.exit_code != 0:
            self.report_subtest(
                name,
                False,
                f"below-limit write ({under_mib} MiB, limit {hard_mib} MiB) failed: "
                f"{(under.stderr or under.stdout).strip()[:200]}",
            )
            return False

        # Keep writing hard-limit-sized chunks (with a settle delay so the
        # backend's usage accounting can refresh) until a write is refused or we
        # have written well past the limit. This tolerates backends that enforce
        # asynchronously without weakening the "must be blocked" assertion.
        chunk_mib = max(16, hard_mib)
        cap_mib = max(hard_mib * 6, hard_mib + 512)
        written_mib = under_mib
        chunk = 0
        last = under
        while written_mib < cap_mib:
            chunk += 1
            last = writer(f"{subdir}/over_{chunk}", chunk_mib)
            if last.exit_code != 0:
                break
            written_mib += chunk_mib
            time.sleep(_ENFORCE_SETTLE_S)

        blocked = last.exit_code != 0
        combined = f"{last.stdout}\n{last.stderr}".lower()
        right_reason = any(tok in combined for tok in _ENOSPC_TOKENS)
        if blocked and right_reason:
            self.report_subtest(
                name,
                True,
                f"limit {hard_mib} MiB enforced: writes blocked after ~{written_mib} MiB with a no-space/quota error",
            )
            return True
        if blocked and not right_reason:
            self.report_subtest(
                name,
                False,
                f"over-limit write failed but not with a no-space/quota error: {combined.strip()[:200]}",
            )
            return False
        self.report_subtest(
            name,
            False,
            f"wrote ~{written_mib} MiB into a {hard_mib} MiB quota without being blocked - not enforced",
        )
        return False

    # ------------------------------------------------------------------
    # Volume resolution
    # ------------------------------------------------------------------

    def _resolve_volume(
        self, api: StorageProvider, namespace: str, pvc_name: str, tenant_id: str | None
    ) -> tuple[Volume | None, str]:
        """Map the bound PVC to the shim's Volume via the PV's csi.volumeHandle."""
        payload, err = _get_pvc_json(self.run_command, self._kubectl_base, namespace, pvc_name)
        if payload is None:
            return None, f"kubectl get pvc failed: {err}"
        pv_name = str((payload.get("spec") or {}).get("volumeName") or "")
        if not pv_name:
            return None, "PVC has no spec.volumeName (not Bound?)"
        handle_res = self.run_command(
            f"{self._kubectl_base} get pv {shlex.quote(pv_name)} -o jsonpath={shlex.quote('{.spec.csi.volumeHandle}')}"
        )
        handle = handle_res.stdout.strip()
        if handle_res.exit_code != 0 or not handle:
            return None, f"could not read PV {pv_name!r} csi.volumeHandle: {handle_res.stderr.strip()}"

        # Fast path: the shim resolves a CSI volume handle directly.
        try:
            return api.get_volume(GetVolumeRequest(volume_id=handle, tenant_id=tenant_id)), ""
        except (NotFoundError, StorageApiError):
            pass
        # Fallback: match by CSI handle or absolute path across the inventory.
        try:
            volumes = api.list_volumes(ListVolumesRequest(tenant_id=tenant_id)).volumes
        except StorageApiError as exc:
            return None, f"list_volumes raised {type(exc).__name__}: {exc}"
        for vol in volumes:
            if vol.csi is not None and vol.csi.volume_handle == handle:
                return vol, ""
            if handle.endswith(str(vol.attributes.get("path") or "\x00")):
                return vol, ""
        return None, f"no shim volume matches PV handle {handle!r}"

    # ------------------------------------------------------------------
    # Kubernetes helpers
    # ------------------------------------------------------------------

    def _create_namespace(self, namespace: str) -> bool:
        res = self.run_command(f"{self._kubectl_base} create namespace {shlex.quote(namespace)}")
        return res.exit_code == 0

    def _provision_pvc(self, namespace: str, pvc_name: str) -> bool:
        rc, err = _apply_pvc_manifest(
            self._kubectl_parts, namespace, pvc_name, self._storage_class, "ReadWriteMany", self._pvc_size, self.timeout
        )
        if rc != 0:
            self.log.error("kubectl apply failed for PVC %s: %s", pvc_name, err.strip())
            return False
        return True

    def _launch_mount_pod(self, namespace: str, pod_name: str, pvc_name: str) -> bool:
        rc, err = _apply_mount_pod_manifest(self._kubectl_parts, namespace, pod_name, pvc_name, self.timeout)
        if rc != 0:
            self.log.error("kubectl apply failed for mount pod %s: %s", pod_name, err.strip())
            return False
        ready, werr = _wait_pod_ready(self.run_command, self._kubectl_base, namespace, pod_name, self._bind_timeout)
        if not ready:
            self.log.error("mount pod %s not Ready: %s", pod_name, werr)
            return False
        # For WaitForFirstConsumer classes the PVC binds only once the pod is
        # scheduled; confirm it reached Bound before proceeding.
        return _poll_pvc_bound(self.run_command, self._kubectl_base, namespace, pvc_name, 10)

    def _exec(self, namespace: str, pod_name: str, inner: str, timeout: int | None = None) -> CommandResult:
        cmd = (
            f"{self._kubectl_base} exec -n {shlex.quote(namespace)} {shlex.quote(pod_name)} "
            f"-- sh -c {shlex.quote(inner)}"
        )
        return self.run_command(cmd, timeout=timeout or self.timeout)

    def _exec_local(self, inner: str, timeout: int | None = None) -> CommandResult:
        """Run a local shell command through the validation runner."""
        return self.run_command(inner, timeout=timeout or self.timeout)

    def _dd(self, namespace: str, pod_name: str, path: str, count_mib: int) -> CommandResult:
        """Write ``count_mib`` MiB to ``path`` in the Kubernetes probe pod."""
        inner = f"dd if=/dev/zero of={shlex.quote(path)} bs=1M count={count_mib} 2>&1; ec=$?; sync; exit $ec"
        return self._exec(namespace, pod_name, inner, timeout=self._write_timeout)

    def _dd_local(self, mount_dir: str, relpath: str, count_mib: int) -> CommandResult:
        """Write ``count_mib`` MiB to a path under a native local mount."""
        path = f"{mount_dir.rstrip('/')}/{relpath.lstrip('/')}"
        inner = f"dd if=/dev/zero of={shlex.quote(path)} bs=1M count={count_mib} 2>&1; ec=$?; sync; exit $ec"
        return self._exec_local(inner, timeout=self._write_timeout)

    def _mount_native_volume(self, mount: MountSpec, mount_dir: str) -> bool:
        """Mount a backend-native volume on the validation host."""
        opts = f" -o {shlex.quote(mount.options)}" if mount.options else ""
        res = self.run_command(
            f"mount -t {shlex.quote(mount.fs_type)}{opts} {shlex.quote(mount.source)} {shlex.quote(mount_dir)}"
        )
        if res.exit_code != 0:
            self.log.error("mount failed for %s at %s: %s", mount.source, mount_dir, res.stderr or res.stdout)
            return False
        return True

    def _cleanup(
        self,
        namespace: str,
        pod_name: str,
        pvc_name: str,
        subdir: str,
        api: StorageProvider | None,
        volume_id: str,
        tenant_id: str | None = None,
        *,
        ns_created: bool,
        pvc_created: bool,
        pod_created: bool,
    ) -> None:
        try:
            if ns_created:
                self.run_command(
                    f"{self._kubectl_base} delete namespace {shlex.quote(namespace)} "
                    f"--wait=false --ignore-not-found=true"
                )
                return
            # Reused PVC/pod: drop only the probe quota and subdirectory.
            if subdir and volume_id and api is not None:
                try:
                    api.delete_directory_quota(
                        DeleteDirectoryQuotaRequest(volume_id=volume_id, tenant_id=tenant_id, path=subdir)
                    )
                except (StorageApiError, NotFoundError):
                    pass
            if subdir and pod_name:
                self._exec(namespace, pod_name, f"rm -rf /data/{shlex.quote(subdir)}")
            if pod_created:
                self.run_command(
                    f"{self._kubectl_base} delete pod {shlex.quote(pod_name)} -n {shlex.quote(namespace)} "
                    f"--wait=false --ignore-not-found=true"
                )
            if pvc_created:
                self.run_command(
                    f"{self._kubectl_base} delete pvc {shlex.quote(pvc_name)} -n {shlex.quote(namespace)} "
                    f"--wait=false --ignore-not-found=true"
                )
        except Exception as exc:  # cleanup is best-effort
            self.log.warning("cleanup failed in namespace %s: %s", namespace, exc)

    def _cleanup_native(
        self,
        api: StorageProvider | None,
        volume_id: str,
        tenant_id: str | None,
        mount_dir: str,
        mounted: bool,
        subdir: str,
    ) -> None:
        """Best-effort cleanup for a native volume-acquisition run."""
        try:
            if subdir and volume_id and api is not None:
                try:
                    api.delete_directory_quota(
                        DeleteDirectoryQuotaRequest(volume_id=volume_id, tenant_id=tenant_id, path=subdir)
                    )
                except (StorageApiError, NotFoundError):
                    pass
            if mounted and subdir:
                self._exec_local(f"rm -rf {shlex.quote(f'{mount_dir}/{subdir}')}")
            if mounted:
                self.run_command(f"umount {shlex.quote(mount_dir)}")
            if mount_dir:
                self.run_command(f"rmdir {shlex.quote(mount_dir)}")
            if volume_id and api is not None:
                api.delete_volume(DeleteVolumeRequest(volume_id=volume_id, tenant_id=tenant_id))
        except Exception as exc:  # cleanup is best-effort
            self.log.warning("native cleanup failed for volume %s: %s", volume_id, exc)

    def _fail_both(self, tag: str, message: str) -> None:
        self.report_subtest(f"directory-quota-crud[{tag}]", False, message)
        self.report_subtest(f"directory-quota-enforcement[{tag}]", False, message)

    def _skip_both(self, tag: str, message: str) -> None:
        self.report_subtest(f"directory-quota-crud[{tag}]", True, message, skipped=True)
        self.report_subtest(f"directory-quota-enforcement[{tag}]", True, message, skipped=True)


def _hard(quota: DirectoryQuota) -> int | None:
    """Return the byte hard limit from a directory-quota record."""
    return None if quota.hard is None else quota.hard.bytes


def _norm(path: str | None) -> str:
    """Normalize provider-returned paths for volume-relative comparisons."""
    return (path or "").strip("/")


def _native_volume_lifecycle(provider: Provider) -> bool:
    """Return True when the manifest declares backend-native volume lifecycle."""
    return (
        provider.capability_states.get(CAP_VOLUME_CREATE) == "native"
        and provider.capability_states.get(CAP_VOLUME_DELETE) == "native"
    )
