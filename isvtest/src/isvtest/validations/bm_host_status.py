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

"""Bare metal per-host status log validations."""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation

DEFAULT_SOURCES: tuple[str, ...] = ("journalctl_recent", "dmesg_recent")


class BmHostStatusLog(BaseValidation):
    """Verify the BM host emits a per-host status log over time.

    Default: pass if at least one configured source has fresh entries.
    Strict (``required_sources`` non-empty): every named source must pass.

    Config:
        required_sources: Optional list of source keys (e.g.
            ``["journalctl_recent"]``) that must all pass.
    """

    description: ClassVar[str] = "Verify per-host status log (journalctl/dmesg) is producing fresh entries"
    timeout: ClassVar[int] = 60
    labels: ClassVar[tuple[str, ...]] = ("bare_metal",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests: dict[str, Any] = step_output.get("tests", {}) or {}

        required_raw = self.config.get("required_sources", []) or []
        if not isinstance(required_raw, list) or not all(isinstance(s, str) and s for s in required_raw):
            self.set_failed(f"`required_sources` must be a list of non-empty strings, got {required_raw!r}")
            return

        strict = bool(required_raw)
        sources: tuple[str, ...] = tuple(required_raw) if strict else DEFAULT_SOURCES

        if not tests:
            self.set_failed("No 'tests' in step output; the host_status_log step did not emit per-source results")
            return

        passed_sources: list[str] = []
        failed_sources: list[str] = []
        missing_sources: list[str] = []
        summaries: list[str] = []

        for source in sources:
            entry = tests.get(source)
            if entry is None:
                missing_sources.append(source)
                self.report_subtest(source, False, "source not reported")
                continue
            if entry.get("passed"):
                passed_sources.append(source)
                self.report_subtest(source, True, entry.get("message", ""))
                summaries.append(f"{source}: {entry.get('message', 'ok')}")
            else:
                failed_sources.append(source)
                msg = entry.get("message") or entry.get("error") or "no recent entries"
                self.report_subtest(source, False, msg)
                summaries.append(f"{source}: {msg}")

        if strict:
            if failed_sources or missing_sources:
                problems = failed_sources + [f"{s} (missing)" for s in missing_sources]
                self.set_failed(
                    f"Strict mode: required sources did not all pass ({', '.join(problems)})",
                    output="\n".join(summaries),
                )
                return
            self.set_passed(f"All required sources passed: {', '.join(passed_sources)}")
            return

        if not passed_sources:
            details = "; ".join(summaries) or "no sources reported"
            self.set_failed(f"No status log source has fresh entries: {details}")
            return

        self.set_passed(
            f"Fresh entries from {len(passed_sources)}/{len(sources)} source(s): {', '.join(passed_sources)}"
        )
