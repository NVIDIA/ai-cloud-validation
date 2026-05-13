# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Unit tests for ``isvtest.validations.k8s_storage``."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_storage import (
    K8sCsiProvisioningModesCheck,
    K8sCsiStorageQuotaApiCheck,
    K8sCsiStorageTypesCheck,
    K8sCsiTenantScopedCredentialsCheck,
    _find_cluster_secret_grants,
    _find_shared_cluster_marker,
    _rule_grants_unrestricted_secrets,
    _set_mount_pod_fields,
    _set_pv_fields,
    _set_pvc_fields,
    _set_resourcequota_fields,
)


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult`` with the given stdout/stderr."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failing ``CommandResult`` with the given output and exit code."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _storage_class_json(name: str = "sc") -> str:
    """Return a minimal StorageClass JSON payload."""
    return json.dumps({"kind": "StorageClass", "metadata": {"name": name}})


def _pvc_json(
    *,
    phase: str = "Bound",
    capacity: str | None = "1Gi",
    volume_name: str | None = "pv-123",
) -> str:
    """Return a minimal PVC JSON payload."""
    status: dict[str, Any] = {"phase": phase}
    if capacity is not None:
        status["capacity"] = {"storage": capacity}
    spec: dict[str, Any] = {}
    if volume_name is not None:
        spec["volumeName"] = volume_name
    return json.dumps({"kind": "PersistentVolumeClaim", "status": status, "spec": spec})


class _FakeProc:
    """Minimal stand-in for ``subprocess.CompletedProcess`` used by ``kubectl apply``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeClock:
    """Tiny fake clock - advances on each ``sleep`` call.

    Used to keep poll loops from burning real wall-clock time under mocked
    ``run_command`` / ``subprocess.run``: without it, patching only ``time.sleep``
    makes the deadline-based loops spin for ``timeout_s`` seconds of real time.
    Patch both ``time.sleep`` and ``time.time`` onto this instance's methods.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def time(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += float(seconds)


@contextmanager
def _patched_clock() -> Any:
    """Patch ``k8s_storage.time.sleep`` and ``k8s_storage.time.time`` with a ``_FakeClock``.

    Without patching ``time.time`` as well, deadline-based poll loops would
    spin for ``timeout_s`` seconds of real wall clock even when ``sleep`` is
    a no-op. Using a single fake clock for both keeps the tests in-process
    and deterministic.
    """
    clock = _FakeClock()
    with (
        patch("isvtest.validations.k8s_storage.time.sleep", side_effect=clock.sleep),
        patch("isvtest.validations.k8s_storage.time.time", side_effect=clock.time),
    ):
        yield clock


class TestSetPvcFields:
    """Tests for ``_set_pvc_fields`` - the in-memory manifest mutator."""

    def _base_doc(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "placeholder", "namespace": "placeholder"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
                "storageClassName": "placeholder",
            },
        }

    def test_overrides_all_fields(self) -> None:
        doc = self._base_doc()
        out = _set_pvc_fields(doc, namespace="ns1", name="p1", sc="gp3", mode="ReadWriteMany", size="5Gi")
        assert out["metadata"]["namespace"] == "ns1"
        assert out["metadata"]["name"] == "p1"
        assert out["spec"]["storageClassName"] == "gp3"
        assert out["spec"]["accessModes"] == ["ReadWriteMany"]
        assert out["spec"]["resources"]["requests"]["storage"] == "5Gi"

    def test_mutation_is_in_place(self) -> None:
        doc = self._base_doc()
        out = _set_pvc_fields(doc, namespace="ns", name="p", sc="sc", mode="ReadWriteOnce", size="1Gi")
        assert out is doc

    def test_missing_sections_are_created(self) -> None:
        out = _set_pvc_fields({}, namespace="ns", name="p", sc="sc", mode="ReadWriteOnce", size="1Gi")
        assert out["metadata"]["namespace"] == "ns"
        assert out["spec"]["storageClassName"] == "sc"
        assert out["spec"]["resources"]["requests"]["storage"] == "1Gi"


class TestK8sCsiStorageTypesCheck:
    """Tests for ``K8sCsiStorageTypesCheck``."""

    def _make(self, config: dict[str, Any] | None = None) -> K8sCsiStorageTypesCheck:
        return K8sCsiStorageTypesCheck(config=config or {})

    def _stub_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolate from developer env: never leak a StorageClass from the host.
        for var in ("K8S_CSI_BLOCK_SC", "K8S_CSI_SHARED_FS_SC", "K8S_CSI_NFS_SC"):
            monkeypatch.delenv(var, raising=False)

    def test_no_storage_classes_configured_skips_without_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed
        assert "Skipped" in check._output
        assert "no StorageClass" in check._output

    def test_all_configured_storage_classes_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(
            {
                "block_storage_class": "gp3",
                "shared_fs_storage_class": "efs-sc",
                "nfs_storage_class": "efs-sc",
                "bind_timeout_s": 5,
                "namespace_prefix": "ut",
            }
        )

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout=_storage_class_json("x"))
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd:
                return _ok(stdout=_pvc_json())
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        for t in ("block", "shared-fs", "nfs"):
            assert outcomes[f"sc-exists[{t}]"]["passed"]
            assert outcomes[f"pvc-binds[{t}]"]["passed"]

    def test_only_block_configured_skips_others(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(
            {
                "block_storage_class": "gp3",
                "bind_timeout_s": 5,
                "namespace_prefix": "ut",
            }
        )

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout=_storage_class_json("gp3"))
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd:
                return _ok(stdout=_pvc_json())
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["sc-exists[block]"]["passed"] and not outcomes["sc-exists[block]"]["skipped"]
        assert outcomes["pvc-binds[block]"]["passed"] and not outcomes["pvc-binds[block]"]["skipped"]
        for t in ("shared-fs", "nfs"):
            assert outcomes[f"sc-exists[{t}]"]["skipped"]
            assert outcomes[f"pvc-binds[{t}]"]["skipped"]

    def test_missing_storageclass_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"block_storage_class": "nope", "bind_timeout_s": 5, "namespace_prefix": "ut"})

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _fail(stderr='Error from server (NotFound): storageclasses.storage.k8s.io "nope" not found')
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["sc-exists[block]"]["passed"]
        # Paired PVC subtest must be marked skipped so the failure isn't double-counted.
        assert outcomes["pvc-binds[block]"]["skipped"]

    def test_invalid_storageclass_json_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"block_storage_class": "gp3", "bind_timeout_s": 5, "namespace_prefix": "ut"})

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout="not-json")
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(check, "run_command", side_effect=fake_run):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["sc-exists[block]"]["passed"]
        assert "Failed to parse StorageClass" in outcomes["sc-exists[block]"]["message"]

    def test_pvc_never_binds_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"block_storage_class": "gp3", "bind_timeout_s": 1, "namespace_prefix": "ut"})

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout=_storage_class_json("gp3"))
            # Consumer pod never reaches Ready because the PVC stays Pending
            # under WaitForFirstConsumer with no provisioner available.
            if "wait --for=condition=Ready" in cmd:
                return _fail(stderr="timed out waiting for the condition")
            if "get pvc" in cmd:
                return _ok(stdout=_pvc_json(phase="Pending"))
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["sc-exists[block]"]["passed"]
        assert not outcomes["pvc-binds[block]"]["passed"]
        assert "did not reach Bound" in outcomes["pvc-binds[block]"]["message"]

    def test_pvc_apply_failure_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"block_storage_class": "gp3", "bind_timeout_s": 5, "namespace_prefix": "ut"})

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout=_storage_class_json("gp3"))
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch(
                "isvtest.validations.k8s_storage.subprocess.run",
                return_value=_FakeProc(returncode=1, stderr="admission denied"),
            ),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["pvc-binds[block]"]["passed"]
        assert "kubectl apply failed" in outcomes["pvc-binds[block]"]["message"]

    def test_kubectl_apply_timeout_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"block_storage_class": "gp3", "bind_timeout_s": 5, "namespace_prefix": "ut"})

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout=_storage_class_json("gp3"))
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch(
                "isvtest.validations.k8s_storage.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=1),
            ),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["pvc-binds[block]"]["passed"]

    def test_namespace_create_failure_sets_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"block_storage_class": "gp3"})

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _fail(stderr="forbidden")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(check, "run_command", side_effect=fake_run):
            check.run()

        assert not check.passed
        assert "Failed to create namespace" in check._error

    def test_env_fallback_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        monkeypatch.setenv("K8S_CSI_BLOCK_SC", "gp3-from-env")
        check = self._make({"bind_timeout_s": 5, "namespace_prefix": "ut"})

        seen: list[str] = []

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            seen.append(cmd)
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout=_storage_class_json("gp3-from-env"))
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd:
                return _ok(stdout=_pvc_json())
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert check.passed
        assert any("gp3-from-env" in c for c in seen)

    def test_rendered_manifest_is_valid_yaml_with_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end check that the applied manifest parses and carries the expected fields."""
        self._stub_env(monkeypatch)
        check = self._make(
            {
                "shared_fs_storage_class": "efs-sc",
                "pvc_size": "3Gi",
                "bind_timeout_s": 5,
                "namespace_prefix": "ut",
            }
        )

        captured: list[str] = []

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get storageclass" in cmd:
                return _ok(stdout=_storage_class_json("efs-sc"))
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd:
                return _ok(stdout=_pvc_json())
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        def capture_apply(cmd: list[str], **kwargs: Any) -> _FakeProc:
            captured.append(kwargs.get("input", ""))
            return _FakeProc(returncode=0)

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=capture_apply),
            _patched_clock(),
        ):
            check.run()

        assert check.passed
        # Two applies per storage type (PVC, mount pod); find the PVC doc.
        all_docs = [d for rendered in captured for d in yaml.safe_load_all(rendered) if d]
        pvcs = [d for d in all_docs if d.get("kind") == "PersistentVolumeClaim"]
        assert len(pvcs) == 1, f"expected exactly one rendered PVC, got {len(pvcs)}"
        pvc = pvcs[0]
        assert pvc["spec"]["storageClassName"] == "efs-sc"
        assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
        assert pvc["spec"]["resources"]["requests"]["storage"] == "3Gi"
        assert pvc["metadata"]["namespace"].startswith("ut-")


class TestSetResourceQuotaFields:
    """Tests for ``_set_resourcequota_fields`` - the in-memory manifest mutator."""

    def _base_doc(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "placeholder", "namespace": "placeholder"},
            "spec": {"hard": {"requests.storage": "10Gi"}},
        }

    def test_overrides_all_fields(self) -> None:
        doc = self._base_doc()
        out = _set_resourcequota_fields(
            doc,
            namespace="ns1",
            name="rq1",
            total_quota="20Gi",
            sc_quota_key="gp3.storageclass.storage.k8s.io/requests.storage",
            per_sc_quota="5Gi",
        )
        assert out["metadata"]["namespace"] == "ns1"
        assert out["metadata"]["name"] == "rq1"
        assert out["spec"]["hard"] == {
            "requests.storage": "20Gi",
            "gp3.storageclass.storage.k8s.io/requests.storage": "5Gi",
        }

    def test_mutation_is_in_place(self) -> None:
        doc = self._base_doc()
        out = _set_resourcequota_fields(
            doc,
            namespace="ns",
            name="rq",
            total_quota="10Gi",
            sc_quota_key="sc.storageclass.storage.k8s.io/requests.storage",
            per_sc_quota="5Gi",
        )
        assert out is doc

    def test_hard_replaces_placeholder(self) -> None:
        """The mutator must *replace* spec.hard rather than merge so placeholder keys don't leak."""
        doc = self._base_doc()
        # Add a stale key that must be dropped.
        doc["spec"]["hard"]["legacy.storageclass.storage.k8s.io/requests.storage"] = "1Gi"
        out = _set_resourcequota_fields(
            doc,
            namespace="ns",
            name="rq",
            total_quota="10Gi",
            sc_quota_key="gp3.storageclass.storage.k8s.io/requests.storage",
            per_sc_quota="5Gi",
        )
        assert "legacy.storageclass.storage.k8s.io/requests.storage" not in out["spec"]["hard"]


class TestK8sCsiStorageQuotaApiCheck:
    """Tests for ``K8sCsiStorageQuotaApiCheck``."""

    def _make(self, config: dict[str, Any] | None = None) -> K8sCsiStorageQuotaApiCheck:
        return K8sCsiStorageQuotaApiCheck(config=config or {})

    def _stub_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolate from developer env: never leak a StorageClass from the host.
        for var in ("K8S_CSI_BLOCK_SC", "K8S_CSI_SHARED_FS_SC", "K8S_CSI_NFS_SC"):
            monkeypatch.delenv(var, raising=False)

    def _happy_config(self) -> dict[str, Any]:
        return {
            "storage_class": "gp3",
            "total_quota": "10Gi",
            "per_sc_quota": "5Gi",
            "pvc_request": "1Gi",
            "over_quota_request": "100Gi",
            "bind_timeout_s": 5,
            "quota_settle_s": 5,
            "namespace_prefix": "ut",
        }

    def _quota_json(
        self,
        *,
        sc_key: str,
        hard: dict[str, str] | None,
        used: dict[str, str] | None,
    ) -> str:
        """Build a ``kubectl get resourcequota -o json`` payload with the given status sections."""
        status: dict[str, Any] = {}
        if hard is not None:
            status["hard"] = hard
        if used is not None:
            status["used"] = used
        return json.dumps({"metadata": {"name": "rq"}, "status": status})

    def _pv_json(
        self,
        *,
        claim_ref_name: str = "usage-pvc",
        capacity: str | None = "1Gi",
        csi_driver: str | None = "ebs.csi.aws.com",
    ) -> str:
        """Build a ``kubectl get pv -o json`` payload with tunable fields."""
        spec: dict[str, Any] = {}
        if capacity is not None:
            spec["capacity"] = {"storage": capacity}
        spec["claimRef"] = {"name": claim_ref_name}
        if csi_driver is not None:
            spec["csi"] = {"driver": csi_driver}
        return json.dumps({"kind": "PersistentVolume", "spec": spec})

    @staticmethod
    def _kind_from_input(manifest_yaml: str) -> str:
        """Peek at the first non-empty doc of a manifest and return its ``kind``."""
        for doc in yaml.safe_load_all(manifest_yaml):
            if doc:
                return str(doc.get("kind", ""))
        return ""

    def _apply_router(
        self,
        *,
        rq_rc: int = 0,
        rq_err: str = "",
        usage_rc: int = 0,
        usage_err: str = "",
        over_rc: int = 1,
        over_err: str = 'error: persistentvolumeclaims "foo" is forbidden: exceeded quota',
    ) -> Any:
        """Return a ``subprocess.run`` side_effect that routes by manifest kind/name.

        The happy path returns success for ResourceQuota and usage-PVC apply and
        a forbidden/exceeded-quota rejection for the over-quota PVC apply. Any
        knob can be tweaked to simulate a specific failure.
        """

        def _route(cmd: list[str], **kwargs: Any) -> _FakeProc:
            manifest = kwargs.get("input", "") or ""
            kind = self._kind_from_input(manifest)
            if kind == "ResourceQuota":
                return _FakeProc(returncode=rq_rc, stderr=rq_err)
            if kind == "Pod":
                # Consumer pod applied alongside the usage PVC so Bound can
                # happen under WaitForFirstConsumer. Success by default.
                return _FakeProc(returncode=0)
            if kind == "PersistentVolumeClaim":
                # The usage PVC is applied before the over-quota PVC, so track order.
                # We distinguish by name via the metadata. The over-quota PVC name
                # starts with "quota-over-"; usage PVC name starts with "quota-usage-".
                for doc in yaml.safe_load_all(manifest):
                    if not doc:
                        continue
                    name = doc.get("metadata", {}).get("name", "")
                    if name.startswith("quota-over-"):
                        return _FakeProc(returncode=over_rc, stderr=over_err)
                    return _FakeProc(returncode=usage_rc, stderr=usage_err)
            raise AssertionError(f"unexpected manifest kind={kind!r}")

        return _route

    def _run_command_router(
        self,
        *,
        quota_hard: dict[str, str] | None,
        quota_used: dict[str, str] | None,
        pvc_phase: str = "Bound",
        pvc_capacity: str = "1Gi",
        volume_name: str = "pv-123",
        pv_payload: str | None = None,
        sc_key: str = "gp3.storageclass.storage.k8s.io/requests.storage",
    ) -> Any:
        """Build a ``run_command`` side_effect that answers every query the check issues."""
        pv_json = pv_payload if pv_payload is not None else self._pv_json()

        def _route(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get resourcequota" in cmd and "-o json" in cmd:
                return _ok(stdout=self._quota_json(sc_key=sc_key, hard=quota_hard, used=quota_used))
            if "wait --for=condition=Ready" in cmd:
                # Consumer pod reaches Ready whenever the PVC phase is Bound;
                # when the PVC stays Pending, the wait fails so the check
                # reports the timeout correctly.
                return _ok() if pvc_phase == "Bound" else _fail(stderr="timed out waiting for the condition")
            if "get pvc" in cmd and "-o json" in cmd:
                return _ok(stdout=_pvc_json(phase=pvc_phase, capacity=pvc_capacity, volume_name=volume_name))
            if "get pv " in cmd and "-o json" in cmd:
                return _ok(stdout=pv_json)
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        return _route

    # ----- tests -----

    def test_no_storage_class_configured_skips_without_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed
        assert "Skipped" in check._output

    def test_happy_path_all_subtests_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"
        run = self._run_command_router(
            quota_hard={"requests.storage": "10Gi", sc_key: "5Gi"},
            quota_used={"requests.storage": "1Gi", sc_key: "1Gi"},
            pv_payload=self._pv_json(),
        )

        # run_command_router's _pv_json defaults claim_ref_name to "usage-pvc" but
        # the real PVC name is generated with a UUID prefix. Patch accordingly.
        def _run_with_dynamic_pvname(cmd: str, timeout: int | None = None) -> CommandResult:
            if "get pv " in cmd and "-o json" in cmd:
                # Resolve the current usage PVC name so spec.claimRef.name matches.
                return _ok(stdout=self._pv_json(claim_ref_name=getattr(check, "_usage_pvc_name", "usage-pvc")))
            return run(cmd, timeout)

        # Capture the usage PVC name during subprocess.run (ResourceQuota is applied
        # first, then usage PVC, then consumer Pod, then over-quota PVC).
        def _capture_apply(cmd: list[str], **kwargs: Any) -> _FakeProc:
            manifest = kwargs.get("input", "") or ""
            kind = self._kind_from_input(manifest)
            if kind == "PersistentVolumeClaim":
                for doc in yaml.safe_load_all(manifest):
                    if not doc:
                        continue
                    name = doc.get("metadata", {}).get("name", "")
                    if name.startswith("quota-usage-"):
                        check._usage_pvc_name = name  # type: ignore[attr-defined]
                        return _FakeProc(returncode=0)
                    if name.startswith("quota-over-"):
                        return _FakeProc(returncode=1, stderr="forbidden: exceeded quota")
            if kind == "ResourceQuota":
                return _FakeProc(returncode=0)
            if kind == "Pod":
                return _FakeProc(returncode=0)
            raise AssertionError(f"unexpected manifest kind={kind!r}")

        with (
            patch.object(check, "run_command", side_effect=_run_with_dynamic_pvname),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=_capture_apply),
            _patched_clock(),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        for name in ("resourcequota-storage-api", "per-pvc-usage", "quota-enforcement", "pv-usage-api"):
            assert outcomes[name]["passed"], f"{name}: {outcomes[name]['message']}"
            assert not outcomes[name]["skipped"]

    def test_resourcequota_apply_failure_skips_dependents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        run = self._run_command_router(quota_hard=None, quota_used=None)

        with (
            patch.object(check, "run_command", side_effect=run),
            patch(
                "isvtest.validations.k8s_storage.subprocess.run",
                side_effect=self._apply_router(rq_rc=1, rq_err="admission denied"),
            ),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["resourcequota-storage-api"]["passed"]
        for dependent in ("per-pvc-usage", "quota-enforcement", "pv-usage-api"):
            assert outcomes[dependent]["skipped"]

    def test_resourcequota_hard_never_populates_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        # Apply succeeds, but .status.hard is never published.
        run = self._run_command_router(quota_hard=None, quota_used=None)

        with (
            patch.object(check, "run_command", side_effect=run),
            patch(
                "isvtest.validations.k8s_storage.subprocess.run",
                side_effect=self._apply_router(),
            ),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["resourcequota-storage-api"]["passed"]
        assert outcomes["per-pvc-usage"]["skipped"]

    def test_usage_pvc_apply_failure_fails_per_pvc_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"
        run = self._run_command_router(
            quota_hard={"requests.storage": "10Gi", sc_key: "5Gi"},
            quota_used=None,
        )

        with (
            patch.object(check, "run_command", side_effect=run),
            patch(
                "isvtest.validations.k8s_storage.subprocess.run",
                side_effect=self._apply_router(usage_rc=1, usage_err="boom"),
            ),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["resourcequota-storage-api"]["passed"]
        assert not outcomes["per-pvc-usage"]["passed"]
        # pv-usage-api depends on a bound usage PVC; should be skipped.
        assert outcomes["pv-usage-api"]["skipped"]

    def test_usage_pvc_never_binds_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"
        run = self._run_command_router(
            quota_hard={"requests.storage": "10Gi", sc_key: "5Gi"},
            quota_used=None,
            pvc_phase="Pending",
        )

        with (
            patch.object(check, "run_command", side_effect=run),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=self._apply_router()),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["per-pvc-usage"]["passed"]
        assert "did not reach Bound" in outcomes["per-pvc-usage"]["message"]

    def test_quota_used_never_reflects_usage_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"
        # hard is populated but used never includes the per-SC key.
        run = self._run_command_router(
            quota_hard={"requests.storage": "10Gi", sc_key: "5Gi"},
            quota_used={"requests.storage": "1Gi"},  # missing per-SC entry
        )

        with (
            patch.object(check, "run_command", side_effect=run),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=self._apply_router()),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["resourcequota-storage-api"]["passed"]
        assert not outcomes["per-pvc-usage"]["passed"]
        assert "did not reflect" in outcomes["per-pvc-usage"]["message"]

    def test_over_quota_pvc_admitted_fails_enforcement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"
        run = self._run_command_router(
            quota_hard={"requests.storage": "10Gi", sc_key: "5Gi"},
            quota_used={"requests.storage": "1Gi", sc_key: "1Gi"},
        )
        # Over-quota PVC incorrectly succeeds (returncode 0).
        with (
            patch.object(check, "run_command", side_effect=run),
            patch(
                "isvtest.validations.k8s_storage.subprocess.run",
                side_effect=self._apply_router(over_rc=0, over_err=""),
            ),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["quota-enforcement"]["passed"]
        assert "admitted" in outcomes["quota-enforcement"]["message"]

    def test_over_quota_pvc_rejected_without_quota_message_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"
        run = self._run_command_router(
            quota_hard={"requests.storage": "10Gi", sc_key: "5Gi"},
            quota_used={"requests.storage": "1Gi", sc_key: "1Gi"},
        )
        with (
            patch.object(check, "run_command", side_effect=run),
            patch(
                "isvtest.validations.k8s_storage.subprocess.run",
                side_effect=self._apply_router(over_rc=1, over_err="network error"),
            ),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["quota-enforcement"]["passed"]
        assert "did not mention quota" in outcomes["quota-enforcement"]["message"]

    @pytest.mark.parametrize(
        ("mutator_kwargs", "expected_missing"),
        [
            ({"csi_driver": None}, "spec.csi.driver"),
            ({"capacity": None}, "spec.capacity.storage"),
            ({"claim_ref_name": "other-pvc"}, "spec.claimRef.name"),
        ],
    )
    def test_pv_usage_api_detects_missing_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mutator_kwargs: dict[str, Any],
        expected_missing: str,
    ) -> None:
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"

        # We need the PV's claimRef.name to match the generated usage-PVC name on
        # the "happy" cases. To keep the parametrisation simple we let the check
        # record the usage name during apply, then rebuild the PV payload against
        # it when responding to `get pv ... -o json`.
        usage_name_holder: dict[str, str] = {}

        def _apply_side_effect(cmd: list[str], **kwargs: Any) -> _FakeProc:
            manifest = kwargs.get("input", "") or ""
            kind = self._kind_from_input(manifest)
            if kind == "ResourceQuota":
                return _FakeProc(returncode=0)
            if kind == "Pod":
                return _FakeProc(returncode=0)
            if kind == "PersistentVolumeClaim":
                for doc in yaml.safe_load_all(manifest):
                    if not doc:
                        continue
                    name = doc.get("metadata", {}).get("name", "")
                    if name.startswith("quota-usage-"):
                        usage_name_holder["name"] = name
                        return _FakeProc(returncode=0)
                    if name.startswith("quota-over-"):
                        return _FakeProc(returncode=1, stderr="forbidden: exceeded quota")
            raise AssertionError(f"unexpected kind={kind!r}")

        def _run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get resourcequota" in cmd:
                return _ok(
                    stdout=self._quota_json(
                        sc_key=sc_key,
                        hard={"requests.storage": "10Gi", sc_key: "5Gi"},
                        used={"requests.storage": "1Gi", sc_key: "1Gi"},
                    )
                )
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd and "-o json" in cmd:
                return _ok(stdout=_pvc_json(volume_name="pv-123"))
            if "get pv " in cmd and "-o json" in cmd:
                # Build PV payload; default claim ref matches usage PVC unless
                # overridden by mutator_kwargs.
                claim_name = mutator_kwargs.get("claim_ref_name", usage_name_holder.get("name", "usage-pvc"))
                capacity = mutator_kwargs.get("capacity", "1Gi") if "capacity" in mutator_kwargs else "1Gi"
                csi_driver = (
                    mutator_kwargs.get("csi_driver", "ebs.csi.aws.com")
                    if "csi_driver" in mutator_kwargs
                    else "ebs.csi.aws.com"
                )
                return _ok(
                    stdout=self._pv_json(
                        claim_ref_name=claim_name,
                        capacity=capacity,
                        csi_driver=csi_driver,
                    )
                )
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=_run),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=_apply_side_effect),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["pv-usage-api"]["passed"]
        assert expected_missing in outcomes["pv-usage-api"]["message"]

    def test_env_fallback_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        monkeypatch.setenv("K8S_CSI_BLOCK_SC", "gp3-from-env")
        sc_key = "gp3-from-env.storageclass.storage.k8s.io/requests.storage"
        # Drop storage_class so env has to provide it.
        cfg = self._happy_config()
        cfg.pop("storage_class")
        check = self._make(cfg)

        usage_name_holder: dict[str, str] = {}

        def _apply(cmd: list[str], **kwargs: Any) -> _FakeProc:
            manifest = kwargs.get("input", "") or ""
            kind = self._kind_from_input(manifest)
            if kind == "ResourceQuota":
                # Verify the per-SC key inside the ResourceQuota references the env SC.
                for doc in yaml.safe_load_all(manifest):
                    if not doc:
                        continue
                    assert sc_key in doc["spec"]["hard"]
                return _FakeProc(returncode=0)
            if kind == "Pod":
                return _FakeProc(returncode=0)
            if kind == "PersistentVolumeClaim":
                for doc in yaml.safe_load_all(manifest):
                    if not doc:
                        continue
                    name = doc.get("metadata", {}).get("name", "")
                    if name.startswith("quota-usage-"):
                        usage_name_holder["name"] = name
                        assert doc["spec"]["storageClassName"] == "gp3-from-env"
                        return _FakeProc(returncode=0)
                    if name.startswith("quota-over-"):
                        return _FakeProc(returncode=1, stderr="forbidden: exceeded quota")
            raise AssertionError(f"unexpected kind={kind!r}")

        def _run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "get resourcequota" in cmd:
                return _ok(
                    stdout=self._quota_json(
                        sc_key=sc_key,
                        hard={"requests.storage": "10Gi", sc_key: "5Gi"},
                        used={"requests.storage": "1Gi", sc_key: "1Gi"},
                    )
                )
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd and "-o json" in cmd:
                return _ok(stdout=_pvc_json(volume_name="pv-env"))
            if "get pv " in cmd:
                return _ok(
                    stdout=self._pv_json(
                        claim_ref_name=usage_name_holder.get("name", "usage-pvc"),
                    )
                )
            if "delete namespace" in cmd:
                return _ok()
            raise AssertionError(f"unexpected command: {cmd}")

        with (
            patch.object(check, "run_command", side_effect=_run),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=_apply),
            _patched_clock(),
        ):
            check.run()

        assert check.passed, check._error

    def test_namespace_create_failure_sets_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"storage_class": "gp3"})

        def _run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _fail(stderr="forbidden")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(check, "run_command", side_effect=_run):
            check.run()

        assert not check.passed
        assert "Failed to create namespace" in check._error

    def test_rendered_resourcequota_manifest_is_valid_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end check that the applied ResourceQuota carries the expected keys."""
        self._stub_env(monkeypatch)
        check = self._make(self._happy_config())
        sc_key = "gp3.storageclass.storage.k8s.io/requests.storage"

        captured: dict[str, str] = {}
        usage_name_holder: dict[str, str] = {}

        def _apply(cmd: list[str], **kwargs: Any) -> _FakeProc:
            manifest = kwargs.get("input", "") or ""
            kind = self._kind_from_input(manifest)
            if kind == "ResourceQuota":
                captured["rq"] = manifest
                return _FakeProc(returncode=0)
            if kind == "Pod":
                return _FakeProc(returncode=0)
            if kind == "PersistentVolumeClaim":
                for doc in yaml.safe_load_all(manifest):
                    if not doc:
                        continue
                    name = doc.get("metadata", {}).get("name", "")
                    if name.startswith("quota-usage-"):
                        usage_name_holder["name"] = name
                        return _FakeProc(returncode=0)
                    if name.startswith("quota-over-"):
                        return _FakeProc(returncode=1, stderr="forbidden: exceeded quota")
            raise AssertionError(f"unexpected kind={kind!r}")

        run = self._run_command_router(
            quota_hard={"requests.storage": "10Gi", sc_key: "5Gi"},
            quota_used={"requests.storage": "1Gi", sc_key: "1Gi"},
        )

        def _run_with_dynamic_pvname(cmd: str, timeout: int | None = None) -> CommandResult:
            if "get pv " in cmd and "-o json" in cmd:
                return _ok(stdout=self._pv_json(claim_ref_name=usage_name_holder.get("name", "usage-pvc")))
            return run(cmd, timeout)

        with (
            patch.object(check, "run_command", side_effect=_run_with_dynamic_pvname),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=_apply),
            _patched_clock(),
        ):
            check.run()

        assert check.passed, check._error
        rq_yaml = captured["rq"]
        docs = [d for d in yaml.safe_load_all(rq_yaml) if d]
        assert len(docs) == 1
        rq = docs[0]
        assert rq["kind"] == "ResourceQuota"
        assert rq["metadata"]["namespace"].startswith("ut-")
        assert rq["spec"]["hard"] == {"requests.storage": "10Gi", sc_key: "5Gi"}


def _items_json(items: list[dict[str, Any]]) -> str:
    """Wrap a list of objects in a ``kubectl get -o json`` ``items`` envelope."""
    return json.dumps({"items": items})


def _pod(
    *,
    name: str,
    namespace: str = "kube-system",
    images: list[str] | None = None,
    service_account: str = "default",
    volumes: list[dict[str, Any]] | None = None,
    env_from_secrets: list[str] | None = None,
    env_secret_keys: list[str] | None = None,
    projected_secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal Pod object with just the fields the check inspects."""
    containers: list[dict[str, Any]] = []
    for idx, image in enumerate(images or ["unrelated:latest"]):
        container: dict[str, Any] = {"name": f"c{idx}", "image": image}
        if env_from_secrets and idx == 0:
            container["envFrom"] = [{"secretRef": {"name": s}} for s in env_from_secrets]
        if env_secret_keys and idx == 0:
            container["env"] = [
                {"name": f"K{i}", "valueFrom": {"secretKeyRef": {"name": s, "key": "k"}}}
                for i, s in enumerate(env_secret_keys)
            ]
        containers.append(container)
    spec: dict[str, Any] = {
        "containers": containers,
        "serviceAccountName": service_account,
    }
    rendered_volumes = list(volumes or [])
    if projected_secrets:
        rendered_volumes.append(
            {
                "name": "projected",
                "projected": {"sources": [{"secret": {"name": s}} for s in projected_secrets]},
            }
        )
    if rendered_volumes:
        spec["volumes"] = rendered_volumes
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _pv_with_csi_secrets(
    *,
    name: str = "pv-1",
    driver: str = "ebs.csi.aws.com",
    secret_namespace: str | None = None,
    secret_name: str | None = None,
) -> dict[str, Any]:
    """Build a PV carrying a ``spec.csi.nodePublishSecretRef`` if name/namespace given."""
    csi: dict[str, Any] = {"driver": driver, "volumeHandle": "vol-1"}
    if secret_name and secret_namespace:
        csi["nodePublishSecretRef"] = {"name": secret_name, "namespace": secret_namespace}
    return {
        "kind": "PersistentVolume",
        "metadata": {"name": name},
        "spec": {"csi": csi, "capacity": {"storage": "1Gi"}},
    }


def _secret(
    *,
    name: str,
    namespace: str,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    if labels:
        metadata["labels"] = labels
    if annotations:
        metadata["annotations"] = annotations
    return {"kind": "Secret", "metadata": metadata, "type": "Opaque"}


def _crb(
    *,
    name: str,
    cluster_role: str,
    subject_namespace: str,
    subject_name: str,
) -> dict[str, Any]:
    return {
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name},
        "roleRef": {"kind": "ClusterRole", "name": cluster_role, "apiGroup": "rbac.authorization.k8s.io"},
        "subjects": [{"kind": "ServiceAccount", "namespace": subject_namespace, "name": subject_name}],
    }


def _cluster_role_secrets(
    *,
    name: str,
    verbs: list[str] | None = None,
    resource_names: list[str] | None = None,
    resources: list[str] | None = None,
    api_groups: list[str] | None = None,
) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "apiGroups": api_groups if api_groups is not None else [""],
        "resources": resources if resources is not None else ["secrets"],
        "verbs": verbs if verbs is not None else ["get", "list"],
    }
    if resource_names is not None:
        rule["resourceNames"] = resource_names
    return {
        "kind": "ClusterRole",
        "metadata": {"name": name},
        "rules": [rule],
    }


class TestRuleGrantsUnrestrictedSecrets:
    """Direct tests for ``_rule_grants_unrestricted_secrets``."""

    def test_unrestricted_secrets_grant_flagged(self) -> None:
        rule = {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "list"]}
        assert _rule_grants_unrestricted_secrets(rule) is True

    def test_wildcard_resource_and_verb_flagged(self) -> None:
        rule = {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}
        assert _rule_grants_unrestricted_secrets(rule) is True

    def test_resourcenames_restriction_not_flagged(self) -> None:
        rule = {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["get"],
            "resourceNames": ["csi-creds"],
        }
        assert _rule_grants_unrestricted_secrets(rule) is False

    def test_non_core_api_group_not_flagged(self) -> None:
        rule = {"apiGroups": ["custom.example.com"], "resources": ["secrets"], "verbs": ["get"]}
        assert _rule_grants_unrestricted_secrets(rule) is False

    def test_unrelated_resource_not_flagged(self) -> None:
        rule = {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]}
        assert _rule_grants_unrestricted_secrets(rule) is False

    def test_non_secret_verbs_not_flagged(self) -> None:
        rule = {"apiGroups": [""], "resources": ["secrets"], "verbs": ["impersonate"]}
        assert _rule_grants_unrestricted_secrets(rule) is False


class TestFindSharedClusterMarker:
    """Direct tests for ``_find_shared_cluster_marker``."""

    def test_matching_label_flagged(self) -> None:
        secret = _secret(name="s", namespace="kube-system", labels={"shared-across-clusters": "true"})
        marker = _find_shared_cluster_marker(secret, [("shared-across-clusters", "true")])
        assert "shared-across-clusters=true" in marker

    def test_label_with_empty_value_matches_any_value(self) -> None:
        secret = _secret(name="s", namespace="kube-system", labels={"tenant": "shared-cluster-a"})
        marker = _find_shared_cluster_marker(secret, [("tenant", "")])
        assert "tenant" in marker

    def test_shared_annotation_flagged(self) -> None:
        secret = _secret(
            name="s",
            namespace="kube-system",
            annotations={"csi.nvidia.com/shared": "true"},
        )
        assert "csi.nvidia.com/shared" in _find_shared_cluster_marker(secret, [])

    def test_clean_secret_returns_empty(self) -> None:
        secret = _secret(name="s", namespace="kube-system", labels={"app": "ebs-csi"})
        assert _find_shared_cluster_marker(secret, [("shared-across-clusters", "true")]) == ""


class TestFindClusterSecretGrants:
    """Direct tests for ``_find_cluster_secret_grants``."""

    def test_matching_cluster_role_flagged(self) -> None:
        bindings = [_crb(name="crb-1", cluster_role="cr-1", subject_namespace="kube-system", subject_name="sa-1")]
        roles = {"cr-1": _cluster_role_secrets(name="cr-1")}
        viols = _find_cluster_secret_grants(bindings, roles, {("kube-system", "sa-1")})
        assert len(viols) == 1
        assert "crb-1" in viols[0]

    def test_other_sa_not_flagged(self) -> None:
        bindings = [_crb(name="crb-1", cluster_role="cr-1", subject_namespace="default", subject_name="other")]
        roles = {"cr-1": _cluster_role_secrets(name="cr-1")}
        assert _find_cluster_secret_grants(bindings, roles, {("kube-system", "sa-1")}) == []

    def test_resourcenames_restriction_not_flagged(self) -> None:
        bindings = [_crb(name="crb-1", cluster_role="cr-1", subject_namespace="kube-system", subject_name="sa-1")]
        roles = {"cr-1": _cluster_role_secrets(name="cr-1", resource_names=["csi-creds"])}
        assert _find_cluster_secret_grants(bindings, roles, {("kube-system", "sa-1")}) == []

    def test_missing_role_is_ignored(self) -> None:
        bindings = [_crb(name="crb-1", cluster_role="cr-missing", subject_namespace="kube-system", subject_name="sa-1")]
        assert _find_cluster_secret_grants(bindings, {}, {("kube-system", "sa-1")}) == []


class TestK8sCsiTenantScopedCredentialsCheck:
    """Tests for ``K8sCsiTenantScopedCredentialsCheck``."""

    def _make(self, config: dict[str, Any] | None = None) -> K8sCsiTenantScopedCredentialsCheck:
        return K8sCsiTenantScopedCredentialsCheck(config=config or {})

    def _router(
        self,
        *,
        csi_drivers: list[dict[str, Any]],
        pods_by_ns: dict[str, list[dict[str, Any]]],
        pvs: list[dict[str, Any]],
        secrets_by_ref: dict[tuple[str, str], dict[str, Any] | None] | None = None,
        missing_secret_refs: set[tuple[str, str]] | None = None,
        cluster_role_bindings: list[dict[str, Any]] | None = None,
        cluster_roles: list[dict[str, Any]] | None = None,
        fail_on: str | None = None,
    ) -> Any:
        """Build a ``run_command`` side_effect that answers every kubectl query the check issues.

        ``fail_on`` forces a non-zero exit on the named command substring so
        we can exercise top-level error paths (e.g. ``"get csidriver"``,
        ``"get pv -o"``). ``secrets_by_ref`` maps (namespace, name) to a
        Secret object; a value of ``None`` simulates a Forbidden fetch.
        ``missing_secret_refs`` simulates a Secret that does not exist -
        with ``--ignore-not-found=true`` kubectl returns rc=0 and empty
        stdout, which maps to the check's ``"missing"`` status.
        """
        secrets_by_ref = secrets_by_ref or {}
        missing_secret_refs = missing_secret_refs or set()
        cluster_role_bindings = cluster_role_bindings or []
        cluster_roles = cluster_roles or []

        def _route(cmd: str, timeout: int | None = None) -> CommandResult:
            if fail_on and fail_on in cmd:
                return _fail(stderr="boom")
            if "get csidriver -o json" in cmd:
                return _ok(stdout=_items_json(csi_drivers))
            if "get pods -n " in cmd and "-o json" in cmd:
                # Extract the namespace from the quoted `-n '<ns>'` fragment.
                # shlex.quote renders most identifiers without quoting, so
                # fall back to a simple split on whitespace.
                parts = cmd.split()
                ns = ""
                for i, part in enumerate(parts):
                    if part == "-n" and i + 1 < len(parts):
                        ns = parts[i + 1].strip("'\"")
                        break
                return _ok(stdout=_items_json(pods_by_ns.get(ns, [])))
            if cmd.rstrip().endswith("get pv -o json"):
                return _ok(stdout=_items_json(pvs))
            if "get secret " in cmd and "-o json" in cmd:
                # Parse out `secret <name> -n <ns>`.
                parts = cmd.split()
                name = ""
                ns = ""
                for i, part in enumerate(parts):
                    if part == "secret" and i + 1 < len(parts):
                        name = parts[i + 1].strip("'\"")
                    if part == "-n" and i + 1 < len(parts):
                        ns = parts[i + 1].strip("'\"")
                if (ns, name) in missing_secret_refs:
                    # `--ignore-not-found=true` returns rc=0 + empty stdout.
                    return _ok(stdout="")
                secret = secrets_by_ref.get((ns, name))
                if secret is None and (ns, name) in secrets_by_ref:
                    return _fail(stderr="forbidden")
                if secret is None:
                    # Unknown Secret: return a minimal clean object.
                    return _ok(stdout=json.dumps(_secret(name=name, namespace=ns)))
                return _ok(stdout=json.dumps(secret))
            if "get clusterrolebinding -o json" in cmd:
                return _ok(stdout=_items_json(cluster_role_bindings))
            if "get clusterrole -o json" in cmd:
                return _ok(stdout=_items_json(cluster_roles))
            raise AssertionError(f"unexpected command: {cmd}")

        return _route

    # ----- tests -----

    def test_no_csi_drivers_skips_all_subtests(self) -> None:
        check = self._make({})
        with patch.object(check, "run_command", side_effect=self._router(csi_drivers=[], pods_by_ns={}, pvs=[])):
            check.run()

        assert check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        for name in (
            "csi-secrets-discovered",
            "secrets-not-cross-namespace",
            "no-shared-cluster-markers",
            "serviceaccount-rbac-scoped",
            "node-plugin-uses-hostpath-not-shared-mount",
        ):
            assert outcomes[name]["skipped"], name

    def test_happy_path_no_secret_refs(self) -> None:
        """Clean cluster: one CSI driver, node-plugin + controller pods with no Secret refs."""
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "ebs.csi.aws.com"}}]
        pods = [
            _pod(
                name="ebs-csi-controller-abc",
                namespace="kube-system",
                images=["public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1", "csi-provisioner:v4"],
                service_account="ebs-csi-controller-sa",
            ),
            _pod(
                name="ebs-csi-node-xyz",
                namespace="kube-system",
                images=["public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1", "csi-node-driver-registrar:v2"],
                service_account="ebs-csi-node-sa",
                volumes=[
                    {"name": "plugin-dir", "hostPath": {"path": "/var/lib/kubelet/plugins"}},
                    {"name": "scratch", "emptyDir": {}},
                ],
            ),
        ]
        pvs = [_pv_with_csi_secrets()]  # No SecretRefs.
        crbs = [
            _crb(
                name="ebs-csi-provisioner",
                cluster_role="external-provisioner",
                subject_namespace="kube-system",
                subject_name="ebs-csi-controller-sa",
            )
        ]
        # Provisioner role grants events, not unrestricted secrets.
        croles = [
            {
                "kind": "ClusterRole",
                "metadata": {"name": "external-provisioner"},
                "rules": [
                    {"apiGroups": [""], "resources": ["events"], "verbs": ["create", "patch"]},
                    {
                        "apiGroups": [""],
                        "resources": ["secrets"],
                        "verbs": ["get"],
                        "resourceNames": ["ebs-csi-creds"],
                    },
                ],
            }
        ]

        with patch.object(
            check,
            "run_command",
            side_effect=self._router(
                csi_drivers=csi_drivers,
                pods_by_ns={"kube-system": pods},
                pvs=pvs,
                cluster_role_bindings=crbs,
                cluster_roles=croles,
            ),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        for name in (
            "csi-secrets-discovered",
            "secrets-not-cross-namespace",
            "no-shared-cluster-markers",
            "serviceaccount-rbac-scoped",
            "node-plugin-uses-hostpath-not-shared-mount",
        ):
            assert outcomes[name]["passed"], f"{name}: {outcomes[name]['message']}"

    def test_cross_namespace_secret_reference_fails(self) -> None:
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "custom.csi.example.com"}}]
        pods = [
            _pod(
                name="csi-node",
                images=["csi-node-driver-registrar:v2"],
                volumes=[{"name": "plugin-dir", "hostPath": {"path": "/var/lib/kubelet/plugins"}}],
            )
        ]
        # PV references a Secret in the "default" namespace - a workload ns.
        pvs = [_pv_with_csi_secrets(secret_namespace="default", secret_name="exposed-creds")]
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(csi_drivers=csi_drivers, pods_by_ns={"kube-system": pods}, pvs=pvs),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["secrets-not-cross-namespace"]["passed"]
        assert "default/exposed-creds" in outcomes["secrets-not-cross-namespace"]["message"]

    def test_allowed_workload_namespace_accepts_secret(self) -> None:
        check = self._make({"allowed_workload_namespaces": ["csi-tenant-a"]})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "x"}}]
        pods = [_pod(name="csi-node", images=["csi-node-driver-registrar:v2"])]
        pvs = [_pv_with_csi_secrets(secret_namespace="csi-tenant-a", secret_name="tenant-creds")]
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(csi_drivers=csi_drivers, pods_by_ns={"kube-system": pods}, pvs=pvs),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["secrets-not-cross-namespace"]["passed"]

    def test_shared_cluster_label_on_secret_fails(self) -> None:
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "x"}}]
        pods = [
            _pod(
                name="csi-controller",
                images=["csi-provisioner:v4"],
                service_account="csi-controller-sa",
                env_from_secrets=["csi-creds"],
            ),
            _pod(name="csi-node", images=["csi-node-driver-registrar:v2"]),
        ]
        pvs: list[dict[str, Any]] = []
        secrets = {
            ("kube-system", "csi-creds"): _secret(
                name="csi-creds",
                namespace="kube-system",
                labels={"shared-across-clusters": "true"},
            )
        }
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(
                csi_drivers=csi_drivers,
                pods_by_ns={"kube-system": pods},
                pvs=pvs,
                secrets_by_ref=secrets,
            ),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["no-shared-cluster-markers"]["passed"]
        assert "shared-across-clusters" in outcomes["no-shared-cluster-markers"]["message"]

    def test_unrestricted_cluster_secret_grant_fails(self) -> None:
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "x"}}]
        pods = [
            _pod(
                name="csi-controller",
                images=["csi-provisioner:v4"],
                service_account="csi-controller-sa",
            ),
            _pod(name="csi-node", images=["csi-node-driver-registrar:v2"]),
        ]
        pvs: list[dict[str, Any]] = []
        crbs = [
            _crb(
                name="csi-secret-reader",
                cluster_role="cluster-wide-secrets",
                subject_namespace="kube-system",
                subject_name="csi-controller-sa",
            )
        ]
        croles = [_cluster_role_secrets(name="cluster-wide-secrets", verbs=["get", "list", "watch"])]
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(
                csi_drivers=csi_drivers,
                pods_by_ns={"kube-system": pods},
                pvs=pvs,
                cluster_role_bindings=crbs,
                cluster_roles=croles,
            ),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["serviceaccount-rbac-scoped"]["passed"]
        assert "csi-secret-reader" in outcomes["serviceaccount-rbac-scoped"]["message"]

    def test_node_plugin_with_persistent_volume_claim_fails(self) -> None:
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "x"}}]
        pods = [
            _pod(
                name="csi-node",
                images=["csi-node-driver-registrar:v2"],
                volumes=[
                    {"name": "plugin-dir", "hostPath": {"path": "/var/lib/kubelet/plugins"}},
                    {
                        "name": "shared-state",
                        "persistentVolumeClaim": {"claimName": "shared-pvc"},
                    },
                ],
            )
        ]
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(csi_drivers=csi_drivers, pods_by_ns={"kube-system": pods}, pvs=[]),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["node-plugin-uses-hostpath-not-shared-mount"]["passed"]
        assert "persistentVolumeClaim" in outcomes["node-plugin-uses-hostpath-not-shared-mount"]["message"]

    def test_no_node_plugin_skips_mount_check(self) -> None:
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "x"}}]
        # Only a controller pod - no node-plugin.
        pods = [
            _pod(
                name="csi-controller",
                images=["csi-provisioner:v4"],
                service_account="csi-controller-sa",
            )
        ]
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(csi_drivers=csi_drivers, pods_by_ns={"kube-system": pods}, pvs=[]),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["node-plugin-uses-hostpath-not-shared-mount"]["skipped"]

    def test_csidriver_list_failure_sets_failed(self) -> None:
        check = self._make({})
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(csi_drivers=[], pods_by_ns={}, pvs=[], fail_on="get csidriver"),
        ):
            check.run()

        assert not check.passed
        assert "Failed to list CSIDriver" in check._error

    def test_secret_fetch_forbidden_is_surfaced(self) -> None:
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "x"}}]
        pods = [
            _pod(
                name="csi-controller",
                images=["csi-provisioner:v4"],
                env_from_secrets=["csi-creds"],
            ),
            _pod(name="csi-node", images=["csi-node-driver-registrar:v2"]),
        ]
        # Record the ref as known but mark the fetch itself as forbidden.
        secrets: dict[tuple[str, str], dict[str, Any] | None] = {("kube-system", "csi-creds"): None}
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(
                csi_drivers=csi_drivers,
                pods_by_ns={"kube-system": pods},
                pvs=[],
                secrets_by_ref=secrets,
            ),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["no-shared-cluster-markers"]["passed"]
        assert "unreadable" in outcomes["no-shared-cluster-markers"]["message"]

    def test_dangling_envfrom_secret_ref_does_not_fail(self) -> None:
        """An envFrom Secret ref that does not exist (EKS with IRSA) must not fail the check.

        The AWS EFS/EBS CSI drivers ship with an optional ``envFrom.secretRef: aws-secret``
        on the controller pod; when the cluster uses IRSA / workload identity
        the Secret is never created. ``kubectl get secret --ignore-not-found``
        returns rc=0 and empty stdout, which maps to the ``"missing"`` status
        and must be treated as non-failure.
        """
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "efs.csi.aws.com"}}]
        # Realistic EKS EFS controller: the driver container sits alongside
        # the csi-provisioner sidecar that makes this pod recognisable as a
        # CSI controller. The aws-secret envFrom on the controller is
        # optional - omitted on clusters using IRSA.
        pods = [
            _pod(
                name="efs-csi-controller",
                images=["aws-efs-csi-driver:v2", "csi-provisioner:v4"],
                env_from_secrets=["aws-secret"],
            ),
            _pod(
                name="efs-csi-node",
                images=["csi-node-driver-registrar:v2"],
                volumes=[{"name": "plugin", "hostPath": {"path": "/var/lib/kubelet/plugins"}}],
            ),
        ]
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(
                csi_drivers=csi_drivers,
                pods_by_ns={"kube-system": pods},
                pvs=[],
                missing_secret_refs={("kube-system", "aws-secret")},
            ),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["no-shared-cluster-markers"]["passed"]
        assert "kube-system/aws-secret" in outcomes["no-shared-cluster-markers"]["message"]
        assert "do not exist" in outcomes["no-shared-cluster-markers"]["message"]

    def test_projected_secret_volume_is_discovered(self) -> None:
        check = self._make({})
        csi_drivers = [{"kind": "CSIDriver", "metadata": {"name": "x"}}]
        pods = [
            _pod(
                name="csi-controller",
                images=["csi-provisioner:v4"],
                service_account="csi-controller-sa",
                projected_secrets=["proj-secret"],
            ),
            _pod(
                name="csi-node",
                images=["csi-node-driver-registrar:v2"],
                volumes=[{"name": "plugin", "hostPath": {"path": "/var/lib/kubelet/plugins"}}],
            ),
        ]
        with patch.object(
            check,
            "run_command",
            side_effect=self._router(csi_drivers=csi_drivers, pods_by_ns={"kube-system": pods}, pvs=[]),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert "proj-secret" in outcomes["csi-secrets-discovered"]["message"]


class TestSetPvFields:
    """Tests for ``_set_pv_fields`` - the static PV mutator."""

    def _base_doc(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": "placeholder"},
            "spec": {
                "capacity": {"storage": "1Gi"},
                "accessModes": ["ReadWriteOnce"],
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "",
                "csi": {"driver": "placeholder", "volumeHandle": "placeholder", "fsType": "ext4"},
            },
        }

    def test_overrides_all_fields(self) -> None:
        doc = self._base_doc()
        out = _set_pv_fields(
            doc,
            name="pv-x",
            driver="ebs.csi.aws.com",
            volume_handle="vol-abc",
            fs_type="xfs",
            capacity="5Gi",
            access_mode="ReadWriteOnce",
            claim_namespace="ns1",
            claim_name="pvc-x",
        )
        assert out["metadata"]["name"] == "pv-x"
        assert out["spec"]["capacity"] == {"storage": "5Gi"}
        assert out["spec"]["accessModes"] == ["ReadWriteOnce"]
        assert out["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
        assert out["spec"]["storageClassName"] == ""
        assert out["spec"]["csi"] == {"driver": "ebs.csi.aws.com", "volumeHandle": "vol-abc", "fsType": "xfs"}
        assert out["spec"]["claimRef"] == {"namespace": "ns1", "name": "pvc-x"}

    def test_missing_sections_are_created(self) -> None:
        out = _set_pv_fields(
            {},
            name="pv-y",
            driver="d",
            volume_handle="vh",
            fs_type="ext4",
            capacity="1Gi",
            access_mode="ReadWriteOnce",
            claim_namespace="ns",
            claim_name="pvc",
        )
        assert out["metadata"]["name"] == "pv-y"
        assert out["spec"]["csi"]["volumeHandle"] == "vh"
        assert out["spec"]["claimRef"]["name"] == "pvc"


class TestSetMountPodFields:
    """Tests for ``_set_mount_pod_fields`` - the BusyBox mount-pod mutator."""

    def _base_doc(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "placeholder", "namespace": "placeholder"},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "probe",
                        "image": "busybox:1.36",
                        "command": ["sh", "-c", "sleep 3600"],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    }
                ],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "placeholder"}}],
            },
        }

    def test_sets_name_namespace_and_pvc(self) -> None:
        out = _set_mount_pod_fields(self._base_doc(), namespace="ns1", name="probe-1", pvc_name="pvc-1")
        assert out["metadata"]["name"] == "probe-1"
        assert out["metadata"]["namespace"] == "ns1"
        assert out["spec"]["volumes"][0]["persistentVolumeClaim"] == {"claimName": "pvc-1"}
        # volumeMounts inside the container are template-defined; the mutator
        # only touches volumes so the mount path stays /data as documented.
        assert out["spec"]["containers"][0]["volumeMounts"][0]["mountPath"] == "/data"


class TestK8sCsiProvisioningModesCheck:
    """Tests for ``K8sCsiProvisioningModesCheck``."""

    _CANARY_PATTERN = "csi-prov-"

    def _make(self, config: dict[str, Any] | None = None) -> K8sCsiProvisioningModesCheck:
        return K8sCsiProvisioningModesCheck(config=config or {})

    def _stub_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("K8S_CSI_BLOCK_SC", "K8S_CSI_SHARED_FS_SC", "K8S_CSI_NFS_SC"):
            monkeypatch.delenv(var, raising=False)

    def _pv_payload(self, driver: str = "ebs.csi.aws.com") -> str:
        return json.dumps({"spec": {"csi": {"driver": driver}, "capacity": {"storage": "1Gi"}}})

    def _make_router(
        self,
        *,
        phase: str = "Bound",
        volume_name: str = "pv-dyn-xyz",
        pv_payload: str | None = None,
        wait_exit: int = 0,
        write_exit: int = 0,
        read_exit: int = 0,
        read_stdout_transform=None,
    ):
        """Build a ``run_command`` side-effect router covering every kubectl call.

        The router keeps the last canary value it saw on a write, so the
        read-side can echo it back (simulating the pod having persisted the
        file); override by passing ``read_stdout_transform``.
        """
        state = {"canary": ""}
        pv_json = pv_payload if pv_payload is not None else self._pv_payload()

        def router(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _ok()
            if "delete namespace" in cmd or "delete pv" in cmd:
                return _ok()
            if "get pvc" in cmd and "-o json" in cmd:
                return _ok(stdout=_pvc_json(phase=phase, volume_name=volume_name))
            if "get pv" in cmd and "-o json" in cmd:
                return _ok(stdout=pv_json)
            if "wait --for=condition=Ready" in cmd:
                return _ok() if wait_exit == 0 else _fail(exit_code=wait_exit, stderr="timeout")
            if "exec" in cmd and "echo " in cmd:
                # Parse out the canary value so the read-side can return it.
                # Command form: ... -- sh -c 'echo csi-prov-<hex> > /data/canary.txt'
                import re

                m = re.search(r"echo (?:'|\")?(csi-prov-[0-9a-f]+)(?:'|\")? > /data/canary\.txt", cmd)
                if m:
                    state["canary"] = m.group(1)
                return _ok() if write_exit == 0 else _fail(exit_code=write_exit, stderr="write failed")
            if "exec" in cmd and "cat /data/canary.txt" in cmd:
                if read_exit != 0:
                    return _fail(exit_code=read_exit, stderr="read failed")
                stdout = state["canary"] if read_stdout_transform is None else read_stdout_transform(state["canary"])
                return _ok(stdout=stdout)
            raise AssertionError(f"unexpected command: {cmd}")

        return router

    def test_no_dynamic_sc_configured_skips_without_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed
        assert "Skipped" in check._output
        assert "dynamic_storage_class" in check._output

    def test_dynamic_happy_path_static_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(
            {
                "dynamic_storage_class": "gp3",
                "bind_timeout_s": 5,
                "namespace_prefix": "ut",
            }
        )
        with (
            patch.object(check, "run_command", side_effect=self._make_router()),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["dynamic"]["passed"]
        assert outcomes["static"].get("skipped")
        assert "static_pv.volume_handle" in outcomes["static"]["message"]

    def test_dynamic_pvc_never_binds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"dynamic_storage_class": "gp3", "bind_timeout_s": 5, "namespace_prefix": "ut"})
        with (
            patch.object(check, "run_command", side_effect=self._make_router(phase="Pending")),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["dynamic"]["passed"]
        assert "did not reach Bound" in outcomes["dynamic"]["message"]

    def test_dynamic_pv_missing_csi_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"dynamic_storage_class": "gp3", "bind_timeout_s": 5, "namespace_prefix": "ut"})
        empty_pv = json.dumps({"spec": {"csi": {}, "capacity": {"storage": "1Gi"}}})
        with (
            patch.object(check, "run_command", side_effect=self._make_router(pv_payload=empty_pv)),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["dynamic"]["passed"]
        assert "spec.csi.driver" in outcomes["dynamic"]["message"]

    def test_dynamic_canary_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"dynamic_storage_class": "gp3", "bind_timeout_s": 5, "namespace_prefix": "ut"})
        router = self._make_router(read_stdout_transform=lambda _: "not-the-canary")
        with (
            patch.object(check, "run_command", side_effect=router),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert not outcomes["dynamic"]["passed"]
        assert "canary write/read failed" in outcomes["dynamic"]["message"]

    def test_static_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make(
            {
                "dynamic_storage_class": "gp3",
                "static_pv": {
                    "volume_handle": "vol-0abc",
                    "csi_driver": "ebs.csi.aws.com",
                    "capacity": "1Gi",
                    "access_mode": "ReadWriteOnce",
                },
                "bind_timeout_s": 5,
                "namespace_prefix": "ut",
            }
        )
        delete_pv_cmds: list[str] = []
        router = self._make_router()

        def spy(cmd: str, timeout: int | None = None) -> CommandResult:
            if "delete pv " in cmd:
                delete_pv_cmds.append(cmd)
            return router(cmd, timeout)

        with (
            patch.object(check, "run_command", side_effect=spy),
            patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(returncode=0)),
            _patched_clock(),
        ):
            check.run()

        assert check.passed, check._error
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["dynamic"]["passed"]
        assert outcomes["static"]["passed"]
        assert "vol-0abc" in outcomes["static"]["message"]
        # Cleanup must delete the cluster-scoped PV even on success so the
        # next run can re-apply its own PV against the same volume handle.
        assert any("delete pv" in c for c in delete_pv_cmds), "static PV was not cleaned up"

    def test_static_subprocess_failure_on_pv_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-zero exit from ``kubectl apply`` for the static PV fails the static subtest only."""
        self._stub_env(monkeypatch)
        check = self._make(
            {
                "dynamic_storage_class": "gp3",
                "static_pv": {
                    "volume_handle": "vol-0abc",
                    "csi_driver": "ebs.csi.aws.com",
                },
                "bind_timeout_s": 5,
                "namespace_prefix": "ut",
            }
        )

        def fake_subprocess(*args, **kwargs):
            # Dispatch off the manifest YAML so the failure is specific to
            # the static PV apply (which is the only document containing
            # ``persistentVolumeReclaimPolicy: Retain``).
            manifest = kwargs.get("input") or ""
            if "persistentVolumeReclaimPolicy: Retain" in manifest:
                return _FakeProc(returncode=1, stderr="forbidden: PersistentVolumes already exist")
            return _FakeProc(returncode=0)

        with (
            patch.object(check, "run_command", side_effect=self._make_router()),
            patch("isvtest.validations.k8s_storage.subprocess.run", side_effect=fake_subprocess),
            _patched_clock(),
        ):
            check.run()

        assert not check.passed
        outcomes = {r["name"]: r for r in check._subtest_results}
        assert outcomes["dynamic"]["passed"]
        assert not outcomes["static"]["passed"]
        assert "kubectl apply failed for PV" in outcomes["static"]["message"]

    def test_namespace_create_failure_fails_check_without_subtests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub_env(monkeypatch)
        check = self._make({"dynamic_storage_class": "gp3", "namespace_prefix": "ut"})

        def fake_run(cmd: str, timeout: int | None = None) -> CommandResult:
            if "create namespace" in cmd:
                return _fail(stderr="forbidden")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(check, "run_command", side_effect=fake_run):
            check.run()

        assert not check.passed
        assert "Failed to create namespace" in check._error
        assert check._subtest_results == []
