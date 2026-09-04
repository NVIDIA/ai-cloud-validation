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

"""Test run reporting for ISV Lab Service.

This module provides functions for creating and updating test runs
in the ISV Lab Service.
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from isvreporter.version import build_ref, parse_build_ref

from isvctl.redaction import redact_text

logger = logging.getLogger(__name__)


def check_upload_credentials() -> tuple[bool, str | None, str | None]:
    """Check if ISV Lab Service credentials are available.

    Returns:
        Tuple of (can_upload, client_id, client_secret)
    """
    client_id = os.environ.get("ISV_CLIENT_ID")
    client_secret = os.environ.get("ISV_CLIENT_SECRET")

    if not client_id or not client_secret:
        return False, None, None

    return True, client_id, client_secret


def get_environment_config() -> tuple[str, str]:
    """Get ISV Lab Service endpoint and SSA issuer from environment.

    Returns:
        Tuple of (endpoint, ssa_issuer)
    """
    from isvreporter.config import get_endpoint, get_ssa_issuer

    endpoint = get_endpoint()
    ssa_issuer = get_ssa_issuer()
    return endpoint, ssa_issuer


def get_isv_test_version() -> str | None:
    """Get the isvctl package version for isv_test_version field.

    Returns:
        Package version string or None if not available
    """
    try:
        from isvctl import __version__

        return __version__
    except Exception:
        return None


def warn_if_unreleased(isv_test_version: str | None) -> None:
    """Warn at run creation when this checkout is demonstrably not a release.

    Said here rather than only in the report because this is the moment the
    operator is watching. A build taken between releases is supported, but it
    is not the exact source release named by the static package version.

    Only speaks when the checkout proves the point. There is no warning when
    there is nothing to go on, which is the ordinary case for a partner running
    from a copied tree or an air-gapped cluster; the service reports that source
    fact as unverified without guessing.

    Reads the reference itself rather than asking ``build_is_release`` and then
    re-reading it, so the parts named in the message are the same parts the
    decision was made from.
    """
    parsed = parse_build_ref(build_ref())
    if isv_test_version is None or parsed is None:
        return
    tag, distance, commit, dirty = parsed
    if distance == 0 and not dirty and tag == isv_test_version:
        return

    if tag != isv_test_version:
        logger.warning(
            "Reporting as %s, but this checkout is at v%s. The installed packages are stale "
            "with respect to the tree. Re-sync the workspace (uv sync) to report "
            "the intended version; results are still accepted.",
            isv_test_version,
            tag,
        )
        return

    logger.warning(
        "Reporting as %s, but this checkout is %d commit(s) past v%s (at %s%s). "
        "Results are accepted, and the service will label their source as custom. "
        "Check out a release tag for a supported ISV production run.",
        isv_test_version,
        distance,
        tag,
        commit,
        ", with uncommitted changes" if dirty else "",
    )


def _catalog_digest_of(catalog_document: dict[str, Any] | None) -> str | None:
    """The digest of the catalog this run built, or None when it has none.

    Read from the document rather than recomputed, so the value sent to the
    service is by construction the one written to _output/test_catalog.json and
    printed during the run. An operator comparing the two is then comparing the
    same number, not two numbers that ought to agree.

    Never fatal. A run that has produced results must be able to report them
    even when its own provenance could not be established; the service reads a
    missing digest as "unknown" and says nothing rather than guessing.
    """
    if not catalog_document:
        return None
    return catalog_document.get("catalogDigest")


def create_test_run(
    lab_id: int,
    platform: str,
    tags: list[str],
    start_time: str,
    executed_by: str = "isvctl",
    ci_reference: str = "local-run",
    isv_software_version: str | None = None,
    suite: str | None = None,
    capability: str | None = None,
) -> str | None:
    """Create a test run in ISV Lab Service.

    Args:
        lab_id: ISV Lab ID
        platform: Target platform (e.g., "kubernetes", "slurm")
        tags: List of tags for the test run
        start_time: ISO 8601 formatted start time
        executed_by: Tool that executed the test
        ci_reference: CI/CD reference identifier
        isv_software_version: ISV software stack version (opaque string from ISV)
        suite: Suite that was run (e.g. "network", "vm")
        capability: Capability context resolved from ``--capability``; None for
            a core-only run

    Returns:
        Test run ID if successful, None otherwise
    """
    try:
        from isvreporter.auth import get_jwt_token
        from isvreporter.client import create_test_run as client_create_test_run
    except ImportError:
        logger.warning("isvreporter not available, skipping result upload")
        return None

    can_upload, client_id, client_secret = check_upload_credentials()
    if not can_upload or not client_id or not client_secret:
        logger.info("ISV_CLIENT_ID / ISV_CLIENT_SECRET not set; skipping test-run creation")
        return None

    endpoint, ssa_issuer = get_environment_config()

    isv_test_version = get_isv_test_version()
    warn_if_unreleased(isv_test_version)

    try:
        jwt_token = get_jwt_token(ssa_issuer, client_id, client_secret)
        result = client_create_test_run(
            endpoint=endpoint,
            lab_id=lab_id,
            jwt_token=jwt_token,
            test_target_type=platform.upper(),
            tags=tags,
            executed_by=executed_by,
            ci_reference=ci_reference,
            start_time=start_time,
            isv_software_version=isv_software_version,
            isv_test_version=isv_test_version,
            suite=suite,
            capability=capability,
        )
        return result.get("data", {}).get("testRunId")
    except SystemExit:
        # create_test_run calls sys.exit on failure, catch it
        logger.warning("Failed to create test run in ISV Lab Service")
        return None
    except Exception as e:
        logger.warning(f"Failed to create test run: {e}")
        return None


def update_test_run(
    lab_id: int,
    test_run_id: str,
    success: bool,
    start_time: str,
    log_file: Path | None = None,
    junit_xml: Path | None = None,
    log_content: str | None = None,
    isv_software_version: str | None = None,
    catalog_document: dict[str, Any] | None = None,
) -> bool:
    """Update a test run in ISV Lab Service.

    Args:
        lab_id: ISV Lab ID
        test_run_id: Test run ID to update
        success: Whether tests succeeded
        start_time: ISO 8601 formatted start time (for duration calculation)
        log_file: Path to log file (optional)
        junit_xml: Path to JUnit XML file (optional)
        log_content: Direct log content string (optional, alternative to log_file)
        isv_software_version: ISV software stack version (opaque string from ISV)
        catalog_document: Catalog identity artifact produced by the executing build (optional)

    Returns:
        True if successful, False otherwise
    """
    try:
        from isvreporter.auth import get_jwt_token
        from isvreporter.client import report_test_results
        from isvreporter.client import update_test_run as client_update_test_run
    except ImportError:
        return False

    can_upload, client_id, client_secret = check_upload_credentials()
    if not can_upload or not client_id or not client_secret:
        logger.info("ISV_CLIENT_ID / ISV_CLIENT_SECRET not set; skipping test-run update")
        return False

    endpoint, ssa_issuer = get_environment_config()

    # Calculate duration
    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        # Treat naive timestamps as UTC to avoid offset-naive/aware subtraction issues
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        duration_seconds = int((datetime.now(UTC) - start_dt).total_seconds())
    except Exception as e:
        logger.warning("Failed to compute test run duration from start_time=%r: %s", start_time, e)
        duration_seconds = None

    # Read log file if provided
    log_output = log_content
    if log_file and log_file.exists() and not log_output:
        try:
            log_output = log_file.read_text()
        except Exception as e:
            logger.warning(f"Failed to read log file: {e}")

    # Sanitize and redact before uploading to external service
    if log_output:
        log_output = log_output.replace("\x00", "")
        log_output = redact_text(log_output)

    # Auto-detect ISV test version from package
    # An artifact may have crossed machines in a remote or split flow. Prefer
    # the version recorded by the code that executed over this reporter's local
    # package metadata.
    isv_test_version = (catalog_document or {}).get("isvTestVersion") or get_isv_test_version()

    try:
        jwt_token = get_jwt_token(ssa_issuer, client_id, client_secret)

        # Upload JUnit XML test results first (if provided)
        if junit_xml and junit_xml.exists():
            try:
                logger.info("Uploading JUnit XML: %s", junit_xml)
                junit_content = redact_text(junit_xml.read_text())
                report_test_results(
                    endpoint=endpoint,
                    lab_id=lab_id,
                    test_run_id=test_run_id,
                    jwt_token=jwt_token,
                    junit_xml=junit_content,
                )
            except SystemExit:
                # report_test_results uses sys.exit() on failure; log and continue
                logger.warning("Failed to upload JUnit XML to ISV Lab Service")
            except Exception as e:
                logger.warning("Failed to upload JUnit XML: %s", e)

        # Update test run with status and log, even if JUnit upload failed.
        #
        # The digest and source reference come from the identity artifact the
        # executing build produced. They remain independent facts even when the
        # artifact crosses machines for reporting.
        client_update_test_run(
            endpoint=endpoint,
            lab_id=lab_id,
            test_run_id=test_run_id,
            jwt_token=jwt_token,
            status="SUCCESS" if success else "FAILED",
            duration_seconds=duration_seconds,
            log_output=log_output,
            isv_software_version=isv_software_version,
            isv_test_version=isv_test_version,
            isv_test_catalog_digest=_catalog_digest_of(catalog_document),
            # The execution artifact is authoritative, including an explicit
            # null. Falling back here could attribute a split or remote run to
            # the later reporting machine's checkout.
            isv_test_build_ref=(catalog_document or {}).get("isvTestBuildRef"),
        )
        return True
    except SystemExit:
        logger.warning("Failed to update test run in ISV Lab Service")
        return False
    except Exception as e:
        logger.warning(f"Failed to update test run: {e}")
        return False
