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

"""Tests for the isvctl test orchestration summary lines."""

import isvctl.cli.test as test_cli


def test_validation_result_detail_compacts_successful_subtests() -> None:
    """Successful validations with probes render an aggregate instead of their long message."""
    detail = test_cli._validation_result_detail(
        {
            "passed": True,
            "skipped": False,
            "state": "passed",
            "message": "long; member; output",
            "subtest_summary": {"total": 6, "passed": 6, "failed": 0, "skipped": 0},
        }
    )

    assert detail == "6 subtests passed"


def test_validation_result_detail_preserves_failures() -> None:
    """Failed validations keep their actionable message even when they report probes."""
    detail = test_cli._validation_result_detail(
        {
            "passed": False,
            "skipped": False,
            "state": "failed",
            "message": "RdmaCheck: worker-a -> worker-b timed out",
            "subtest_summary": {"total": 6, "passed": 5, "failed": 1, "skipped": 0},
        }
    )

    assert detail == "RdmaCheck: worker-a -> worker-b timed out"


def test_validation_result_detail_counts_successful_runs_with_skips() -> None:
    """An allowed skipped probe remains visible in the concise success summary."""
    detail = test_cli._validation_result_detail(
        {
            "passed": True,
            "skipped": False,
            "state": "passed",
            "message": "long output",
            "subtest_summary": {"total": 6, "passed": 5, "failed": 0, "skipped": 1},
        }
    )

    assert detail == "6 subtests: 5 passed, 0 failed, 1 skipped"
