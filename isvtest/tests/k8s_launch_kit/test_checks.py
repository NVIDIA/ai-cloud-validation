# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Kubernetes Launch Kit connectivity interpretation."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.k8s_launch_kit.checks import LaunchKitConnectivityCheck

pytestmark = pytest.mark.unit


def _matrix_row(
    family: str | None,
    kind: int,
    *,
    passed: bool = True,
    destination_rail: str = "rail-0",
) -> dict[str, Any]:
    """Build one Launch Kit connectivity result."""
    bandwidth = family in {"ib_write_bw", "gpudirect_dmabuf"}
    return {
        "Test": {
            "Kind": kind,
            "SrcNode": "worker-a",
            "DstNode": "worker-b",
            "SrcRail": "rail-0",
            "DstRail": destination_rail,
            "Expectation": "required",
            **(
                {
                    "SrcGPUIndex": 2,
                    "DstGPUIndex": 5,
                    "SrcGPUPCIAddress": "0000:41:00.0",
                    "DstGPUPCIAddress": "0000:71:00.0",
                }
                if family == "gpudirect_dmabuf"
                else {}
            ),
        },
        **({"Family": family} if family is not None else {}),
        "OK": passed,
        "ObservedOK": passed,
        "Expectation": "required",
        **(
            {
                "BandwidthGbps": 187.6 if passed else 42.5,
                "MinBandwidthGbps": 100.0,
            }
            if bandwidth
            else {}
        ),
        **(
            {
                "Stderr": f"{family}: connection refused on rail-0",
                "Error": f"{family} validation failed",
            }
            if not passed
            else {}
        ),
    }


def _validate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap matrix rows in the provider contract consumed by the check."""
    failed = sum(row.get("OK") is not True for row in rows)
    return {
        "success": failed == 0,
        "platform": "kubernetes",
        "operation": "validate",
        "exit_code": 0 if failed == 0 else 4,
        "documents": [
            {"versionCheck": {}, "manifests": [], "summary": {}},
            {
                "connectivity": {
                    "PingResults": rows,
                    "Summary": {"TotalTests": len(rows), "Failed": failed},
                }
            },
        ],
        **({"error": "one or more connectivity rows failed"} if failed else {}),
    }


def _execute(output: dict[str, Any]) -> dict[str, Any]:
    """Execute the catalog check against one provider envelope."""
    return LaunchKitConnectivityCheck(config={"step_output": output}).execute()


def test_reports_every_emitted_connectivity_family() -> None:
    """The single check exposes every Launch Kit matrix row in stream order."""
    rows = [
        _matrix_row("icmp", 0),
        _matrix_row("rping", 2),
        _matrix_row("ib_write_bw", 4),
        _matrix_row("gpudirect_dmabuf", 6),
    ]

    result = _execute(_validate(rows))

    assert result["passed"] is True
    assert [subtest["name"] for subtest in result["subtests"]] == [
        f"{family}/worker-a->worker-b/rail-0->rail-0" for family in ("icmp", "rping", "ib_write_bw", "gpudirect_dmabuf")
    ]


def test_failure_preserves_endpoints_rails_bandwidth_and_stderr() -> None:
    """A failed row retains the diagnostics emitted by Launch Kit."""
    result = _execute(_validate([_matrix_row("ib_write_bw", 4, passed=False)]))

    assert result["passed"] is False
    assert result["subtests"][0]["passed"] is False
    assert "ib_write_bw/worker-a->worker-b/rail-0->rail-0" in result["error"]
    assert "bandwidthGbps=42.5" in result["error"]
    assert "minimumGbps=100.0" in result["error"]
    assert "connection refused on rail-0" in result["error"]


def test_gpudirect_details_are_reported_without_a_separate_expected_check() -> None:
    """GPUDirect rows are ordinary connectivity rows when Launch Kit emits them."""
    result = _execute(_validate([_matrix_row("gpudirect_dmabuf", 6)]))

    assert result["passed"] is True
    message = result["subtests"][0]["message"]
    assert "gpuIndices=2->5" in message
    assert "sourceGpuPci=0000:41:00.0" in message
    assert "destinationGpuPci=0000:71:00.0" in message


def test_disabled_gpudirect_requires_no_skip_or_placeholder() -> None:
    """Families disabled by user config are absent instead of becoming skipped tests."""
    result = _execute(_validate([_matrix_row("icmp", 0), _matrix_row("rping", 2)]))

    assert result["passed"] is True
    assert all("gpudirect" not in subtest["name"] for subtest in result["subtests"])
    assert all(subtest["skipped"] is False for subtest in result["subtests"])


def test_explicit_future_family_is_not_filtered_out() -> None:
    """The wrapper forwards new Launch Kit families without a catalog update."""
    result = _execute(_validate([_matrix_row("future_connectivity", 999)]))

    assert result["passed"] is True
    assert result["subtests"][0]["name"].startswith("future_connectivity/")


def test_legacy_numeric_kind_resolves_a_family() -> None:
    """Older Launch Kit output without Family remains readable."""
    result = _execute(_validate([_matrix_row(None, 3)]))

    assert result["passed"] is True
    assert result["subtests"][0]["name"].startswith("rping/")


@pytest.mark.parametrize(
    "output",
    [
        {"operation": "validate", "documents": [], "error": "Kubernetes client failed"},
        {"operation": "discover", "documents": [{"connectivity": {"PingResults": []}}]},
    ],
)
def test_rejects_missing_or_wrong_validate_output(output: dict[str, Any]) -> None:
    """Transport failures remain actionable instead of passing vacuously."""
    result = _execute(output)

    assert result["passed"] is False


def test_empty_connectivity_matrix_fails() -> None:
    """A validate response with no connectivity rows is not evidence of success."""
    result = _execute(_validate([]))

    assert result["passed"] is False
    assert result["error"] == "Launch Kit connectivity matrix produced no results"
