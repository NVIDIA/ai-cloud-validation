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

"""Governance and capacity-fleet metrics validations.

Validations that assert a provider's governance API exposes the Delivered,
Healthy, Reserved, and Active capacity metrics for nodes and GPUs (test ID CAP01-01).
"""

from __future__ import annotations

from typing import ClassVar

from isvtest.core.validation import BaseValidation

# Metric buckets the governance API must expose, in the conventional ordering
# (Delivered ⊇ Reserved ⊇ Active; Healthy is independent of allocation state).
REQUIRED_METRICS: tuple[str, ...] = ("delivered", "healthy", "reserved", "active")

# Resource dimensions surfaced per metric.
REQUIRED_RESOURCES: tuple[str, ...] = ("nodes", "gpus")


class GovernanceMetricsCheck(BaseValidation):
    """Validate the governance API returns the required capacity metrics.

    Asserts that the step output exposes per-resource counts (nodes, GPUs) for
    the four governance metric buckets (Delivered, Healthy, Reserved, Active)
    and that the relationships between them are internally consistent.

    Config:
        step_output: Step output containing the governance metrics.
        min_delivered_nodes: Optional minimum Delivered node count (default: 0).
        min_delivered_gpus: Optional minimum Delivered GPU count (default: 0).

    Step output:
        success: bool
        platform: str
        metrics: dict[str, dict[str, int]]:
            delivered: {"nodes": int, "gpus": int}
            healthy:   {"nodes": int, "gpus": int}
            reserved:  {"nodes": int, "gpus": int}
            active:    {"nodes": int, "gpus": int}

    Definitions (provider-agnostic):
        Delivered: hardware the provider has onboarded and made available to
            tenants (any reservable/active state).
        Healthy:   subset of Delivered passing the provider's health probes.
        Reserved:  subset of Delivered allocated to a tenant (in-use or held).
        Active:    subset of Reserved currently running tenant workloads.
    """

    description: ClassVar[str] = "Check governance API exposes Delivered/Healthy/Reserved/Active node and GPU metrics"
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        """Validate metric presence, value sanity, and inter-metric relationships."""
        step_output = self.config.get("step_output", {})

        if not step_output.get("success"):
            self.set_failed(f"Governance metrics step failed: {step_output.get('error', 'Unknown error')}")
            return

        metrics = step_output.get("metrics")
        if not isinstance(metrics, dict):
            self.set_failed("Governance step output is missing the 'metrics' object")
            return

        missing_metrics = [m for m in REQUIRED_METRICS if m not in metrics]
        if missing_metrics:
            self.set_failed(f"Governance metrics missing required buckets: {', '.join(missing_metrics)}")
            return

        # Validate the shape and values of every bucket up front; bail out
        # before relationship checks if anything is malformed (so we report
        # the schema problem rather than a misleading consistency error).
        bucket_values: dict[str, dict[str, int]] = {}
        for metric_name in REQUIRED_METRICS:
            bucket = metrics[metric_name]
            if not isinstance(bucket, dict):
                self.set_failed(f"Governance metric '{metric_name}' is not an object")
                return

            resources: dict[str, int] = {}
            for resource in REQUIRED_RESOURCES:
                if resource not in bucket:
                    self.set_failed(f"Governance metric '{metric_name}' is missing required resource '{resource}'")
                    return
                value = bucket[resource]
                # bool is a subclass of int in Python; reject it explicitly so a
                # truthy/falsy flag is not silently accepted as a count.
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    self.set_failed(
                        f"Governance metric '{metric_name}.{resource}' must be a non-negative integer, got {value!r}"
                    )
                    return
                resources[resource] = value
            bucket_values[metric_name] = resources
            self.report_subtest(
                f"metric_{metric_name}",
                passed=True,
                message=f"{metric_name}: nodes={resources['nodes']}, gpus={resources['gpus']}",
            )

        min_nodes = self._coerce_non_negative_int("min_delivered_nodes", default=0)
        min_gpus = self._coerce_non_negative_int("min_delivered_gpus", default=0)
        if min_nodes is None or min_gpus is None:
            return

        delivered_nodes = bucket_values["delivered"]["nodes"]
        delivered_gpus = bucket_values["delivered"]["gpus"]

        threshold_failures: list[str] = []
        if delivered_nodes < min_nodes:
            threshold_failures.append(f"Delivered nodes {delivered_nodes} < min {min_nodes}")
        if delivered_gpus < min_gpus:
            threshold_failures.append(f"Delivered gpus {delivered_gpus} < min {min_gpus}")

        # Inter-metric ordering: Delivered ⊇ Reserved ⊇ Active and Delivered ⊇ Healthy.
        relationship_failures = self._check_relationships(bucket_values)

        failures = threshold_failures + relationship_failures
        if failures:
            self.set_failed(f"Governance metrics invariants violated: {'; '.join(failures)}")
            return

        self.set_passed(f"Governance metrics OK (delivered: nodes={delivered_nodes}, gpus={delivered_gpus})")

    def _coerce_non_negative_int(self, key: str, *, default: int) -> int | None:
        """Read ``key`` from config and coerce to int >= 0; fail otherwise."""
        raw = self.config.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            self.set_failed(f"`{key}` must be a non-negative integer, got {raw!r}")
            return None
        return raw

    def _check_relationships(self, buckets: dict[str, dict[str, int]]) -> list[str]:
        """Return a list of human-readable invariant violations (empty if OK)."""
        # subset pairs: (subset_metric, superset_metric)
        invariants: tuple[tuple[str, str], ...] = (
            ("healthy", "delivered"),
            ("reserved", "delivered"),
            ("active", "reserved"),
        )
        failures: list[str] = []
        for subset, superset in invariants:
            for resource in REQUIRED_RESOURCES:
                sub = buckets[subset][resource]
                sup = buckets[superset][resource]
                if sub > sup:
                    failures.append(f"{subset} {resource} ({sub}) exceeds {superset} {resource} ({sup})")
        return failures
