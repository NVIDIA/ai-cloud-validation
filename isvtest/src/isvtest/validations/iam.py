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

"""IAM and tenant validations for step outputs.

Validations for access keys, users, authentication, service accounts,
and tenant/resource groups.
"""

from typing import ClassVar

from isvtest.core.validation import BaseValidation

# =============================================================================
# Access Key Validations
# =============================================================================


class AccessKeyCreatedCheck(BaseValidation):
    """Validate access key was created successfully.

    Config:
        step_output: The step output to check

    Step output:
        access_key_id: The created access key ID
        username: The user the key belongs to
    """

    description: ClassVar[str] = "Check access key was created"
    labels: ClassVar[tuple[str, ...]] = ("iam",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        access_key_id = step_output.get("access_key_id")
        username = step_output.get("username")

        if not access_key_id:
            self.set_failed("No 'access_key_id' in output")
            return

        if not username:
            self.set_failed("No 'username' in output")
            return

        self.set_passed(f"Access key {access_key_id[:8]}... created for {username}")


class AccessKeyAuthenticatedCheck(BaseValidation):
    """Validate access key can authenticate.

    Config:
        step_output: The step output to check

    Step output:
        authenticated: Boolean
        caller_arn: The ARN of the authenticated identity
    """

    description: ClassVar[str] = "Check access key can authenticate"
    labels: ClassVar[tuple[str, ...]] = ("iam",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        authenticated = step_output.get("authenticated")
        if authenticated is None:
            self.set_failed("No 'authenticated' in output")
            return

        if authenticated:
            arn = step_output.get("identity_id", step_output.get("caller_arn", "unknown"))
            self.set_passed(f"Authenticated as {arn}")
        else:
            error = step_output.get("error", "Unknown error")
            self.set_failed(f"Authentication failed: {error}")


class AccessKeyDisabledCheck(BaseValidation):
    """Validate access key was disabled.

    Config:
        step_output: The step output to check

    Step output:
        status: Should be "Inactive"
    """

    description: ClassVar[str] = "Check access key was disabled"
    labels: ClassVar[tuple[str, ...]] = ("iam",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        status = step_output.get("status")
        if status == "Inactive":
            self.set_passed("Access key disabled (Inactive)")
        else:
            self.set_failed(f"Access key status: {status}, expected Inactive")


class AccessKeyRejectedCheck(BaseValidation):
    """Validate disabled access key is rejected.

    Config:
        step_output: The step output to check

    Step output:
        rejected: Boolean - True if key was rejected
        error_code: The error code from rejection
    """

    description: ClassVar[str] = "Check disabled key is rejected"
    labels: ClassVar[tuple[str, ...]] = ("iam",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        rejected = step_output.get("rejected")
        if rejected is None:
            self.set_failed("No 'rejected' in output")
            return

        if rejected:
            error_code = step_output.get("error_code", "")
            self.set_passed(f"Disabled key correctly rejected ({error_code})")
        else:
            self.set_failed("Disabled key was NOT rejected - still active!")


# =============================================================================
# Service Account Validations
# =============================================================================


class ServiceAccountCredentialCheck(BaseValidation):
    """Validate out-of-cluster service accounts can authenticate with long-lived credentials.

    Verifies that service accounts intended for out-of-cluster use (CI/CD
    pipelines, external tooling) can authenticate using long-lived credentials
    and that the credentials grant the expected identity.

    Config:
        step_output: The step output to check

    Step output:
        authenticated: Boolean - True if SA authenticated successfully
        credential_type: Type of credential (e.g. "api_key", "service_account_key")
        identity: The authenticated identity / principal
        expires_at: Optional expiry (null/absent for truly long-lived)
    """

    description: ClassVar[str] = "Check service account long-lived credential auth"
    labels: ClassVar[tuple[str, ...]] = ("iam", "security")

    def run(self) -> None:
        """Validate SA credential authentication from step output."""
        step_output = self.config.get("step_output", {})

        authenticated = step_output.get("authenticated")
        if authenticated is None:
            self.set_failed("No 'authenticated' in step output")
            return

        if not authenticated:
            error = step_output.get("error", "unknown error")
            self.set_failed(f"Service account authentication failed: {error}")
            return

        credential_type = step_output.get("credential_type")
        identity = step_output.get("identity")
        if not credential_type:
            self.set_failed("No 'credential_type' in step output")
            return
        if not identity:
            self.set_failed("No 'identity' in step output")
            return
        self.set_passed(f"Service account authenticated via {credential_type} as {identity}")


# =============================================================================
# Tenant/Resource Group Validations
# =============================================================================


class TenantCreatedCheck(BaseValidation):
    """Validate tenant was created.

    Config:
        step_output: The step output to check

    Step output:
        tenant_name: The created tenant name
        tenant_id: The tenant unique identifier
    """

    description: ClassVar[str] = "Check tenant was created"
    labels: ClassVar[tuple[str, ...]] = ("iam",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        tenant_name = step_output.get("tenant_name", step_output.get("group_name"))
        tenant_id = step_output.get("tenant_id", step_output.get("group_id"))

        if not tenant_name:
            self.set_failed("No 'tenant_name' in output")
            return

        if not tenant_id:
            self.set_failed("No 'tenant_id' in output")
            return

        self.set_passed(f"Tenant '{tenant_name}' created")


class TenantListedCheck(BaseValidation):
    """Validate tenant appears in list.

    Config:
        step_output: The step output to check

    Step output:
        found_target: Boolean - True if target tenant was found
        target_tenant: The tenant name we're looking for
        count: Number of tenants
    """

    description: ClassVar[str] = "Check tenant appears in list"
    labels: ClassVar[tuple[str, ...]] = ("iam",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        found = step_output.get("found_target")
        target = step_output.get("target_tenant", step_output.get("target_group", "unknown"))

        if found is None:
            # No specific target - just check list succeeded
            count = step_output.get("count", 0)
            self.set_passed(f"Listed {count} tenants")
            return

        if found:
            self.set_passed(f"Tenant '{target}' found in list")
        else:
            self.set_failed(f"Tenant '{target}' NOT found in list")


class TenantInfoCheck(BaseValidation):
    """Validate tenant info was retrieved.

    Config:
        step_output: The step output to check

    Step output:
        tenant_name: The tenant name
        tenant_id: The tenant unique identifier
        description: Optional description
    """

    description: ClassVar[str] = "Check tenant info retrieved"
    labels: ClassVar[tuple[str, ...]] = ("iam",)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        tenant_name = step_output.get("tenant_name", step_output.get("group_name"))
        tenant_id = step_output.get("tenant_id", step_output.get("group_id"))

        if not tenant_name or not tenant_id:
            self.set_failed("Missing tenant_name or tenant_id")
            return

        description = step_output.get("description", "")
        self.set_passed(f"Tenant '{tenant_name}' info retrieved: {description[:50]}")
