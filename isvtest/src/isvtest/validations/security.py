# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Security validations for infrastructure hardening.

Validations for BMC isolation, BMC protocol posture, API endpoint exposure,
MFA enforcement, tenant isolation, console RBAC, and other platform security
requirements (SEC* test IDs).
"""

from typing import ClassVar

import pytest

from isvtest.core.validation import BaseValidation, check_required_tests


class BmcManagementNetworkCheck(BaseValidation):
    """Validate BMC management is on a dedicated, restricted network.

    Verifies that out-of-band BMC/IPMI/Redfish management networks are not
    shared with tenant networks and that management routes and ACLs are
    restricted.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with dedicated_management_network,
               restricted_management_routes, tenant_network_not_management,
               management_acl_enforced
    """

    description: ClassVar[str] = "Check BMC management network is dedicated and restricted"
    markers: ClassVar[list[str]] = ["security", "network"]

    def run(self) -> None:
        """Validate required BMC management-network results from step output."""
        required = [
            "dedicated_management_network",
            "restricted_management_routes",
            "tenant_network_not_management",
            "management_acl_enforced",
        ]
        if not check_required_tests(self, required, "BMC management network tests failed"):
            return
        network_count = self.config.get("step_output", {}).get("management_networks_checked", "N/A")
        self.set_passed(f"BMC management network dedicated and restricted ({network_count} networks checked)")


class BmcTenantIsolationCheck(BaseValidation):
    """Validate BMC interfaces are not reachable from tenant networks.

    Verifies that management interfaces (BMC/IPMI/Redfish) are isolated
    from tenant-accessible networks - probes from the tenant network to
    known BMC endpoints must be refused or time out.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with probe_bmc_from_tenant, probe_ipmi_port,
               probe_redfish_port, reverse_path_check
    """

    description: ClassVar[str] = "Check BMC not reachable from tenant network"
    markers: ClassVar[list[str]] = ["security", "network"]

    def run(self) -> None:
        """Validate required BMC isolation probe results from step output."""
        required = [
            "probe_bmc_from_tenant",
            "probe_ipmi_port",
            "probe_redfish_port",
            "reverse_path_check",
        ]
        if not check_required_tests(self, required, "BMC isolation tests failed"):
            return
        bmc_count = self.config.get("step_output", {}).get("bmc_endpoints_tested", "N/A")
        self.set_passed(f"BMC interfaces unreachable from tenant network ({bmc_count} endpoints tested)")


class TenantIsolationCheck(BaseValidation):
    """Validate hard isolation between tenants (SEC11-01).

    Verifies four orthogonal isolation surfaces by inspecting the boolean
    sub-claims a provider script reports after running cross-tenant
    negative probes (tenant A's principal acting against tenant B's
    resources):

    * ``network_isolated``  -- tenant A's network has no route to tenant B's
      network (no peering, no shared route, SG/NACL deny).
    * ``data_isolated``     -- tenant A is denied ``kms:Encrypt`` on tenant
      B's CMK and ``s3:GetObject`` on tenant B's bucket.
    * ``compute_isolated``  -- tenant A is denied ``ec2:*`` /
      ``ssm:StartSession`` against tenant B's instance.
    * ``storage_isolated``  -- tenant A is denied ``ebs:*`` /
      snapshot/attach/copy against tenant B's volume.

    Physical isolation (bare-metal) and switch-fabric isolation are out of
    scope here -- they are covered by SDN04-04/05.

    Config:
        step_output: The step output to check

    Step output:
        tenant_a_id: Non-empty identifier for the source tenant
        tenant_b_id: Non-empty identifier for the target tenant
        tests: dict with network_isolated, data_isolated,
               compute_isolated, storage_isolated
    """

    description: ClassVar[str] = "Check hard tenant isolation across network, data, compute, and storage"
    markers: ClassVar[list[str]] = ["security", "network", "iam"]

    def run(self) -> None:
        """Validate the four tenant-isolation sub-claims from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Tenant isolation validation skipped (not configured)")

        for field in ("tenant_a_id", "tenant_b_id"):
            value = step_output.get(field)
            if not isinstance(value, str) or not value.strip():
                self.set_failed(f"Tenant isolation output missing non-empty '{field}'")
                return

        required = [
            "network_isolated",
            "data_isolated",
            "compute_isolated",
            "storage_isolated",
        ]
        if not check_required_tests(self, required, "Tenant isolation tests failed"):
            return

        tenant_a = step_output["tenant_a_id"].strip()
        tenant_b = step_output["tenant_b_id"].strip()
        self.set_passed(f"Hard tenant isolation verified ({tenant_a} cannot reach {tenant_b})")


class BmcProtocolSecurityCheck(BaseValidation):
    """Validate BMC management protocols enforce CNP10-01 controls.

    Verifies the management protocol posture for BMC endpoints:
    IPMI must be disabled, Redfish must require TLS and authentication,
    role authorization must be enforced, and AAA/accounting evidence must
    be present.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with ipmi_disabled, redfish_tls_enabled,
               redfish_plain_http_disabled, redfish_authentication_required,
               redfish_authorization_enforced, redfish_accounting_enabled
    """

    description: ClassVar[str] = "Check BMC protocol security posture"
    markers: ClassVar[list[str]] = ["security", "network"]

    def run(self) -> None:
        """Validate required BMC protocol security probe results."""
        required = [
            "ipmi_disabled",
            "redfish_tls_enabled",
            "redfish_plain_http_disabled",
            "redfish_authentication_required",
            "redfish_authorization_enforced",
            "redfish_accounting_enabled",
        ]
        if not check_required_tests(self, required, "BMC protocol security tests failed"):
            return
        bmc_count = self.config.get("step_output", {}).get("bmc_endpoints_tested", "N/A")
        self.set_passed(f"BMC protocol security posture verified ({bmc_count} endpoints tested)")


class BmcBastionAccessCheck(BaseValidation):
    """Validate BMC is only accessible via a hardened bastion.

    Verifies that out-of-band management interfaces (BMC/IPMI/Redfish) accept
    ingress only through a designated bastion/jumphost, that the bastion
    itself is hardened (no world-open SSH), and that BMC-tagged subnets have
    no direct route to the public internet.

    Hyperscalers that hide the BMC plane from customers (e.g. AWS) cannot
    fully exercise this check; the AWS reference reports each subtest as
    passed with a ``provider_hidden`` note when no customer-visible BMC
    network is present. Self-managed NCPs running their own BMC fabric
    should report concrete pass/fail per subtest.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with bastion_identifiable, management_ingress_via_bastion_only,
               no_direct_public_route, bastion_hardened
    """

    description: ClassVar[str] = "Check BMC reachable only via hardened bastion"
    markers: ClassVar[list[str]] = ["security", "network"]

    def run(self) -> None:
        """Validate required BMC bastion-access results from step output."""
        required = [
            "bastion_identifiable",
            "management_ingress_via_bastion_only",
            "no_direct_public_route",
            "bastion_hardened",
        ]
        if not check_required_tests(self, required, "BMC bastion access tests failed"):
            return
        endpoints = self.config.get("step_output", {}).get("management_networks_checked", "N/A")
        self.set_passed(f"BMC reachable only via hardened bastion ({endpoints} networks checked)")


class MfaEnforcedCheck(BaseValidation):
    """Validate all administrative interfaces are protected by MFA.

    Verifies that the platform enforces Multi-Factor Authentication on
    UI (console), CLI, and API administrative access -- covering root/admin
    accounts, interactive console users, and programmatic access policies.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with root_mfa_enabled, console_users_mfa,
               api_mfa_policy, cli_mfa_policy
    """

    description: ClassVar[str] = "Check admin interfaces protected by MFA"
    markers: ClassVar[list[str]] = ["security", "iam"]

    def run(self) -> None:
        """Validate required MFA enforcement results from step output."""
        required = [
            "root_mfa_enabled",
            "console_users_mfa",
            "api_mfa_policy",
            "cli_mfa_policy",
        ]
        if not check_required_tests(self, required, "MFA enforcement tests failed"):
            return
        interfaces = self.config.get("step_output", {}).get("interfaces_checked", "N/A")
        self.set_passed(f"Admin interfaces protected by MFA ({interfaces} interfaces checked)")


class CentralizedKmsCheck(BaseValidation):
    """Validate encrypted resources reference centralized KMS-backed keys.

    Config:
        step_output: The centralized_kms_test step output to check

    Step output:
        kms_keys_total: Positive number of visible KMS keys
        encrypted_resources_inspected: Positive number of encrypted resources inspected
        non_kms_resources: Number of encrypted resources that do not resolve to KMS
        tests: dict with kms_service_reachable, kms_keys_present,
               all_encrypted_resources_use_kms
    """

    description: ClassVar[str] = "Check encrypted resources use centralized KMS"
    markers: ClassVar[list[str]] = ["security"]

    def run(self) -> None:
        """Validate required centralized-KMS results from step output."""
        required = [
            "kms_service_reachable",
            "kms_keys_present",
            "all_encrypted_resources_use_kms",
        ]
        if not check_required_tests(self, required, "Centralized KMS tests failed"):
            return

        step_output = self.config.get("step_output", {})
        kms_keys_total = step_output.get("kms_keys_total")
        if type(kms_keys_total) is not int or kms_keys_total < 1:
            self.set_failed("Centralized KMS output missing positive 'kms_keys_total'")
            return

        non_kms_resources = step_output.get("non_kms_resources")
        if type(non_kms_resources) is not int:
            self.set_failed("Centralized KMS output missing integer 'non_kms_resources'")
            return
        if non_kms_resources != 0:
            self.set_failed(f"Centralized KMS found {non_kms_resources} encrypted resource(s) not using KMS")
            return

        inspected = step_output.get("encrypted_resources_inspected")
        if type(inspected) is not int or inspected < 1:
            self.set_failed("Centralized KMS output missing positive 'encrypted_resources_inspected'")
            return
        self.set_passed(f"Centralized KMS verified ({kms_keys_total} keys, {inspected} encrypted resources inspected)")


class CertRotationCycleCheck(BaseValidation):
    """Validate TLS certificate rotation evidence is present.

    Config:
        step_output: The cert_rotation_test step output to check

    Step output:
        rotation_window_days: Maximum allowed rotation window, expected 1..60
        certs_inspected: Positive number of customer-visible certs inspected
        skipped: True when no managed TLS certificate evidence is available
        skip_reason: Human-readable skip reason when skipped is True
        tests: dict with cert_inventory_non_empty, no_certs_out_of_policy,
               rotation_evidence_present
    """

    description: ClassVar[str] = "Check TLS certificates rotate within 60 days or auto-renew"
    markers: ClassVar[list[str]] = ["security"]

    def run(self) -> None:
        """Validate required certificate-rotation results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Certificate rotation validation skipped")

        required = [
            "cert_inventory_non_empty",
            "no_certs_out_of_policy",
            "rotation_evidence_present",
        ]
        if not check_required_tests(self, required, "Certificate rotation tests failed"):
            return

        certs_inspected = step_output.get("certs_inspected")
        if type(certs_inspected) is not int or certs_inspected < 1:
            self.set_failed("Certificate rotation output missing positive 'certs_inspected'")
            return

        rotation_window_days = step_output.get("rotation_window_days")
        if type(rotation_window_days) is not int or not 1 <= rotation_window_days <= 60:
            self.set_failed("Certificate rotation output missing integer 'rotation_window_days' in range 1..60")
            return

        out_of_policy = step_output.get("out_of_policy")
        if type(out_of_policy) is not int:
            self.set_failed("Certificate rotation output missing integer 'out_of_policy'")
            return
        if out_of_policy != 0:
            self.set_failed(f"Certificate rotation found {out_of_policy} out-of-policy certificate(s)")
            return

        self.set_passed(
            f"Certificate rotation verified ({certs_inspected} certs inspected, window={rotation_window_days} days)"
        )


class CustomerManagedKeyCheck(BaseValidation):
    """Validate resources can be encrypted with customer-managed keys.

    Verifies that the platform exposes customer-managed key support, the
    reported key is customer-owned rather than provider-managed, encryption
    and decryption work with that key, and a provider resource is encrypted
    with that exact customer-managed key.

    Config:
        step_output: The step output to check

    Step output:
        key_id or key_arn: Non-empty customer-managed key evidence
        encrypted_resource_id or resource_id: Non-empty encrypted resource evidence
        tests: dict with customer_managed_key_available,
               key_manager_is_customer, encrypt_decrypt_roundtrip,
               resource_encrypted_with_customer_key,
               provider_managed_key_not_used
    """

    description: ClassVar[str] = "Check BYOK/customer-managed key encryption support"
    markers: ClassVar[list[str]] = ["security", "workload", "slow"]

    def run(self) -> None:
        """Validate required BYOK/customer-managed key results from step output."""
        required = [
            "customer_managed_key_available",
            "key_manager_is_customer",
            "encrypt_decrypt_roundtrip",
            "resource_encrypted_with_customer_key",
            "provider_managed_key_not_used",
        ]
        if not check_required_tests(self, required, "Customer-managed key tests failed"):
            return

        step_output = self.config.get("step_output", {})
        key_evidence = next(
            (
                value.strip()
                for value in (step_output.get("key_id"), step_output.get("key_arn"))
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        if not key_evidence:
            self.set_failed("Customer-managed key output missing non-empty key evidence")
            return

        resource_evidence = next(
            (
                value.strip()
                for value in (step_output.get("encrypted_resource_id"), step_output.get("resource_id"))
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        if not resource_evidence:
            self.set_failed("Customer-managed key output missing non-empty encrypted resource evidence")
            return

        self.set_passed(f"Customer-managed key encryption verified (key={key_evidence}, resource={resource_evidence})")


class KmsEncryptionOptionCheck(BaseValidation):
    """Validate provider-managed and customer-managed KMS options exist.

    Config:
        step_output: The kms_encryption_options_test step output to check

    Step output:
        provider_managed_key_id: Non-empty provider-managed key evidence
        customer_managed_key_id: Non-empty CMK evidence
        skipped: True when scoped provider-managed evidence is unavailable
        skip_reason: Human-readable skip reason when skipped is True
        tests: dict with provider_managed_key_available,
               customer_managed_key_available, both_options_supported
    """

    description: ClassVar[str] = "Check provider-managed and customer-managed KMS options exist"
    markers: ClassVar[list[str]] = ["security"]

    def run(self) -> None:
        """Validate required KMS encryption-option results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "KMS encryption options validation skipped")

        required = [
            "provider_managed_key_available",
            "customer_managed_key_available",
            "both_options_supported",
        ]
        if not check_required_tests(self, required, "KMS encryption option tests failed"):
            return

        provider_key_id = step_output.get("provider_managed_key_id")
        if not isinstance(provider_key_id, str) or not provider_key_id.strip():
            self.set_failed("KMS encryption options output missing non-empty provider-managed key evidence")
            return

        customer_key_id = step_output.get("customer_managed_key_id")
        if not isinstance(customer_key_id, str) or not customer_key_id.strip():
            self.set_failed("KMS encryption options output missing non-empty customer-managed key evidence")
            return

        self.set_passed(
            f"KMS encryption options verified (provider={provider_key_id.strip()}, customer={customer_key_id.strip()})"
        )


class ApiEndpointIsolationCheck(BaseValidation):
    """Validate no public internet access to API endpoints by default.

    Verifies that platform API endpoints (control plane, management APIs)
    are not directly accessible from the public internet - connections
    from outside the private network must be refused.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with probe_api_from_public, probe_mgmt_from_public,
               verify_private_only, dns_not_public
    """

    description: ClassVar[str] = "Check API endpoints not publicly accessible"
    markers: ClassVar[list[str]] = ["security", "network"]

    def run(self) -> None:
        """Validate required API endpoint isolation probe results from step output."""
        required = [
            "probe_api_from_public",
            "probe_mgmt_from_public",
            "verify_private_only",
            "dns_not_public",
        ]
        if not check_required_tests(self, required, "API endpoint isolation tests failed"):
            return
        endpoints = self.config.get("step_output", {}).get("endpoints_tested", "N/A")
        self.set_passed(f"API endpoints not publicly accessible ({endpoints} endpoints tested)")


class ConsoleRbacCheck(BaseValidation):
    """Validate interactive console access is restricted by RBAC.

    Config:
        step_output: The console_rbac step output to check

    Step output:
        instance_id: VM identifier
        access_restricted: True when unauthorized console access is denied
        restricted_actions: Non-empty list of console access permissions
        tests: dict with denied_principal_cannot_access_console,
               allowed_principal_can_access_console,
               allowed_principal_is_resource_scoped
    """

    description: ClassVar[str] = "Check console access is restricted by RBAC"
    markers: ClassVar[list[str]] = ["vm", "security", "iam"]

    def run(self) -> None:
        """Validate console RBAC provider proof from step output."""
        step_output = self.config.get("step_output", {})

        instance_id = step_output.get("instance_id")
        if not instance_id:
            self.set_failed("No 'instance_id' in step output")
            return

        access_restricted = step_output.get("access_restricted")
        if access_restricted is not True:
            self.set_failed(
                f"Console access for {instance_id} is not affirmatively restricted "
                f"(access_restricted={access_restricted!r})"
            )
            return

        restricted_actions = step_output.get("restricted_actions")
        if not isinstance(restricted_actions, list) or not restricted_actions:
            self.set_failed(f"No restricted console actions reported for {instance_id}")
            return

        required = [
            "denied_principal_cannot_access_console",
            "allowed_principal_can_access_console",
            "allowed_principal_is_resource_scoped",
        ]
        if not check_required_tests(self, required, "Console RBAC tests failed"):
            return

        rbac_model = step_output.get("rbac_model", "unknown")
        self.set_passed(
            f"Console RBAC restricted for {instance_id} "
            f"(model={rbac_model}, actions={', '.join(str(action) for action in restricted_actions)})"
        )


class OidcUserAuthCheck(BaseValidation):
    """Validate user authentication via OIDC for platform services.

    Verifies that a configured platform endpoint accepts a properly issued
    token and rejects tokens with bad signature, wrong issuer, wrong
    audience, expired exp, or missing required claims, and that the
    issuer's discovery + JWKS endpoints serve the expected metadata.

    Config:
        step_output: The step output to check

    Step output:
        issuer_url: Non-empty OIDC issuer URL
        audience: Non-empty expected audience
        target_url: Non-empty platform endpoint probed with bearer tokens
        endpoints_tested: Positive integer
        tests: dict with valid_token_accepted, bad_signature_rejected,
               wrong_issuer_rejected, wrong_audience_rejected,
               expired_token_rejected, missing_required_claim_rejected,
               discovery_and_jwks_reachable
    """

    description: ClassVar[str] = (
        "Check user auth via OIDC validates signature, issuer, audience, expiration, and required claims"
    )
    markers: ClassVar[list[str]] = ["security", "iam"]

    def run(self) -> None:
        """Validate required OIDC token verification probe results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "OIDC validation skipped (not configured)")

        required = [
            "valid_token_accepted",
            "bad_signature_rejected",
            "wrong_issuer_rejected",
            "wrong_audience_rejected",
            "expired_token_rejected",
            "missing_required_claim_rejected",
            "discovery_and_jwks_reachable",
        ]
        if not check_required_tests(self, required, "OIDC user auth tests failed"):
            return

        for field in ("issuer_url", "audience", "target_url"):
            value = step_output.get(field)
            if not isinstance(value, str) or not value.strip():
                self.set_failed(f"OIDC user auth output missing non-empty '{field}'")
                return

        endpoints_tested = step_output.get("endpoints_tested")
        if type(endpoints_tested) is not int or endpoints_tested < 1:
            self.set_failed("OIDC user auth did not probe any platform endpoint")
            return

        issuer = step_output["issuer_url"]
        target_url = step_output["target_url"]
        self.set_passed(f"OIDC user auth verified (issuer={issuer}, target={target_url})")


class ShortLivedCredentialsCheck(BaseValidation):
    """Validate workloads and nodes receive short-lived credentials/tokens.

    Verifies that the platform issues credentials with a finite expiry on
    both surfaces SEC02-01 cares about:

    * Node-side: credentials a host/instance role acquires from the
      platform identity service (AWS reference: ``sts:GetSessionToken``;
      mirrors instance-metadata role chaining on EC2).
    * Workload-side: credentials an in-cluster workload acquires through
      the workload identity flow (AWS reference: ``sts:GetFederationToken``
      with a deny-all session policy; mirrors IRSA on EKS / GKE Workload
      Identity / AKS Workload Identity in shape).

    The validation also enforces an upper TTL bound so "short-lived" is
    enforced numerically, not just by virtue of an ``Expiration`` field
    being present.

    Like ``OidcUserAuthCheck``, the step may emit a structured top-level
    ``skipped`` payload when no real issuance path is available (e.g. the
    AWS reference cannot probe ``sts:GetSessionToken`` from an assumed-role
    session). In that case the validation skips rather than fabricates a
    pass.

    Config:
        step_output: The step output to check

    Step output:
        node_credential_ttl_seconds: Positive integer
        workload_credential_ttl_seconds: Positive integer
        max_ttl_seconds: Positive integer (configured upper bound)
        tests: dict with node_credential_has_expiry,
               node_credential_ttl_within_bound,
               workload_credential_has_expiry,
               workload_credential_ttl_within_bound
    """

    description: ClassVar[str] = "Check workloads and nodes receive credentials with finite, bounded TTL"
    markers: ClassVar[list[str]] = ["security", "iam"]

    def run(self) -> None:
        """Validate required short-lived credentials results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Short-lived credentials validation skipped (not configured)")

        required = [
            "node_credential_has_expiry",
            "node_credential_ttl_within_bound",
            "workload_credential_has_expiry",
            "workload_credential_ttl_within_bound",
        ]
        if not check_required_tests(self, required, "Short-lived credentials tests failed"):
            return

        max_ttl = step_output.get("max_ttl_seconds")
        if type(max_ttl) is not int or max_ttl < 1:
            self.set_failed("Short-lived credentials output missing positive int 'max_ttl_seconds'")
            return

        for field in ("node_credential_ttl_seconds", "workload_credential_ttl_seconds"):
            value = step_output.get(field)
            if type(value) is not int or value < 1:
                self.set_failed(f"Short-lived credentials output missing positive int '{field}'")
                return
            if value > max_ttl:
                self.set_failed(f"{field}={value}s exceeds max_ttl_seconds={max_ttl}s")
                return

        node_ttl = step_output["node_credential_ttl_seconds"]
        workload_ttl = step_output["workload_credential_ttl_seconds"]
        self.set_passed(
            f"Short-lived credentials verified (node TTL={node_ttl}s, workload TTL={workload_ttl}s, bound={max_ttl}s)"
        )


class LeastPrivilegePolicyCheck(BaseValidation):
    """Validate least-privilege policy dimensions are enforced (SEC04-01).

    Verifies the provider probe exercised user-scoped, resource-scoped, and
    network-scoped access policy constraints. The probe may emit a structured
    top-level skip when it cannot provision the temporary identity needed for
    the check.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with policy_dimensions_user_based,
               policy_dimensions_resource_based,
               policy_dimensions_network_based,
               policy_dimensions_allowed_action_succeeds
    """

    description: ClassVar[str] = "Check least-privilege access policies are user, resource, and network scoped"
    markers: ClassVar[list[str]] = ["security", "iam"]

    def run(self) -> None:
        """Validate required least-privilege policy-dimension results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Least-privilege policy validation skipped")

        required = [
            "policy_dimensions_user_based",
            "policy_dimensions_resource_based",
            "policy_dimensions_network_based",
            "policy_dimensions_allowed_action_succeeds",
        ]
        if not check_required_tests(self, required, "Least-privilege policy tests failed"):
            return

        identity = step_output.get("test_identity", "temporary test identity")
        resource = step_output.get("allowed_resource", "scoped resource")
        self.set_passed(f"Least-privilege policy dimensions verified for {identity} on {resource}")


class MinimalRoleEnforcementCheck(BaseValidation):
    """Validate a minimal role denies out-of-scope actions (SEC04-02).

    Verifies the same minimal identity used for SEC04-01 cannot perform
    compute, storage, or network actions outside its grant.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with out_of_scope_compute_denied,
               out_of_scope_storage_denied,
               out_of_scope_network_denied
    """

    description: ClassVar[str] = "Check minimal role denies out-of-scope compute, storage, and network APIs"
    markers: ClassVar[list[str]] = ["security", "iam"]

    def run(self) -> None:
        """Validate required out-of-scope denial results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Minimal-role enforcement validation skipped")

        required = [
            "out_of_scope_compute_denied",
            "out_of_scope_storage_denied",
            "out_of_scope_network_denied",
        ]
        if not check_required_tests(self, required, "Minimal-role enforcement tests failed"):
            return

        identity = step_output.get("test_identity", "temporary test identity")
        self.set_passed(f"Minimal role denied out-of-scope compute, storage, and network APIs for {identity}")


class AuditLogEntryCheck(BaseValidation):
    """Validate management API calls produce audit-log entries (SEC08-01).

    Verifies the provider emitted a known management API call, found the
    corresponding audit event, and checked required metadata fields. Cloud
    audit pipelines can legitimately lag; the probe may emit a check-specific
    structured skip if the event has not propagated within its poll budget.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with audit_log_entry_found,
               audit_log_event_name_matches,
               audit_log_event_time_in_window,
               audit_log_user_identity_present,
               audit_log_source_ip_present,
               audit_log_user_agent_matches,
               audit_log_region_matches,
               audit_log_event_source_matches
    """

    description: ClassVar[str] = "Check management API calls are recorded in audit logs with required metadata"
    markers: ClassVar[list[str]] = ["security"]

    def run(self) -> None:
        """Validate required audit-log entry and metadata results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Audit-log entry validation skipped")
        if step_output.get("audit_log_entry_skipped") is True:
            pytest.skip(step_output.get("audit_log_entry_skip_reason") or "Audit-log entry validation skipped")

        required = [
            "audit_log_entry_found",
            "audit_log_event_name_matches",
            "audit_log_event_time_in_window",
            "audit_log_user_identity_present",
            "audit_log_source_ip_present",
            "audit_log_user_agent_matches",
            "audit_log_region_matches",
            "audit_log_event_source_matches",
        ]
        if not check_required_tests(self, required, "Audit-log entry tests failed"):
            return

        event_name = step_output.get("event_name", "management API call")
        request_id = step_output.get("request_id", "unknown request id")
        self.set_passed(f"Audit log entry verified for {event_name} request {request_id}")


class AuditLogRetentionCheck(BaseValidation):
    """Validate audit logs are retained for at least 30 days (SEC08-02).

    Verifies the provider found an active audit trail and confirmed its log
    destination has no current-object expiration or only expiration rules with
    at least a 30-day retention period.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with audit_log_trail_logging_enabled,
               audit_log_retention_at_least_30_days
    """

    description: ClassVar[str] = "Check audit logs are retained for at least 30 days"
    markers: ClassVar[list[str]] = ["security"]

    def run(self) -> None:
        """Validate required audit-log retention results from step output."""
        step_output = self.config.get("step_output", {})
        if step_output.get("skipped") is True:
            pytest.skip(step_output.get("skip_reason") or "Audit-log retention validation skipped")
        if step_output.get("audit_log_retention_skipped") is True:
            pytest.skip(step_output.get("audit_log_retention_skip_reason") or "Audit-log retention validation skipped")

        required = [
            "audit_log_trail_logging_enabled",
            "audit_log_retention_at_least_30_days",
        ]
        if not check_required_tests(self, required, "Audit-log retention tests failed"):
            return

        bucket = step_output.get("audit_log_bucket") or step_output.get(
            "audit_log_destination", "audit log destination"
        )
        retention_days = step_output.get("minimum_retention_days", ">=30")
        if isinstance(retention_days, int):
            retention_summary = f"{retention_days} days"
        else:
            retention_summary = str(retention_days)
        self.set_passed(f"Audit log retention verified for {bucket} (minimum retention {retention_summary})")
