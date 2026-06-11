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

import json
from typing import Any
from unittest.mock import MagicMock

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_oidc import K8sOidcIssuerCheck

VALID_OIDC_RESPONSE = {
    "issuer": "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE",
    "jwks_uri": "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE/keys",
    "response_types_supported": ["id_token"],
    "subject_types_supported": ["public"],
    "id_token_signing_alg_values_supported": ["RS256"],
}


def _make_check(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    config: dict[str, Any] | None = None,
) -> K8sOidcIssuerCheck:
    """Create a K8sOidcIssuerCheck instance with a mocked command runner."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = CommandResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration=0.1,
    )
    return K8sOidcIssuerCheck(runner=mock_runner, config=config or {})


class TestK8sOidcIssuerCheck:
    """Unit tests for K8sOidcIssuerCheck."""

    def test_success_with_all_required_fields(self) -> None:
        check = _make_check(stdout=json.dumps(VALID_OIDC_RESPONSE))
        result = check.execute()
        assert result["passed"] is True
        assert "OIDC discovery endpoint is valid" in result["output"]
        assert VALID_OIDC_RESPONSE["issuer"] in result["output"]

    def test_missing_required_fields(self) -> None:
        incomplete = {"issuer": "https://example.com", "jwks_uri": "https://example.com/keys"}
        check = _make_check(stdout=json.dumps(incomplete))
        result = check.execute()
        assert result["passed"] is False
        assert "missing required fields" in result["error"]
        assert "response_types_supported" in result["error"]

    def test_invalid_json_response(self) -> None:
        check = _make_check(stdout="not-json")
        result = check.execute()
        assert result["passed"] is False
        assert "Failed to parse OIDC discovery response as JSON" in result["error"]

    def test_non_object_json_response(self) -> None:
        check = _make_check(stdout="[]")
        result = check.execute()
        assert result["passed"] is False
        assert "must be a JSON object" in result["error"]

    def test_kubectl_command_failure(self) -> None:
        check = _make_check(exit_code=1, stderr="connection refused")
        result = check.execute()
        assert result["passed"] is False
        assert "Failed to query OIDC discovery endpoint" in result["error"]
        assert "connection refused" in result["error"]

    def test_custom_required_fields(self) -> None:
        response = {"issuer": "https://example.com", "custom_field": "value"}
        check = _make_check(
            stdout=json.dumps(response),
            config={"required_fields": ["issuer", "custom_field"]},
        )
        result = check.execute()
        assert result["passed"] is True

    def test_required_fields_single_string_is_accepted(self) -> None:
        response = {"issuer": "https://example.com"}
        check = _make_check(
            stdout=json.dumps(response),
            config={"required_fields": "issuer"},
        )
        result = check.execute()
        assert result["passed"] is True

    def test_required_fields_single_string_with_whitespace_is_trimmed(self) -> None:
        response = {"issuer": "https://example.com"}
        check = _make_check(
            stdout=json.dumps(response),
            config={"required_fields": " issuer "},
        )
        result = check.execute()
        assert result["passed"] is True

    def test_required_fields_whitespace_string_is_rejected(self) -> None:
        check = _make_check(
            stdout=json.dumps(VALID_OIDC_RESPONSE),
            config={"required_fields": "   "},
        )
        result = check.execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_required_fields_none_is_rejected(self) -> None:
        check = _make_check(
            stdout=json.dumps(VALID_OIDC_RESPONSE),
            config={"required_fields": None},
        )
        result = check.execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_required_fields_non_string_items_are_rejected(self) -> None:
        check = _make_check(
            stdout=json.dumps(VALID_OIDC_RESPONSE),
            config={"required_fields": ["issuer", 1]},
        )
        result = check.execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_required_fields_whitespace_items_are_rejected(self) -> None:
        check = _make_check(
            stdout=json.dumps(VALID_OIDC_RESPONSE),
            config={"required_fields": ["issuer", "   "]},
        )
        result = check.execute()
        assert result["passed"] is False
        assert "Invalid 'required_fields' config" in result["error"]

    def test_issuer_not_https(self) -> None:
        response = dict(VALID_OIDC_RESPONSE, issuer="http://insecure.example.com")
        check = _make_check(stdout=json.dumps(response))
        result = check.execute()
        assert result["passed"] is False
        assert "not a valid HTTPS URL" in result["error"]

    def test_issuer_empty_string(self) -> None:
        response = dict(VALID_OIDC_RESPONSE, issuer="")
        check = _make_check(stdout=json.dumps(response))
        result = check.execute()
        assert result["passed"] is False
        assert "not a valid HTTPS URL" in result["error"]
