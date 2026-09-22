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

"""Hermetic tests for the AWS FSx Lustre storage provider shim."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from isvtest.core.storage_provider import StorageApiError

_SHIM = (
    Path(__file__).resolve().parents[2]
    / "isvctl"
    / "configs"
    / "providers"
    / "aws"
    / "scripts"
    / "storage"
    / "fsx-lustre"
    / "api.py"
)


def _load_shim():
    """Load the FSx Lustre shim by path, matching manifest-loader behavior."""
    spec = importlib.util.spec_from_file_location("fsx_lustre_api_under_test", _SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fsx = _load_shim()


class _FakeSession:
    """boto3 Session stand-in: a resolved region and clients that record their region."""

    def __init__(self, region_name: str | None) -> None:
        self.region_name = region_name
        self.client_regions: dict[str, str | None] = {}

    def client(self, service: str, region_name: str | None = None) -> object:
        self.client_regions[service] = region_name
        return object()


def _api(session: _FakeSession):
    """Build the shim without STS or other AWS calls."""
    return fsx.AwsFsxLustreApi(session=session, account_id="123456789012")


def test_region_falls_back_to_the_session_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """A region from AWS_DEFAULT_REGION or the AWS profile is enough; AWS_REGION is not required."""
    monkeypatch.delenv("AWS_REGION", raising=False)
    session = _FakeSession("us-west-2")

    _api(session)

    assert session.client_regions == {"service-quotas": "us-west-2", "fsx": "us-west-2", "sts": "us-west-2"}


def test_aws_region_env_wins_over_the_session_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS_REGION keeps precedence so an explicit override still targets the cluster's region."""
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    session = _FakeSession("us-west-2")

    _api(session)

    assert set(session.client_regions.values()) == {"eu-central-1"}


def test_missing_region_names_every_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no region anywhere, the error lists each place a region can come from."""
    monkeypatch.delenv("AWS_REGION", raising=False)

    with pytest.raises(StorageApiError, match="AWS_REGION, AWS_DEFAULT_REGION, or a region in the AWS profile"):
        _api(_FakeSession(None))
