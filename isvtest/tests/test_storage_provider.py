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

"""Unit tests for ``StorageProviderApiCheck`` driven through ``MockStorageApi``."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from isvtest.core.storage import Provider
from isvtest.core.storage_provider import (
    CAP_TENANT_LIST,
    CAP_USER_QUOTA_SET,
    CAP_VOLUME_CREATE,
    AuthenticationError,
    CreateVolumeRequest,
    GetTenantQuotaRequest,
    ListTenantsRequest,
    ListVolumesRequest,
    MockStorageApi,
    NotSupportedError,
    StorageProvider,
    TagFilter,
    TenantQuota,
    Volume,
    new_implementation,
)
from isvtest.core.storage_provider.mock import default_mock_core
from isvtest.validations.storage_provider import StorageProviderApiCheck

_MOCK_SHIM_BODY = (
    "from isvtest.core.storage_provider import MockStorageApi, new_implementation\n"
    "from isvtest.core.storage_provider.mock import default_mock_core\n"
    "def build_api():\n"
    "    return new_implementation(\n"
    "        core=default_mock_core(),\n"
    "        impl=MockStorageApi(tenant_id='unit-test', hard_limit_bytes=10 * 1024**3),\n"
    "    )\n"
)


def _served(impl: MockStorageApi) -> StorageProvider:
    """Serve a mock ``Implementation`` the way a shim's ``build_api()`` would.

    Composes it through ``new_implementation`` so ``properties()`` and capability
    gating are live (the mock resolves its own default tenant internally, so no
    ``default_tenant`` wrapping is applied).
    """
    return new_implementation(core=default_mock_core(), impl=impl)


def _write_manifest_with_mock_shim(tmp_path: Path) -> Path:
    (tmp_path / "shim.py").write_text(_MOCK_SHIM_BODY)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "v1alpha2",
                "providers": [
                    {
                        "name": "mock-fs",
                        "type": "file",
                        "tenant_id": "unit-test",
                        "shim": {"kind": "python", "module": "shim.py"},
                    }
                ],
            },
            sort_keys=False,
        )
    )
    return manifest


def _outcomes(check: StorageProviderApiCheck) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in check._subtest_results}


class TestStorageProviderApiCheckSkipPaths:
    def test_missing_manifest_path_skips_cleanly(self) -> None:
        check = StorageProviderApiCheck(config={})
        with pytest.raises(pytest.skip.Exception, match="manifest_path unset"):
            check.run()

    def test_manifest_missing_file_fails(self, tmp_path: Path) -> None:
        check = StorageProviderApiCheck(config={"manifest_path": str(tmp_path / "nope.yaml")})
        check.run()
        assert not check.passed
        assert "Failed to load provider manifest" in check._error

    def test_no_python_shim_skips_with_note(self, tmp_path: Path) -> None:
        manifest = tmp_path / "m.yaml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "v1alpha2",
                    "providers": [
                        {"name": "ebs", "type": "block", "tenant_id": "t"},
                        {
                            "name": "rest-fs",
                            "type": "file",
                            "tenant_id": "t",
                            "shim": {"kind": "rest", "endpoint": "https://x"},
                        },
                    ],
                },
                sort_keys=False,
            )
        )
        check = StorageProviderApiCheck(config={"manifest_path": str(manifest)})
        with pytest.raises(pytest.skip.Exception) as skipped:
            check.run()
        assert "rest-fs" in skipped.value.msg
        assert "ebs" in skipped.value.msg


class TestStorageProviderApiCheckHappyPath:
    def test_full_n019_n020_n021_path(self, tmp_path: Path) -> None:
        manifest = _write_manifest_with_mock_shim(tmp_path)
        check = StorageProviderApiCheck(config={"manifest_path": str(manifest), "volume_size_bytes": 1024 * 1024})
        check.run()
        assert check.passed, f"unexpected error: {check._error}"
        outcomes = _outcomes(check)
        assert outcomes["api-authentication[mock-fs]"]["passed"]
        assert outcomes["volume-provisioning[mock-fs]"]["passed"]
        assert outcomes["tenant-quota[mock-fs]"]["passed"]
        assert all(not r["skipped"] for r in check._subtest_results)

    def test_volume_is_deleted_on_pass(self, tmp_path: Path) -> None:
        manifest = _write_manifest_with_mock_shim(tmp_path)
        check = StorageProviderApiCheck(config={"manifest_path": str(manifest)})

        captured: dict[str, Provider] = {}
        original = StorageProviderApiCheck._exercise_provider

        def spy(self: StorageProviderApiCheck, provider: Provider, **kw: Any) -> bool:
            captured["provider"] = provider
            return original(self, provider, **kw)

        with patch.object(StorageProviderApiCheck, "_exercise_provider", new=spy):
            check.run()

        api = captured["provider"].api
        assert isinstance(api, StorageProvider)
        assert api.list_volumes(ListVolumesRequest()).volumes == ()


class _FailHealth(MockStorageApi):
    def health_check(self) -> None:
        raise AuthenticationError("nope")


class _NoCreate(MockStorageApi):
    def create_volume(self, req: CreateVolumeRequest) -> Volume:
        raise NotSupportedError("CSI owns lifecycle")

    def delete_volume(self, req: Any) -> None:
        raise NotSupportedError("CSI owns lifecycle")


class _NoCreateAndListBroken(_NoCreate):
    def list_volumes(self, req: ListVolumesRequest) -> Any:
        raise RuntimeError("list_volumes blew up")


class _NoListTenants(MockStorageApi):
    def list_tenants(self, req: ListTenantsRequest) -> Any:
        raise NotSupportedError("tenant enumeration not supported")


class _NoDirectorySet(MockStorageApi):
    """Backs everything except directory-quota set, which is a raising stub."""

    def set_directory_quota(self, req: Any) -> Any:
        raise NotSupportedError("directory quota set not supported")


class _NoUserSet(MockStorageApi):
    """Backs everything except user-quota set (raises NotImplementedError stub)."""

    def set_user_quota(self, req: Any) -> Any:
        raise NotImplementedError("user quota set not wired up")


class _ZeroQuota(MockStorageApi):
    def get_tenant_quota(self, req: GetTenantQuotaRequest) -> TenantQuota:
        return TenantQuota(tenant_id=req.tenant_id or "t", hard_limit_bytes=0, used_bytes=0, name="empty")


class _BrokenQuota(MockStorageApi):
    def get_tenant_quota(self, req: GetTenantQuotaRequest) -> TenantQuota:
        raise RuntimeError("kaboom")


def _check_with_api(api: StorageProvider, *, name: str = "p") -> StorageProviderApiCheck:
    """Construct a StorageProviderApiCheck wired around a synthetic ``Provider``."""
    provider = Provider(name=name, volume_type="file", tenant_id="t", shim_kind="python", api=api)
    check = StorageProviderApiCheck(config={"manifest_path": "ignored"})

    def _fake_registry(_config: Any) -> list[Provider]:
        return [provider]

    check.__dict__["__patch_registry__"] = _fake_registry
    return check


@pytest.fixture
def patched_registry():
    """Yield a context manager that swaps in ``load_provider_registry`` per check."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx(check: StorageProviderApiCheck):
        with patch(
            "isvtest.validations.storage_provider.load_provider_registry",
            side_effect=check.__dict__["__patch_registry__"],
        ):
            yield

    return _ctx


class TestStorageProviderApiCheckFailureModes:
    def test_authentication_error_short_circuits_provider(self, patched_registry: Any) -> None:
        check = _check_with_api(_served(_FailHealth(tenant_id="t")), name="bad-auth")
        with patched_registry(check):
            check.run()
        assert not check.passed
        outcomes = _outcomes(check)
        assert not outcomes["api-authentication[bad-auth]"]["passed"]
        assert outcomes["volume-provisioning[bad-auth]"]["skipped"]
        assert outcomes["tenant-quota[bad-auth]"]["skipped"]

    def test_not_supported_create_falls_back_to_list_volumes(self, patched_registry: Any) -> None:
        check = _check_with_api(_served(_NoCreate(tenant_id="t")), name="csi-only")
        with patched_registry(check):
            check.run()
        outcomes = _outcomes(check)
        assert outcomes["api-authentication[csi-only]"]["passed"]
        assert outcomes["volume-provisioning[csi-only]"]["skipped"]
        assert outcomes["tenant-quota[csi-only]"]["passed"]
        assert check.passed, check._error

    def test_not_supported_create_with_broken_list_fails(self, patched_registry: Any) -> None:
        check = _check_with_api(_served(_NoCreateAndListBroken(tenant_id="t")), name="busted")
        with patched_registry(check):
            check.run()
        outcomes = _outcomes(check)
        assert not outcomes["volume-provisioning[busted]"]["passed"]
        assert "list_volumes" in outcomes["volume-provisioning[busted]"]["message"]
        assert not check.passed

    def test_zero_hard_limit_fails_tenant_quota_subtest(self, patched_registry: Any) -> None:
        check = _check_with_api(_served(_ZeroQuota(tenant_id="t")), name="zero")
        with patched_registry(check):
            check.run()
        outcomes = _outcomes(check)
        assert not outcomes["tenant-quota[zero]"]["passed"]
        assert "hard_limit_bytes=0" in outcomes["tenant-quota[zero]"]["message"]
        assert not check.passed

    def test_get_tenant_quota_exception_fails_subtest(self, patched_registry: Any) -> None:
        check = _check_with_api(_served(_BrokenQuota(tenant_id="t")), name="broken")
        with patched_registry(check):
            check.run()
        outcomes = _outcomes(check)
        assert not outcomes["tenant-quota[broken]"]["passed"]
        assert "kaboom" in outcomes["tenant-quota[broken]"]["message"]
        assert not check.passed


def _run_provider(provider: Provider, patched_registry: Any) -> StorageProviderApiCheck:
    """Run the check against a fully-specified ``Provider`` (identity/capabilities)."""
    check = StorageProviderApiCheck(config={"manifest_path": "ignored"})
    check.__dict__["__patch_registry__"] = lambda _config: [provider]
    with patched_registry(check):
        check.run()
    return check


class TestStorageProviderApiCheckManifestContract:
    """The manifest is a contract: declared identity/capabilities must match the shim."""

    def test_consistency_passes_when_manifest_matches_shim(self, patched_registry: Any) -> None:
        provider = Provider(
            name="ok",
            volume_type="file",
            tenant_id="t",
            storage_protocols=("mock",),
            provider_version="0.1.0",
            expected_capabilities={
                "quota.directory.set": True,
                "quota.user.set": True,
                CAP_TENANT_LIST: True,
            },
            shim_kind="python",
            api=_served(MockStorageApi(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        assert _outcomes(check)["manifest-consistency[ok]"]["passed"]
        assert check.passed, check._error

    def test_storage_type_mismatch_fails(self, patched_registry: Any) -> None:
        provider = Provider(
            name="bt",
            volume_type="block",
            tenant_id="t",
            shim_kind="python",
            api=_served(MockStorageApi(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["manifest-consistency[bt]"]
        assert not result["passed"]
        assert "storage_type" in result["message"]
        assert not check.passed

    def test_protocol_mismatch_fails(self, patched_registry: Any) -> None:
        provider = Provider(
            name="p",
            volume_type="file",
            tenant_id="t",
            storage_protocols=("nfsv4",),
            shim_kind="python",
            api=_served(MockStorageApi(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["manifest-consistency[p]"]
        assert not result["passed"]
        assert "storage_protocols" in result["message"]

    def test_version_mismatch_fails(self, patched_registry: Any) -> None:
        provider = Provider(
            name="v",
            volume_type="file",
            tenant_id="t",
            provider_version="9.9.9",
            shim_kind="python",
            api=_served(MockStorageApi(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["manifest-consistency[v]"]
        assert not result["passed"]
        assert "provider.version" in result["message"]

    def test_directory_set_declared_supported_but_raises_fails(self, patched_registry: Any) -> None:
        # Declaring quota.directory.set supported while the shim raises the
        # not-implemented sentinel is a contract violation.
        provider = Provider(
            name="dq",
            volume_type="file",
            tenant_id="t",
            expected_capabilities={"quota.directory.set": True},
            shim_kind="python",
            api=_served(_NoDirectorySet(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["manifest-consistency[dq]"]
        assert not result["passed"]
        assert "quota.directory.set" in result["message"]

    def test_user_set_declared_supported_but_raises_fails(self, patched_registry: Any) -> None:
        # NotImplementedError is treated as the not-implemented sentinel too.
        provider = Provider(
            name="uq",
            volume_type="file",
            tenant_id="t",
            expected_capabilities={CAP_USER_QUOTA_SET: True},
            shim_kind="python",
            api=_served(_NoUserSet(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["manifest-consistency[uq]"]
        assert not result["passed"]
        assert "quota.user.set" in result["message"]

    def test_capability_declared_none_is_not_probed(self, patched_registry: Any) -> None:
        # A surface the manifest declares unsupported (none/False) is not probed,
        # so a shim that happens to back it is fine - only ``supported`` claims
        # are enforced.
        provider = Provider(
            name="lt",
            volume_type="file",
            tenant_id="t",
            expected_capabilities={CAP_TENANT_LIST: False, CAP_USER_QUOTA_SET: False},
            shim_kind="python",
            api=_served(MockStorageApi(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        assert _outcomes(check)["manifest-consistency[lt]"]["passed"]

    def test_list_tenants_declared_true_but_unsupported_fails(self, patched_registry: Any) -> None:
        provider = Provider(
            name="lt2",
            volume_type="file",
            tenant_id="t",
            expected_capabilities={CAP_TENANT_LIST: True},
            shim_kind="python",
            api=_served(_NoListTenants(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["manifest-consistency[lt2]"]
        assert not result["passed"]
        assert "tenant.list" in result["message"]

    def test_create_volume_declared_true_but_unsupported_fails(self, patched_registry: Any) -> None:
        provider = Provider(
            name="cv",
            volume_type="file",
            tenant_id="t",
            expected_capabilities={CAP_VOLUME_CREATE: True},
            shim_kind="python",
            api=_served(_NoCreate(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["volume-provisioning[cv]"]
        assert not result["passed"]
        assert "volume.create=true" in result["message"]
        assert not check.passed

    def test_create_volume_declared_false_but_supported_fails(self, patched_registry: Any) -> None:
        provider = Provider(
            name="cv2",
            volume_type="file",
            tenant_id="t",
            expected_capabilities={CAP_VOLUME_CREATE: False},
            shim_kind="python",
            api=_served(MockStorageApi(tenant_id="t")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["volume-provisioning[cv2]"]
        assert not result["passed"]
        assert "volume.create=false" in result["message"]

    def test_tenant_id_mismatch_fails(self, patched_registry: Any) -> None:
        provider = Provider(
            name="tn",
            volume_type="file",
            tenant_id="declared-tenant",
            shim_kind="python",
            api=_served(MockStorageApi(tenant_id="actual-tenant")),
        )
        check = _run_provider(provider, patched_registry)
        result = _outcomes(check)["tenant-quota[tn]"]
        assert not result["passed"]
        assert "tenant_id" in result["message"]
        assert not check.passed


class TestStorageProviderApiCheckVolumeTags:
    def test_probe_volume_is_tagged_with_run_id(self, patched_registry: Any) -> None:
        api = _served(MockStorageApi(tenant_id="t"))
        check = _check_with_api(api, name="tagged")
        check.config["run_id"] = "abc123"

        seen: dict[str, dict[str, str]] = {}
        original_create = api.create_volume

        def spy_create(req: CreateVolumeRequest) -> Volume:
            vol = original_create(req)
            seen[vol.id] = dict(vol.tags)
            return vol

        with patched_registry(check), patch.object(api, "create_volume", side_effect=spy_create):
            check.run()

        assert seen, "create_volume was not exercised"
        tags = next(iter(seen.values()))
        assert tags.get("isvtest-run-id") == "abc123"
        assert tags.get("test-case") == "N-020"
        assert tags.get("provider") == "tagged"

        # Orphan-sweep convention: filtering by run id matches the (deleted) probe.
        assert (
            api.list_volumes(ListVolumesRequest(tag_filters=(TagFilter("isvtest-run-id", ("abc123",)),))).volumes == ()
        )
