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

"""Key-secret-management validations (requirement AUTH-XX).

``SpecifiedKeyAccessCheck`` (AUTH-XX-03): verify that a tenant-specified key
can be used to access other components "as possible" -- the serial console
(SOL) and network devices being the called-out examples.

The validation is provider-neutral: a step script reports the number of
specified keys available plus a list of access targets, each carrying a
tri-state ``key_access_enabled`` flag (``true`` = key access is enabled,
``false`` = explicitly disabled, ``null`` = could not be verified) and a
``reachable`` flag (the key has actually propagated and the component endpoint
is present). The check passes when at least one target is reachable with the
key, fails when a target's key-access path is explicitly disabled or broken,
and skips when access can only be left unverified.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from isvtest.core.validation import BaseValidation


class SpecifiedKeyAccessCheck(BaseValidation):
    """Validate specified-key access to out-of-band components (AUTH-XX-03).

    AUTH-XX-03 requires that a tenant-supplied key (e.g. an SSH key) can be used
    to reach other components, with the serial console (SOL) and network devices
    given as examples. Proving this reduces to checking the end-to-end access
    path for each reported target:

    * a specified key exists (``specified_keys >= 1``); without one there is
      nothing to evidence access with, so the check skips;
    * for each access target, ``key_access_enabled`` records whether key-based
      access to that component is enabled, and ``reachable`` records whether the
      key has actually propagated and the component endpoint is present.

    A target is "accessible" when key access is enabled *and* it is reachable.
    The check passes when at least ``min_accessible_targets`` targets are
    accessible. A target whose key access is explicitly disabled, or enabled but
    not reachable, is a concrete failure. A target the script could only leave
    unverified (``key_access_enabled`` is ``null``, e.g. provider-managed
    network-device access) neither passes nor fails -- if no verifiable target
    is accessible, the check skips rather than fabricating a pass.

    Config:
        step_output: Step output containing the specified-key access evidence.
        min_accessible_targets: Minimum number of targets that must be accessible
            via the specified key for the check to pass (default: 1).

    Step output (from query_key_access.py):
        success: bool
        platform: str
        site_id: str
        specified_keys: int -- distinct tenant-specified keys synced to the site
        access_targets: list[dict]:
            type: str -- e.g. "serial_console", "network_device"
            name: str
            key_access_enabled: bool | None -- true / false / null (unverified)
            reachable: bool
            detail: str
    """

    description: ClassVar[str] = "Check a tenant-specified key can access out-of-band components (SOL, network devices)"
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Validate that a specified key can access at least one reported component."""
        step_output = self.config.get("step_output", {})

        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Specified-key access validation skipped")

        if not step_output.get("success"):
            self.set_failed(f"Specified-key access step failed: {step_output.get('error', 'Unknown error')}")
            return

        targets = step_output.get("access_targets")
        if not isinstance(targets, list):
            self.set_failed("Specified-key access step output is missing the 'access_targets' list")
            return

        specified_keys = step_output.get("specified_keys")
        if not isinstance(specified_keys, int) or isinstance(specified_keys, bool):
            self.set_failed("Specified-key access step output is missing integer 'specified_keys'")
            return

        min_accessible = self._parse_positive_int("min_accessible_targets", default=1)
        if min_accessible is None:
            return

        if specified_keys < 1:
            pytest.skip("No tenant-specified key is registered/synced to the site; cannot evidence key-based access")

        accessible = 0
        concrete_failures: list[str] = []
        unverified: list[str] = []

        for idx, raw_target in enumerate(targets):
            target = raw_target if isinstance(raw_target, dict) else {}
            label = target.get("name") or target.get("type") or f"target_{idx}"
            subtest_name = f"target_{target.get('type') or idx}"
            enabled = target.get("key_access_enabled")
            reachable = target.get("reachable") is True
            detail = target.get("detail") or ""

            if enabled is True and reachable:
                accessible += 1
                self.report_subtest(
                    subtest_name, passed=True, message=f"{label}: accessible via specified key ({detail})"
                )
            elif enabled is True:
                concrete_failures.append(f"{label} (enabled but not reachable: {detail})")
                self.report_subtest(
                    subtest_name, passed=False, message=f"{label}: key access enabled but not reachable ({detail})"
                )
            elif enabled is False:
                concrete_failures.append(f"{label} (key access disabled: {detail})")
                self.report_subtest(subtest_name, passed=False, message=f"{label}: key access disabled ({detail})")
            else:
                unverified.append(f"{label} ({detail})" if detail else label)
                self.report_subtest(subtest_name, passed=False, skipped=True, message=f"{label}: unverified ({detail})")

        if concrete_failures:
            self.set_failed(f"Specified-key access not established: {'; '.join(concrete_failures)}")
            return

        if accessible >= min_accessible:
            self.set_passed(
                f"Specified key ({specified_keys} key(s)) can access "
                f"{accessible} of {len(targets)} reported component(s)"
            )
            return

        if unverified:
            pytest.skip(f"Specified-key access incomplete; could not verify target(s): {', '.join(unverified)}")

        self.set_failed(f"No component is accessible via the specified key (need {min_accessible}, found {accessible})")
