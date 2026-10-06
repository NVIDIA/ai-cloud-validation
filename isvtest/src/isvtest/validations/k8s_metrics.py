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

import re
import shlex
from typing import ClassVar

import pytest

from isvtest.core.k8s import get_kubectl_command
from isvtest.core.validation import BaseValidation

DEFAULT_EXPECTED_METRICS = [
    "apiserver_request_total",
    "apiserver_request_duration_seconds",
]

# Kubernetes' stable request metrics expose these dimensions for SLO queries.
# https://kubernetes.io/docs/reference/instrumentation/metrics/
SLO_LABELS = {
    "apiserver_request_total": {"code", "resource", "scope", "verb"},
    "apiserver_request_duration_seconds_bucket": {"resource", "scope", "verb", "le"},
    "apiserver_request_duration_seconds_count": {"resource", "scope", "verb"},
    "apiserver_request_duration_seconds_sum": {"resource", "scope", "verb"},
    "apiserver_request_duration_seconds": {"resource", "scope", "verb"},
}
_SAMPLE = re.compile(
    r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{((?:[^"{}]|"(?:\\.|[^"\\])*")*)\})?'
    r"[ \t]+([^ \t]+)(?:[ \t]+(-?[0-9]+))?[ \t]*"
)
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"(?:[^"\\]|\\[\\n"])*"\s*(?:,|$)')


def _sample_labels(line: str) -> tuple[str, set[str]]:
    """Parse one text-format sample without mistaking quoted values for label keys."""
    match = _SAMPLE.fullmatch(line)
    if match is None:
        raise ValueError("invalid sample syntax")
    name, raw_labels, value, _timestamp = match.groups()
    float(value)  # Reject name-only or nonnumeric lines as evidence of a metric.
    labels: set[str] = set()
    rest = raw_labels or ""
    while rest.strip():
        label = _LABEL.match(rest)
        if label is None or label[1] in labels:
            raise ValueError("invalid or duplicate label")
        labels.add(label[1])
        rest = rest[label.end() :]
    return name, labels


def _metric_samples(metric: str, names: set[str]) -> set[str]:
    """Match exact names or standard histogram/summary samples, never arbitrary prefixes."""
    candidates = {metric}
    if metric != "apiserver_request_total":
        candidates.update(metric + suffix for suffix in ("_bucket", "_count", "_sum"))
    return candidates & names


class K8sApiServerMetricsCheck(BaseValidation):
    """Verify kube-apiserver exposes /metrics in Prometheus text exposition format.

    Queries the API server's ``/metrics`` endpoint via ``kubectl get --raw``
    and validates:

    - The response contains ``# HELP`` and ``# TYPE`` headers.
    - At least one metric sample is present.
    - All metric names configured via ``expected_metrics`` (or the defaults
      ``apiserver_request_total`` / ``apiserver_request_duration_seconds``)
      are exposed. Exact names and standard histogram/summary suffixes match.
    - Every observed request counter or latency sample selected by the check
      carries its required SLO labels. Empty label values are allowed, as on
      non-resource requests. Latency histograms require bucket samples with ``le``;
      count/sum samples alone do not establish latency-distribution coverage.

    Requires the caller to have RBAC ``get`` on the non-resource URL
    ``/metrics`` (typically granted by the ``system:monitoring`` ClusterRole
    or cluster-admin).
    """

    description: ClassVar[str] = "Verify kube-apiserver exposes /metrics in Prometheus text format."
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Query /metrics and verify metric samples and their SLO dimensions."""
        expected_metrics = self.config.get("expected_metrics", DEFAULT_EXPECTED_METRICS)
        if not isinstance(expected_metrics, list) or not all(
            isinstance(metric, str) and metric for metric in expected_metrics
        ):
            self.set_failed("'expected_metrics' must be a list[str] with non-empty metric names")
            return

        try:
            kubectl_parts = get_kubectl_command()
            kubectl_base = " ".join(shlex.quote(part) for part in kubectl_parts)
            result = self.run_command(f"{kubectl_base} get --raw /metrics")
        except FileNotFoundError:
            pytest.skip("kubectl-compatible CLI is unavailable; cannot query API server metrics")
        if result.exit_code == 127:
            pytest.skip("kubectl-compatible CLI was not found; cannot query API server metrics")

        if result.exit_code != 0:
            self.set_failed(
                f"Failed to query API server metrics endpoint (check RBAC for 'get' on /metrics): {result.stderr}"
            )
            return

        output = result.stdout.strip()
        if not output:
            self.set_failed("API server metrics endpoint returned empty response")
            return

        has_help = False
        has_type = False
        labels_by_metric: dict[str, set[str]] = {}

        for line_number, line in enumerate(output.splitlines(), 1):
            line = line.strip()
            if line.startswith("# HELP "):
                has_help = True
            elif line.startswith("# TYPE "):
                has_type = True
            elif line and not line.startswith("#"):
                try:
                    name, labels = _sample_labels(line)
                except ValueError:
                    self.set_failed(
                        f"Response is not in Prometheus text exposition format: invalid sample on line {line_number}"
                    )
                    return
                if name in labels_by_metric:
                    labels_by_metric[name].intersection_update(labels)
                else:
                    labels_by_metric[name] = labels

        metric_names = set(labels_by_metric)
        if not has_help or not has_type or not metric_names:
            self.set_failed(
                "Response is not in Prometheus text exposition format "
                f"(HELP: {has_help}, TYPE: {has_type}, metrics: {len(metric_names)})"
            )
            return

        missing = [
            m
            for m in expected_metrics
            if not _metric_samples(m, metric_names)
            or (m == "apiserver_request_duration_seconds" and m + "_bucket" not in metric_names)
        ]

        if missing:
            self.set_failed(f"Missing expected metrics: {', '.join(missing)}")
            return

        incomplete = []
        for metric in expected_metrics:
            for name in sorted(_metric_samples(metric, metric_names)):
                absent = SLO_LABELS.get(name, set()) - labels_by_metric[name]
                if absent:
                    incomplete.append(f"{name}: {', '.join(sorted(absent))}")
        if incomplete:
            self.set_failed("Missing SLO labels on metric samples: " + "; ".join(incomplete))
            return

        self.set_passed(f"API server metrics endpoint is valid Prometheus format with {len(metric_names)} metrics")
