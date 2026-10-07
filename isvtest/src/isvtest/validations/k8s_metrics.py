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

# Consume the whole quoted value so text inside it cannot become another label key.
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"(?:\\.|[^"\\])*"')


def _sample_labels(line: str) -> tuple[str, set[str]]:
    """Extract sample names and label keys with basic checks for malformed evidence."""
    name, separator, rest = line.partition("{")
    labels: set[str] = set()
    if separator:
        label_text, closing, value_text = rest.rpartition("}")
        keys = _LABEL.findall(label_text)
        labels = set(keys)
        if not closing or len(keys) != len(labels) or _LABEL.sub("", label_text).strip(" ,\t"):
            raise ValueError("invalid or duplicate label")
    else:
        name, value_text = line.split(maxsplit=1)
    value, *_ = value_text.split()
    float(value)
    return name.strip(), labels


class K8sApiServerMetricsCheck(BaseValidation):
    """Verify kube-apiserver exposes /metrics in Prometheus text exposition format.

    Queries the API server's ``/metrics`` endpoint via ``kubectl get --raw``
    and validates:

    - The response contains ``# HELP`` and ``# TYPE`` headers.
    - At least one metric sample is present.
    - All metric names configured via ``expected_metrics`` (or the defaults
      ``apiserver_request_total`` / ``apiserver_request_duration_seconds``)
      are exposed. Exact sample names and declared histogram/summary families match.
    - Every observed request counter or latency sample selected by the check
      carries its required SLO labels. Empty label values are allowed, as on
      non-resource requests. Latency buckets require the ``le`` label;
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
            # Check in the runner's environment; an installed wrapper may itself return 127.
            executable = self.run_command(
                f"if command -v {shlex.quote(kubectl_parts[0])} >/dev/null 2>&1; then exit 0; else exit 1; fi"
            )
            if executable.exit_code == 1:
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

        lines = output.splitlines()
        has_help = any(line.lstrip().startswith("# HELP ") for line in lines)
        has_type = any(line.lstrip().startswith("# TYPE ") for line in lines)
        labels_by_metric: dict[str, set[str]] = {}
        samples_by_metric: dict[str, set[str]] = {}
        family = ""
        family_samples: set[str] = set()
        try:
            for line in lines:
                line = line.strip()
                if line.startswith("# HELP "):
                    _, _, declared, *_ = line.split(maxsplit=3)
                    if declared != family:
                        family_samples = set()
                elif line.startswith("# TYPE "):
                    _, _, family, kind = line.split(maxsplit=3)
                    suffixes = {"histogram": ("_bucket", "_count", "_sum"), "summary": ("", "_count", "_sum")}
                    family_samples = {family + suffix for suffix in suffixes.get(kind, ())}
                    if family == "apiserver_request_total":
                        family_samples = set()
                elif line and not line.startswith("#"):
                    name, labels = _sample_labels(line)
                    samples_by_metric.setdefault(name, set()).add(name)
                    if name in family_samples:
                        samples_by_metric.setdefault(family, set()).add(name)
                    if name in labels_by_metric:
                        labels_by_metric[name].intersection_update(labels)
                    else:
                        labels_by_metric[name] = labels
        except ValueError:
            self.set_failed("Response is not in Prometheus text exposition format: could not parse metric samples")
            return

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
            if not samples_by_metric.get(m)
            or (m == "apiserver_request_duration_seconds" and m + "_bucket" not in samples_by_metric.get(m, set()))
        ]

        if missing:
            self.set_failed(f"Missing expected metrics: {', '.join(missing)}")
            return

        incomplete = []
        for metric in expected_metrics:
            for name in sorted(samples_by_metric[metric]):
                absent = SLO_LABELS.get(name, set()) - labels_by_metric[name]
                if absent:
                    incomplete.append(f"{name}: {', '.join(sorted(absent))}")
        if incomplete:
            self.set_failed("Missing SLO labels on metric samples: " + "; ".join(incomplete))
            return

        self.set_passed(f"API server metrics parsed in Prometheus format with {len(metric_names)} metrics")
