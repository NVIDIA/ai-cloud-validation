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

"""Tests for the Kubernetes ServiceAccount IAM evidence contract."""

from typing import Any

import pytest

from isvtest.validations.k8s_service_account_iam import K8sServiceAccountIamCheck

PROBES = ("identity", "allowed_access", "out_of_scope_denied")


def evidence() -> dict[str, Any]:
    """Return complete evidence from a workload using a scoped platform identity."""
    return {
        "success": True,
        "platform": "kubernetes",
        "tests": {probe: {"passed": True} for probe in PROBES},
    }


def run_check(output: Any) -> K8sServiceAccountIamCheck:
    """Run the check with provider output."""
    check = K8sServiceAccountIamCheck(config={"step_output": output})
    check.run()
    return check


def test_complete_evidence_passes() -> None:
    """The pod must exercise its bound identity and both permission outcomes."""
    check = run_check(evidence())
    assert check.passed, check.message


@pytest.mark.parametrize("probe", PROBES)
def test_missing_or_failed_probe_fails(probe: str) -> None:
    """Each probe is required; one cannot be inferred from the others."""
    missing = evidence()
    del missing["tests"][probe]
    assert not run_check(missing).passed
    failed = evidence()
    failed["tests"][probe] = {"passed": False, "error": "AccessDenied"}
    check = run_check(failed)
    assert not check.passed and "AccessDenied" in check.message


def test_failed_execution_without_tests_fails() -> None:
    """A probe that never reported results is a failure, not a pass."""
    assert not run_check({"success": False, "platform": "kubernetes", "error": "API unreachable"}).passed


def test_missing_provider_step_skips() -> None:
    """Standalone suites without a provider probe cannot start this test."""
    with pytest.raises(pytest.skip.Exception, match="provider"):
        run_check(None)


def test_cleanup_failure_overrides_success() -> None:
    """A leaked test resource is never hidden by an otherwise good result."""
    output = evidence()
    output["cleanup_errors"] = ["delete role failed"]
    assert not run_check(output).passed
