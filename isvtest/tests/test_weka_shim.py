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

"""Tests for the WEKA ``weka/v2`` CSI volume model and its quota surfaces.

Hermetic: loads ``weka/api.py`` by path (same as the manifest loader) and stubs
``_request_envelope`` so quota REST calls assert the correct filesystem uid.
"""

from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path
from typing import Any

import pytest

from isvtest.core.storage_provider import (
    DeleteDirectoryQuotaRequest,
    DeleteUserQuotaRequest,
    DirectoryQuota,
    GetUserQuotaRequest,
    GetVolumeRequest,
    ListDirectoryQuotasRequest,
    ListUserQuotasRequest,
    NotFoundError,
    NotSupportedError,
    QuotaLimits,
    SetDirectoryQuotaRequest,
    SetUserQuotaRequest,
    UserQuota,
    ValidationError,
)

_SHIM = (
    Path(__file__).resolve().parents[2]
    / "isvctl"
    / "configs"
    / "providers"
    / "weka"
    / "scripts"
    / "storage"
    / "weka"
    / "api.py"
)


def _load_shim():
    spec = importlib.util.spec_from_file_location("weka_api_under_test", _SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


weka = _load_shim()

# Filesystem named by WEKA_FILESYSTEM (tenant-quota source).
_SHARED_FS = {"name": "test_fs", "uid": "uid-shared", "status": "READY", "total_budget": 1000, "used_total": 10}
# Filesystem the CSI driver minted for one PVC.
_PVC_FS = {
    "name": "csivol-pvc-abc",
    "uid": "uid-pvc-abc",
    "status": "READY",
    "total_budget": 50 << 30,
    "used_total": 1 << 20,
}
_PVC_HANDLE = "weka/v2/csivol-pvc-abc"


def _make_api(
    quotas: dict[str, list[dict[str, Any]]] | None = None,
    storage_path: str | None = None,
    user_quotas: dict[str, list[dict[str, Any]]] | None = None,
):
    """A WekaApi answering from canned REST responses, plus a call log.

    Only ``_request_envelope`` is stubbed: ``_request`` is implemented on top
    of it, so the unwrapping and pagination under test stay real. User-quota
    rows are held per filesystem uid so a write is visible to the read-back.
    """
    api = weka.WekaApi(
        endpoint="weka.invalid:14000",
        username="u",
        password="p",
        organization="Root",
        filesystem="test_fs",
        storage_path=storage_path,
        insecure_skip_verify=True,
    )
    quotas = quotas or {}
    user_quotas = {uid: list(rows) for uid, rows in (user_quotas or {}).items()}
    calls: list[tuple[str, str, Any, Any]] = []

    def _envelope(method: str, path: str, *, body=None, params=None):
        calls.append((method, path, body, params))
        if path == "/api/v2/fileSystems":
            return {"data": [_SHARED_FS, _PVC_FS]}
        if m := re.fullmatch(r"/api/v2/fileSystems/([^/]+)/quota/user", path):
            return _user_quota_response(user_quotas, m.group(1), method, params)
        if m := re.fullmatch(r"/api/v2/fileSystems/([^/]+)/quota", path):
            return {"data": list(quotas.get(m.group(1), []))}
        if m := re.fullmatch(r"/api/v2/fileSystems/([^/]+)/resolvePath", path):
            # Real inode ids are opaque and slash-free; keep that so the quota
            # URL below stays a single path segment.
            return {"data": {"inode_id": "inode-" + (params or {})["path"].strip("/").replace("/", "-")}}
        if m := re.fullmatch(r"/api/v2/fileSystems/([^/]+)/quota/([^/]+)", path):
            return {
                "data": {
                    "quota_id": "new-id",
                    "inode_id": m.group(2),
                    "hard_limit_bytes": (body or {}).get("hard_limit_bytes", 0),
                }
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    api._request_envelope = _envelope  # type: ignore[method-assign]
    return api, calls


def _user_quota_response(store: dict[str, list[dict[str, Any]]], fs_uid: str, method: str, params):
    """Serve /quota/user against ``store`` so writes round-trip into reads."""
    rows = store.setdefault(fs_uid, [])
    if method == "GET":
        return {"data": list(rows)}
    uid = int((params or {})["user_id"])

    def _uid_of(row: dict[str, Any]) -> int:
        if row.get("uid_or_gid") is not None:
            return int(row["uid_or_gid"])
        qid = str(row.get("quota_id") or "")
        if qid.upper().startswith("USER:"):
            return int(qid.split(":", 1)[1])
        return -1

    rows[:] = [row for row in rows if _uid_of(row) != uid]
    if method == "POST":
        hard = int(params["hard_limit_bytes"])
        # Mirror the live 5.1.31 shape (quota_id USER:<uid>, no uid_or_gid).
        # hard_limit_bytes=0 means unlimited on the backend and is omitted from LIST.
        if hard > 0:
            rows.append(
                {
                    "quota_id": f"USER:{uid}",
                    "total_bytes": 0,
                    "hard_limit_bytes": hard,
                    "soft_limit_bytes": int(params["soft_limit_bytes"]),
                }
            )
    return {"data": {}}


def _quota_writes(calls) -> list[tuple[str, str]]:
    """(method, filesystem uid) for every call that mutates a quota."""
    out = []
    for method, path, _, _ in calls:
        if method in ("PUT", "PATCH", "DELETE") and (
            m := re.fullmatch(r"/api/v2/fileSystems/([^/]+)/quota/(?!user$).+", path)
        ):
            out.append((method, m.group(1)))
    return out


def _user_quota_calls(calls) -> list[tuple[str, str, Any]]:
    """(method, filesystem uid, query params) for every /quota/user call."""
    out = []
    for method, path, _, params in calls:
        if m := re.fullmatch(r"/api/v2/fileSystems/([^/]+)/quota/user", path):
            out.append((method, m.group(1), params))
    return out


class TestFsNameFromVolumeId:
    """Which volume ids name a filesystem, and which do not."""

    @pytest.mark.parametrize(
        "volume_id,expected",
        [
            ("weka/v2/csivol-pvc-abc", "csivol-pvc-abc"),
            ("csivol-pvc-abc", "csivol-pvc-abc"),
            ("/vols/pvc-1", ""),
            ("", ""),
        ],
    )
    def test_classification(self, volume_id, expected):
        assert weka._fs_name_from_volume_id(volume_id) == expected


class TestGetVolume:
    def test_weka_v2_handle_resolves_to_its_filesystem(self):
        """The PV handle the CSI driver writes must be answerable as a volume."""
        api, _ = _make_api()
        vol = api.get_volume(GetVolumeRequest(volume_id=_PVC_HANDLE))
        assert vol.name == "csivol-pvc-abc"
        assert vol.size_bytes == 50 << 30
        assert vol.used_bytes == 1 << 20
        assert vol.state == "available"
        assert vol.csi is not None and vol.csi.volume_handle == _PVC_HANDLE

    def test_volume_id_round_trips_into_quota_calls(self):
        """get_volume().id must be usable as the volume_id for quota calls."""
        api, calls = _make_api(quotas={"uid-pvc-abc": []})
        vol = api.get_volume(GetVolumeRequest(volume_id=_PVC_HANDLE))
        api.set_directory_quota(
            SetDirectoryQuotaRequest(
                DirectoryQuota(tenant_id="Root", volume_id=vol.id, path="sub", hard=QuotaLimits(bytes=1))
            )
        )
        assert _quota_writes(calls) == [("PUT", "uid-pvc-abc")]

    def test_unknown_volume_is_not_found(self):
        api, _ = _make_api()
        with pytest.raises(NotFoundError):
            api.get_volume(GetVolumeRequest(volume_id="weka/v2/does-not-exist"))


class TestListVolumes:
    def test_returns_filesystems_as_weka_v2_volumes(self):
        api, _ = _make_api()
        volumes = api.list_volumes(weka.ListVolumesRequest()).volumes
        assert {v.id for v in volumes} == {"weka/v2/test_fs", _PVC_HANDLE}
        assert all(v.csi is not None and v.csi.volume_handle == v.id for v in volumes)

    def test_filters_by_volume_id(self):
        api, _ = _make_api()
        volumes = api.list_volumes(weka.ListVolumesRequest(ids=(_PVC_HANDLE,))).volumes
        assert [v.id for v in volumes] == [_PVC_HANDLE]


class TestSetDirectoryQuotaTargetsTheRightFilesystem:
    """Quota mutations must hit the volume's filesystem."""

    def test_weka_v2_writes_to_the_pvc_filesystem_at_its_root(self):
        api, calls = _make_api(quotas={"uid-pvc-abc": []})
        api.set_directory_quota(
            SetDirectoryQuotaRequest(
                DirectoryQuota(
                    tenant_id="Root", volume_id=_PVC_HANDLE, path="isvtest-dq", hard=QuotaLimits(bytes=1 << 30)
                )
            )
        )
        assert any(call[:2] == ("GET", "/api/v2/fileSystems/uid-pvc-abc/resolvePath") for call in calls)
        assert _quota_writes(calls) == [("PUT", "uid-pvc-abc")]
        assert not any("uid-shared" in path for _, path, _, _ in calls if "quota" in path)

    def test_existing_quota_is_patched_not_recreated(self):
        """An update must PATCH the record already on the PVC filesystem."""
        existing = {"quota_id": "77", "inode_id": "77", "path": "/isvtest-dq", "hard_limit_bytes": 1}
        api, calls = _make_api(quotas={"uid-pvc-abc": [existing]})
        api.set_directory_quota(
            SetDirectoryQuotaRequest(
                DirectoryQuota(
                    tenant_id="Root", volume_id=_PVC_HANDLE, path="isvtest-dq", hard=QuotaLimits(bytes=2 << 30)
                )
            )
        )
        assert _quota_writes(calls) == [("PATCH", "uid-pvc-abc")]

    def test_returned_path_is_relative_to_the_volume(self):
        api, _ = _make_api(quotas={"uid-pvc-abc": []})
        got = api.set_directory_quota(
            SetDirectoryQuotaRequest(
                DirectoryQuota(
                    tenant_id="Root", volume_id=_PVC_HANDLE, path="isvtest-dq", hard=QuotaLimits(bytes=1 << 30)
                )
            )
        )
        assert got.path == "isvtest-dq"
        assert got.hard is not None and got.hard.bytes == 1 << 30


class TestSetDirectoryQuotaRefusesTheVolumeRoot:
    """Empty / ``/`` paths target the CSI capacity quota; reject them."""

    @pytest.mark.parametrize("path", ["", "/", "///"])
    def test_root_path_is_rejected(self, path):
        api, calls = _make_api(quotas={"uid-pvc-abc": []})
        with pytest.raises(ValidationError):
            api.set_directory_quota(
                SetDirectoryQuotaRequest(
                    DirectoryQuota(tenant_id="Root", volume_id=_PVC_HANDLE, path=path, hard=QuotaLimits(bytes=1 << 30))
                )
            )
        assert _quota_writes(calls) == []

    def test_a_subdirectory_is_still_accepted(self):
        api, calls = _make_api(quotas={"uid-pvc-abc": []})
        api.set_directory_quota(
            SetDirectoryQuotaRequest(
                DirectoryQuota(tenant_id="Root", volume_id=_PVC_HANDLE, path="sub", hard=QuotaLimits(bytes=1 << 30))
            )
        )
        assert _quota_writes(calls) == [("PUT", "uid-pvc-abc")]


class TestListAndDelete:
    def test_list_returns_quotas_under_the_pvc_filesystem_root(self):
        api, _ = _make_api(
            quotas={
                "uid-pvc-abc": [
                    {"quota_id": "1", "inode_id": "1", "path": "/isvtest-dq", "hard_limit_bytes": 8},
                    {"quota_id": "2", "inode_id": "2", "path": "/", "hard_limit_bytes": 9},
                ]
            }
        )
        resp = api.list_directory_quotas(ListDirectoryQuotasRequest(tenant_id="Root", volume_id=_PVC_HANDLE))
        # The filesystem root is the volume itself, not a quota under it.
        assert [q.path for q in resp.directory_quotas] == ["isvtest-dq"]

    def test_list_does_not_leak_another_filesystems_quotas(self):
        other_quota = {"quota_id": "900", "inode_id": "900", "path": "/other", "hard_limit_bytes": 4096}
        api, _ = _make_api(quotas={"uid-shared": [other_quota], "uid-pvc-abc": []})
        resp = api.list_directory_quotas(ListDirectoryQuotasRequest(tenant_id="Root", volume_id=_PVC_HANDLE))
        assert resp.directory_quotas == ()

    def test_delete_targets_the_pvc_filesystem(self):
        api, calls = _make_api(
            quotas={"uid-pvc-abc": [{"quota_id": "1", "inode_id": "1", "path": "/isvtest-dq", "hard_limit_bytes": 8}]}
        )
        api.delete_directory_quota(
            DeleteDirectoryQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, path="isvtest-dq")
        )
        assert _quota_writes(calls) == [("DELETE", "uid-pvc-abc")]

    def test_delete_of_a_missing_quota_is_a_no_op(self):
        api, calls = _make_api(quotas={"uid-pvc-abc": []})
        api.delete_directory_quota(DeleteDirectoryQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, path="gone"))
        assert _quota_writes(calls) == []


class TestStoragePathScoping:
    def test_does_not_hide_a_volumes_quotas(self):
        api, _ = _make_api(
            quotas={"uid-pvc-abc": [{"quota_id": "1", "inode_id": "1", "path": "/isvtest-dq", "hard_limit_bytes": 8}]},
            storage_path="/vols",
        )
        resp = api.list_directory_quotas(ListDirectoryQuotasRequest(tenant_id="Root", volume_id=_PVC_HANDLE))
        assert [q.path for q in resp.directory_quotas] == ["isvtest-dq"]


class TestUserQuotaCrud:
    """The WEKA 5.1.26 REST surface at /fileSystems/{uid}/quota/user."""

    def test_list_returns_rows_from_the_volumes_filesystem(self):
        api, calls = _make_api(
            user_quotas={
                "uid-pvc-abc": [{"uid_or_gid": 1000, "total_bytes": 512, "hard_limit_bytes": 1 << 20}],
                "uid-shared": [{"uid_or_gid": 2000, "total_bytes": 1, "hard_limit_bytes": 1}],
            }
        )
        quotas = api.list_user_quotas(ListUserQuotasRequest(tenant_id="Root", volume_id=_PVC_HANDLE)).user_quotas
        assert [q.user for q in quotas] == ["1000"]
        assert quotas[0].hard is not None and quotas[0].hard.bytes == 1 << 20
        assert quotas[0].usage.bytes == 512
        assert _user_quota_calls(calls) == [("GET", "uid-pvc-abc", None)]

    def test_get_finds_the_uid_and_reports_a_miss_as_not_found(self):
        api, _ = _make_api(user_quotas={"uid-pvc-abc": [{"uid_or_gid": 1000, "hard_limit_bytes": 4096}]})
        got = api.get_user_quota(GetUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, user="1000"))
        assert got.user == "1000"
        assert got.hard is not None and got.hard.bytes == 4096
        with pytest.raises(NotFoundError):
            api.get_user_quota(GetUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, user="2000"))

    def test_list_parses_quota_id_user_prefix_without_uid_or_gid(self):
        """5.1.31 rows use quota_id USER:<uid> and omit uid_or_gid."""
        api, _ = _make_api(
            user_quotas={
                "uid-pvc-abc": [
                    {"quota_id": "USER:1000", "total_bytes": 512, "hard_limit_bytes": 1 << 20},
                    {"quota_id": "USER:2000", "total_bytes": 0, "hard_limit_bytes": None},
                ]
            }
        )
        quotas = api.list_user_quotas(ListUserQuotasRequest(tenant_id="Root", volume_id=_PVC_HANDLE)).user_quotas
        assert [(q.user, None if q.hard is None else q.hard.bytes) for q in quotas] == [
            ("1000", 1 << 20),
            ("2000", None),
        ]
        got = api.get_user_quota(GetUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, user="1000"))
        assert got.user == "1000"

    def test_set_sends_the_limits_as_query_params_and_reads_back(self):
        api, calls = _make_api(user_quotas={"uid-pvc-abc": []})
        got = api.set_user_quota(
            SetUserQuotaRequest(
                UserQuota(tenant_id="Root", volume_id=_PVC_HANDLE, user="1000", hard=QuotaLimits(bytes=4096))
            )
        )
        assert got.user == "1000"
        assert got.hard is not None and got.hard.bytes == 4096
        # Both limits go on every write: WEKA reads 0 as unlimited, so an
        # explicit zero is how a limit gets cleared.
        assert ("POST", "uid-pvc-abc", {"user_id": "1000", "hard_limit_bytes": "4096", "soft_limit_bytes": "0"}) in (
            _user_quota_calls(calls)
        )

    def test_delete_addresses_the_uid_on_the_volumes_filesystem(self):
        api, calls = _make_api(user_quotas={"uid-pvc-abc": [{"uid_or_gid": 1000, "hard_limit_bytes": 4096}]})
        api.delete_user_quota(DeleteUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, user="1000"))
        assert _user_quota_calls(calls) == [("DELETE", "uid-pvc-abc", {"user_id": "1000"})]
        remaining = api.list_user_quotas(ListUserQuotasRequest(tenant_id="Root", volume_id=_PVC_HANDLE)).user_quotas
        assert remaining == ()

    def test_delete_of_missing_uid_is_a_no_op(self):
        api, _ = _make_api()

        def _missing(method: str, fs_uid: str, *, params=None):
            raise NotFoundError("No quota for UID")

        api._user_quota_request = _missing  # type: ignore[method-assign]
        api.delete_user_quota(DeleteUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, user="1000"))

    def test_list_follows_next_token(self):
        api, calls = _make_api()
        pages = [
            {"data": [{"uid_or_gid": 1000, "total_bytes": 1, "hard_limit_bytes": 10}], "next_token": 3214121},
            # The final page omits next_token entirely.
            {"data": [{"uid_or_gid": 1001, "total_bytes": 2, "hard_limit_bytes": 20}]},
        ]

        def _paged(method: str, path: str, *, body=None, params=None):
            if path == "/api/v2/fileSystems":
                return {"data": [_SHARED_FS, _PVC_FS]}
            calls.append((method, path, body, params))
            return pages[1] if params else pages[0]

        api._request_envelope = _paged  # type: ignore[method-assign]
        quotas = api.list_user_quotas(ListUserQuotasRequest(tenant_id="Root", volume_id=_PVC_HANDLE)).user_quotas
        assert [q.user for q in quotas] == ["1000", "1001"]
        assert [params for _, _, params in _user_quota_calls(calls)] == [None, {"next_token": "3214121"}]


class TestUserQuotaSemantics:
    @pytest.mark.parametrize("hard_limit_bytes", [0, None])
    def test_zero_or_absent_hard_limit_is_unlimited(self, hard_limit_bytes):
        """WEKA reports an unlimited user as 0, not as a zero-byte allowance."""
        api, _ = _make_api(
            user_quotas={
                "uid-pvc-abc": [{"uid_or_gid": 1000, "total_bytes": 512, "hard_limit_bytes": hard_limit_bytes}]
            }
        )
        got = api.get_user_quota(GetUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, user="1000"))
        assert got.hard is not None and got.hard.bytes is None
        assert got.usage.bytes == 512

    @pytest.mark.parametrize("user", ["alice", "", "10x"])
    def test_a_non_numeric_subject_is_rejected(self, user):
        """WEKA's user_id is numeric; usernames have no REST equivalent."""
        api, calls = _make_api(user_quotas={"uid-pvc-abc": []})
        with pytest.raises(ValidationError):
            api.get_user_quota(GetUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE, user=user))
        assert _user_quota_calls(calls) == []

    def test_a_directory_backed_volume_is_refused(self):
        """A filesystem-wide quota on a shared filesystem would bind every PVC on it."""
        api, calls = _make_api(user_quotas={"uid-pvc-abc": []})
        with pytest.raises(NotSupportedError):
            api.list_user_quotas(ListUserQuotasRequest(tenant_id="Root", volume_id="DIR:0xf9a7:0"))
        assert _user_quota_calls(calls) == []


class TestUserQuotaRefusesTheDefaultUserSlot:
    """WEKA addresses one uid per call, so user=None has no backing endpoint."""

    def test_the_qualifier_is_advertised_false(self):
        api, _ = _make_api()
        caps = weka.new_implementation(core=api._core, impl=api).properties().capabilities()
        assert caps.quota().user().default_user_slot() is False
        assert caps.quota().user().set().is_supported()

    def test_get_set_and_delete_all_refuse(self):
        api, calls = _make_api(user_quotas={"uid-pvc-abc": []})
        with pytest.raises(NotSupportedError):
            api.get_user_quota(GetUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE))
        with pytest.raises(NotSupportedError):
            api.set_user_quota(
                SetUserQuotaRequest(
                    UserQuota(tenant_id="Root", volume_id=_PVC_HANDLE, user=None, hard=QuotaLimits(bytes=4096))
                )
            )
        with pytest.raises(NotSupportedError):
            api.delete_user_quota(DeleteUserQuotaRequest(tenant_id="Root", volume_id=_PVC_HANDLE))
        # Refusal must not reach the backend and write a per-uid row.
        assert _user_quota_calls(calls) == []


class TestUnknownRouteIsACapabilityGap:
    """An older cluster answers the route with 404; that is not a missing quota."""

    def test_plain_404_maps_to_not_found(self, monkeypatch):
        api, _ = _make_api()

        def _missing(*_args, **_kwargs):
            raise weka.urllib.error.HTTPError(
                url="https://weka.invalid/api/v2/fileSystems/fs/quota/user",
                code=404,
                msg="Not Found",
                hdrs={},
                fp=io.BytesIO(b"No quota for UID"),
            )

        monkeypatch.setattr(weka.urllib.request, "urlopen", _missing)
        with pytest.raises(NotFoundError):
            api._raw_request("DELETE", "/api/v2/fileSystems/fs/quota/user", base_url="https://weka.invalid", auth=False)

    @pytest.mark.parametrize(
        "message,expected",
        [
            ('{"message":"Route GET - /api/v2/fileSystems/x/quota/user does not exist"}', True),
            ('{"message":"uid: \'x\' not found"}', False),
            ("", False),
        ],
    )
    def test_route_message_classification(self, message, expected):
        assert weka._is_unknown_route(message) is expected

    def test_the_required_release_is_named(self):
        with pytest.raises(NotSupportedError, match=r"5\.1\.26"):
            with weka._user_quota_route():
                raise NotSupportedError("route not implemented on this cluster release")
