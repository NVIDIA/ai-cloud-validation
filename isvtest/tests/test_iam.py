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

"""Tests for IAM validations."""

from __future__ import annotations

from typing import Any

from isvtest.validations.iam import ServiceAccountCredentialCheck


def _sa_credential_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid service-account credential step output."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "sa_credential_test",
        "authenticated": True,
        "credential_type": "access_key",
        "credential_source": "long_lived_key",
        "identity": "arn:aws:iam::123456789012:user/isv-sa-test-abcd",
        "expires_at": None,
    }
    output.update(overrides)
    return output


def test_sa_credential_check_passes_with_long_lived_key() -> None:
    """Passes for the AWS-style long-lived access-key source."""
    result = ServiceAccountCredentialCheck(config={"step_output": _sa_credential_output()}).execute()

    assert result["passed"] is True
    assert "authenticated via access_key (long_lived_key)" in result["output"]


def test_sa_credential_check_passes_with_keyless_source() -> None:
    """Passes for a keyless source (impersonation / WIF / short-lived token) -- a platform
    that disables long-lived key download proves authentication this way."""
    result = ServiceAccountCredentialCheck(
        config={
            "step_output": _sa_credential_output(
                credential_type="oauth2_token",
                credential_source="impersonation",
                expires_at="2026-06-09T01:00:00Z",
                identity="sa-test@project.iam.gserviceaccount.com",
            )
        }
    ).execute()

    assert result["passed"] is True
    assert "authenticated via oauth2_token (impersonation)" in result["output"]


def test_sa_credential_check_passes_without_credential_source() -> None:
    """credential_source is optional and informational; its absence does not fail."""
    out = _sa_credential_output()
    del out["credential_source"]

    result = ServiceAccountCredentialCheck(config={"step_output": out}).execute()

    assert result["passed"] is True
    assert "authenticated via access_key as" in result["output"]


def test_sa_credential_check_fails_when_not_authenticated() -> None:
    """Fails when authentication did not succeed (e.g. key creation blocked by policy)."""
    result = ServiceAccountCredentialCheck(
        config={"step_output": _sa_credential_output(authenticated=False, error="key creation blocked")}
    ).execute()

    assert result["passed"] is False
    assert "authentication failed" in result["error"]


def test_sa_credential_check_fails_without_identity() -> None:
    """Fails when no identity resolved from the credential."""
    result = ServiceAccountCredentialCheck(config={"step_output": _sa_credential_output(identity="")}).execute()

    assert result["passed"] is False
    assert "identity" in result["error"]
