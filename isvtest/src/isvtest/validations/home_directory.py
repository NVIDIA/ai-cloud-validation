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

"""Provider-neutral home-directory storage validations."""

from __future__ import annotations

from typing import ClassVar

from isvtest.core.validation import BaseValidation, check_required_tests


class DirectoryFilesystemQuotaCheck(BaseValidation):
    """Validate configurable filesystem-wide quota limits (DIR01-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with filesystem_quota_configured, filesystem_quota_updated,
            filesystem_quota_enforced
    """

    description: ClassVar[str] = "Verify configurable filesystem-wide quota limits"

    def run(self) -> None:
        """Require quota configuration, update, and enforcement probes."""
        required = ["filesystem_quota_configured", "filesystem_quota_updated", "filesystem_quota_enforced"]
        if not check_required_tests(self, required, "Filesystem quota tests failed"):
            return
        self.set_passed("Filesystem-wide quota was configured, updated, and enforced")


class DirectoryUsageAccountingCheck(BaseValidation):
    """Validate storage usage accounting by numeric UID and GID (DIR01-02).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with uid_usage_accounted, gid_usage_accounted,
            identity_usage_isolated
    """

    description: ClassVar[str] = "Verify usage accounting for uid/gids"

    def run(self) -> None:
        """Require UID, GID, and identity-isolation accounting probes."""
        required = ["uid_usage_accounted", "gid_usage_accounted", "identity_usage_isolated"]
        if not check_required_tests(self, required, "UID/GID usage accounting tests failed"):
            return
        self.set_passed("Storage usage was accounted independently by UID and GID")


class DirectoryNfsAvailabilityCheck(BaseValidation):
    """Validate NFSv4 shared storage availability (DIR02-01).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with nfsv4_mounted, nfs_read_write, nfs_shared_visibility
    """

    description: ClassVar[str] = "Verify NFS protocol shared storage is available"

    def run(self) -> None:
        """Require NFSv4 mount, read/write, and shared-visibility probes."""
        required = ["nfsv4_mounted", "nfs_read_write", "nfs_shared_visibility"]
        if not check_required_tests(self, required, "NFS shared-storage tests failed"):
            return
        self.set_passed("NFSv4 shared storage mounted read/write with cross-mount visibility")
