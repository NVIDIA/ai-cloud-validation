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

"""Tests for security validations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from isvtest.validations.security import (
    BmcBastionAccessCheck,
    BmcManagementNetworkCheck,
    BmcProtocolSecurityCheck,
    CentralizedKmsCheck,
    CertRotationCycleCheck,
    CustomerManagedKeyCheck,
    InsecureProtocolsCheck,
    KmsEncryptionOptionCheck,
    MfaEnforcedCheck,
    OidcUserAuthCheck,
    ShortLivedCredentialsCheck,
    TenantIsolationCheck,
)

REQUIRED_BMC_PROTOCOL_TESTS = [
    "ipmi_disabled",
    "redfish_tls_enabled",
    "redfish_plain_http_disabled",
    "redfish_authentication_required",
    "redfish_authorization_enforced",
    "redfish_accounting_enabled",
]

OIDC_REQUIRED_TESTS = {
    "valid_token_accepted": {"passed": True},
    "bad_signature_rejected": {"passed": True},
    "wrong_issuer_rejected": {"passed": True},
    "wrong_audience_rejected": {"passed": True},
    "expired_token_rejected": {"passed": True},
    "missing_required_claim_rejected": {"passed": True},
    "discovery_and_jwks_reachable": {"passed": True},
}

BYOK_REQUIRED_TESTS = {
    "customer_managed_key_available": {"passed": True},
    "key_manager_is_customer": {"passed": True},
    "encrypt_decrypt_roundtrip": {"passed": True},
    "resource_encrypted_with_customer_key": {"passed": True},
    "provider_managed_key_not_used": {"passed": True},
}

CERT_ROTATION_REQUIRED_TESTS = {
    "cert_inventory_non_empty": {"passed": True},
    "no_certs_out_of_policy": {"passed": True},
    "rotation_evidence_present": {"passed": True},
}

KMS_OPTIONS_REQUIRED_TESTS = {
    "provider_managed_key_available": {"passed": True},
    "customer_managed_key_available": {"passed": True},
    "both_options_supported": {"passed": True},
}

CENTRALIZED_KMS_REQUIRED_TESTS = {
    "kms_service_reachable": {"passed": True},
    "kms_keys_present": {"passed": True},
    "all_encrypted_resources_use_kms": {"passed": True},
}


def _bmc_management_config(tests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a minimal BMC management-network validation config."""
    return {
        "step_output": {
            "success": True,
            "platform": "security",
            "test_name": "bmc_management_network",
            "management_networks_checked": 1,
            "tests": tests,
        }
    }


def _mfa_output(**overrides: Any) -> dict[str, Any]:
    """Build a minimal passing MFA enforcement step_output."""
    base: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "mfa_enforcement",
        "interfaces_checked": 4,
        "tests": {
            "root_mfa_enabled": {"passed": True, "message": "Root MFA enabled"},
            "console_users_mfa": {"passed": True, "message": "3/3 users have MFA"},
            "api_mfa_policy": {"passed": True, "message": "MFA condition found"},
            "cli_mfa_policy": {"passed": True, "message": "MFA condition found"},
        },
    }
    base.update(overrides)
    return base


class TestBmcManagementNetworkCheck:
    """Tests for SEC12-01 BMC management-network validation."""

    def test_all_required_tests_pass(self) -> None:
        """Pass when all SEC12-01 contract checks passed."""
        tests = {
            "dedicated_management_network": {"passed": True},
            "restricted_management_routes": {"passed": True},
            "tenant_network_not_management": {"passed": True},
            "management_acl_enforced": {"passed": True},
        }

        result = BmcManagementNetworkCheck(config=_bmc_management_config(tests)).execute()

        assert result["passed"] is True
        assert "BMC management network dedicated and restricted" in result["output"]

    def test_failed_acl_reports_contract_key(self) -> None:
        """Fail with the specific contract key when ACL enforcement fails."""
        tests = {
            "dedicated_management_network": {"passed": True},
            "restricted_management_routes": {"passed": True},
            "tenant_network_not_management": {"passed": True},
            "management_acl_enforced": {"passed": False, "error": "ACL allows tenant CIDR"},
        }

        result = BmcManagementNetworkCheck(config=_bmc_management_config(tests)).execute()

        assert result["passed"] is False
        assert "management_acl_enforced" in result["error"]
        assert "ACL allows tenant CIDR" in result["error"]

    def test_missing_required_key_fails(self) -> None:
        """Fail when one of the four required contract keys is absent."""
        tests = {
            "dedicated_management_network": {"passed": True},
            "restricted_management_routes": {"passed": True},
            "tenant_network_not_management": {"passed": True},
        }

        result = BmcManagementNetworkCheck(config=_bmc_management_config(tests)).execute()

        assert result["passed"] is False
        assert "management_acl_enforced" in result["error"]

    def test_missing_tests_fails(self) -> None:
        """Fail when the step output does not include contract tests."""
        result = BmcManagementNetworkCheck(config={"step_output": {}}).execute()

        assert result["passed"] is False
        assert "tests" in result["error"]


def _bmc_protocol_config(
    tests: dict[str, dict[str, Any]] | None = None,
    *,
    bmc_endpoints_tested: int = 1,
) -> dict[str, Any]:
    """Build a BMC protocol validation config."""
    default_tests = {name: {"passed": True, "message": f"{name} passed"} for name in REQUIRED_BMC_PROTOCOL_TESTS}
    return {
        "step_output": {
            "bmc_endpoints_tested": bmc_endpoints_tested,
            "tests": tests if tests is not None else default_tests,
        },
    }


def test_bmc_protocol_security_check_passes_with_required_tests() -> None:
    """BmcProtocolSecurityCheck passes when every required probe passed."""
    validation = BmcProtocolSecurityCheck(config=_bmc_protocol_config())

    result = validation.execute()

    assert result["passed"] is True
    assert "BMC protocol security posture verified (1 endpoints tested)" in result["output"]


def test_bmc_protocol_security_check_reports_failed_and_missing_tests() -> None:
    """BmcProtocolSecurityCheck reports both failed and missing probes."""
    tests = {
        name: {"passed": True}
        for name in REQUIRED_BMC_PROTOCOL_TESTS
        if name not in {"redfish_tls_enabled", "redfish_accounting_enabled"}
    }
    tests["redfish_tls_enabled"] = {"passed": False, "error": "certificate expired"}
    validation = BmcProtocolSecurityCheck(config=_bmc_protocol_config(tests))

    result = validation.execute()

    assert result["passed"] is False
    assert "BMC protocol security tests failed" in result["error"]
    assert "redfish_tls_enabled: certificate expired" in result["error"]
    assert "redfish_accounting_enabled: test not found" in result["error"]


def test_bmc_protocol_security_check_preserves_empty_tests_map() -> None:
    """BmcProtocolSecurityCheck fails when an explicit empty tests map is provided."""
    validation = BmcProtocolSecurityCheck(config=_bmc_protocol_config({}))

    result = validation.execute()

    assert result["passed"] is False
    assert result["error"] == "No 'tests' in step output"


def _bastion_access_config(tests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a minimal BMC bastion-access validation config."""
    return {
        "step_output": {
            "success": True,
            "platform": "security",
            "test_name": "bmc_bastion_access",
            "management_networks_checked": 1,
            "tests": tests,
        }
    }


class TestBmcBastionAccessCheck:
    """Tests for SEC12-03 BMC bastion-access validation."""

    def test_all_required_tests_pass(self) -> None:
        """Pass when all SEC12-03 contract checks passed."""
        tests = {
            "bastion_identifiable": {"passed": True},
            "management_ingress_via_bastion_only": {"passed": True},
            "no_direct_public_route": {"passed": True},
            "bastion_hardened": {"passed": True},
        }

        result = BmcBastionAccessCheck(config=_bastion_access_config(tests)).execute()

        assert result["passed"] is True
        assert "BMC reachable only via hardened bastion" in result["output"]

    def test_failed_bastion_hardened_reports_contract_key(self) -> None:
        """Fail with the specific contract key when bastion hardening fails."""
        tests = {
            "bastion_identifiable": {"passed": True},
            "management_ingress_via_bastion_only": {"passed": True},
            "no_direct_public_route": {"passed": True},
            "bastion_hardened": {"passed": False, "error": "SSH open to 0.0.0.0/0"},
        }

        result = BmcBastionAccessCheck(config=_bastion_access_config(tests)).execute()

        assert result["passed"] is False
        assert "bastion_hardened" in result["error"]
        assert "0.0.0.0/0" in result["error"]

    def test_missing_required_key_fails(self) -> None:
        """Fail when one of the four required contract keys is absent."""
        tests = {
            "bastion_identifiable": {"passed": True},
            "management_ingress_via_bastion_only": {"passed": True},
            "no_direct_public_route": {"passed": True},
        }

        result = BmcBastionAccessCheck(config=_bastion_access_config(tests)).execute()

        assert result["passed"] is False
        assert "bastion_hardened" in result["error"]

    def test_missing_tests_fails(self) -> None:
        """Fail when the step output does not include contract tests."""
        result = BmcBastionAccessCheck(config={"step_output": {}}).execute()

        assert result["passed"] is False
        assert "tests" in result["error"]


class TestMfaEnforcedCheck:
    """Tests for MfaEnforcedCheck validation."""

    def test_passes_all_mfa_checks(self) -> None:
        """Happy path: all four MFA checks pass."""
        v = MfaEnforcedCheck(config={"step_output": _mfa_output()})
        result = v.execute()
        assert result["passed"] is True
        assert "4 interfaces checked" in result["output"]

    def test_fails_when_root_mfa_disabled(self) -> None:
        """Fail when root MFA is not enabled."""
        out = _mfa_output()
        out["tests"]["root_mfa_enabled"] = {"passed": False, "error": "Root MFA off"}
        v = MfaEnforcedCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "root_mfa_enabled" in result["error"]

    def test_fails_when_console_users_lack_mfa(self) -> None:
        """Fail when console users don't have MFA."""
        out = _mfa_output()
        out["tests"]["console_users_mfa"] = {"passed": False, "error": "1/3 lack MFA"}
        v = MfaEnforcedCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "console_users_mfa" in result["error"]

    def test_fails_when_api_mfa_policy_missing(self) -> None:
        """Fail when no API MFA policy exists."""
        out = _mfa_output()
        out["tests"]["api_mfa_policy"] = {"passed": False, "error": "No MFA policy"}
        v = MfaEnforcedCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "api_mfa_policy" in result["error"]

    def test_fails_when_cli_mfa_policy_missing(self) -> None:
        """Fail when no CLI MFA policy exists."""
        out = _mfa_output()
        out["tests"]["cli_mfa_policy"] = {"passed": False, "error": "No MFA policy"}
        v = MfaEnforcedCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "cli_mfa_policy" in result["error"]

    def test_fails_when_tests_dict_empty(self) -> None:
        """Fail when tests dict is missing."""
        v = MfaEnforcedCheck(config={"step_output": {"success": False}})
        result = v.execute()
        assert result["passed"] is False
        assert "tests" in result["error"].lower()

    def test_fails_when_tests_key_missing(self) -> None:
        """Fail when a required test key is absent."""
        out = _mfa_output()
        del out["tests"]["cli_mfa_policy"]
        v = MfaEnforcedCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is False
        assert "cli_mfa_policy" in result["error"]

    def test_uses_interfaces_checked_in_message(self) -> None:
        """Pass output should include the interfaces_checked count."""
        out = _mfa_output(interfaces_checked=7)
        v = MfaEnforcedCheck(config={"step_output": out})
        result = v.execute()
        assert result["passed"] is True
        assert "7 interfaces checked" in result["output"]


def _byok_step_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid BYOK step output with optional overrides."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "customer_managed_key_test",
        "key_id": "cmk-test-123",
        "key_arn": "arn:provider:kms:region:tenant:key/cmk-test-123",
        "encrypted_resource_id": "volume-test-123",
        "encrypted_resource_kms_key_id": "cmk-test-123",
        "tests": deepcopy(BYOK_REQUIRED_TESTS),
    }
    output.update(overrides)
    return output


def test_customer_managed_key_check_passes_with_required_evidence() -> None:
    """CustomerManagedKeyCheck passes with all required BYOK checks and evidence."""
    check = CustomerManagedKeyCheck(config={"step_output": _byok_step_output()})

    result = check.execute()

    assert result["passed"] is True
    assert "Customer-managed key encryption verified" in result["output"]
    assert "cmk-test-123" in result["output"]


def test_byok_step_output_returns_independent_tests() -> None:
    """BYOK step output helper returns independent nested test data."""
    first = _byok_step_output()
    second = _byok_step_output()

    first["tests"]["customer_managed_key_available"]["passed"] = False

    assert second["tests"]["customer_managed_key_available"]["passed"] is True
    assert BYOK_REQUIRED_TESTS["customer_managed_key_available"]["passed"] is True


def test_customer_managed_key_check_fails_when_required_check_fails() -> None:
    """CustomerManagedKeyCheck fails with the specific failed BYOK probe."""
    tests = dict(BYOK_REQUIRED_TESTS)
    tests["provider_managed_key_not_used"] = {"passed": False, "error": "AWS-managed key selected"}
    check = CustomerManagedKeyCheck(config={"step_output": _byok_step_output(tests=tests)})

    result = check.execute()

    assert result["passed"] is False
    assert "provider_managed_key_not_used" in result["error"]
    assert "AWS-managed key selected" in result["error"]


def test_customer_managed_key_check_fails_when_required_check_missing() -> None:
    """CustomerManagedKeyCheck fails when a required BYOK probe is missing."""
    tests = {
        name: result for name, result in BYOK_REQUIRED_TESTS.items() if name != "resource_encrypted_with_customer_key"
    }
    check = CustomerManagedKeyCheck(config={"step_output": _byok_step_output(tests=tests)})

    result = check.execute()

    assert result["passed"] is False
    assert "resource_encrypted_with_customer_key" in result["error"]


def test_customer_managed_key_check_fails_without_key_evidence() -> None:
    """CustomerManagedKeyCheck fails when no key evidence is reported."""
    check = CustomerManagedKeyCheck(config={"step_output": _byok_step_output(key_id="", key_arn="")})

    result = check.execute()

    assert result["passed"] is False
    assert "key evidence" in result["error"]


def test_customer_managed_key_check_fails_without_resource_evidence() -> None:
    """CustomerManagedKeyCheck fails when no encrypted resource evidence is reported."""
    check = CustomerManagedKeyCheck(config={"step_output": _byok_step_output(encrypted_resource_id="", resource_id="")})

    result = check.execute()

    assert result["passed"] is False
    assert "encrypted resource evidence" in result["error"]


def test_customer_managed_key_check_falls_back_from_blank_evidence() -> None:
    """CustomerManagedKeyCheck falls back when preferred evidence fields are blank."""
    check = CustomerManagedKeyCheck(
        config={
            "step_output": _byok_step_output(
                key_id="   ",
                key_arn="  arn:provider:kms:region:tenant:key/fallback-key  ",
                encrypted_resource_id="   ",
                resource_id="  volume-fallback-123  ",
            )
        }
    )

    result = check.execute()

    assert result["passed"] is True
    assert "fallback-key" in result["output"]
    assert "volume-fallback-123" in result["output"]


def _cert_rotation_step_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid certificate-rotation step output with optional overrides."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "cert_rotation_test",
        "rotation_window_days": 60,
        "certs_inspected": 2,
        "out_of_policy": 0,
        "tests": deepcopy(CERT_ROTATION_REQUIRED_TESTS),
    }
    output.update(overrides)
    return output


def test_cert_rotation_cycle_check_passes_with_required_evidence() -> None:
    """CertRotationCycleCheck passes with rotation evidence and a <=60-day window."""
    check = CertRotationCycleCheck(config={"step_output": _cert_rotation_step_output()})

    result = check.execute()

    assert result["passed"] is True
    assert "Certificate rotation verified" in result["output"]


def test_cert_rotation_cycle_check_fails_when_required_check_fails() -> None:
    """CertRotationCycleCheck reports the failed certificate-rotation probe."""
    tests = deepcopy(CERT_ROTATION_REQUIRED_TESTS)
    tests["no_certs_out_of_policy"] = {"passed": False, "error": "certificate valid for 365 days"}
    check = CertRotationCycleCheck(config={"step_output": _cert_rotation_step_output(tests=tests)})

    result = check.execute()

    assert result["passed"] is False
    assert "no_certs_out_of_policy" in result["error"]
    assert "365 days" in result["error"]


def test_cert_rotation_cycle_check_fails_without_cert_inventory_evidence() -> None:
    """CertRotationCycleCheck fails when no non-hidden certificate was inspected."""
    check = CertRotationCycleCheck(config={"step_output": _cert_rotation_step_output(certs_inspected=0)})

    result = check.execute()

    assert result["passed"] is False
    assert "certs_inspected" in result["error"]


def test_cert_rotation_cycle_check_skips_when_no_control_plane_exists() -> None:
    """A structured skipped result skips SEC09-01."""
    with pytest.raises(pytest.skip.Exception, match="No managed TLS certificates found"):
        CertRotationCycleCheck(
            config={
                "step_output": _cert_rotation_step_output(
                    skipped=True,
                    skip_reason="No managed TLS certificates found on this platform",
                )
            }
        ).execute()


@pytest.mark.parametrize("rotation_window_days", [0, -1, 61, "60"])
def test_cert_rotation_cycle_check_fails_with_invalid_rotation_window(rotation_window_days: Any) -> None:
    """CertRotationCycleCheck requires a positive window no greater than 60 days."""
    check = CertRotationCycleCheck(
        config={"step_output": _cert_rotation_step_output(rotation_window_days=rotation_window_days)}
    )

    result = check.execute()

    assert result["passed"] is False
    assert "rotation_window_days" in result["error"]


@pytest.mark.parametrize("out_of_policy", [None, "0"])
def test_cert_rotation_cycle_check_fails_without_integer_out_of_policy(out_of_policy: Any) -> None:
    """CertRotationCycleCheck requires an integer out_of_policy count."""
    check = CertRotationCycleCheck(config={"step_output": _cert_rotation_step_output(out_of_policy=out_of_policy)})

    result = check.execute()

    assert result["passed"] is False
    assert "out_of_policy" in result["error"]


def _kms_options_step_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid KMS options step output with optional overrides."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "kms_encryption_options_test",
        "provider_managed_key_id": "alias/aws/eks",
        "customer_managed_key_id": "cmk-test-123",
        "tests": deepcopy(KMS_OPTIONS_REQUIRED_TESTS),
    }
    output.update(overrides)
    return output


def test_kms_encryption_option_check_passes_with_both_key_types() -> None:
    """KmsEncryptionOptionCheck passes with provider and customer key evidence."""
    check = KmsEncryptionOptionCheck(config={"step_output": _kms_options_step_output()})

    result = check.execute()

    assert result["passed"] is True
    assert "KMS encryption options verified" in result["output"]


def test_kms_encryption_option_check_fails_when_required_check_fails() -> None:
    """KmsEncryptionOptionCheck reports the failed KMS option probe."""
    tests = deepcopy(KMS_OPTIONS_REQUIRED_TESTS)
    tests["provider_managed_key_available"] = {"passed": False, "error": "alias missing"}
    check = KmsEncryptionOptionCheck(config={"step_output": _kms_options_step_output(tests=tests)})

    result = check.execute()

    assert result["passed"] is False
    assert "provider_managed_key_available" in result["error"]
    assert "alias missing" in result["error"]


def test_kms_encryption_option_check_fails_without_customer_evidence() -> None:
    """KmsEncryptionOptionCheck fails when customer-managed key evidence is absent."""
    check = KmsEncryptionOptionCheck(config={"step_output": _kms_options_step_output(customer_managed_key_id="")})

    result = check.execute()

    assert result["passed"] is False
    assert "customer-managed key evidence" in result["error"]


def test_kms_encryption_option_check_fails_without_provider_evidence() -> None:
    """KmsEncryptionOptionCheck fails when provider-managed key evidence is absent."""
    check = KmsEncryptionOptionCheck(config={"step_output": _kms_options_step_output(provider_managed_key_id="")})

    result = check.execute()

    assert result["passed"] is False
    assert "provider-managed key evidence" in result["error"]


def test_kms_encryption_option_check_skips_when_step_marks_skipped() -> None:
    """KmsEncryptionOptionCheck pytest.skips when scoped provider evidence is unavailable."""
    with pytest.raises(pytest.skip.Exception, match="only non-control-plane AWS-managed aliases"):
        KmsEncryptionOptionCheck(
            config={
                "step_output": _kms_options_step_output(
                    skipped=True,
                    skip_reason=(
                        "Control-plane provider-managed KMS evidence is not available; "
                        "only non-control-plane AWS-managed aliases were discovered: alias/aws/ebs"
                    ),
                    provider_managed_key_id="",
                    customer_managed_key_id="",
                )
            }
        ).execute()


def _centralized_kms_step_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid centralized-KMS step output with optional overrides."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "centralized_kms_test",
        "kms_keys_total": 3,
        "encrypted_resources_inspected": 2,
        "non_kms_resources": 0,
        "tests": deepcopy(CENTRALIZED_KMS_REQUIRED_TESTS),
    }
    output.update(overrides)
    return output


def test_centralized_kms_check_passes_when_all_resources_use_kms() -> None:
    """CentralizedKmsCheck passes with KMS keys and zero non-KMS resources."""
    check = CentralizedKmsCheck(config={"step_output": _centralized_kms_step_output()})

    result = check.execute()

    assert result["passed"] is True
    assert "Centralized KMS verified" in result["output"]


def test_centralized_kms_check_fails_when_required_check_fails() -> None:
    """CentralizedKmsCheck reports the failed centralized-KMS probe."""
    tests = deepcopy(CENTRALIZED_KMS_REQUIRED_TESTS)
    tests["all_encrypted_resources_use_kms"] = {"passed": False, "error": "encrypted volume uses legacy key"}
    check = CentralizedKmsCheck(config={"step_output": _centralized_kms_step_output(tests=tests)})

    result = check.execute()

    assert result["passed"] is False
    assert "all_encrypted_resources_use_kms" in result["error"]
    assert "legacy key" in result["error"]


def test_centralized_kms_check_fails_without_kms_key_evidence() -> None:
    """CentralizedKmsCheck fails when no KMS keys are reported."""
    check = CentralizedKmsCheck(config={"step_output": _centralized_kms_step_output(kms_keys_total=0)})

    result = check.execute()

    assert result["passed"] is False
    assert "kms_keys_total" in result["error"]


def test_centralized_kms_check_fails_with_non_kms_resources() -> None:
    """CentralizedKmsCheck fails when encrypted resources do not use KMS."""
    check = CentralizedKmsCheck(config={"step_output": _centralized_kms_step_output(non_kms_resources=1)})

    result = check.execute()

    assert result["passed"] is False
    assert "not using KMS" in result["error"]


@pytest.mark.parametrize("inspected", [0, None, "2"])
def test_centralized_kms_check_fails_without_inspected_resource_evidence(inspected: Any) -> None:
    """CentralizedKmsCheck requires a positive encrypted-resource inspection count."""
    check = CentralizedKmsCheck(
        config={"step_output": _centralized_kms_step_output(encrypted_resources_inspected=inspected)}
    )

    result = check.execute()

    assert result["passed"] is False
    assert "encrypted_resources_inspected" in result["error"]


def _oidc_step_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid OIDC step output with optional overrides."""
    output: dict[str, Any] = {
        "success": True,
        "issuer_url": "https://oidc.example/realms/isv",
        "audience": "isv-validation",
        "target_url": "https://api.example/protected",
        "endpoints_tested": 1,
        "tests": OIDC_REQUIRED_TESTS,
    }
    output.update(overrides)
    return output


def test_oidc_user_auth_check_passes_with_real_endpoint_metadata() -> None:
    """OidcUserAuthCheck passes when all probes and endpoint metadata are present."""
    check = OidcUserAuthCheck(config={"step_output": _oidc_step_output()})

    result = check.execute()

    assert result["passed"] is True
    assert "https://api.example/protected" in result["output"]


def test_oidc_user_auth_check_rejects_missing_target_url() -> None:
    """OidcUserAuthCheck fails old self-contained outputs without a target URL."""
    check = OidcUserAuthCheck(config={"step_output": _oidc_step_output(target_url="")})

    result = check.execute()

    assert result["passed"] is False
    assert "target_url" in result["error"]


def test_oidc_user_auth_check_requires_endpoint_probe_count() -> None:
    """OidcUserAuthCheck fails when no platform endpoint was probed."""
    check = OidcUserAuthCheck(config={"step_output": _oidc_step_output(endpoints_tested=0)})

    result = check.execute()

    assert result["passed"] is False
    assert "did not probe any platform endpoint" in result["error"]


def test_oidc_user_auth_check_skips_when_step_marks_skipped() -> None:
    """OidcUserAuthCheck pytest.skips through both run() and execute() when flagged."""
    step_output = {
        "success": True,
        "skipped": True,
        "skip_reason": "OIDC validation not configured; missing --issuer-url or OIDC_ISSUER_URL",
        "tests": {},
    }

    with pytest.raises(pytest.skip.Exception, match="OIDC validation not configured"):
        OidcUserAuthCheck(config={"step_output": step_output}).run()

    with pytest.raises(pytest.skip.Exception, match="OIDC validation not configured"):
        OidcUserAuthCheck(config={"step_output": step_output}).execute()


SHORT_LIVED_REQUIRED_TESTS = {
    "node_credential_has_expiry": {"passed": True},
    "node_credential_ttl_within_bound": {"passed": True},
    "workload_credential_has_expiry": {"passed": True},
    "workload_credential_ttl_within_bound": {"passed": True},
}


def _short_lived_step_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid short-lived credentials step output with optional overrides."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "short_lived_credentials_test",
        "node_credential_method": "sts:GetSessionToken",
        "workload_credential_method": "sts:GetFederationToken",
        "node_credential_ttl_seconds": 3600,
        "workload_credential_ttl_seconds": 3600,
        "max_ttl_seconds": 43200,
        "tests": deepcopy(SHORT_LIVED_REQUIRED_TESTS),
    }
    output.update(overrides)
    return output


def test_short_lived_credentials_check_passes_with_bounded_ttls() -> None:
    """ShortLivedCredentialsCheck passes when both TTLs sit within the configured bound."""
    check = ShortLivedCredentialsCheck(config={"step_output": _short_lived_step_output()})

    result = check.execute()

    assert result["passed"] is True
    assert "node TTL=3600s" in result["output"]
    assert "workload TTL=3600s" in result["output"]
    assert "bound=43200s" in result["output"]


def test_short_lived_credentials_check_fails_when_required_probe_missing() -> None:
    """ShortLivedCredentialsCheck reports the missing contract probe by name."""
    tests = {
        name: result
        for name, result in SHORT_LIVED_REQUIRED_TESTS.items()
        if name != "workload_credential_ttl_within_bound"
    }
    check = ShortLivedCredentialsCheck(config={"step_output": _short_lived_step_output(tests=tests)})

    result = check.execute()

    assert result["passed"] is False
    assert "workload_credential_ttl_within_bound" in result["error"]


def test_short_lived_credentials_check_fails_when_node_ttl_exceeds_bound() -> None:
    """ShortLivedCredentialsCheck fails when the node TTL is over the configured bound."""
    check = ShortLivedCredentialsCheck(
        config={"step_output": _short_lived_step_output(node_credential_ttl_seconds=99999)},
    )

    result = check.execute()

    assert result["passed"] is False
    assert "node_credential_ttl_seconds=99999s exceeds max_ttl_seconds=43200s" in result["error"]


def test_short_lived_credentials_check_fails_when_workload_ttl_zero() -> None:
    """ShortLivedCredentialsCheck rejects non-positive TTL values."""
    check = ShortLivedCredentialsCheck(
        config={"step_output": _short_lived_step_output(workload_credential_ttl_seconds=0)},
    )

    result = check.execute()

    assert result["passed"] is False
    assert "workload_credential_ttl_seconds" in result["error"]


def test_short_lived_credentials_check_fails_when_max_ttl_missing() -> None:
    """ShortLivedCredentialsCheck fails when max_ttl_seconds is missing or non-positive."""
    check = ShortLivedCredentialsCheck(
        config={"step_output": _short_lived_step_output(max_ttl_seconds=0)},
    )

    result = check.execute()

    assert result["passed"] is False
    assert "max_ttl_seconds" in result["error"]


def test_short_lived_credentials_check_skips_when_step_marks_skipped() -> None:
    """ShortLivedCredentialsCheck pytest.skips through both run() and execute() when flagged."""
    step_output = {
        "success": True,
        "skipped": True,
        "skip_reason": "sts:GetSessionToken not available with current principal (AccessDenied)",
        "tests": {},
    }

    with pytest.raises(pytest.skip.Exception, match="GetSessionToken not available"):
        ShortLivedCredentialsCheck(config={"step_output": step_output}).run()

    with pytest.raises(pytest.skip.Exception, match="GetSessionToken not available"):
        ShortLivedCredentialsCheck(config={"step_output": step_output}).execute()


TENANT_ISOLATION_REQUIRED_TESTS = {
    "network_isolated": {"passed": True},
    "data_isolated": {"passed": True},
    "compute_isolated": {"passed": True},
    "storage_isolated": {"passed": True},
}


def _tenant_isolation_step_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid tenant-isolation step output with optional overrides."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "tenant_isolation_test",
        "tenant_a_id": "isv-sec11-test-aaaa1111",
        "tenant_b_id": "isv-sec11-test-bbbb2222",
        "tests": deepcopy(TENANT_ISOLATION_REQUIRED_TESTS),
    }
    output.update(overrides)
    return output


class TestTenantIsolationCheck:
    """Tests for SEC11-01 hard tenant-isolation validation."""

    def test_passes_when_all_four_dimensions_isolated(self) -> None:
        """Pass when all four sub-claims are reported isolated."""
        result = TenantIsolationCheck(config={"step_output": _tenant_isolation_step_output()}).execute()

        assert result["passed"] is True
        assert "Hard tenant isolation verified" in result["output"]
        assert "isv-sec11-test-aaaa1111" in result["output"]
        assert "isv-sec11-test-bbbb2222" in result["output"]

    def test_fails_when_data_isolation_breached(self) -> None:
        """Fail with the specific contract key when cross-tenant KMS/S3 access leaks."""
        tests = deepcopy(TENANT_ISOLATION_REQUIRED_TESTS)
        tests["data_isolated"] = {"passed": False, "error": "kms:Decrypt unexpectedly allowed across tenants"}
        result = TenantIsolationCheck(config={"step_output": _tenant_isolation_step_output(tests=tests)}).execute()

        assert result["passed"] is False
        assert "data_isolated" in result["error"]
        assert "kms:Decrypt unexpectedly allowed" in result["error"]

    def test_fails_when_compute_isolation_breached(self) -> None:
        """Fail when tenant A can act on tenant B's compute resources."""
        tests = deepcopy(TENANT_ISOLATION_REQUIRED_TESTS)
        tests["compute_isolated"] = {"passed": False, "error": "ec2:DescribeInstances unexpectedly succeeded"}
        result = TenantIsolationCheck(config={"step_output": _tenant_isolation_step_output(tests=tests)}).execute()

        assert result["passed"] is False
        assert "compute_isolated" in result["error"]

    def test_fails_when_required_sub_claim_missing(self) -> None:
        """Fail when one of the four required sub-claims is absent from tests."""
        tests = {name: result for name, result in TENANT_ISOLATION_REQUIRED_TESTS.items() if name != "storage_isolated"}
        result = TenantIsolationCheck(config={"step_output": _tenant_isolation_step_output(tests=tests)}).execute()

        assert result["passed"] is False
        assert "storage_isolated" in result["error"]

    def test_fails_when_tenant_a_id_missing(self) -> None:
        """Fail when the source tenant identifier is empty -- guards against silent fixture failures."""
        result = TenantIsolationCheck(config={"step_output": _tenant_isolation_step_output(tenant_a_id="")}).execute()

        assert result["passed"] is False
        assert "tenant_a_id" in result["error"]

    def test_fails_when_tenant_b_id_missing(self) -> None:
        """Fail when the target tenant identifier is empty."""
        result = TenantIsolationCheck(
            config={"step_output": _tenant_isolation_step_output(tenant_b_id="   ")}
        ).execute()

        assert result["passed"] is False
        assert "tenant_b_id" in result["error"]

    def test_fails_when_tests_dict_missing(self) -> None:
        """Fail when the step output does not include any tests."""
        result = TenantIsolationCheck(config={"step_output": {"tenant_a_id": "a", "tenant_b_id": "b"}}).execute()

        assert result["passed"] is False
        assert "tests" in result["error"].lower()

    def test_skips_when_step_marks_skipped(self) -> None:
        """TenantIsolationCheck pytest.skips through both run() and execute() when flagged."""
        step_output = {
            "success": True,
            "skipped": True,
            "skip_reason": "tenant isolation fixture not provisionable (AccessDenied)",
            "tests": {},
        }

        with pytest.raises(pytest.skip.Exception, match="tenant isolation fixture not provisionable"):
            TenantIsolationCheck(config={"step_output": step_output}).run()

        with pytest.raises(pytest.skip.Exception, match="tenant isolation fixture not provisionable"):
            TenantIsolationCheck(config={"step_output": step_output}).execute()


REQUIRED_INSECURE_PROTOCOL_TESTS = [
    "sslv3_disabled",
    "tlsv1_0_disabled",
    "tlsv1_1_disabled",
    "plain_http_disabled",
]


def _insecure_protocols_config(
    tests: dict[str, dict[str, Any]] | None = None,
    *,
    endpoints_tested: int = 2,
) -> dict[str, Any]:
    """Build an InsecureProtocolsCheck validation config."""
    default_tests = {
        name: {"passed": True, "message": f"{name} confirmed"} for name in REQUIRED_INSECURE_PROTOCOL_TESTS
    }
    return {
        "step_output": {
            "success": True,
            "platform": "security",
            "test_name": "insecure_protocols",
            "endpoints_tested": endpoints_tested,
            "tests": tests if tests is not None else default_tests,
        },
    }


class TestInsecureProtocolsCheck:
    """Tests for SEC13-02 insecure-protocols validation."""

    def test_all_required_tests_pass(self) -> None:
        """Pass when every legacy protocol probe reports disabled."""
        result = InsecureProtocolsCheck(config=_insecure_protocols_config()).execute()

        assert result["passed"] is True
        assert "Insecure protocols disabled (2 endpoints tested)" in result["output"]

    def test_failed_step_without_error_fails_with_default_message(self) -> None:
        """Fail when the step reports success=False without an error message."""
        config = _insecure_protocols_config()
        config["step_output"]["success"] = False
        result = InsecureProtocolsCheck(config=config).execute()

        assert result["passed"] is False
        assert "Insecure protocol step failed: no error message provided" in result["error"]

    def test_failed_probe_reports_contract_key(self) -> None:
        """Fail when one of the four contract keys reports passed=False."""
        tests = {name: {"passed": True} for name in REQUIRED_INSECURE_PROTOCOL_TESTS}
        tests["tlsv1_0_disabled"] = {"passed": False, "error": "accepted on api.example.com:443"}
        result = InsecureProtocolsCheck(config=_insecure_protocols_config(tests)).execute()

        assert result["passed"] is False
        assert "tlsv1_0_disabled" in result["error"]
        assert "api.example.com:443" in result["error"]

    def test_missing_required_key_fails(self) -> None:
        """Fail when one of the four contract keys is absent."""
        tests = {name: {"passed": True} for name in REQUIRED_INSECURE_PROTOCOL_TESTS if name != "plain_http_disabled"}
        result = InsecureProtocolsCheck(config=_insecure_protocols_config(tests)).execute()

        assert result["passed"] is False
        assert "plain_http_disabled" in result["error"]

    def test_missing_endpoints_tested_fails(self) -> None:
        """Fail when endpoints_tested is absent even if all checks pass."""
        config = _insecure_protocols_config()
        del config["step_output"]["endpoints_tested"]
        result = InsecureProtocolsCheck(config=config).execute()

        assert result["passed"] is False
        assert "endpoints_tested" in result["error"]

    def test_zero_endpoints_tested_fails(self) -> None:
        """Fail when endpoints_tested is non-positive (claims pass on nothing)."""
        config = _insecure_protocols_config(endpoints_tested=0)
        result = InsecureProtocolsCheck(config=config).execute()

        assert result["passed"] is False
        assert "endpoints_tested" in result["error"]

    def test_skips_when_step_marks_skipped(self) -> None:
        """pytest.skip when the step emits a structured skip."""
        step_output = {
            "success": True,
            "skipped": True,
            "skip_reason": "No SEC13-02 endpoints configured",
        }
        with pytest.raises(pytest.skip.Exception, match="No SEC13-02 endpoints configured"):
            InsecureProtocolsCheck(config={"step_output": step_output}).execute()

    def test_missing_tests_fails(self) -> None:
        """Fail when the step output does not include contract tests."""
        result = InsecureProtocolsCheck(config={"step_output": {}}).execute()

        assert result["passed"] is False
        assert "tests" in result["error"].lower()
