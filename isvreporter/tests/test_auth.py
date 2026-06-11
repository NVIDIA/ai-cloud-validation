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

"""Tests for auth module."""

import base64
import json
import urllib.parse
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from isvreporter.auth import get_jwt_token


def make_mock_response(payload: dict[str, Any]) -> MagicMock:
    """Create a mock urlopen response with the given JSON payload."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(payload).encode()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


class TestGetJwtToken:
    """Tests for get_jwt_token function."""

    def test_successful_token_retrieval(self) -> None:
        """Test successful JWT token retrieval."""
        mock_response = make_mock_response({"access_token": "test-jwt-token"})

        with patch("isvreporter.auth.urlopen", return_value=mock_response) as mock_urlopen:
            token = get_jwt_token(
                ssa_issuer="https://example.com",
                client_id="test-client",
                client_secret="test-secret",
            )
            assert token == "test-jwt-token"

            # Verify request was made correctly
            call_args = mock_urlopen.call_args
            request = call_args[0][0]
            assert request.full_url == "https://example.com/token"
            assert request.get_method() == "POST"

            # Verify form body contains expected OAuth parameters
            form_data = urllib.parse.parse_qs(request.data.decode())
            assert form_data["grant_type"] == ["client_credentials"]
            assert "create-isv-lab-test-run" in form_data["scope"][0]
            assert "update-isv-lab-test-run" in form_data["scope"][0]

            # Verify authorization header is base64 encoded
            auth_header = request.get_header("Authorization")
            expected_creds = base64.b64encode(b"test-client:test-secret").decode()
            assert auth_header == f"Basic {expected_creds}"

    def test_http_error_exits_with_code_1(self) -> None:
        """Test that HTTP errors cause sys.exit(1)."""
        http_error = HTTPError(
            url="https://example.com/token",
            code=401,
            msg="Unauthorized",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(b""),
        )

        with patch("isvreporter.auth.urlopen", side_effect=http_error):
            with pytest.raises(SystemExit) as exc_info:
                get_jwt_token(
                    ssa_issuer="https://example.com",
                    client_id="bad-client",
                    client_secret="bad-secret",
                )
            assert exc_info.value.code == 1

    def test_url_error_exits_with_code_1(self) -> None:
        """Test that URL errors cause sys.exit(1)."""
        url_error = URLError("Connection refused")

        with patch("isvreporter.auth.urlopen", side_effect=url_error):
            with pytest.raises(SystemExit) as exc_info:
                get_jwt_token(
                    ssa_issuer="https://example.com",
                    client_id="test-client",
                    client_secret="test-secret",
                )
            assert exc_info.value.code == 1

    def test_missing_access_token_exits_with_code_1(self) -> None:
        """Test that missing access_token in response causes sys.exit(1)."""
        mock_response = make_mock_response({"token": "wrong-key"})

        with patch("isvreporter.auth.urlopen", return_value=mock_response):
            with pytest.raises(SystemExit) as exc_info:
                get_jwt_token(
                    ssa_issuer="https://example.com",
                    client_id="test-client",
                    client_secret="test-secret",
                )
            assert exc_info.value.code == 1

    def test_request_includes_correct_scope(self) -> None:
        """Test that request includes correct OAuth scope."""
        mock_response = make_mock_response({"access_token": "token"})

        with patch("isvreporter.auth.urlopen", return_value=mock_response) as mock_urlopen:
            get_jwt_token(
                ssa_issuer="https://example.com",
                client_id="client",
                client_secret="secret",
            )

            call_args = mock_urlopen.call_args
            request = call_args[0][0]
            data = request.data.decode()
            assert "scope=create-isv-lab-test-run" in data
            assert "update-isv-lab-test-run" in data
            assert "grant_type=client_credentials" in data
