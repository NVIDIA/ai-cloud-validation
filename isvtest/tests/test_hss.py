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

"""Unit tests for High-Speed Storage (HSS) validations."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.hss import (
    HssChangelogAuditCheck,
    HssFlockMountCheck,
    HssLiveExpansionCheck,
    HssMultipathCheck,
    HssMultipleFilesystemsCheck,
    HssNonDisruptiveUpgradeCheck,
    HssParallelFsProvisioningCheck,
    HssQosThroughputCheck,
    HssQuotaEnforcementCheck,
    HssRdmaMemoryProtectionCheck,
    HssRootSquashCheck,
    HssStorageProvisioningCheck,
)

pytestmark = pytest.mark.unit


def _step_output(tests: dict[str, Any]) -> dict[str, Any]:
    """Build a step_output config dict for HSS tests."""
    return {"step_output": {"success": True, "platform": "storage", "tests": tests}}


def _all_passed(keys: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a tests dict where every required key passed, merged with extras."""
    tests = {key: {"passed": True} for key in keys}
    for key, value in (extra or {}).items():
        tests.setdefault(key, {})
        tests[key] = {**tests[key], **value}
    return tests


# (validation class, required keys, extras for a richer success message)
_CASES = [
    (
        HssStorageProvisioningCheck,
        ["api_available", "provisioned", "capacity_matches"],
        {"capacity_matches": {"capacity_gib": 100}},
        "100 GiB",
    ),
    (
        HssQosThroughputCheck,
        ["bandwidth_meets_min", "iops_meets_min"],
        {"bandwidth_meets_min": {"measured_mbps": 1200}, "iops_meets_min": {"measured_iops": 60000}},
        "1200 MB/s",
    ),
    (
        HssNonDisruptiveUpgradeCheck,
        ["upgrade_available", "io_continuity", "maintenance_deferrable"],
        {"maintenance_deferrable": {"max_defer_days": 14}},
        "14 days",
    ),
    (
        HssRdmaMemoryProtectionCheck,
        ["rdma_enabled", "local_key_enforced", "remote_key_enforced", "unauthorized_access_blocked"],
        {},
        "RDMA memory protection",
    ),
    (
        HssParallelFsProvisioningCheck,
        ["api_available", "filesystem_provisioned", "mount_successful"],
        {"filesystem_provisioned": {"fs_type": "lustre"}},
        "lustre",
    ),
    (
        HssMultipleFilesystemsCheck,
        ["multiple_filesystems", "within_total_capacity", "min_fs_size"],
        {"multiple_filesystems": {"filesystem_count": 2}, "min_fs_size": {"min_size_tib": 1}},
        "50 TiB",
    ),
    (
        HssLiveExpansionCheck,
        ["capacity_expanded", "inodes_expanded", "io_uninterrupted", "metadata_consistent"],
        {},
        "Live filesystem expansion",
    ),
    (
        HssQuotaEnforcementCheck,
        [
            "uid_quota_enforced",
            "gid_quota_enforced",
            "project_quota_enforced",
            "soft_quota_grace",
            "hard_quota_blocks",
        ],
        {},
        "quotas enforced",
    ),
    (
        HssRootSquashCheck,
        ["enable_root_squash", "root_squashed", "disable_root_squash", "root_unsquashed"],
        {},
        "Root-squash",
    ),
    (
        HssFlockMountCheck,
        ["mounted_with_flock", "flock_exclusive", "flock_shared", "flock_contention"],
        {},
        "flock",
    ),
    (
        HssChangelogAuditCheck,
        ["changelog_enabled", "records_file_ops", "records_dir_ops", "tracks_uid_gid"],
        {},
        "Changelog/audit",
    ),
    (
        HssMultipathCheck,
        ["multiple_paths", "all_servers_reachable", "failover_works"],
        {"multiple_paths": {"path_count": 2}, "all_servers_reachable": {"server_count": 3}},
        "2 paths to 3 storage servers",
    ),
]


@pytest.mark.parametrize(("cls", "required", "extra", "expected"), _CASES, ids=[c[0].__name__ for c in _CASES])
def test_all_passed(cls: type, required: list[str], extra: dict[str, Any], expected: str) -> None:
    """Each HSS check passes when all required subtests pass, with a rich message."""
    v = cls(config=_step_output(_all_passed(required, extra)))
    result = v.execute()
    assert result["passed"] is True
    assert expected in result["output"]


@pytest.mark.parametrize(("cls", "required", "extra", "expected"), _CASES, ids=[c[0].__name__ for c in _CASES])
def test_first_subtest_failed(cls: type, required: list[str], extra: dict[str, Any], expected: str) -> None:
    """Each HSS check fails and names the failing subtest when one does not pass."""
    tests = _all_passed(required, extra)
    failing = required[0]
    tests[failing] = {"passed": False, "error": "boom"}
    v = cls(config=_step_output(tests))
    result = v.execute()
    assert result["passed"] is False
    assert failing in result["error"]
    assert "boom" in result["error"]


@pytest.mark.parametrize("cls", [c[0] for c in _CASES], ids=[c[0].__name__ for c in _CASES])
def test_empty_tests(cls: type) -> None:
    """Each HSS check fails when the step output omits the tests block."""
    v = cls(config={"step_output": {}})
    result = v.execute()
    assert result["passed"] is False
    assert "tests" in result["error"]
