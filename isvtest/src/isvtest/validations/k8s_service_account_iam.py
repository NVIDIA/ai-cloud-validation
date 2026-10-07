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

from isvtest.core.validation import BaseValidation


class K8sServiceAccountIamCheck(BaseValidation):
    """Require observed identity, permitted access, and an authorization denial from a pod."""

    description: ClassVar[str] = "Verify Kubernetes ServiceAccounts assume scoped platform IAM identities."

    def run(self) -> None:
        """Validate evidence from the provider's in-cluster workload probe."""
        output = self.config.get("step_output")
        if output is None:
            pytest.skip("ServiceAccount IAM validation requires a configured provider probe")
        if not isinstance(output, dict):
            self.set_failed("Invalid ServiceAccount IAM step output")
            return
        if output.get("cleanup_errors"):
            self.set_failed(f"ServiceAccount IAM cleanup failed: {output['cleanup_errors']}")
            return
        if output.get("skipped") is True:
            reason = output.get("skip_reason")
            if output.get("success") is False and isinstance(reason, str) and reason.strip():
                pytest.skip(reason)
            self.set_failed("Invalid ServiceAccount IAM skip report")
            return
        if output.get("success") is not True:
            self.set_failed(str(output.get("error") or "ServiceAccount IAM probe did not succeed"))
            return
        for expected, observed in (
            ("service_account", "workload_service_account"),
            ("expected_identity", "observed_identity"),
        ):
            value = output.get(expected)
            if not isinstance(value, str) or not value.strip() or output.get(observed) != value:
                self.set_failed(f"Missing or mismatched {observed}")
                return
        for field in ("federated_token_used", "allowed_access", "out_of_scope_denied"):
            if output.get(field) is not True:
                self.set_failed(f"ServiceAccount IAM requires {field}=true from the workload")
                return
        self.set_passed("Workload assumed the expected ServiceAccount IAM identity; allowed and denied scopes verified")
