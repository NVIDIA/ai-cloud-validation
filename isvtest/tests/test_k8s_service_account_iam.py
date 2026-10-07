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

from copy import deepcopy
from typing import Any

import pytest

from isvtest.validations.k8s_service_account_iam import K8sServiceAccountIamCheck


def evidence() -> dict[str, Any]:
    """Return complete evidence from a workload using a scoped platform identity."""
    return {
        "success": True,
        "platform": "kubernetes",
        "test_name": "service_account_iam",
        "service_account": "test/workload",
        "workload_service_account": "test/workload",
        "expected_identity": "role-id",
        "observed_identity": "role-id",
        "federated_token_used": True,
        "allowed_access": True,
        "out_of_scope_denied": True,
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


@pytest.mark.parametrize("field", [field for field in evidence() if field not in {"platform", "test_name"}])
def test_missing_proof_never_passes(field: str) -> None:
    """Required runtime proof cannot be inferred from other successful checks."""
    output = evidence()
    del output[field]
    assert not run_check(output).passed


@pytest.mark.parametrize("field", ["success", "federated_token_used", "allowed_access", "out_of_scope_denied"])
@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_evidence_booleans_are_strict(field: str, value: Any) -> None:
    """Truthy strings and numbers do not prove a probe succeeded."""
    output = evidence()
    output[field] = value
    assert not run_check(output).passed


@pytest.mark.parametrize("field", ["observed_identity", "workload_service_account"])
def test_wrong_identity_or_service_account_fails(field: str) -> None:
    """A node identity or a different ServiceAccount must not satisfy the check."""
    output = evidence()
    output[field] = "different"
    assert not run_check(output).passed


@pytest.mark.parametrize("field", ["service_account", "expected_identity"])
@pytest.mark.parametrize("value", ["", "  ", None, 42])
def test_empty_or_invalid_identifiers_fail(field: str, value: Any) -> None:
    """Even matching empty identity values provide no evidence."""
    output = evidence()
    output[field] = value
    output[{"service_account": "workload_service_account", "expected_identity": "observed_identity"}[field]] = value
    assert not run_check(output).passed


def test_missing_provider_step_skips() -> None:
    """Standalone suites without a provider probe cannot start this test."""
    with pytest.raises(pytest.skip.Exception, match="provider"):
        run_check(None)


def test_missing_prerequisite_skips() -> None:
    """An explicitly missing component is reported as a skip, not a pass."""
    with pytest.raises(pytest.skip.Exception, match="kubectl"):
        run_check({"success": False, "skipped": True, "skip_reason": "kubectl missing"})


@pytest.mark.parametrize(
    "output",
    [
        {},
        [],
        "bad",
        {"success": False, "error": "API unreachable"},
        {"success": False, "skipped": True},
        {"success": True, "skipped": True, "skip_reason": "bad"},
    ],
)
def test_invalid_or_failed_execution_fails(output: Any) -> None:
    """Attempted execution errors and malformed skip reports are failures."""
    assert not run_check(output).passed


def test_cleanup_failure_overrides_success_or_skip() -> None:
    """A leaked test resource is never hidden by an otherwise good result."""
    for output in [evidence(), {"success": False, "skipped": True, "skip_reason": "missing"}]:
        output = deepcopy(output)
        output["cleanup_errors"] = ["delete role failed"]
        assert not run_check(output).passed
