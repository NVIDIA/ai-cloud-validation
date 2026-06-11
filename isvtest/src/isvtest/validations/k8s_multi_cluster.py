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

"""Multi-cluster Kubernetes checks over provider step output."""

from __future__ import annotations

from decimal import Decimal
from numbers import Integral, Rational
from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation


class K8sMultiClusterSameVpcCheck(BaseValidation):
    """Verify two or more Kubernetes clusters coexist in one tenancy and VPC.

    The validation is intentionally provider-neutral and reads only
    ``step_output``. Provider scripts must emit the tenancy/account ID, shared
    VPC/network ID, and a ``clusters`` array with each cluster's name,
    tenancy, network, status, and optional role/Ready-node metadata.
    """

    description: ClassVar[str] = "Verify multiple Kubernetes clusters share the same tenancy and VPC."
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        """Validate the multi-cluster proof emitted by the bound setup step."""
        step_output = self.config.get("step_output")
        if not isinstance(step_output, dict):
            self.set_failed("Missing step_output for multi-cluster validation")
            return

        if step_output.get("success") is False:
            self.set_failed(str(step_output.get("error") or step_output.get("message") or "Step reported failure"))
            return

        tenancy_id = _required_string(step_output.get("tenancy_id"), "tenancy_id")
        if tenancy_id is None:
            self.set_failed("Missing or empty tenancy_id in step output")
            return
        network_id = _required_string(step_output.get("network_id"), "network_id")
        if network_id is None:
            self.set_failed("Missing or empty network_id in step output")
            return

        clusters = step_output.get("clusters")
        if not isinstance(clusters, list):
            self.set_failed("Missing or invalid clusters list in step output")
            return
        if len(clusters) < 2:
            self.set_failed(f"Expected at least 2 clusters, got {len(clusters)}")
            return

        parsed_clusters: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        tenancy_ids: set[str] = set()
        network_ids: set[str] = set()
        inactive: list[str] = []

        for index, cluster in enumerate(clusters):
            if not isinstance(cluster, dict):
                self.set_failed(f"Cluster entry {index} must be an object")
                return

            name = _required_string(cluster.get("name"), f"clusters[{index}].name")
            if name is None:
                self.set_failed(f"Cluster entry {index} missing name")
                return
            if name in seen_names:
                self.set_failed(f"Duplicate cluster name: {name}")
                return
            seen_names.add(name)

            cluster_tenancy = _required_string(cluster.get("tenancy_id"), f"{name}.tenancy_id")
            if cluster_tenancy is None:
                self.set_failed(f"Cluster {name} missing tenancy_id")
                return
            cluster_network = _required_string(cluster.get("network_id"), f"{name}.network_id")
            if cluster_network is None:
                self.set_failed(f"Cluster {name} missing network_id")
                return

            tenancy_ids.add(cluster_tenancy)
            network_ids.add(cluster_network)

            status = str(cluster.get("status") or "").strip().upper()
            if status != "ACTIVE":
                inactive.append(f"{name}={status or '<missing>'}")

            parsed_clusters.append(cluster)

        if tenancy_ids != {tenancy_id}:
            self.set_failed(f"Mixed tenancy IDs: top-level {tenancy_id}, clusters {sorted(tenancy_ids)}")
            return
        if network_ids != {network_id}:
            self.set_failed(f"Mixed network IDs: top-level {network_id}, clusters {sorted(network_ids)}")
            return
        if inactive:
            self.set_failed(f"Cluster(s) not ACTIVE: {', '.join(inactive)}")
            return

        ready_counts: list[int] = []
        for cluster in parsed_clusters:
            if "ready_node_count" not in cluster:
                continue
            name = str(cluster.get("name"))
            ready_node_count = _non_negative_int(cluster.get("ready_node_count"), f"{name}.ready_node_count")
            if ready_node_count is None:
                self.set_failed(f"Cluster {name} has invalid ready_node_count")
                return
            if ready_node_count < 1:
                self.set_failed(f"Cluster {name} has no Ready node")
                return
            ready_counts.append(ready_node_count)

        ready_message = f"; Ready nodes reported: {sum(ready_counts)}" if ready_counts else ""
        self.set_passed(
            f"{len(parsed_clusters)} cluster(s) ACTIVE in tenancy {tenancy_id} and VPC {network_id}{ready_message}"
        )


def _required_string(value: Any, _field: str) -> str | None:
    """Return a stripped non-empty string, or ``None`` when missing/invalid."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _non_negative_int(value: Any, _field: str) -> int | None:
    """Return a non-negative integer, rejecting bools and non-integral values."""
    if isinstance(value, bool):
        return None

    if isinstance(value, Integral):
        parsed = int(value)
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        parsed = int(value)
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return None
        parsed = int(value)
    elif isinstance(value, Rational):
        if value.denominator != 1:
            return None
        parsed = int(value)
    elif isinstance(value, str):
        stripped = value.strip()
        signless = stripped[1:] if stripped.startswith(("+", "-")) else stripped
        if not signless.isdecimal():
            return None
        parsed = int(stripped)
    else:
        return None

    if parsed < 0:
        return None
    return parsed
