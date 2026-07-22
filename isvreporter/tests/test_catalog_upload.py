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

"""Tests for the test catalog upload functionality in the API client."""

import json
from http import HTTPStatus
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from isvreporter.client import upload_test_catalog


class TestUploadTestCatalog:
    """Tests for upload_test_catalog function."""

    @patch("isvreporter.client.urlopen")
    def test_successful_upload(self, mock_urlopen: MagicMock) -> None:
        """Test successful catalog upload returns True."""
        # GET returns empty list (version not found), POST returns success
        get_response = MagicMock()
        get_response.read.return_value = json.dumps([]).encode()
        get_response.__enter__ = MagicMock(return_value=get_response)
        get_response.__exit__ = MagicMock(return_value=False)

        post_response = MagicMock()
        post_response.read.return_value = json.dumps({"status": "created"}).encode()
        post_response.__enter__ = MagicMock(return_value=post_response)
        post_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [get_response, post_response]

        entries = [
            {
                "name": "TestA",
                "description": "Test A",
                "labels": ["kubernetes"],
                "platforms": ["kubernetes"],
                "modules": [],
                "test_ids": ["K8S06-01"],
            },
            {
                "name": "TestB",
                "description": "Test B",
                "labels": ["iam"],
                "platforms": [],
                "modules": ["iam"],
            },
        ]

        result = upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=entries,
        )

        assert result is True
        assert mock_urlopen.call_count == 2

        post_call = mock_urlopen.call_args_list[1]
        request = post_call[0][0]
        assert request.full_url == "https://api.example.com/v1/test-catalog"
        assert request.method == "POST"

        payload = json.loads(request.data.decode())
        assert payload["isvTestVersion"] == "1.2.3"
        # Envelope defaults when axis metadata is not supplied.
        assert payload["schemaVersion"] == 1
        assert payload["platforms"] == []
        assert payload["modules"] == []
        assert len(payload["entries"]) == 2
        assert payload["entries"][0]["name"] == "TestA"
        assert payload["entries"][0]["labels"] == ["kubernetes"]
        assert payload["entries"][0]["platforms"] == ["kubernetes"]
        assert payload["entries"][0]["modules"] == []
        assert payload["entries"][0]["test_ids"] == ["K8S06-01"]
        assert "markers" not in payload["entries"][0]
        assert payload["entries"][1]["labels"] == ["iam"]
        assert payload["entries"][1]["platforms"] == []
        assert payload["entries"][1]["modules"] == ["iam"]
        assert payload["entries"][1]["test_ids"] == []

    @patch("isvreporter.client.urlopen")
    def test_skips_upload_when_version_exists(self, mock_urlopen: MagicMock) -> None:
        """Test that upload is skipped when version already exists."""
        get_response = MagicMock()
        get_response.read.return_value = json.dumps(["1.2.3"]).encode()
        get_response.__enter__ = MagicMock(return_value=get_response)
        get_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = get_response

        result = upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=[{"name": "TestA"}],
        )

        assert result is True
        mock_urlopen.assert_called_once()

    @patch("isvreporter.client.urlopen")
    def test_conflict_returns_true(self, mock_urlopen: MagicMock) -> None:
        """Test that 409 Conflict (catalog already exists) returns True."""
        mock_urlopen.side_effect = HTTPError(
            url="https://api.example.com/v1/test-catalog",
            code=HTTPStatus.CONFLICT,
            msg="Conflict",
            hdrs=MagicMock(),
            fp=MagicMock(read=MagicMock(return_value=b"already exists")),
        )

        result = upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=[{"name": "TestA"}],
        )

        assert result is True

    @patch("isvreporter.client.urlopen")
    def test_server_error_returns_false(self, mock_urlopen: MagicMock) -> None:
        """Test that 500 error returns False."""
        mock_urlopen.side_effect = HTTPError(
            url="https://api.example.com/v1/test-catalog",
            code=HTTPStatus.INTERNAL_SERVER_ERROR,
            msg="Server Error",
            hdrs=MagicMock(),
            fp=MagicMock(read=MagicMock(return_value=b"internal error")),
        )

        result = upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=[{"name": "TestA"}],
        )

        assert result is False

    @patch("isvreporter.client.urlopen")
    def test_connection_error_returns_false(self, mock_urlopen: MagicMock) -> None:
        """Test that connection error returns False."""
        mock_urlopen.side_effect = URLError("Connection refused")

        result = upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=[{"name": "TestA"}],
        )

        assert result is False

    @patch("isvreporter.client.urlopen")
    def test_empty_optional_fields_use_defaults(self, mock_urlopen: MagicMock) -> None:
        """Test that missing optional fields get empty defaults."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "created"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        entries = [{"name": "TestA"}]

        upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.0.0",
            entries=entries,
        )

        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        payload = json.loads(request.data.decode())
        entry = payload["entries"][0]

        assert entry["name"] == "TestA"
        assert entry["description"] == ""
        assert entry["labels"] == []
        assert entry["platforms"] == []
        assert entry["modules"] == []
        assert "markers" not in entry
        assert entry["test_ids"] == []

    @patch("isvreporter.client.urlopen")
    def test_forwards_axis_metadata(self, mock_urlopen: MagicMock) -> None:
        """schema_version/platforms/modules are sent in the top-level envelope."""
        get_response = MagicMock()
        get_response.read.return_value = json.dumps([]).encode()
        get_response.__enter__ = MagicMock(return_value=get_response)
        get_response.__exit__ = MagicMock(return_value=False)
        post_response = MagicMock()
        post_response.read.return_value = json.dumps({"status": "created"}).encode()
        post_response.__enter__ = MagicMock(return_value=post_response)
        post_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [get_response, post_response]

        upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=[{"name": "TestA"}],
            schema_version=1,
            platforms=["bare_metal", "vm"],
            modules=["iam", "network"],
        )

        request = mock_urlopen.call_args_list[1][0][0]
        payload = json.loads(request.data.decode())
        assert payload["schemaVersion"] == 1
        assert payload["platforms"] == ["bare_metal", "vm"]
        assert payload["modules"] == ["iam", "network"]

    @patch("isvreporter.client.urlopen")
    def test_markers_field_is_not_forwarded(self, mock_urlopen: MagicMock) -> None:
        """The upload payload no longer carries the legacy ``markers`` field."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "created"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.0.0",
            entries=[{"name": "TestA", "labels": ["gpu"], "markers": ["gpu"]}],
        )

        request = mock_urlopen.call_args[0][0]
        payload = json.loads(request.data.decode())
        entry = payload["entries"][0]

        assert entry["labels"] == ["gpu"]
        assert "markers" not in entry
