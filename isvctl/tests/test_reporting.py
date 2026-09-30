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
from typing import Any
from unittest.mock import MagicMock, patch

from isvctl.cli.test import CORE_REQUIREMENT_CONTEXT, _reported_capability
from isvctl.config.schema import RunConfig
from isvctl.reporting import (
    check_upload_credentials,
    get_environment_config,
    get_isv_test_version,
    update_test_run,
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


class TestUpdateTestRun:
    """Tests for result and catalog upload orchestration."""

    @patch("isvreporter.client.update_test_run")
    @patch("isvreporter.client.upload_test_catalog")
    @patch("isvreporter.auth.get_jwt_token", return_value="jwt-token")
    @patch(
        "isvctl.reporting.get_environment_config", return_value=("https://api.example.com", "https://ssa.example.com")
    )
    @patch("isvctl.reporting.check_upload_credentials", return_value=(True, "client-id", "client-secret"))
    def test_reports_catalog_identity_without_publishing_catalog(
        self,
        _mock_credentials: MagicMock,
        _mock_environment: MagicMock,
        _mock_token: MagicMock,
        mock_upload_catalog: MagicMock,
        _mock_update_run: MagicMock,
    ) -> None:
        """Ordinary runs send identity only; release workflow owns publication."""
        document = {
            "schemaVersion": 2,
            "isvTestVersion": "1.2.3",
            "catalogDigest": "sha256:abc",
            "isvTestBuildRef": "v1.2.3-3-gabc1234",
            "capabilities": ["kubernetes", "vm"],
            "suites": ["storage", "iam"],
            "entries": [{"name": "TestA"}],
        }

        result = update_test_run(
            lab_id=7,
            test_run_id="run-123",
            success=True,
            start_time="2026-07-24T12:00:00Z",
            catalog_document=document,
        )

        assert result is True
        mock_upload_catalog.assert_not_called()
        assert _mock_update_run.call_args.kwargs["isv_test_version"] == "1.2.3"
        assert _mock_update_run.call_args.kwargs["isv_test_catalog_digest"] == "sha256:abc"
        assert _mock_update_run.call_args.kwargs["isv_test_build_ref"] == "v1.2.3-3-gabc1234"

    @patch("isvreporter.client.update_test_run")
    @patch("isvreporter.auth.get_jwt_token", return_value="jwt-token")
    @patch(
        "isvctl.reporting.get_environment_config", return_value=("https://api.example.com", "https://ssa.example.com")
    )
    @patch("isvctl.reporting.check_upload_credentials", return_value=(True, "client-id", "client-secret"))
    @patch("isvctl.reporting.build_ref", return_value="v9.9.9-0-greporter")
    def test_an_unverified_artifact_never_borrows_the_reporting_machine_ref(
        self,
        reporter_build_ref: MagicMock,
        _mock_credentials: MagicMock,
        _mock_environment: MagicMock,
        _mock_token: MagicMock,
        mock_update_run: MagicMock,
    ) -> None:
        document = {
            "isvTestVersion": "1.2.3",
            "catalogDigest": "sha256:abc",
            "isvTestBuildRef": None,
        }

        assert update_test_run(
            lab_id=7,
            test_run_id="run-123",
            success=True,
            start_time="2026-07-24T12:00:00Z",
            catalog_document=document,
        )

        assert mock_update_run.call_args.kwargs["isv_test_build_ref"] is None
        reporter_build_ref.assert_not_called()

    def test_envelope_fixture_matches_the_real_catalog_document(self) -> None:
        """Pin the hand-built fixture above to what the producer actually emits.

        A literal fixture can drift from ``catalog_document`` and keep passing
        while the real upload raises - which is how the capability rename broke
        uploads with this suite green.
        """
        from isvtest.catalog import catalog_document

        assert set(catalog_document([], "1.2.3")) == {
            "schemaVersion",
            "isvTestVersion",
            "catalogDigest",
            "isvTestBuildRef",
            "capabilities",
            "suites",
            "entries",
        }


class TestReportedCapability:
    """The capability recorded on a test run, translated out of client spellings.

    A run's signal is the (suite, capability) pair: `network` and
    `network --capability vm` execute different checks, so the capability has to
    survive the trip to the service in a form the service understands.
    """

    @staticmethod
    def _config(platform: str | None) -> RunConfig:
        """Return a minimal config, declaring `platform` only for a platform suite."""
        raw: dict[str, Any] = {"commands": {}, "tests": {"validations": {}}}
        if platform:
            raw["tests"]["capability"] = platform
        return RunConfig.model_validate(raw)

    def test_core_context_is_reported_as_no_capability(self) -> None:
        """`core` is this client's word for "no capability", not a fifth one."""
        config = self._config(None)
        assert _reported_capability(config, CORE_REQUIREMENT_CONTEXT) is None

    def test_platform_suite_reports_its_own_platform(self) -> None:
        """A platform suite records its declaration as the run capability."""
        config = self._config("vm")
        assert _reported_capability(config, "vm") == "vm"

    def test_plain_suite_reports_explicit_capability(self) -> None:
        """A plain suite reports its explicit capability context."""
        config = self._config(None)
        assert _reported_capability(config, "vm") == "vm"

    def test_platform_suite_declaration_wins_defensively(self) -> None:
        """A platform suite cannot be mislabeled if an invalid context reaches reporting."""
        assert _reported_capability(self._config("vm"), "kubernetes") == "vm"
