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

"""Tests for ``StorageDirectoryQuotaEnforcementCheck`` helpers.

Covers ``_await_hard`` read-back polling, reused PVC/pod cleanup, and
``pod_name`` without ``pvc_name`` config rejection.
"""

from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.core.storage import Provider
from isvtest.core.storage_provider import (
    CAP_DIRECTORY_QUOTA_DELETE,
    CAP_DIRECTORY_QUOTA_GET,
    CAP_DIRECTORY_QUOTA_LIST,
    CAP_DIRECTORY_QUOTA_SET,
    CAP_VOLUME_CREATE,
    CAP_VOLUME_DELETE,
    DirectoryQuota,
    MountSpec,
    NotFoundError,
    QuotaLimits,
    Volume,
)
from isvtest.validations.storage_quota_enforcement import StorageDirectoryQuotaEnforcementCheck

_WANT = 1 << 30
_FULL_DIRECTORY_QUOTA_CAPS = (
    CAP_DIRECTORY_QUOTA_SET,
    CAP_DIRECTORY_QUOTA_GET,
    CAP_DIRECTORY_QUOTA_LIST,
    CAP_DIRECTORY_QUOTA_DELETE,
)


def _directory_quota_provider(
    name: str = "full",
    *,
    api: object | None = None,
    capability_states: dict[str, str] | None = None,
) -> Provider:
    return Provider(
        name=name,
        volume_type="file",
        tenant_id="tenant",
        shim_kind="python",
        api=api or object(),
        expected_capabilities={cap: True for cap in _FULL_DIRECTORY_QUOTA_CAPS},
        capability_states=capability_states or {},
    )


class _Api:
    """Minimal StorageProvider stand-in returning a scripted read sequence.

    Each entry is a hard-byte value, or an exception instance to raise.
    """

    def __init__(self, *sequence):
        self.sequence = list(sequence)
        self.calls = 0
        self.requests = []

    def get_directory_quota(self, req):
        self.calls += 1
        self.requests.append(req)
        item = self.sequence[min(self.calls - 1, len(self.sequence) - 1)]
        if isinstance(item, Exception):
            raise item
        return DirectoryQuota(
            tenant_id="t",
            volume_id=req.volume_id,
            path=req.path,
            hard=None if item is None else QuotaLimits(bytes=item),
        )


@pytest.fixture
def check():
    return StorageDirectoryQuotaEnforcementCheck()


@pytest.fixture(autouse=True)
def no_sleep():
    """Keep the poll's wall-clock behaviour out of the test's runtime."""
    with patch("isvtest.validations.storage_quota_enforcement.time.sleep"):
        yield


class TestAwaitHard:
    def test_returns_on_first_read_when_already_published(self, check):
        api = _Api(_WANT)
        assert check._await_hard(api, "v1", "sub", _WANT) == (True, _WANT)
        assert api.calls == 1

    def test_threads_explicit_tenant_to_provider_calls(self, check):
        api = _Api(_WANT)
        assert check._await_hard(api, "v1", "sub", _WANT, tenant_id="tenant-a") == (True, _WANT)
        assert api.requests[0].tenant_id == "tenant-a"

    def test_converges_when_limit_is_published_late(self, check):
        api = _Api(None, None, _WANT)
        assert check._await_hard(api, "v1", "sub", _WANT) == (True, _WANT)
        assert api.calls == 3

    def test_tolerates_not_found_before_the_record_appears(self, check):
        api = _Api(NotFoundError("nope"), _WANT)
        assert check._await_hard(api, "v1", "sub", _WANT) == (True, _WANT)

    def test_sees_through_a_stale_previous_value(self, check):
        api = _Api(_WANT, _WANT, 64 << 20)
        assert check._await_hard(api, "v1", "sub", 64 << 20) == (True, 64 << 20)

    def test_gives_up_and_reports_the_last_value_seen(self, check):
        api = _Api(None)
        with patch(
            "isvtest.validations.storage_quota_enforcement.time.monotonic",
            side_effect=[0.0, 100.0, 200.0],
        ):
            assert check._await_hard(api, "v1", "sub", _WANT) == (False, None)

    def test_reports_a_wrong_stored_value_rather_than_none(self, check):
        api = _Api(123)
        with patch(
            "isvtest.validations.storage_quota_enforcement.time.monotonic",
            side_effect=[0.0, 100.0, 200.0],
        ):
            assert check._await_hard(api, "v1", "sub", _WANT) == (False, 123)


class _CleanupApi:
    """Records directory-quota deletions attempted during cleanup."""

    def __init__(self):
        self.deleted: list[tuple[str, str | None, str]] = []

    def delete_directory_quota(self, req):
        self.deleted.append((req.volume_id, req.tenant_id, req.path))


class TestCleanup:
    def test_own_namespace_is_deleted_wholesale(self, check):
        check._kubectl_base = "kubectl"
        api = _CleanupApi()
        with patch.object(check, "run_command") as rc, patch.object(check, "_exec") as ex:
            check._cleanup("ns", "pod", "pvc", "sub", api, "v1", ns_created=True, pvc_created=True, pod_created=True)
        assert [c for c in rc.call_args_list if "delete namespace ns" in c.args[0]]
        ex.assert_not_called()
        assert api.deleted == []
        assert not [c for c in rc.call_args_list if "delete pod" in c.args[0]]

    def test_reused_pod_and_pvc_survive(self, check):
        check._kubectl_base = "kubectl"
        api = _CleanupApi()
        with patch.object(check, "run_command") as rc, patch.object(check, "_exec") as ex:
            check._cleanup("ns", "pod", "pvc", "sub", api, "v1", ns_created=False, pvc_created=False, pod_created=False)
        joined = " ".join(c.args[0] for c in rc.call_args_list)
        assert "delete pod" not in joined
        assert "delete pvc" not in joined
        assert "delete namespace" not in joined
        assert api.deleted == [("v1", None, "sub")]
        ex.assert_called_once()
        assert "rm -rf /data/sub" in ex.call_args.args[2]

    def test_cleanup_threads_explicit_tenant_to_quota_delete(self, check):
        check._kubectl_base = "kubectl"
        api = _CleanupApi()
        with patch.object(check, "run_command"), patch.object(check, "_exec"):
            check._cleanup(
                "ns",
                "pod",
                "pvc",
                "sub",
                api,
                "v1",
                tenant_id="tenant-a",
                ns_created=False,
                pvc_created=False,
                pod_created=False,
            )
        assert api.deleted == [("v1", "tenant-a", "sub")]

    def test_quota_is_dropped_before_its_directory(self, check):
        check._kubectl_base = "kubectl"
        order: list[str] = []
        api = _CleanupApi()
        api.delete_directory_quota = lambda req: order.append("quota")
        with (
            patch.object(check, "run_command"),
            patch.object(check, "_exec", side_effect=lambda *a: order.append("rm")),
        ):
            check._cleanup("ns", "pod", "pvc", "sub", api, "v1", ns_created=False, pvc_created=False, pod_created=False)
        assert order == ["quota", "rm"]

    def test_already_deleted_quota_is_not_an_error(self, check):
        check._kubectl_base = "kubectl"
        api = _CleanupApi()
        api.delete_directory_quota = Mock(side_effect=NotFoundError("gone"))
        with patch.object(check, "run_command"), patch.object(check, "_exec") as ex:
            check._cleanup("ns", "pod", "pvc", "sub", api, "v1", ns_created=False, pvc_created=False, pod_created=False)
        ex.assert_called_once()

    def test_own_pod_in_reused_pvc_is_removed(self, check):
        check._kubectl_base = "kubectl"
        with patch.object(check, "run_command") as rc, patch.object(check, "_exec"):
            check._cleanup(
                "ns", "pod", "pvc", "sub", _CleanupApi(), "v1", ns_created=False, pvc_created=False, pod_created=True
            )
        joined = " ".join(c.args[0] for c in rc.call_args_list)
        assert "delete pod pod" in joined
        assert "delete pvc" not in joined

    def test_nothing_removed_before_a_subdir_was_created(self, check):
        check._kubectl_base = "kubectl"
        api = _CleanupApi()
        with patch.object(check, "run_command"), patch.object(check, "_exec") as ex:
            check._cleanup("ns", "pod", "pvc", "", api, "v1", ns_created=False, pvc_created=False, pod_created=True)
        ex.assert_not_called()
        assert api.deleted == []

    def test_unresolved_volume_skips_the_quota_delete(self, check):
        check._kubectl_base = "kubectl"
        api = _CleanupApi()
        with patch.object(check, "run_command"), patch.object(check, "_exec"):
            check._cleanup("ns", "pod", "pvc", "sub", api, "", ns_created=False, pvc_created=False, pod_created=False)
        assert api.deleted == []


class TestCandidateSelection:
    """Provider prefiltering for the directory-quota CRUD surface."""

    def test_set_only_directory_quota_provider_is_skipped(self):
        provider = Provider(
            name="set-only",
            volume_type="file",
            tenant_id="tenant",
            shim_kind="python",
            api=object(),
            expected_capabilities={CAP_DIRECTORY_QUOTA_SET: True},
        )
        check = StorageDirectoryQuotaEnforcementCheck(config={"manifest_path": "manifest.yaml"})
        with (
            patch("isvtest.validations.storage_quota_enforcement.load_provider_registry", return_value=[provider]),
            patch("isvtest.validations.storage_quota_enforcement.is_k8s_available") as available,
            pytest.raises(pytest.skip.Exception) as skipped,
        ):
            check.run()
        assert "full directory-quota CRUD" in skipped.value.msg
        assert CAP_DIRECTORY_QUOTA_LIST in skipped.value.msg
        available.assert_not_called()

    def test_full_directory_quota_provider_reaches_k8s_availability_check(self):
        provider = _directory_quota_provider()
        check = StorageDirectoryQuotaEnforcementCheck(
            config={"manifest_path": "manifest.yaml", "storage_class": "shared-fs"}
        )
        with (
            patch("isvtest.validations.storage_quota_enforcement.load_provider_registry", return_value=[provider]),
            patch("isvtest.validations.storage_quota_enforcement.is_k8s_available", return_value=False) as available,
        ):
            check.run()
        assert check.passed
        assert any("no reachable Kubernetes" in result["message"] for result in check._subtest_results)
        available.assert_called_once_with()


class TestAcquisitionRouting:
    def test_csi_provider_uses_k8s_path_when_k8s_available(self):
        provider = _directory_quota_provider(
            capability_states={CAP_VOLUME_CREATE: "default", CAP_VOLUME_DELETE: "default"}
        )
        check = StorageDirectoryQuotaEnforcementCheck(
            config={"manifest_path": "manifest.yaml", "storage_class": "shared-fs"}
        )

        with (
            patch("isvtest.validations.storage_quota_enforcement.load_provider_registry", return_value=[provider]),
            patch("isvtest.validations.storage_quota_enforcement.is_k8s_available", return_value=True),
            patch("isvtest.validations.storage_quota_enforcement.get_kubectl_command", return_value=["kubectl"]),
            patch("isvtest.validations.storage_quota_enforcement.get_kubectl_base_shell", return_value="kubectl"),
            patch.object(check, "_exercise_provider_k8s", return_value=True) as k8s,
            patch.object(check, "_exercise_provider_native", return_value=True) as native,
        ):
            check.run()

        assert check.passed
        k8s.assert_called_once_with(provider)
        native.assert_not_called()

    def test_native_provider_uses_native_path_when_k8s_unavailable(self):
        provider = _directory_quota_provider(
            capability_states={CAP_VOLUME_CREATE: "native", CAP_VOLUME_DELETE: "native"}
        )
        check = StorageDirectoryQuotaEnforcementCheck(config={"manifest_path": "manifest.yaml"})

        with (
            patch("isvtest.validations.storage_quota_enforcement.load_provider_registry", return_value=[provider]),
            patch("isvtest.validations.storage_quota_enforcement.is_k8s_available", return_value=False),
            patch.object(check, "_exercise_provider_native", return_value=True) as native,
            patch.object(check, "_exercise_provider_k8s", return_value=True) as k8s,
        ):
            check.run()

        assert check.passed
        native.assert_called_once_with(provider)
        k8s.assert_not_called()


class _NativeApi:
    def __init__(self, *, mount: MountSpec | None = MountSpec(fs_type="nfs", source="server:/export")):
        self.mount = mount
        self.created = []
        self.deleted = []
        self.deleted_quotas = []

    def create_volume(self, req):
        self.created.append(req)
        return Volume(
            tenant_id=req.tenant_id or "tenant",
            id="vol-1",
            size_bytes=req.size_bytes,
            created_at=datetime.now(UTC),
            type=req.volume_type,
            state="available",
            mount=self.mount,
        )

    def delete_volume(self, req):
        self.deleted.append(req)

    def delete_directory_quota(self, req):
        self.deleted_quotas.append(req)


class TestNativeLifecycle:
    def test_native_path_creates_mounts_runs_and_deletes_volume(self):
        api = _NativeApi()
        provider = _directory_quota_provider(
            api=api,
            capability_states={CAP_VOLUME_CREATE: "native", CAP_VOLUME_DELETE: "native"},
        )
        check = StorageDirectoryQuotaEnforcementCheck()
        check._volume_size = 123
        check._ns_prefix = "isvtest-dq"
        check._write_timeout = 30
        check._create_hard = 64
        check._enforce_hard = 32

        with (
            patch("isvtest.validations.storage_quota_enforcement.uuid.uuid4") as uuid4,
            patch("isvtest.validations.storage_quota_enforcement.tempfile.mkdtemp", return_value="/tmp/isvtest-native"),
            patch.object(check, "_mount_native_volume", return_value=True) as mount,
            patch.object(check, "_exec_local", return_value=CommandResult(0, "", "", 0.0)) as exec_local,
            patch.object(check, "run_command", return_value=CommandResult(0, "", "", 0.0)) as run_command,
            patch.object(check, "_run_crud", return_value=True) as crud,
            patch.object(check, "_run_enforcement", return_value=True) as enforcement,
        ):
            uuid4.return_value.hex = "abcdef123456"
            assert check._exercise_provider_native(provider)

        assert api.created[0].size_bytes == 123
        assert api.created[0].tenant_id == "tenant"
        assert api.created[0].name == "isvtest-dq-abcdef12"
        mount.assert_called_once_with(MountSpec(fs_type="nfs", source="server:/export"), "/tmp/isvtest-native")
        exec_local.assert_any_call("mkdir -p /tmp/isvtest-native/isvtest-dq-abcdef12")
        crud.assert_called_once()
        enforcement.assert_called_once()
        assert api.deleted[0].volume_id == "vol-1"
        assert api.deleted[0].tenant_id == "tenant"
        run_command.assert_any_call("umount /tmp/isvtest-native")
        run_command.assert_any_call("rmdir /tmp/isvtest-native")

    def test_native_path_skips_when_created_volume_has_no_mount_spec(self):
        api = _NativeApi(mount=None)
        provider = _directory_quota_provider(
            api=api,
            capability_states={CAP_VOLUME_CREATE: "native", CAP_VOLUME_DELETE: "native"},
        )
        check = StorageDirectoryQuotaEnforcementCheck()
        check._volume_size = 123
        check._ns_prefix = "isvtest-dq"

        assert check._exercise_provider_native(provider)

        assert [result["skipped"] for result in check._subtest_results] == [True, True]
        assert "returned no mount instructions" in check._subtest_results[0]["message"]
        assert api.deleted[0].volume_id == "vol-1"


class TestPodReuseConfig:
    def test_pod_name_without_pvc_name_fails_loudly(self):
        check = StorageDirectoryQuotaEnforcementCheck(config={"pod_name": "probe"})
        check.run()
        assert not check.passed
        assert "pod_name requires pvc_name" in check.message
