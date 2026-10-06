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

"""API-server metric samples must carry the dimensions used for SLO queries."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_metrics import K8sApiServerMetricsCheck

COUNTER = 'apiserver_request_total{code="200",resource="pods",scope="resource",verb="GET"} 12'
BUCKET = 'apiserver_request_duration_seconds_bucket{resource="pods",scope="resource",verb="GET",le="1"} 12'
HEADERS = (
    "# HELP apiserver_request_total Requests\n# TYPE apiserver_request_total counter\n"
    "# TYPE apiserver_request_duration_seconds histogram\n"
)


def check_samples(*samples: str, code: int = 0, expected_metrics: list[str] | None = None) -> dict:
    """Run the real validator against a captured metrics response without a cluster."""
    runner = MagicMock()
    runner.run.return_value = CommandResult(
        exit_code=code, stdout=HEADERS + "\n".join(samples) + "\n", stderr="probe error", duration=0
    )
    config = {} if expected_metrics is None else {"expected_metrics": expected_metrics}
    return K8sApiServerMetricsCheck(runner=runner, config=config).execute()


@pytest.mark.parametrize("timestamp", [str(-(1 << 63) - 1), str(1 << 63), "9" * 100])
def test_timestamp_outside_int64_fails(timestamp: str) -> None:
    """An integer token is not a valid timestamp unless it fits signed int64."""
    assert check_samples(COUNTER + " " + timestamp, BUCKET)["passed"] is False


@pytest.mark.parametrize("timestamp", [str(-(1 << 63)), str((1 << 63) - 1), "+123", "0"])
def test_valid_signed_timestamp_passes(timestamp: str) -> None:
    """Signed timestamps, including both int64 bounds, remain valid."""
    assert check_samples(COUNTER + " " + timestamp, BUCKET)["passed"] is True


@pytest.mark.parametrize("metric_type", ["counter", "gauge", "untyped", None])
def test_unrelated_suffix_does_not_satisfy_custom_metric(metric_type: str | None) -> None:
    """Suffix expansion requires a histogram or summary declaration for the base name."""
    headers = "# TYPE my_slo_count counter"
    if metric_type is not None:
        headers += f"\n# TYPE my_slo {metric_type}"
    result = check_samples(headers, "my_slo_count 1", expected_metrics=["my_slo"])
    assert result["passed"] is False
    assert "Missing expected metrics: my_slo" in result["error"]


@pytest.mark.parametrize(
    ("metric_type", "suffix"),
    [
        ("histogram", "_bucket"),
        ("histogram", "_count"),
        ("histogram", "_sum"),
        ("summary", "_count"),
        ("summary", "_sum"),
        ("summary", ""),
        ("counter", ""),
        ("gauge", ""),
    ],
)
def test_declared_metric_family_matches(metric_type: str, suffix: str) -> None:
    """Declared families match their standard samples while exact metric names still work."""
    result = check_samples(f"# TYPE my_slo {metric_type}", f"my_slo{suffix} 1", expected_metrics=["my_slo"])
    assert result["passed"] is True


def test_summary_does_not_match_bucket_suffix() -> None:
    """Only histograms can use bucket samples to satisfy an expected family."""
    result = check_samples("# TYPE my_slo summary", "my_slo_bucket 1", expected_metrics=["my_slo"])
    assert result["passed"] is False


@pytest.mark.parametrize("boundary", ["", "invalid", "NaN", "-Inf", "Inf", "1e999", " 1", "1 ", "1_000"])
def test_invalid_latency_bucket_boundary_fails(boundary: str) -> None:
    """A present le label must contain a finite number or the positive-infinity sentinel."""
    assert check_samples(COUNTER, BUCKET.replace('le="1"', f'le="{boundary}"'))["passed"] is False


@pytest.mark.parametrize("boundary", ["0", "0.5", ".5", "1.", "1e-3", "-1", "+Inf"])
def test_valid_latency_bucket_boundary_passes(boundary: str) -> None:
    """Finite numeric bounds and +Inf remain usable for latency distribution queries."""
    assert check_samples(COUNTER, BUCKET.replace('le="1"', f'le="{boundary}"'))["passed"] is True


@pytest.mark.parametrize("label", ["code", "resource", "scope", "verb"])
def test_counter_requires_each_slo_label(label: str) -> None:
    """Metric names alone do not prove that availability can be grouped by dimension."""
    counter = re.sub(rf'{label}="[^"]*",?', "", COUNTER).replace(",}", "}")
    result = check_samples(counter, BUCKET)
    assert result["passed"] is False
    assert label in result["error"]


@pytest.mark.parametrize("label", ["resource", "scope", "verb", "le"])
def test_histogram_requires_each_slo_label(label: str) -> None:
    """Latency buckets need both request dimensions and their bucket boundary."""
    bucket = re.sub(rf'{label}="[^"]*",?', "", BUCKET).replace(",}", "}")
    result = check_samples(COUNTER, bucket)
    assert result["passed"] is False
    assert label in result["error"]


def test_complete_labels_pass_even_with_empty_values() -> None:
    """Empty resource/scope labels are valid for non-resource API requests."""
    assert check_samples(COUNTER, BUCKET)["passed"] is True
    assert check_samples(COUNTER.replace('resource="pods"', 'resource=""'), BUCKET)["passed"] is True


def test_one_complete_sample_cannot_hide_an_incomplete_series() -> None:
    """Every observed series used by an SLO query must carry the required labels."""
    result = check_samples(COUNTER, COUNTER.replace('code="200",', ""), BUCKET)
    assert result["passed"] is False


def test_labels_cannot_be_combined_across_samples() -> None:
    """A union of incomplete label sets cannot stand in for one complete sample."""
    result = check_samples(
        'apiserver_request_total{code="200",resource="pods"} 1',
        'apiserver_request_total{scope="resource",verb="GET"} 1',
        BUCKET,
    )
    assert result["passed"] is False


def test_label_names_inside_values_are_not_evidence() -> None:
    """Quoted text containing fake assignments must not create label keys."""
    forged = r'apiserver_request_total{note="code=\"200\",resource=\"pods\",scope=\"resource\",verb=\"GET\""} 1'
    assert check_samples(forged, BUCKET)["passed"] is False


@pytest.mark.parametrize("suffix", ["_unrelated", "_sum"])
def test_counter_prefix_is_not_the_required_counter(suffix: str) -> None:
    """An unrelated prefixed metric cannot replace the request counter."""
    assert (
        check_samples(COUNTER.replace("apiserver_request_total", "apiserver_request_total" + suffix), BUCKET)["passed"]
        is False
    )


def test_histogram_count_and_sum_labels_are_checked() -> None:
    """Incomplete count/sum series must not be masked by valid bucket labels."""
    for suffix in ("count", "sum"):
        bad = f'apiserver_request_duration_seconds_{suffix}{{verb="GET"}} 1'
        assert check_samples(COUNTER, BUCKET, bad)["passed"] is False


def test_histogram_requires_buckets_not_just_counts() -> None:
    """A request count does not provide latency-distribution evidence."""
    count = BUCKET.replace("_bucket", "_count").replace(',le="1"', "")
    assert check_samples(COUNTER, count)["passed"] is False


def test_valid_escaped_labels_whitespace_and_timestamps() -> None:
    """Valid punctuation inside a label value does not change the label set."""
    counter = COUNTER.replace('code="200"', r'code = "200", note="comma, brace}, quote\" and slash\\"')
    assert check_samples(counter + " 123456789", BUCKET)["passed"] is True


def test_complete_histogram_family_passes() -> None:
    """Count and sum use request dimensions without a bucket-boundary label."""
    count = BUCKET.replace("_bucket", "_count").replace(',le="1"', "")
    total = count.replace("_count", "_sum")
    assert check_samples(COUNTER, BUCKET, count, total)["passed"] is True


@pytest.mark.parametrize(
    "sample",
    [
        'apiserver_request_total{code="200",resource="pods",scope="resource",verb="GET"} invalid',
        'apiserver_request_total{code="200",resource="pods",scope="resource",verb="GET",verb="POST"} 1',
        'apiserver_request_total{code="200",resource="pods",scope="resource",verb="GET",broken} 1',
    ],
)
def test_malformed_samples_fail(sample: str) -> None:
    """Invalid exposition cannot supply positive SLO evidence."""
    assert check_samples(sample, BUCKET)["passed"] is False


def test_missing_kubectl_skips() -> None:
    """A missing CLI prevents the probe from starting, so it is not a test failure."""
    with patch("isvtest.validations.k8s_metrics.get_kubectl_command", side_effect=FileNotFoundError("kubectl missing")):
        with pytest.raises(pytest.skip.Exception, match="kubectl"):
            K8sApiServerMetricsCheck().run()


@pytest.mark.parametrize("installed", [False, True])
def test_exit_127_skips_only_when_selected_executable_is_missing(tmp_path: Path, installed: bool) -> None:
    """Distinguish shell command-not-found from an installed wrapper's query failure."""
    executable = tmp_path / "kubectl wrapper"
    if installed:
        executable.write_text("#!/bin/sh\necho 'metrics query failed' >&2\nexit 127\n")
        executable.chmod(0o755)
    with patch("isvtest.validations.k8s_metrics.get_kubectl_command", return_value=[str(executable), "kubectl"]):
        check = K8sApiServerMetricsCheck()
        if installed:
            try:
                result = check.execute()
            except pytest.skip.Exception:
                pytest.fail("An installed executable returning 127 must fail, not skip")
            assert result["passed"] is False
            assert "metrics query failed" in result["error"]
        else:
            with pytest.raises(pytest.skip.Exception, match="not found"):
                check.execute()


def test_api_error_fails() -> None:
    """Attempted API requests that fail must remain failures."""
    assert check_samples(code=1)["passed"] is False
