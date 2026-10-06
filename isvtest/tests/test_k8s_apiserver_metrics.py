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
from unittest.mock import MagicMock, patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_metrics import K8sApiServerMetricsCheck

COUNTER = 'apiserver_request_total{code="200",resource="pods",scope="resource",verb="GET"} 12'
BUCKET = 'apiserver_request_duration_seconds_bucket{resource="pods",scope="resource",verb="GET",le="1"} 12'
HEADERS = "# HELP apiserver_request_total Requests\n# TYPE apiserver_request_total counter\n"


def check_samples(*samples: str, code: int = 0) -> dict:
    """Run the real validator against a captured metrics response without a cluster."""
    runner = MagicMock()
    runner.run.return_value = CommandResult(
        exit_code=code, stdout=HEADERS + "\n".join(samples) + "\n", stderr="probe error", duration=0
    )
    return K8sApiServerMetricsCheck(runner=runner).execute()


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


def test_command_not_found_skips_but_api_error_fails() -> None:
    """Remote missing executables skip; attempted API requests that fail remain failures."""
    with pytest.raises(pytest.skip.Exception):
        check_samples(code=127)
    assert check_samples(code=1)["passed"] is False
