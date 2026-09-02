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

"""Tests for ``StorageUserQuotaEnforcementCheck`` helpers."""

from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.core.storage import Provider
from isvtest.core.storage_provider import (
    CAP_USER_QUOTA_DELETE,
    CAP_USER_QUOTA_GET,
    CAP_USER_QUOTA_LIST,
    CAP_USER_QUOTA_SET,
    NotFoundError,
    QuotaLimits,
    UserQuota,
)
from isvtest.validations.storage_user_quota_enforcement import StorageUserQuotaEnforcementCheck

_WANT = 1 << 30
_FULL_USER_QUOTA_CAPS = (
    CAP_USER_QUOTA_SET,
    CAP_USER_QUOTA_GET,
    CAP_USER_QUOTA_LIST,
    CAP_USER_QUOTA_DELETE,
)


def _user_quota_provider(name: str = "full", *, api: object | None = None) -> Provider:
    return Provider(
        name=name,
        volume_type="file",
        tenant_id="tenant",
        shim_kind="python",
        api=api or object(),
        expected_capabilities={cap: True for cap in _FULL_USER_QUOTA_CAPS},
        capability_states={},
    )


class _Api:
    def __init__(self, *sequence):
        self.sequence = list(sequence)
        self.calls = 0
        self.requests = []

    def get_user_quota(self, req):
        self.calls += 1
        self.requests.append(req)
        item = self.sequence[min(self.calls - 1, len(self.sequence) - 1)]
        if isinstance(item, Exception):
            raise item
        return UserQuota(
            tenant_id="t",
            volume_id=req.volume_id,
            user=req.user,
            hard=None if item is None else QuotaLimits(bytes=item),
        )


@pytest.fixture
def check():
    c = StorageUserQuotaEnforcementCheck()
    c._probe_user = "65534"
    return c


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("isvtest.validations.storage_user_quota_enforcement.time.sleep"):
        yield


class TestAwaitHard:
    def test_returns_on_first_read_when_already_published(self, check):
        api = _Api(_WANT)
        assert check._await_hard(api, "v1", _WANT) == (True, _WANT)
        assert api.calls == 1
        assert api.requests[0].user == "65534"

    def test_tolerates_not_found_before_the_record_appears(self, check):
        api = _Api(NotFoundError("nope"), _WANT)
        assert check._await_hard(api, "v1", _WANT) == (True, _WANT)

    def test_gives_up_and_reports_the_last_value_seen(self, check):
        api = _Api(None)
        with patch(
            "isvtest.validations.storage_user_quota_enforcement.time.monotonic",
            side_effect=[0.0, 100.0, 200.0],
        ):
            assert check._await_hard(api, "v1", _WANT) == (False, None)


class TestCandidateSelection:
    def test_set_only_user_quota_provider_is_skipped(self):
        provider = Provider(
            name="set-only",
            volume_type="file",
            tenant_id="tenant",
            shim_kind="python",
            api=object(),
            expected_capabilities={CAP_USER_QUOTA_SET: True},
        )
        check = StorageUserQuotaEnforcementCheck(config={"manifest_path": "manifest.yaml"})
        with (
            patch(
                "isvtest.validations.storage_user_quota_enforcement.load_provider_registry",
                return_value=[provider],
            ),
            patch("isvtest.validations.storage_user_quota_enforcement.is_k8s_available") as available,
        ):
            check.run()
        assert check.passed
        assert "full user-quota CRUD" in check.message
        available.assert_not_called()

    def test_full_user_quota_provider_reaches_k8s_availability_check(self):
        provider = _user_quota_provider()
        check = StorageUserQuotaEnforcementCheck(
            config={"manifest_path": "manifest.yaml", "storage_class": "shared-fs"}
        )
        with (
            patch(
                "isvtest.validations.storage_user_quota_enforcement.load_provider_registry",
                return_value=[provider],
            ),
            patch("isvtest.validations.storage_user_quota_enforcement.is_k8s_available", return_value=False),
        ):
            check.run()
        assert check.passed
        assert any("no reachable Kubernetes" in result["message"] for result in check._subtest_results)


class TestPodReuseConfig:
    def test_pod_name_without_pvc_name_fails_loudly(self):
        check = StorageUserQuotaEnforcementCheck(config={"pod_name": "probe"})
        check.run()
        assert not check.passed
        assert "pod_name requires pvc_name" in check.message


class TestK8sWriterUid:
    def test_match_is_silent(self, check):
        check._probe_user = "65534"
        with patch.object(
            check, "_exec", return_value=CommandResult(exit_code=0, stdout="65534\n", stderr="", duration=0.0)
        ):
            assert check._k8s_writer_uid_error("ns", "pod") is None

    def test_mismatch_tells_operator_to_align(self, check):
        check._probe_user = "65534"
        with patch.object(
            check, "_exec", return_value=CommandResult(exit_code=0, stdout="0\n", stderr="", duration=0.0)
        ):
            msg = check._k8s_writer_uid_error("ns", "pod")
        assert msg is not None
        assert "uid 0" in msg
        assert "65534" in msg
        assert "align" in msg

    def test_non_numeric_probe_user_fails_the_identity_check(self, check):
        check._probe_user = "alice"
        msg = check._k8s_writer_uid_error("ns", "pod")
        assert msg is not None
        assert "not a numeric UID" in msg
