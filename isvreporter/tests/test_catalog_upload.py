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
from collections.abc import Iterator
from http import HTTPStatus
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from isvreporter.client import upload_test_catalog

DIGEST = "sha256:" + "a" * 64
RELEASE_REF = "v1.2.3-0-gdeadbee"


@pytest.fixture
def _on_a_release_tag() -> Iterator[None]:
    """Present the build as a clean release tag, so the upload gate lets it past.

    Applied to the mechanics tests below, which are about the HTTP exchange
    rather than about who is allowed to publish. The gate itself is exercised by
    TestUploadTestCatalogReleaseGate.
    """
    with patch("isvreporter.client.build_is_release", return_value=True):
        yield


class TestUploadTestCatalogReleaseGate:
    """Who is allowed to publish a catalog, and who is only presumed to be."""

    @patch("isvreporter.client.urlopen")
    def test_a_clean_release_tag_may_publish(self, mock_urlopen: MagicMock) -> None:
        post_response = MagicMock()
        post_response.read.return_value = json.dumps({"status": "created"}).encode()
        post_response.__enter__ = MagicMock(return_value=post_response)
        post_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = post_response

        assert _upload("1.2.3", source_ref=RELEASE_REF) is True
        assert mock_urlopen.call_count == 1

    @patch("isvreporter.client.urlopen")
    def test_a_build_past_the_tag_may_not(self, mock_urlopen: MagicMock) -> None:
        """The lab-42 build: its checks are not the ones 1.2.3 published."""
        assert _upload("1.2.3", source_ref="v1.2.3-9-g08339c7") is False
        mock_urlopen.assert_not_called()

    @patch("isvreporter.client.urlopen")
    def test_a_build_that_cannot_tell_may_not_either(self, mock_urlopen: MagicMock) -> None:
        """A copied tree or an air-gapped cluster is not thereby a release.

        Letting the unknown case through is precisely how a working tree's
        catalog would come to be published under a release's number, which
        "latest catalog" then resolves to for good.
        """
        assert _upload("1.2.3", source_ref="unknown") is False
        mock_urlopen.assert_not_called()

    @patch("isvreporter.client.urlopen")
    def test_a_stale_install_may_not_either(self, mock_urlopen: MagicMock) -> None:
        """Metadata says 1.2.3, the tree is at v2.0.0: neither is safe to publish."""
        assert _upload("1.2.3", source_ref="v2.0.0-0-gdeadbee") is False
        mock_urlopen.assert_not_called()

    def test_refusing_to_publish_is_reported_to_the_publication_command(self) -> None:
        """Catalog publication fails independently from ordinary result upload."""
        assert _upload("1.2.3", source_ref="v1.2.3-9-g08339c7") is False


def _upload(version: str, *, source_ref: str = RELEASE_REF) -> bool:
    return upload_test_catalog(
        endpoint="https://api.example.com",
        jwt_token="test-token",
        isv_test_version=version,
        entries=[{"name": "TestA"}],
        schema_version=2,
        capabilities=["KUBERNETES"],
        suites=["network"],
        catalog_digest=DIGEST,
        isv_test_build_ref=source_ref,
    )


@pytest.mark.usefixtures("_on_a_release_tag")
class TestUploadTestCatalog:
    """Tests for upload_test_catalog function."""

    def test_requires_the_complete_catalog_envelope(self) -> None:
        """Callers cannot silently upload a v2 catalog without its axes."""
        with pytest.raises(TypeError):
            upload_test_catalog(
                endpoint="https://api.example.com",
                jwt_token="test-token",
                isv_test_version="1.2.3",
                entries=[{"name": "TestA"}],
            )

    @patch("isvreporter.client.urlopen")
    def test_successful_upload(self, mock_urlopen: MagicMock) -> None:
        """Test successful catalog upload returns True."""
        post_response = MagicMock()
        post_response.read.return_value = json.dumps({"status": "created"}).encode()
        post_response.__enter__ = MagicMock(return_value=post_response)
        post_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.return_value = post_response

        entries = [
            {
                "name": "TestA",
                "description": "Test A",
                "labels": ["k8s"],
                "source": "mod.a",
                "suite": "kubernetes",
                "capability": "kubernetes",
                "requires": [],
                "test_ids": ["K8S06-01"],
            },
            {
                "name": "TestB",
                "description": "Test B",
                "labels": [],
                "source": "mod.b",
                "suite": "storage",
                "capability": None,
                "requires": ["vm", "bare_metal"],
            },
        ]

        result = upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=entries,
            schema_version=2,
            capabilities=["kubernetes", "vm"],
            suites=["storage"],
            catalog_digest=DIGEST,
            isv_test_build_ref=RELEASE_REF,
        )

        assert result is True
        assert mock_urlopen.call_count == 1

        post_call = mock_urlopen.call_args
        request = post_call[0][0]
        assert request.full_url == "https://api.example.com/v1/test-catalog"
        assert request.method == "POST"

        payload = json.loads(request.data.decode())
        assert payload["isvTestVersion"] == "1.2.3"
        assert payload["catalogDigest"] == DIGEST
        assert payload["isvTestBuildRef"] == RELEASE_REF
        assert payload["schemaVersion"] == 2
        assert payload["capabilities"] == ["kubernetes", "vm"]
        assert payload["suites"] == ["storage"]
        assert len(payload["entries"]) == 2
        assert payload["entries"][0]["name"] == "TestA"
        assert payload["entries"][0]["labels"] == ["k8s"]
        assert payload["entries"][0]["test_ids"] == ["K8S06-01"]
        assert "markers" not in payload["entries"][0]
        assert payload["entries"][1]["labels"] == []
        assert payload["entries"][1]["test_ids"] == []

    @patch("isvreporter.client.urlopen")
    def test_idempotent_backend_response_returns_true(self, mock_urlopen: MagicMock) -> None:
        """The backend accepts an identical repeat upload."""
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
            schema_version=2,
            capabilities=["kubernetes"],
            suites=["storage"],
            catalog_digest=DIGEST,
            isv_test_build_ref=RELEASE_REF,
        )

        assert result is True
        mock_urlopen.assert_called_once()

    @patch("isvreporter.client.urlopen")
    def test_conflict_returns_false(self, mock_urlopen: MagicMock) -> None:
        """Changed identity under an existing version is a real conflict."""
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
            schema_version=2,
            capabilities=["kubernetes"],
            suites=["storage"],
            catalog_digest=DIGEST,
            isv_test_build_ref=RELEASE_REF,
        )

        assert result is False

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
            schema_version=2,
            capabilities=["kubernetes"],
            suites=["storage"],
            catalog_digest=DIGEST,
            isv_test_build_ref=RELEASE_REF,
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
            schema_version=2,
            capabilities=["kubernetes"],
            suites=["storage"],
            catalog_digest=DIGEST,
            isv_test_build_ref=RELEASE_REF,
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
            schema_version=2,
            capabilities=["kubernetes"],
            suites=["storage"],
            catalog_digest=DIGEST,
            isv_test_build_ref="v1.0.0-0-gdeadbee",
        )

        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        payload = json.loads(request.data.decode())
        entry = payload["entries"][0]

        assert entry["name"] == "TestA"
        assert entry["description"] == ""
        assert entry["labels"] == []
        assert "markers" not in entry
        assert "source" not in entry
        assert entry["suite"] == ""
        assert entry["capability"] is None
        assert entry["requires"] == []
        assert entry["test_ids"] == []

    @patch("isvreporter.client.urlopen")
    def test_forwards_catalog_axis_vocabulary(self, mock_urlopen: MagicMock) -> None:
        """Schema version and catalog axis lists are sent in the envelope."""
        post_response = MagicMock()
        post_response.read.return_value = json.dumps({"status": "created"}).encode()
        post_response.__enter__ = MagicMock(return_value=post_response)
        post_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = post_response

        upload_test_catalog(
            endpoint="https://api.example.com",
            jwt_token="test-token",
            isv_test_version="1.2.3",
            entries=[{"name": "TestA"}],
            schema_version=2,
            capabilities=["kubernetes", "vm"],
            suites=["storage", "iam"],
            catalog_digest=DIGEST,
            isv_test_build_ref=RELEASE_REF,
        )

        request = mock_urlopen.call_args[0][0]
        payload = json.loads(request.data.decode())
        assert payload["schemaVersion"] == 2
        assert payload["capabilities"] == ["kubernetes", "vm"]
        assert payload["suites"] == ["storage", "iam"]

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
            schema_version=2,
            capabilities=["kubernetes"],
            suites=["storage"],
            catalog_digest=DIGEST,
            isv_test_build_ref="v1.0.0-0-gdeadbee",
        )

        request = mock_urlopen.call_args[0][0]
        payload = json.loads(request.data.decode())
        entry = payload["entries"][0]

        assert entry["labels"] == ["gpu"]
        assert "markers" not in entry
