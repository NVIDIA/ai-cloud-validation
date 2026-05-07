# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for subtest -> JUnit XML injection."""

import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from isvtest.testing.subtests import SubTestReport, _inject_subtests_into_junit


def _parent_junit(tmp_path: Path, parent_name: str) -> Path:
    """Write a minimal pytest-style JUnit with a single parent testcase."""
    suite = ET.Element("testsuite", attrib={"name": "phase", "tests": "1", "skipped": "0", "failures": "0"})
    ET.SubElement(suite, "testcase", attrib={"name": parent_name, "classname": "", "time": "0.000"})
    path = tmp_path / "junit.xml"
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _stub_report(
    parent_nodeid: str,
    subtest_msg: str,
    *,
    skipped: bool = False,
    failed: bool = False,
    longrepr: object | None = None,
    duration: float = 0.0,
) -> SimpleNamespace:
    """Build the minimum surface that ``_inject_subtests_into_junit`` reads.

    The real ``SubTestReport`` is a pytest TestReport subclass that's awkward
    to construct in isolation; the injector only touches a handful of fields.
    """
    return SimpleNamespace(
        nodeid=parent_nodeid,
        duration=duration,
        failed=failed,
        skipped=skipped,
        longrepr=longrepr,
        context=SimpleNamespace(msg=subtest_msg),
    )


def test_skipped_subtest_uses_real_message_from_longrepr(tmp_path: Path) -> None:
    """The injector pulls the human reason out of longrepr, not a canned line."""
    junit = _parent_junit(tmp_path, "K8sCsiTenantScopedCredentialsCheck")
    real_message = "No CSIDriver objects present"
    report = _stub_report(
        "::K8sCsiTenantScopedCredentialsCheck",
        "serviceaccount-rbac-scoped",
        skipped=True,
        longrepr=("/some/file.py", 0, real_message),
    )

    _inject_subtests_into_junit(junit, cast(list[SubTestReport], [report]))

    cases = list(ET.parse(junit).iter("testcase"))
    subtest_case = next(c for c in cases if "::serviceaccount-rbac-scoped" in (c.get("name") or ""))
    skipped = subtest_case.find("skipped")
    assert skipped is not None
    assert skipped.get("message") == real_message


def test_skipped_subtest_falls_back_when_longrepr_is_missing(tmp_path: Path) -> None:
    """No longrepr -> fall back to the canned 'Subtest X skipped' string."""
    junit = _parent_junit(tmp_path, "ParentCheck")
    report = _stub_report(
        "::ParentCheck",
        "noisy-subtest",
        skipped=True,
        longrepr=None,
    )

    _inject_subtests_into_junit(junit, cast(list[SubTestReport], [report]))

    subtest_case = next(c for c in ET.parse(junit).iter("testcase") if "::noisy-subtest" in (c.get("name") or ""))
    skipped = subtest_case.find("skipped")
    assert skipped is not None
    assert skipped.get("message") == "Subtest noisy-subtest skipped"
