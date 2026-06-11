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

import shlex
from typing import ClassVar

from isvtest.core.k8s import get_kubectl_command
from isvtest.core.validation import BaseValidation

DEFAULT_EXPECTED_METRICS = [
    "apiserver_request_total",
    "apiserver_request_duration_seconds",
]


class K8sApiServerMetricsCheck(BaseValidation):
    """Verify kube-apiserver exposes /metrics in Prometheus text exposition format.

    Queries the API server's ``/metrics`` endpoint via ``kubectl get --raw``
    and validates:

    - The response contains ``# HELP`` and ``# TYPE`` headers.
    - At least one metric sample is present.
    - All metric names configured via ``expected_metrics`` (or the defaults
      ``apiserver_request_total`` / ``apiserver_request_duration_seconds``)
      are exposed. Matching is by name prefix so histogram and summary
      sub-metrics (``_count``/``_bucket``/``_sum``) satisfy the parent name.

    Requires the caller to have RBAC ``get`` on the non-resource URL
    ``/metrics`` (typically granted by the ``system:monitoring`` ClusterRole
    or cluster-admin).
    """

    description: ClassVar[str] = "Verify kube-apiserver exposes /metrics in Prometheus text format."
    timeout: ClassVar[int] = 120
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        """Query the API server /metrics endpoint and verify expected metric names are exposed."""
        expected_metrics = self.config.get("expected_metrics", DEFAULT_EXPECTED_METRICS)
        if not isinstance(expected_metrics, list) or not all(
            isinstance(metric, str) and metric for metric in expected_metrics
        ):
            self.set_failed("'expected_metrics' must be a list[str] with non-empty metric names")
            return

        kubectl_parts = get_kubectl_command()
        kubectl_base = " ".join(shlex.quote(part) for part in kubectl_parts)

        cmd = f"{kubectl_base} get --raw /metrics"
        result = self.run_command(cmd)

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
        metric_names = set()

        for line in output.splitlines():
            if line.startswith("# HELP "):
                has_help = True
            elif line.startswith("# TYPE "):
                has_type = True
            elif line and not line.startswith("#"):
                parts = line.split("{")[0].split()
                if parts:
                    metric_names.add(parts[0])

        if not has_help or not has_type or not metric_names:
            self.set_failed(
                "Response is not in Prometheus text exposition format "
                f"(HELP: {has_help}, TYPE: {has_type}, metrics: {len(metric_names)})"
            )
            return

        missing = [m for m in expected_metrics if not any(name.startswith(m) for name in metric_names)]

        if missing:
            self.set_failed(f"Missing expected metrics: {', '.join(missing)}")
            return

        self.set_passed(f"API server metrics endpoint is valid Prometheus format with {len(metric_names)} metrics")
