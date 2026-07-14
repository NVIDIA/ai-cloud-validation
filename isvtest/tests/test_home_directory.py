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

"""Unit tests for home-directory storage validations."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.home_directory import (
    DirectoryFilesystemQuotaCheck,
    DirectoryNfsAvailabilityCheck,
    DirectoryUsageAccountingCheck,
)

pytestmark = pytest.mark.unit

_CASES = [
    (
        DirectoryFilesystemQuotaCheck,
        ["filesystem_quota_configured", "filesystem_quota_updated", "filesystem_quota_enforced"],
        "configured, updated, and enforced",
    ),
    (
        DirectoryUsageAccountingCheck,
        ["uid_usage_accounted", "gid_usage_accounted", "identity_usage_isolated"],
        "accounted independently",
    ),
    (
        DirectoryNfsAvailabilityCheck,
        ["nfsv4_mounted", "nfs_read_write", "nfs_shared_visibility"],
        "NFSv4 shared storage",
    ),
]


def _config(tests: dict[str, Any]) -> dict[str, Any]:
    """Build validation config containing a provider step output."""
    return {"step_output": {"success": True, "platform": "storage", "tests": tests}}


@pytest.mark.parametrize(("validation_class", "required", "message"), _CASES)
def test_home_directory_check_passes(
    validation_class: type,
    required: list[str],
    message: str,
) -> None:
    """Each check passes when all of its required provider probes pass."""
    validation = validation_class(config=_config({name: {"passed": True} for name in required}))
    result = validation.execute()
    assert result["passed"] is True
    assert message in result["output"]


@pytest.mark.parametrize(("validation_class", "required", "message"), _CASES)
def test_home_directory_check_surfaces_probe_failure(
    validation_class: type,
    required: list[str],
    message: str,
) -> None:
    """Each check reports the failed provider probe and its error."""
    tests = {name: {"passed": True} for name in required}
    tests[required[0]] = {"passed": False, "error": "probe failed"}
    validation = validation_class(config=_config(tests))
    result = validation.execute()
    assert result["passed"] is False
    assert required[0] in result["error"]
    assert "probe failed" in result["error"]


@pytest.mark.parametrize("validation_class", [case[0] for case in _CASES])
def test_home_directory_check_rejects_missing_tests(validation_class: type) -> None:
    """Each check fails when the provider omitted its tests block."""
    result = validation_class(config={"step_output": {}}).execute()
    assert result["passed"] is False
    assert "tests" in result["error"]
