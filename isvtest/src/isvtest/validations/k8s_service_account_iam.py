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

"""Provider-neutral proof that a workload uses its ServiceAccount's scoped IAM identity."""

from typing import ClassVar

import pytest

from isvtest.core.validation import BaseValidation, check_required_tests


class K8sServiceAccountIamCheck(BaseValidation):
    """Require observed identity, permitted access, and an authorization denial from a pod.

    Step output:
        tests.identity.passed: The pod assumed its ServiceAccount's IAM identity
        tests.allowed_access.passed: The pod read a resource its policy allows
        tests.out_of_scope_denied.passed: The pod was denied a resource outside its policy
        cleanup_errors: Test fixtures the probe could not remove (fails the check)
    """

    description: ClassVar[str] = "Verify Kubernetes ServiceAccounts assume scoped platform IAM identities."

    def run(self) -> None:
        """Validate evidence from the provider's in-cluster workload probe."""
        output = self.config.get("step_output")
        if output is None:
            pytest.skip("ServiceAccount IAM validation requires a configured provider probe")
        if not check_required_tests(
            self, ["identity", "allowed_access", "out_of_scope_denied"], "ServiceAccount IAM probes failed"
        ):
            return
        if output.get("cleanup_errors"):
            self.set_failed(f"ServiceAccount IAM cleanup failed: {'; '.join(output['cleanup_errors'])}")
            return
        self.set_passed("Workload assumed the expected ServiceAccount IAM identity; allowed and denied scopes verified")
