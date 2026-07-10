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

"""High-Speed Storage (HSS) validations for step outputs.

Provider-neutral assertions over the JSON contract emitted by storage
provisioning/parallel-filesystem scripts. Each validation only inspects
``step_output.tests`` field names/values, so any provider (vendor/NCP API,
parallel filesystem, ...) passes as long as its script emits the right fields.

Classes map to the HSS requirement family in ``docs/test-plan.yaml``:

SDS Controller
    - HssStorageProvisioningCheck    HSS01-01  provisioning via vendor/NCP API
    - HssQosThroughputCheck          HSS02-01  QoS: min bandwidth and IOPS
    - HssNonDisruptiveUpgradeCheck   HSS05-01  non-disruptive upgrades
    - HssRdmaMemoryProtectionCheck   HSS06-01  RDMA memory-protection keys

Parallel File System Services
    - HssParallelFsProvisioningCheck HSS07-01  parallel FS provisioned via API
    - HssMultipleFilesystemsCheck    HSS09-01  multiple FS within total capacity
    - HssLiveExpansionCheck          HSS10-01  live FS expansion
    - HssQuotaEnforcementCheck       HSS12-01  uid/gid/project quotas
    - HssRootSquashCheck             HSS13-01  root-squash enable/disable
    - HssFlockMountCheck             HSS14-01  mount with flock
    - HssChangelogAuditCheck         HSS15-01  changelog/audit accessible
    - HssMultipathCheck              HSS18-01  client multipathing
"""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation, check_required_tests


def _tests(validation: BaseValidation) -> dict[str, Any]:
    """Return the ``tests`` block from a validation's step output."""
    return validation.config.get("step_output", {}).get("tests", {})


class HssStorageProvisioningCheck(BaseValidation):
    """Validate storage provisioning via a vendor/NCP API (HSS01-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with api_available, provisioned, capacity_matches
    """

    description: ClassVar[str] = "Check storage provisioning via vendor/NCP API"

    def run(self) -> None:
        """Validate that a volume was provisioned via the storage API."""
        required = ["api_available", "provisioned", "capacity_matches"]
        if not check_required_tests(self, required, "Storage provisioning tests failed"):
            return

        capacity = _tests(self).get("capacity_matches", {}).get("capacity_gib", "N/A")
        self.set_passed(f"Storage provisioned via vendor/NCP API ({capacity} GiB, capacity matches request)")


class HssQosThroughputCheck(BaseValidation):
    """Validate provisioned QoS meets requested minimum bandwidth and IOPS (HSS02-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with bandwidth_meets_min, iops_meets_min
    """

    description: ClassVar[str] = "Check QoS provisioned throughput meets minimum bandwidth and IOPS"

    def run(self) -> None:
        """Validate measured bandwidth and IOPS meet the requested minimums."""
        required = ["bandwidth_meets_min", "iops_meets_min"]
        if not check_required_tests(self, required, "QoS throughput tests failed"):
            return

        tests = _tests(self)
        bw = tests.get("bandwidth_meets_min", {}).get("measured_mbps", "N/A")
        iops = tests.get("iops_meets_min", {}).get("measured_iops", "N/A")
        self.set_passed(f"QoS met: {bw} MB/s bandwidth and {iops} IOPS >= requested minimums")


class HssNonDisruptiveUpgradeCheck(BaseValidation):
    """Validate non-disruptive upgrades with deferrable maintenance (HSS05-01).

    NVIDIA can defer maintenance up to 2 weeks, and the upgrade must not
    interrupt in-flight I/O.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with upgrade_available, io_continuity, maintenance_deferrable
    """

    description: ClassVar[str] = "Check non-disruptive upgrades with deferrable maintenance"

    def run(self) -> None:
        """Validate the upgrade is non-disruptive and can be deferred >= 14 days."""
        required = ["upgrade_available", "io_continuity", "maintenance_deferrable"]
        if not check_required_tests(self, required, "Non-disruptive upgrade tests failed"):
            return

        days = _tests(self).get("maintenance_deferrable", {}).get("max_defer_days", "N/A")
        self.set_passed(
            f"Non-disruptive upgrade verified (I/O uninterrupted, maintenance deferrable up to {days} days)"
        )


class HssRdmaMemoryProtectionCheck(BaseValidation):
    """Validate RDMA memory protection via authorization keys (HSS06-01).

    Storage systems using RDMA must enforce memory protection via authorization
    keys for both local and remote access.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with rdma_enabled, local_key_enforced, remote_key_enforced,
               unauthorized_access_blocked
    """

    description: ClassVar[str] = "Check RDMA memory protection via authorization keys"

    def run(self) -> None:
        """Validate RDMA local/remote key enforcement blocks unauthorized access."""
        required = [
            "rdma_enabled",
            "local_key_enforced",
            "remote_key_enforced",
            "unauthorized_access_blocked",
        ]
        if not check_required_tests(self, required, "RDMA memory protection tests failed"):
            return

        self.set_passed("RDMA memory protection enforced via authorization keys (local + remote, unauthorized blocked)")


class HssParallelFsProvisioningCheck(BaseValidation):
    """Validate a parallel high-speed filesystem can be provisioned via API (HSS07-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with api_available, filesystem_provisioned, mount_successful
    """

    description: ClassVar[str] = "Check parallel high-speed filesystem provisioning via API"

    def run(self) -> None:
        """Validate a parallel filesystem was provisioned via API and mounted."""
        required = ["api_available", "filesystem_provisioned", "mount_successful"]
        if not check_required_tests(self, required, "Parallel filesystem provisioning tests failed"):
            return

        fs_type = _tests(self).get("filesystem_provisioned", {}).get("fs_type", "parallel")
        self.set_passed(f"Parallel high-speed filesystem provisioned via API and mounted ({fs_type})")


class HssMultipleFilesystemsCheck(BaseValidation):
    """Validate multiple filesystems within total capacity (HSS09-01).

    Multiple filesystems can exist within total capacity, and the minimum
    filesystem size is <= 50 TiB.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with multiple_filesystems, within_total_capacity, min_fs_size
    """

    description: ClassVar[str] = "Check multiple filesystems can exist within total capacity"

    def run(self) -> None:
        """Validate multiple filesystems fit total capacity with min size <= 50 TiB."""
        required = ["multiple_filesystems", "within_total_capacity", "min_fs_size"]
        if not check_required_tests(self, required, "Multiple filesystem tests failed"):
            return

        tests = _tests(self)
        count = tests.get("multiple_filesystems", {}).get("filesystem_count", "N/A")
        min_size = tests.get("min_fs_size", {}).get("min_size_tib", "N/A")
        self.set_passed(f"{count} filesystems within total capacity (minimum FS size {min_size} TiB <= 50 TiB)")


class HssLiveExpansionCheck(BaseValidation):
    """Validate live filesystem expansion (HSS10-01).

    Capacity, inodes, I/O throughput, and metadata all expand live without
    disrupting active workloads.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with capacity_expanded, inodes_expanded, io_uninterrupted,
               metadata_consistent
    """

    description: ClassVar[str] = "Check live filesystem expansion (capacity, inodes, IO, metadata)"

    def run(self) -> None:
        """Validate capacity/inodes/IO/metadata expand live without interruption."""
        required = [
            "capacity_expanded",
            "inodes_expanded",
            "io_uninterrupted",
            "metadata_consistent",
        ]
        if not check_required_tests(self, required, "Live expansion tests failed"):
            return

        self.set_passed("Live filesystem expansion verified (capacity, inodes, IO uninterrupted, metadata consistent)")


class HssQuotaEnforcementCheck(BaseValidation):
    """Validate uid/gid/project-id soft and hard quotas with enforcement (HSS12-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with uid_quota_enforced, gid_quota_enforced,
               project_quota_enforced, soft_quota_grace, hard_quota_blocks
    """

    description: ClassVar[str] = "Check uid/gid/project-id soft and hard quotas with enforcement"

    def run(self) -> None:
        """Validate uid/gid/project quotas enforce soft grace and hard limits."""
        required = [
            "uid_quota_enforced",
            "gid_quota_enforced",
            "project_quota_enforced",
            "soft_quota_grace",
            "hard_quota_blocks",
        ]
        if not check_required_tests(self, required, "Quota enforcement tests failed"):
            return

        self.set_passed("uid/gid/project-id quotas enforced (soft grace honored, hard limit blocks writes)")


class HssRootSquashCheck(BaseValidation):
    """Validate root-squash can be enabled and disabled at any time (HSS13-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with enable_root_squash, root_squashed, disable_root_squash,
               root_unsquashed
    """

    description: ClassVar[str] = "Check root-squash can be enabled and disabled at any time"

    def run(self) -> None:
        """Validate root-squash toggles on/off and takes effect each way."""
        required = [
            "enable_root_squash",
            "root_squashed",
            "disable_root_squash",
            "root_unsquashed",
        ]
        if not check_required_tests(self, required, "Root-squash tests failed"):
            return

        self.set_passed("Root-squash toggled on and off at runtime (root mapped to anon when enabled)")


class HssFlockMountCheck(BaseValidation):
    """Validate the filesystem can be mounted with flock (HSS14-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with mounted_with_flock, flock_exclusive, flock_shared,
               flock_contention
    """

    description: ClassVar[str] = "Check the filesystem can be mounted with flock"

    def run(self) -> None:
        """Validate flock mount grants exclusive/shared locks and enforces contention."""
        required = [
            "mounted_with_flock",
            "flock_exclusive",
            "flock_shared",
            "flock_contention",
        ]
        if not check_required_tests(self, required, "flock mount tests failed"):
            return

        self.set_passed("Filesystem mounted with flock (exclusive + shared locks work, contention enforced)")


class HssChangelogAuditCheck(BaseValidation):
    """Validate changelog/audit data is accessible (HSS15-01).

    Tracking by uid/gid for file and directory operations.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with changelog_enabled, records_file_ops, records_dir_ops,
               tracks_uid_gid
    """

    description: ClassVar[str] = "Check changelog/audit data is accessible (uid/gid, file/dir ops)"

    def run(self) -> None:
        """Validate the changelog records file/dir ops with uid/gid attribution."""
        required = [
            "changelog_enabled",
            "records_file_ops",
            "records_dir_ops",
            "tracks_uid_gid",
        ]
        if not check_required_tests(self, required, "Changelog/audit tests failed"):
            return

        self.set_passed("Changelog/audit data accessible (file + dir operations tracked by uid/gid)")


class HssMultipathCheck(BaseValidation):
    """Validate client multipathing to all storage servers (HSS18-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with multiple_paths, all_servers_reachable, failover_works
    """

    description: ClassVar[str] = "Check client multipathing to all storage servers"

    def run(self) -> None:
        """Validate the client has redundant paths to every storage server."""
        required = ["multiple_paths", "all_servers_reachable", "failover_works"]
        if not check_required_tests(self, required, "Multipath tests failed"):
            return

        tests = _tests(self)
        paths = tests.get("multiple_paths", {}).get("path_count", "N/A")
        servers = tests.get("all_servers_reachable", {}).get("server_count", "N/A")
        self.set_passed(f"Client multipathing verified ({paths} paths to {servers} storage servers, failover works)")
