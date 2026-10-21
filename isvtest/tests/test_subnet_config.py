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

"""Regression coverage for independent baseline and multi-AZ subnet checks."""

from copy import deepcopy
from typing import Any

import pytest

from isvtest.validations import SubnetConfigCheck, SubnetMultiAzCheck


@pytest.fixture
def single_az_output() -> dict[str, Any]:
    """Successful baseline operations with an explicitly failed multi-AZ result."""
    return {
        "success": True,
        "subnets": [{"az": "site-a"}, {"az": "site-a"}],
        "tests": {
            "create_vpc": {"passed": True},
            "create_subnets": {"passed": True},
            "subnets_available": {"passed": True},
            "route_table_exists": {"passed": True},
            "az_distribution": {"passed": False, "az_count": 1, "azs": ["site-a"]},
        },
    }


@pytest.mark.parametrize("az_result", [None, {"passed": False}, {"skipped": True}])
def test_baseline_does_not_require_multi_az(single_az_output: dict[str, Any], az_result: Any) -> None:
    """Absent, failed, or skipped distribution does not invalidate baseline evidence."""
    single_az_output["tests"].pop("az_distribution")
    if az_result is not None:
        single_az_output["tests"]["az_distribution"] = az_result
    result = SubnetConfigCheck(config={"step_output": single_az_output}).execute()
    assert result["passed"] is True
    assert "az_distribution" not in {t["name"] for t in result["subtests"]}


@pytest.mark.parametrize("legacy_option", [False, True])
def test_legacy_multi_az_option(single_az_output: dict[str, Any], legacy_option: bool) -> None:
    """Explicit legacy opt-in remains strict, while opt-out finally works."""
    result = SubnetConfigCheck(config={"step_output": single_az_output, "require_multi_az": legacy_option}).execute()
    assert result["passed"] is (not legacy_option)


@pytest.mark.parametrize("check", [SubnetConfigCheck, SubnetMultiAzCheck])
@pytest.mark.parametrize("missing", ["create_vpc", "create_subnets", "subnets_available", "route_table_exists"])
def test_both_checks_require_baseline_operations(
    single_az_output: dict[str, Any], check: type[SubnetConfigCheck], missing: str
) -> None:
    """Splitting the checks must not weaken baseline evidence requirements."""
    single_az_output["tests"]["az_distribution"] = {"passed": True, "az_count": 2}
    single_az_output["tests"].pop(missing)
    result = check(config={"step_output": single_az_output}).execute()
    assert result["passed"] is False
    assert missing in result["error"]


@pytest.mark.parametrize("check", [SubnetConfigCheck, SubnetMultiAzCheck])
def test_minimum_subnets_still_enforced(single_az_output: dict[str, Any], check: type[SubnetConfigCheck]) -> None:
    """Neither check may pass on fewer than its configured subnet count."""
    result = check(config={"step_output": single_az_output, "min_subnets": 3}).execute()
    assert result["passed"] is False


@pytest.mark.parametrize("az_count", [None, 0, 1, True, "2"])
def test_multi_az_requires_valid_count(single_az_output: dict[str, Any], az_count: Any) -> None:
    """A reported pass cannot replace the evidence of at least two zones."""
    single_az_output["tests"]["az_distribution"] = {"passed": True, "az_count": az_count}
    result = SubnetMultiAzCheck(config={"step_output": single_az_output}).execute()
    assert result["passed"] is False


def test_multi_az_cannot_be_disabled(single_az_output: dict[str, Any]) -> None:
    """The separately named check always enforces its advertised requirement."""
    result = SubnetMultiAzCheck(config={"step_output": single_az_output, "require_multi_az": False}).execute()
    assert result["passed"] is False


def test_multi_az_positive_case(single_az_output: dict[str, Any]) -> None:
    """The same two-zone evidence passes both independent validators."""
    output = deepcopy(single_az_output)
    output["subnets"][1]["az"] = "site-b"
    output["tests"]["az_distribution"] = {"passed": True, "az_count": 2, "azs": ["site-a", "site-b"]}
    for check in (SubnetConfigCheck, SubnetMultiAzCheck):
        result = check(config={"step_output": output}).execute()
        assert result["passed"] is True
