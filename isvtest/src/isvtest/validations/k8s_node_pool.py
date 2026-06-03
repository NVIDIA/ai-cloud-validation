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

"""Node pool state check.

This validation is **outcome-only**: it does not create, scale, or delete any
node pool. Provisioning (create/update/scale) is delegated to provider-supplied
setup steps (for AWS EKS, ``terraform apply`` on a small node-group module;
for Cluster API-based providers, ``kubectl apply`` of a ``MachineDeployment``).
Whichever mechanism is used, each step must emit a ``node_pool`` JSON payload
so this check can verify the resulting state from ``kubectl``. Invoke it once
per operation (e.g. after create, again after an update) pointing at that
step's outputs.

What is verified
----------------
* The expected number of nodes matching the provisioning step's
  ``label_selector`` exist and are ``Ready`` within ``wait_timeout``.
* Each node's ``metadata.labels`` is a superset of ``expected_labels``.
* Each node's ``spec.taints`` is a superset of ``expected_taints``
  (compared by ``(key, value, effect)``).
* Each node's ``node.kubernetes.io/instance-type`` label is in
  ``expected_instance_types``.
"""

from __future__ import annotations

import json
import shlex
import time
from typing import Any, ClassVar

from isvtest.core.k8s import get_kubectl_base_shell
from isvtest.core.validation import BaseValidation


class K8sNodePoolCheck(BaseValidation):
    """Verify a provider-managed node pool matches its expected state.

    Config keys (populated from the provisioning step's JSON output):

    * ``label_selector`` - kubectl label selector identifying the new nodes
      (e.g. ``eks.amazonaws.com/nodegroup=isv-test-pool``). Required.
    * ``expected_replicas`` - node count that must reach Ready. Required.
    * ``expected_labels`` - mapping or JSON string; subset check per node.
    * ``expected_taints`` - list of ``{key, value, effect}`` or JSON string;
      subset check per node.
    * ``expected_instance_types`` - list of instance type strings or JSON
      string; each node's type must be in this set.
    * ``node_type`` - informational ``"cpu"`` or ``"gpu"`` tag surfaced in
      the result message.
    * ``wait_timeout`` - seconds to wait for nodes to reach Ready (default 600).
    * ``poll_interval`` - seconds between polls (default 5).

    The label-check, taint-check, and instance-type-check are each skipped
    when their corresponding ``expected_*`` config is empty, so callers can
    exercise a strict subset of the assertions.
    """

    description: ClassVar[str] = "Verify a node pool matches its expected replicas, labels, taints, and instance type."
    timeout: ClassVar[int] = 900

    def run(self) -> None:
        """Poll until the node pool converges, then assert labels/taints/instance-type per node."""
        try:
            label_selector = str(self.config["label_selector"]).strip()
            expected_replicas = int(self.config["expected_replicas"])
        except (KeyError, TypeError, ValueError) as exc:
            self.set_failed(f"Invalid config: {exc}")
            return
        if not label_selector:
            self.set_failed("Invalid config: label_selector is empty")
            return
        if expected_replicas < 0:
            self.set_failed(f"Invalid config: expected_replicas must be >= 0, got {expected_replicas}")
            return

        try:
            expected_labels = _coerce_mapping(self.config.get("expected_labels"), "expected_labels")
            expected_taints = _coerce_taints(self.config.get("expected_taints"))
            expected_instance_types = _coerce_str_list(
                self.config.get("expected_instance_types"), "expected_instance_types"
            )
        except ValueError as exc:
            self.set_failed(f"Invalid config: {exc}")
            return

        try:
            wait_timeout = int(self.config.get("wait_timeout", 600))
            poll_interval = max(1, int(self.config.get("poll_interval", 5)))
        except (TypeError, ValueError) as exc:
            self.set_failed(f"Invalid config: {exc}")
            return
        if wait_timeout < 0:
            self.set_failed(f"Invalid config: wait_timeout must be >= 0, got {wait_timeout}")
            return
        node_type = str(self.config.get("node_type") or "").strip().lower() or None

        nodes = self._wait_for_ready_nodes(label_selector, expected_replicas, wait_timeout, poll_interval)
        if nodes is None:
            return  # set_failed already called

        failures: list[str] = []
        failing_names: set[str] = set()
        for node in nodes:
            name = node.get("metadata", {}).get("name", "<unknown>")
            node_labels: dict[str, str] = node.get("metadata", {}).get("labels", {}) or {}
            node_taints: list[dict[str, Any]] = node.get("spec", {}).get("taints", []) or []

            missing_labels = {k: v for k, v in expected_labels.items() if node_labels.get(k) != v}
            if missing_labels:
                failures.append(f"{name}: missing/incorrect labels {sorted(missing_labels)}")
                failing_names.add(name)

            missing_taints = _missing_taints(expected_taints, node_taints)
            if missing_taints:
                failures.append(f"{name}: missing taints {missing_taints}")
                failing_names.add(name)

            if expected_instance_types:
                actual_type = node_labels.get("node.kubernetes.io/instance-type", "")
                if actual_type not in expected_instance_types:
                    failures.append(
                        f"{name}: instance-type {actual_type!r} not in allowlist {sorted(expected_instance_types)}"
                    )
                    failing_names.add(name)

        if failures:
            self.set_failed(
                "Node pool shape mismatched on {} of {} node(s):\n  - {}".format(
                    len(failing_names), len(nodes), "\n  - ".join(failures)
                )
            )
            return

        type_suffix = f" ({node_type})" if node_type else ""
        self.set_passed(
            f"Node pool{type_suffix} converged: {len(nodes)} node(s) Ready with expected labels, taints, "
            f"and instance type (selector: {label_selector})"
        )

    def _wait_for_ready_nodes(
        self,
        label_selector: str,
        expected_replicas: int,
        wait_timeout: int,
        poll_interval: int,
    ) -> list[dict[str, Any]] | None:
        """Poll until ``expected_replicas`` nodes match ``label_selector`` and are Ready.

        Returns the list of node objects on success. On failure/timeout,
        calls ``set_failed`` and returns ``None``.
        """
        cmd = f"{get_kubectl_base_shell()} get nodes -l {shlex.quote(label_selector)} -o json"
        deadline = time.monotonic() + wait_timeout
        last_summary = "no nodes seen yet"

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.set_failed(f"Node pool did not converge within {wait_timeout}s ({last_summary})")
                return None

            result = self.run_command(
                cmd,
                timeout=max(1, min(max(30, poll_interval * 4), int(remaining))),
            )
            if result.exit_code != 0:
                last_summary = f"kubectl failed: {result.stderr.strip() or result.stdout.strip()}"
            else:
                try:
                    payload = json.loads(result.stdout or "{}")
                except json.JSONDecodeError as exc:
                    self.set_failed(f"Failed to parse kubectl JSON output: {exc}")
                    return None
                nodes = payload.get("items") or []
                ready_nodes = [n for n in nodes if _is_node_ready(n)]
                if expected_replicas == 0 and len(nodes) == 0:
                    return []
                if len(ready_nodes) == expected_replicas and len(nodes) == expected_replicas:
                    return ready_nodes
                last_summary = f"{len(ready_nodes)} Ready / {len(nodes)} total, want {expected_replicas}"

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.set_failed(f"Node pool did not converge within {wait_timeout}s ({last_summary})")
                return None
            self.log.info("Waiting for node pool (%s)", last_summary)
            time.sleep(min(poll_interval, remaining))


def _is_node_ready(node: dict[str, Any]) -> bool:
    """Return True if the node has a ``Ready=True`` condition."""
    for cond in node.get("status", {}).get("conditions", []) or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def _coerce_mapping(value: Any, field: str) -> dict[str, str]:
    """Coerce ``value`` to a ``{str: str}`` mapping.

    Accepts ``None`` (returns empty), a native ``dict``, or a JSON string.
    Raises ``ValueError`` for anything else or for non-string leaf values.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping, got {type(value).__name__}")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"{field} entries must be str->str, got {k!r}: {v!r}")
        out[k] = v
    return out


def _coerce_str_list(value: Any, field: str) -> list[str]:
    """Coerce ``value`` to a ``list[str]``.

    Accepts ``None`` (returns empty), a native ``list``, or a JSON string.
    Raises ``ValueError`` for anything else or for non-string elements.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings, got {item!r}")
    return list(value)


def _coerce_taints(value: Any) -> list[tuple[str, str, str]]:
    """Coerce ``value`` to a list of ``(key, value, effect)`` tuples.

    Accepts ``None``, a JSON string, or a native list of
    ``{"key": ..., "value": ..., "effect": ...}`` dicts. Missing ``value``
    on a taint is normalized to empty string (matching kubectl's behavior
    for ``key=:NoSchedule``-style taints).
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"expected_taints is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError(f"expected_taints must be a list, got {type(value).__name__}")
    out: list[tuple[str, str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError(f"expected_taints entries must be mappings, got {entry!r}")
        key = entry.get("key")
        effect = entry.get("effect")
        taint_value = entry.get("value", "")
        if not isinstance(key, str) or not key:
            raise ValueError(f"expected_taints entry missing string 'key': {entry!r}")
        if not isinstance(effect, str) or not effect:
            raise ValueError(f"expected_taints entry missing string 'effect': {entry!r}")
        if taint_value is None:
            taint_value = ""
        if not isinstance(taint_value, str):
            raise ValueError(f"expected_taints entry 'value' must be a string, got {taint_value!r}")
        out.append((key, taint_value, effect))
    return out


def _missing_taints(expected: list[tuple[str, str, str]], actual: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Return expected taints that are not present on the node (by tuple equality)."""
    if not expected:
        return []
    have = {(t.get("key", ""), t.get("value", "") or "", t.get("effect", "")) for t in actual}
    return [t for t in expected if t not in have]
