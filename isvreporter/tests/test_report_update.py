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

"""Tests for `isvctl report update`, the split-flow reporting path.

This is the command the troubleshooting guide recommends as the trap or
after_script that guarantees a run gets closed, so it is what CI uses. It closed
runs without any build provenance while reading, from the same file, four other
fields beside the one that carries it -- leaving exactly the population most
likely to be running from a drifted tree permanently unknown.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from isvreporter.main import app

runner = CliRunner()

DIGEST = "sha256:48df75a8524fb5e99b03200a317c424eeb8d7881a6dfa2ee83bd704498000a47"


@pytest.fixture
def _service() -> MagicMock:
    """Stand in for the whole service, yielding the update call to assert on."""
    with (
        patch(
            "isvreporter.main._get_credentials",
            return_value=("https://api.example.com", "issuer", "id", "secret"),
        ),
        patch("isvreporter.main.get_jwt_token", return_value="jwt-token"),
        patch("isvreporter.main.update_test_run") as update,
    ):
        yield update


def _catalog(tmp_path: Path, **overrides: object) -> Path:
    document = {
        "schemaVersion": 2,
        "isvTestVersion": "0.11.0",
        "catalogDigest": DIGEST,
        "isvTestBuildRef": "v0.11.0-3-gabc1234",
        "capabilities": ["KUBERNETES"],
        "suites": ["network"],
        "entries": [{"name": "GpuCheck"}],
        **overrides,
    }
    path = tmp_path / "test_catalog.json"
    path.write_text(json.dumps(document))
    return path


def _update(*args: str) -> None:
    result = runner.invoke(
        app,
        ["update", "--lab-id", "1", "--test-run-id", "58", "--status", "SUCCESS", *args],
    )
    assert result.exit_code == 0, result.output


def test_the_digest_travels_with_the_run(_service: MagicMock, tmp_path: Path) -> None:
    _update("--test-catalog", str(_catalog(tmp_path)))

    assert _service.call_args.kwargs["isv_test_catalog_digest"] == DIGEST


def test_the_digest_is_read_from_the_file_not_from_this_process(_service: MagicMock, tmp_path: Path) -> None:
    """The reporting step may run on a different machine than the tests.

    A CI job closing out a run that executed on a cluster must report the build
    that produced the results, not whatever the runner happens to hold.
    """
    other = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

    _update("--test-catalog", str(_catalog(tmp_path, catalogDigest=other)))

    assert _service.call_args.kwargs["isv_test_catalog_digest"] == other


def test_split_reporting_does_not_publish_a_catalog(_service: MagicMock, tmp_path: Path) -> None:
    """Catalog publication is reserved for the release workflow."""
    _update("--test-catalog", str(_catalog(tmp_path)))
    assert _service.call_args.kwargs["isv_test_catalog_digest"] == DIGEST


def test_the_original_build_reference_travels_with_the_run(_service: MagicMock, tmp_path: Path) -> None:
    """The later reporting machine must not replace the executing source identity."""
    _update("--test-catalog", str(_catalog(tmp_path)))
    assert _service.call_args.kwargs["isv_test_build_ref"] == "v0.11.0-3-gabc1234"


def test_the_original_version_travels_with_the_run(_service: MagicMock, tmp_path: Path) -> None:
    """The artifact version wins when the reporting machine supplies none."""
    _update("--test-catalog", str(_catalog(tmp_path)))
    assert _service.call_args.kwargs["isv_test_version"] == "0.11.0"


def test_an_older_catalog_file_carries_no_digest(_service: MagicMock, tmp_path: Path) -> None:
    """Written before the field existed. Unknown, and not reconstructed."""
    document = json.loads(_catalog(tmp_path).read_text())
    del document["catalogDigest"]
    path = tmp_path / "old_catalog.json"
    path.write_text(json.dumps(document))

    _update("--test-catalog", str(path))

    assert _service.call_args.kwargs["isv_test_catalog_digest"] is None


def test_a_failed_results_upload_still_closes_the_run(_service: MagicMock, tmp_path: Path) -> None:
    """The bug this command exists to prevent, in the command itself.

    report_test_results exits the process on an HTTP failure. SystemExit is not
    an Exception, so it travelled past the handler and ended the command before
    update_test_run ran -- leaving STARTED exactly the run the troubleshooting
    guide recommends this command to close.
    """
    junit = tmp_path / "junit.xml"
    junit.write_text("<testsuite/>")

    with patch("isvreporter.main.report_test_results", side_effect=SystemExit(1)):
        result = runner.invoke(
            app,
            [
                "update",
                "--lab-id",
                "1",
                "--test-run-id",
                "58",
                "--status",
                "SUCCESS",
                "--junit-xml",
                str(junit),
            ],
        )

    _service.assert_called_once()
    assert _service.call_args.kwargs["status"] == "SUCCESS"
    # The failure is still reported, only after the run has been closed.
    assert result.exit_code == 1


def test_a_missing_junit_file_remains_a_warning(_service: MagicMock, tmp_path: Path) -> None:
    """Unchanged: the paths that have always warned still exit zero."""
    result = runner.invoke(
        app,
        [
            "update",
            "--lab-id",
            "1",
            "--test-run-id",
            "58",
            "--status",
            "SUCCESS",
            "--junit-xml",
            str(tmp_path / "absent.xml"),
        ],
    )

    _service.assert_called_once()
    assert result.exit_code == 0, result.output


def test_no_catalog_means_no_provenance(_service: MagicMock) -> None:
    """Nothing to read is unknown, which is the honest answer."""
    _update()

    assert _service.call_args.kwargs["isv_test_catalog_digest"] is None
