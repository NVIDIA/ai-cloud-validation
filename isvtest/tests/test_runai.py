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

"""Tests for the Run:AI compatibility kit validation."""

from __future__ import annotations

from typing import Any

from isvtest.validations.runai import RunAICompatibilityCheck


def _kit_output(**compatibility_overrides: Any) -> dict[str, Any]:
    """Build a passing run_compatibility_kit step output."""
    compatibility: dict[str, Any] = {
        "passed": True,
        "message": "89/106 tests passed (0 failed, 0 broken, 17 skipped)",
    }
    compatibility.update(compatibility_overrides)
    return {
        "success": True,
        "platform": "k8s",
        "test_name": "runai_compatibility",
        "tests": {"compatibility": compatibility},
    }


def _run_check(step_output: dict[str, Any]) -> dict[str, Any]:
    return RunAICompatibilityCheck(config={"step_output": step_output}).execute()


def test_compatibility_check_passes_with_summary_message() -> None:
    result = _run_check(_kit_output())

    assert result["passed"] is True
    assert result["output"] == "89/106 tests passed (0 failed, 0 broken, 17 skipped)"


def test_compatibility_check_fails_with_kit_error_detail() -> None:
    result = _run_check(
        _kit_output(
            passed=False,
            error="Failed tests: Create distributed policy and verify its applied",
        )
    )

    assert result["passed"] is False
    assert "Run:AI compatibility failed" in result["error"]
    assert "Create distributed policy" in result["error"]


def test_compatibility_check_fails_without_tests_block() -> None:
    result = _run_check({"success": True, "platform": "k8s"})

    assert result["passed"] is False
    assert "No 'tests' in step output" in result["error"]


def test_compatibility_check_fails_when_compatibility_missing() -> None:
    result = _run_check({"success": True, "platform": "k8s", "tests": {"other": {"passed": True}}})

    assert result["passed"] is False
    assert "compatibility" in result["error"]
