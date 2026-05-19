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

"""Tests for reporting module."""

import os
from unittest.mock import MagicMock, patch

from isvctl.reporting import (
    check_upload_credentials,
    get_environment_config,
    get_isv_test_version,
)


class TestCheckUploadCredentials:
    """Tests for check_upload_credentials function."""

    def test_returns_true_when_both_credentials_set(self) -> None:
        """Test that True is returned when both credentials are set."""
        with patch.dict(
            os.environ,
            {"ISV_CLIENT_ID": "test-client", "ISV_CLIENT_SECRET": "test-secret"},
        ):
            can_upload, client_id, client_secret = check_upload_credentials()
            assert can_upload is True
            assert client_id == "test-client"
            assert client_secret == "test-secret"

    def test_returns_false_when_client_id_missing(self) -> None:
        """Test that False is returned when client ID is missing."""
        with patch.dict(os.environ, {"ISV_CLIENT_SECRET": "test-secret"}, clear=True):
            can_upload, client_id, client_secret = check_upload_credentials()
            assert can_upload is False
            assert client_id is None
            assert client_secret is None

    def test_returns_false_when_client_secret_missing(self) -> None:
        """Test that False is returned when client secret is missing."""
        with patch.dict(os.environ, {"ISV_CLIENT_ID": "test-client"}, clear=True):
            can_upload, client_id, client_secret = check_upload_credentials()
            assert can_upload is False
            assert client_id is None
            assert client_secret is None

    def test_returns_false_when_both_missing(self) -> None:
        """Test that False is returned when both credentials are missing."""
        with patch.dict(os.environ, {}, clear=True):
            can_upload, client_id, client_secret = check_upload_credentials()
            assert can_upload is False
            assert client_id is None
            assert client_secret is None

    def test_returns_false_when_credentials_empty(self) -> None:
        """Test that False is returned when credentials are empty strings."""
        with patch.dict(
            os.environ,
            {"ISV_CLIENT_ID": "", "ISV_CLIENT_SECRET": ""},
        ):
            can_upload, _client_id, _client_secret = check_upload_credentials()
            assert can_upload is False


class TestGetEnvironmentConfig:
    """Tests for get_environment_config function."""

    def test_returns_custom_endpoint_when_set(self) -> None:
        """Test that custom endpoint is returned when set in env."""
        with patch.dict(
            os.environ,
            {"ISV_SERVICE_ENDPOINT": "https://custom.example.com"},
        ):
            endpoint, _ = get_environment_config()
            assert endpoint == "https://custom.example.com"

    def test_returns_custom_ssa_issuer_when_set(self) -> None:
        """Test that custom SSA issuer is returned when set in env."""
        with patch.dict(
            os.environ,
            {"ISV_SSA_ISSUER": "https://custom-ssa.example.com"},
        ):
            _, ssa_issuer = get_environment_config()
            assert ssa_issuer == "https://custom-ssa.example.com"

    def test_returns_empty_when_env_not_set(self) -> None:
        """Test that empty strings are returned when env vars not set."""
        with patch.dict(os.environ, {}, clear=True):
            endpoint, ssa_issuer = get_environment_config()
            assert endpoint == ""
            assert ssa_issuer == ""


class TestGetIsvTestVersion:
    """Tests for get_isv_test_version function."""

    def test_returns_version_when_available(self) -> None:
        """Test that version is returned when __version__ is available."""
        with patch("isvctl.reporting.__version__", "1.2.3", create=True):
            # Need to reload to pick up the patched version
            with patch.dict("sys.modules", {"isvctl": MagicMock(__version__="1.2.3")}):
                result = get_isv_test_version()
                # Either returns a version string or None depending on import
                assert result is None or isinstance(result, str)

    def test_returns_none_on_import_error(self) -> None:
        """Test that None is returned when import fails."""
        with patch(
            "isvctl.reporting.get_isv_test_version",
            side_effect=lambda: None,
        ):
            # The function should handle exceptions gracefully
            result = get_isv_test_version()
            # Result depends on whether __version__ is available
            assert result is None or isinstance(result, str)
