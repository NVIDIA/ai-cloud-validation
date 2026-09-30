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

"""API client for ISV Lab Service test run management."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from isvreporter.version import build_is_release

# Constants
OUTPUT_DIR = Path("_output")
TEST_RUN_ID_FILE = OUTPUT_DIR / "testrun_id.txt"


def create_test_run(
    endpoint: str,
    lab_id: int,
    jwt_token: str,
    test_target_type: str,
    tags: list[str],
    executed_by: str,
    ci_reference: str,
    start_time: str,
    isv_software_version: str | None = None,
    isv_test_version: str | None = None,
    suite: str | None = None,
    capability: str | None = None,
) -> dict[str, Any]:
    """
    Create a new test run record.

    Args:
        endpoint: ISV Lab Service endpoint URL
        lab_id: Lab ID
        jwt_token: JWT access token
        test_target_type: Type of test target (e.g., BARE_METAL, VM, CONTAINER)
        tags: List of tags for the test run
        executed_by: Who/what executed the test run
        ci_reference: CI job URL or reference
        start_time: Test run start time (ISO 8601 format)
        isv_software_version: ISV software stack version (opaque string from ISV)
        isv_test_version: ISV test tool version (e.g., "1.12.3")
        suite: Suite that was run (e.g. "network", "vm")
        capability: Capability context the suite ran under (e.g. "vm"); None
            means the run was core-only, which is a different signal than the
            same suite run under a capability

    Returns:
        API response dictionary containing test run ID

    Raises:
        SystemExit: If test run creation fails
    """
    url = f"{endpoint}/v1/labs/{lab_id}/test-runs"

    payload: dict[str, Any] = {
        "executedBy": executed_by,
        "ciReference": ci_reference,
        "tags": tags,
        "testTargetType": test_target_type,
        "testRunStartAt": start_time,
    }

    if isv_software_version:
        payload["isvSoftwareVersion"] = isv_software_version
    if isv_test_version:
        payload["isvTestVersion"] = isv_test_version
    if suite:
        payload["suite"] = suite
    # Sent only when set: a core-only run carries no capability, and null is
    # the signal for that rather than a sentinel value.
    if capability:
        payload["capability"] = capability

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    }

    try:
        request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode())
            test_run_id = result["data"]["testRunId"]
            print("Test run created successfully")
            print(f"  Test Run ID: {test_run_id}")
            if suite:
                print(f"  Suite: {suite} ({capability or 'core only'})")
            print(f"  URL: {endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}")

            # Save test run ID to file for later use in after_script
            try:
                OUTPUT_DIR.mkdir(exist_ok=True)
                # Remove existing file first (may be owned by different user, e.g., root)
                if TEST_RUN_ID_FILE.exists():
                    TEST_RUN_ID_FILE.unlink()
                TEST_RUN_ID_FILE.write_text(str(test_run_id))
                print(f"  Saved test run ID to: {TEST_RUN_ID_FILE}")
            except OSError as e:
                # Non-fatal: file saving is optional convenience
                # Catches PermissionError, IsADirectoryError, FileExistsError, etc.
                print(f"  Warning: Could not save test run ID to {TEST_RUN_ID_FILE}: {e}")

            return result
    except HTTPError as e:
        print(f"ERROR: Failed to create test run (HTTP {e.code})", file=sys.stderr)
        print(f"Response: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print("ERROR: Failed to create test run - unable to connect to isvreporter service", file=sys.stderr)
        print(f"       Endpoint: {endpoint}", file=sys.stderr)
        print(f"       Reason: {e.reason}", file=sys.stderr)
        print("       This may be expected in network-restricted environments", file=sys.stderr)
        sys.exit(1)


def update_test_run(
    endpoint: str,
    lab_id: int,
    test_run_id: str,
    jwt_token: str,
    status: str,
    duration_seconds: int | None = None,
    complete_time: str | None = None,
    log_output: str | None = None,
    isv_software_version: str | None = None,
    isv_test_version: str | None = None,
    isv_test_catalog_digest: str | None = None,
    isv_test_build_ref: str | None = None,
) -> dict[str, Any]:
    """
    Update an existing test run record with completion status.

    Args:
        endpoint: ISV Lab Service endpoint URL
        lab_id: Lab ID
        test_run_id: Test run ID to update
        jwt_token: JWT access token
        status: Test run status (SUCCESS, FAILED, etc.)
        duration_seconds: Test duration in seconds
        complete_time: Test completion time (ISO 8601 format, defaults to now)
        log_output: Full test execution log output (optional)
        isv_software_version: ISV software stack version (opaque string from ISV)
        isv_test_version: ISV test tool version (e.g., "1.12.3")
        isv_test_catalog_digest: Digest of the checks this build contains, from
            ``isvtest.catalog.catalog_digest``. The service compares it against
            the catalog published for isv_test_version to classify catalog
            provenance independently. Omitted when it cannot be computed.
        isv_test_build_ref: ``git describe`` output for the checkout, when there
            is one (e.g. ``"v0.9.0-9-g08339c7"``). It independently classifies
            source provenance and is absent for the many partners running from
            a copied tree or an air-gapped cluster.

    Returns:
        API response dictionary

    Raises:
        SystemExit: If test run update fails
    """
    url = f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}"

    if complete_time is None:
        complete_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    payload: dict[str, Any] = {
        "testRunStatus": status,
        "testRunCompleteAt": complete_time,
    }

    if duration_seconds is not None:
        payload["testDurationInSeconds"] = duration_seconds

    if log_output is not None:
        payload["logOutput"] = log_output

    if isv_software_version is not None:
        payload["isvSoftwareVersion"] = isv_software_version

    if isv_test_version is not None:
        payload["isvTestVersion"] = isv_test_version

    if isv_test_catalog_digest is not None:
        payload["isvTestCatalogDigest"] = isv_test_catalog_digest

    if isv_test_build_ref is not None:
        payload["isvTestBuildRef"] = isv_test_build_ref

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    }

    try:
        request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="PUT")
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode())
            print("Test run updated successfully")
            print(f"  Test Run ID: {test_run_id}")
            print(f"  Status: {status}")
            if duration_seconds:
                print(f"  Duration: {duration_seconds}s")
            if log_output:
                log_size = len(log_output)
                print(f"  Log output: {log_size} characters")
            return result
    except HTTPError as e:
        print(f"ERROR: Failed to update test run (HTTP {e.code})", file=sys.stderr)
        print(f"Response: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print("ERROR: Failed to update test run - unable to connect to isvreporter service", file=sys.stderr)
        print(f"       Endpoint: {endpoint}", file=sys.stderr)
        print(f"       Reason: {e.reason}", file=sys.stderr)
        print("       This may be expected in network-restricted environments", file=sys.stderr)
        sys.exit(1)


def load_test_run_id() -> str | None:
    """
    Load test run ID from file saved during creation.

    Returns:
        Test run ID if file exists, None otherwise
    """
    try:
        test_run_id = TEST_RUN_ID_FILE.read_text().strip()
        print(f"Loaded Test Run ID: {test_run_id}")
        return test_run_id
    except FileNotFoundError:
        print(
            f"Warning: {TEST_RUN_ID_FILE} not found. Was the test run created?",
            file=sys.stderr,
        )
        return None


def report_test_results(
    endpoint: str,
    lab_id: int,
    test_run_id: str,
    jwt_token: str,
    junit_xml: str,
) -> dict[str, Any]:
    """
    Upload JUnit XML test results for a test run.

    Args:
        endpoint: ISV Lab Service endpoint URL
        lab_id: Lab ID
        test_run_id: Test run ID to report results for
        jwt_token: JWT access token
        junit_xml: Raw JUnit XML content as string

    Returns:
        API response dictionary

    Raises:
        SystemExit: If reporting fails
    """
    url = f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}/test-results"

    payload = {
        "junitXml": junit_xml,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    }

    try:
        request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode())
            xml_size = len(junit_xml)
            print("Test results uploaded successfully")
            print(f"  Test Run ID: {test_run_id}")
            print(f"  JUnit XML size: {xml_size} bytes")
            return result
    except HTTPError as e:
        print(f"ERROR: Failed to upload test results (HTTP {e.code})", file=sys.stderr)
        error_body = e.read().decode()
        print(f"Response: {error_body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print("ERROR: Failed to upload test results - unable to connect to isvreporter service", file=sys.stderr)
        print(f"       Endpoint: {endpoint}", file=sys.stderr)
        print(f"       Reason: {e.reason}", file=sys.stderr)
        print("       This may be expected in network-restricted environments", file=sys.stderr)
        sys.exit(1)


def upload_test_catalog(
    endpoint: str,
    jwt_token: str,
    isv_test_version: str,
    entries: list[dict[str, Any]],
    *,
    schema_version: int,
    capabilities: list[str],
    suites: list[str],
    catalog_digest: str,
    isv_test_build_ref: str,
) -> bool:
    """Publish a release catalog, idempotent for an identical identity.

    Sends the full list of available validation tests for a given isvtest
    version. A 409 means a different identity already occupies the version and
    is returned as a publication failure.

    Args:
        endpoint: ISV Lab Service endpoint URL
        jwt_token: JWT access token
        isv_test_version: Test suite version string (e.g. "1.2.3")
        entries: List of catalog entry dicts with keys:
            name, description, labels, source, suite, capability, requires, test_ids
        schema_version: Catalog document schema version.
        capabilities: Declarable capability vocabulary (platform suites).
        suites: Plain suite names declared by the catalog.
        catalog_digest: Digest carried by the generated catalog artifact.
        isv_test_build_ref: Build reference carried by that same artifact.

    Returns:
        True if the identical catalog was accepted, False on rejection or error
    """
    # A catalog is a release's published contract: the thing every lab's coverage
    # is scored against, and what "latest catalog" resolves to for good. Publish
    # one only from a build positively known to be that release.
    #
    # Deliberately requires True and not merely "not False". A build that cannot
    # tell - no checkout to describe, which is the ordinary case for a partner
    # running from a copied tree - is not thereby a release, and letting the
    # unknown case through is precisely how a working tree's catalog would come
    # to be published under a release's number. Releases are published by the
    # release pipeline, which does have a checkout, so nothing legitimate is lost.
    if build_is_release(isv_test_version, isv_test_build_ref) is not True:
        print(
            f"Test catalog not uploaded: this build is not verifiably the {isv_test_version} "
            "release, and a catalog is only published for a release. Results are still "
            "uploaded, and the coverage report says which catalog it scored them against."
        )
        return False

    url = f"{endpoint}/v1/test-catalog"

    payload = {
        "schemaVersion": schema_version,
        "isvTestVersion": isv_test_version,
        "catalogDigest": catalog_digest,
        "isvTestBuildRef": isv_test_build_ref,
        "capabilities": capabilities,
        "suites": suites,
        "entries": [
            {
                "name": e["name"],
                "description": e.get("description", ""),
                "labels": e.get("labels", []),
                "suite": e.get("suite", ""),
                "capability": e.get("capability"),
                "requires": e.get("requires", []),
                "test_ids": e.get("test_ids", []),
            }
            for e in entries
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    }

    try:
        request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urlopen(request, timeout=30) as response:
            json.loads(response.read().decode())
            print(f"Test catalog uploaded successfully (version: {isv_test_version}, {len(entries)} entries)")
            return True
    except HTTPError as e:
        if e.code == 409:
            print(
                f"ERROR: A different catalog already exists for version {isv_test_version}",
                file=sys.stderr,
            )
            return False
        print(f"ERROR: Failed to upload test catalog (HTTP {e.code})", file=sys.stderr)
        print(f"Response: {e.read().decode()}", file=sys.stderr)
        return False
    except URLError as e:
        print("ERROR: Failed to upload test catalog - unable to connect to service", file=sys.stderr)
        print(f"       Endpoint: {endpoint}", file=sys.stderr)
        print(f"       Reason: {e.reason}", file=sys.stderr)
        return False


def calculate_duration(start_time_str: str) -> int:
    """
    Calculate duration in seconds from start time to now.

    Args:
        start_time_str: ISO 8601 formatted start time string

    Returns:
        Duration in seconds
    """
    # Parse ISO 8601 format (GitLab CI format: 2024-01-01T12:00:00Z)
    start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
    end_time = datetime.now(UTC)
    duration = int((end_time - start_time).total_seconds())
    return duration
