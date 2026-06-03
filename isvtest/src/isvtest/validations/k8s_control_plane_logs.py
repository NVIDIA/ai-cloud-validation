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

from __future__ import annotations

import shlex
from typing import Any, ClassVar

from isvtest.core.k8s import KubectlParseError, get_kubectl_base_shell, parse_kubectl_json_items
from isvtest.core.validation import BaseValidation
from isvtest.utils.checks import truncate

_VALID_MODES = ("auto", "kubectl", "command")
_DEFAULT_COMPONENTS = (
    "kube-apiserver",
    "kube-scheduler",
    "kube-controller-manager",
)


class K8sControlPlaneLogsCheck(BaseValidation):
    """Verify Kubernetes control-plane logs can be viewed or exported.

    Supports ``kubectl`` (in-cluster static pods), ``command`` (managed
    distributions that expose logs via an out-of-cluster tool such as
    ``aws logs tail``), and ``auto`` (per-component fallback from kubectl
    to the configured command).
    """

    description: ClassVar[str] = "Verify Kubernetes control-plane logs can be viewed or exported."
    timeout: ClassVar[int] = 120

    def run(self) -> None:
        """Parse the check config, resolve each component, and retrieve its logs.

        On any validation failure (bad config, unresolved component, empty
        output, non-zero exit), marks the check failed via ``set_failed``
        and returns; on success marks it passed via ``set_passed``.
        """
        cfg = self._parse_config()
        if cfg is None:
            return

        plan = self._build_plan(
            mode=cfg["mode"],
            namespace=cfg["namespace"],
            components=cfg["components"],
            commands=cfg["commands"],
        )
        if plan is None:
            return

        self._execute_plan(
            plan=plan,
            namespace=cfg["namespace"],
            tail=cfg["tail"],
            since=cfg["since"],
            min_log_lines=cfg["min_log_lines"],
        )

    def _parse_config(self) -> dict[str, Any] | None:
        """Validate and normalize ``self.config`` into a plan-ready mapping.

        Returns a dict with ``mode``, ``components``, ``min_log_lines``,
        ``tail``, ``namespace``, ``since``, and ``commands`` on success.
        On any invalid input calls ``set_failed`` and returns ``None``.
        """
        mode = str(self.config.get("mode", "auto")).lower()
        if mode not in _VALID_MODES:
            self.set_failed(f"Invalid mode: {mode!r} (expected one of {list(_VALID_MODES)})")
            return None

        raw_components = self.config.get("components")
        if raw_components is None:
            components = list(_DEFAULT_COMPONENTS)
        elif not isinstance(raw_components, (list, tuple)):
            # str falls through here too - `isinstance("foo", (list, tuple))` is False -
            # which stops YAML scalars from being iterated character-by-character.
            self.set_failed(
                f"`components` must be a YAML list, got {type(raw_components).__name__}: "
                f"{raw_components!r}. Example: `components: [kube-apiserver, kube-scheduler]`."
            )
            return None
        else:
            components = [str(c) for c in raw_components]
            if not components:
                self.set_failed("components list is empty")
                return None

        min_log_lines = self._parse_positive_int("min_log_lines", default=1)
        if min_log_lines is None:
            return None

        tail = self._parse_positive_int("tail", default=20)
        if tail is None:
            return None

        if mode != "command" and min_log_lines > tail:
            self.set_failed(
                f"`min_log_lines` ({min_log_lines}) cannot exceed `tail` ({tail}): "
                f"kubectl returns at most `tail` lines, so the check would always fail. "
                f"Raise `tail` or lower `min_log_lines`."
            )
            return None

        namespace = str(self.config.get("namespace", "kube-system"))

        since_raw = self.config.get("since")
        since = str(since_raw) if since_raw else None

        raw_commands = self.config.get("commands")
        if raw_commands is None:
            commands: dict[str, str] = {}
        elif not isinstance(raw_commands, dict):
            self.set_failed(
                f"`commands` must be a mapping of component -> shell command, got "
                f"{type(raw_commands).__name__}: {raw_commands!r}. Example: "
                f"`commands: {{kube-apiserver: 'aws logs tail ...'}}`."
            )
            return None
        else:
            commands = {str(k): str(v) for k, v in raw_commands.items() if v}

        if mode == "command" and since:
            self.log.warning("`since` is only used by the kubectl path; ignoring in mode=command")

        return {
            "mode": mode,
            "components": components,
            "min_log_lines": min_log_lines,
            "tail": tail,
            "namespace": namespace,
            "since": since,
            "commands": commands,
        }

    def _build_plan(
        self,
        mode: str,
        namespace: str,
        components: list[str],
        commands: dict[str, str],
    ) -> list[tuple[str, str, str]] | None:
        """Resolve each component to a ``(component, path, target)`` triple.

        In ``auto`` mode we try kubectl first per-component and fall through
        to ``commands[component]`` when the pod isn't present - this covers
        hybrid clusters where some components are static pods and others are
        managed externally.
        """
        pods_by_component: dict[str, str] = {}
        probe_error: str | None = None
        if mode in ("auto", "kubectl"):
            pods_by_component, probe_error = self._find_component_pods(namespace, components)

        plan: list[tuple[str, str, str]] = []
        unresolved: list[str] = []
        for component in components:
            pod = pods_by_component.get(component) if mode in ("kubectl", "auto") else None
            cmd = commands.get(component) if mode in ("command", "auto") else None
            if pod:
                plan.append((component, "kubectl", pod))
            elif cmd:
                plan.append((component, "command", cmd))
            else:
                unresolved.append(component)

        if unresolved:
            self._fail_unresolved(
                mode=mode,
                namespace=namespace,
                unresolved=unresolved,
                pods_by_component=pods_by_component,
                probe_error=probe_error,
            )
            return None

        # Auto mode: a probe_error means kubectl itself failed (kubeconfig,
        # RBAC, or context), not "no matching pods". Don't mask that by
        # falling through to commands for every component - the operator
        # needs to see the access failure and explicitly choose `mode: command`.
        if mode == "auto" and probe_error is not None:
            self.set_failed(
                f"Unable to list pods in namespace {namespace!r} (kubectl error: "
                f"{probe_error}). Auto mode cannot verify which components should "
                f"use kubectl vs. commands - fix cluster access or set "
                f"`mode: command` to use the `commands` mapping explicitly."
            )
            return None

        return plan

    def _fail_unresolved(
        self,
        mode: str,
        namespace: str,
        unresolved: list[str],
        pods_by_component: dict[str, str],
        probe_error: str | None,
    ) -> None:
        """Emit a mode-specific ``set_failed`` message for unresolved components.

        Distinguishes between kubectl probe failure, absent pods, and missing
        ``commands`` entries so the operator sees actionable remediation.
        """
        if mode == "kubectl":
            if probe_error and not pods_by_component:
                self.set_failed(
                    f"Unable to list pods in namespace {namespace!r} (kubectl "
                    f"error: {probe_error}). Fix cluster access, or switch to "
                    f"`mode: command` with a `commands` mapping."
                )
            else:
                self.set_failed(f"No control-plane pod found for component(s) {unresolved} in namespace {namespace!r}")
            return

        if mode == "command":
            self.set_failed(
                f"Mode 'command' but no command configured for component(s): {unresolved}. "
                f"Populate `commands` in the check config with one shell command per component "
                f"that prints log lines."
            )
            return

        # auto mode
        if probe_error and not pods_by_component:
            self.set_failed(
                f"Unable to list pods in namespace {namespace!r} (kubectl "
                f"error: {probe_error}). Cannot decide between kubectl and "
                f"command paths - fix cluster access or set `mode: command` "
                f"with a `commands` mapping."
            )
            return
        if not pods_by_component:
            missing = sorted(unresolved)
            self.set_failed(
                f"No control-plane pods found in namespace {namespace!r} and no "
                f"`commands` entries configured for component(s): {missing}. "
                f"Supply `commands` for managed clusters (one shell command per "
                f"component that prints log lines)."
            )
            return
        self.set_failed(
            f"Auto mode could not resolve component(s) {unresolved}: no matching pod "
            f"in namespace {namespace!r} and no `commands[{unresolved[0]}]` configured. "
            f"Either expose the component as a pod or add it to the `commands` mapping."
        )

    def _execute_plan(
        self,
        plan: list[tuple[str, str, str]],
        namespace: str,
        tail: int,
        since: str | None,
        min_log_lines: int,
    ) -> None:
        """Run each ``(component, path, target)`` entry and aggregate results.

        For ``path == "kubectl"`` builds a ``kubectl logs`` invocation with
        ``--tail`` and optional ``--since``; otherwise runs ``target`` as a
        shell command. Each component must emit at least ``min_log_lines``
        non-empty lines. Concludes with ``set_passed`` or ``set_failed``.
        """
        failures: list[str] = []
        summaries: list[str] = []
        kubectl_base = get_kubectl_base_shell()
        paths_used: set[str] = set()

        for component, path, target in plan:
            paths_used.add(path)
            if path == "kubectl":
                pod = target
                parts = [
                    kubectl_base,
                    "logs",
                    shlex.quote(pod),
                    "-n",
                    shlex.quote(namespace),
                    f"--tail={tail}",
                ]
                if since:
                    parts.append(f"--since={shlex.quote(since)}")
                cmd = " ".join(parts)
                label = f"{component} (pod {pod})"
            else:
                cmd = target
                label = component

            result = self.run_command(cmd)
            if result.exit_code != 0:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
                if path == "kubectl":
                    failures.append(f"{label}: kubectl logs failed: {detail}")
                else:
                    snippet = truncate(cmd)
                    failures.append(f"{label}: command exited {result.exit_code}: {detail} (cmd: {snippet})")
                continue

            line_count = _count_nonempty_lines(result.stdout)
            if line_count < min_log_lines:
                failures.append(f"{label}: retrieved {line_count} lines (required >= {min_log_lines})")
                continue
            summaries.append(f"{component}={line_count} lines")

        via = _format_via(paths_used)
        if failures:
            self.set_failed(
                f"{len(failures)} component(s) failed control-plane log retrieval via {via}: {'; '.join(failures)}"
            )
            return
        self.set_passed(f"Control-plane logs retrieved via {via}: {', '.join(summaries)}")

    def _find_component_pods(self, namespace: str, components: list[str]) -> tuple[dict[str, str], str | None]:
        """Return ``({component: pod_name}, probe_error)`` for the namespace.

        Resolution runs from a single ``kubectl get pods -o json`` call.
        Label matching and the name-prefix fallback are both resolved
        client-side from that one response, so the cost is one round-trip
        regardless of how many components are requested.

        ``probe_error`` carries the stderr when the call fails, so auto-
        mode can distinguish "cluster reachable but control plane is
        hidden" from "kubectl itself is broken".
        """
        kubectl_base = get_kubectl_base_shell()
        cmd = f"{kubectl_base} get pods -n {shlex.quote(namespace)} -o json"
        result = self.run_command(cmd)
        if result.exit_code != 0:
            probe_error = result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
            return {}, probe_error

        try:
            items = parse_kubectl_json_items(result, f"pod list in namespace {namespace!r}")
        except KubectlParseError as exc:
            return {}, str(exc)

        pods: list[tuple[str, str]] = []
        for item in items:
            metadata = item.get("metadata") or {}
            pod_name = str(metadata.get("name") or "").strip()
            if pod_name:
                labels = metadata.get("labels") or {}
                label = str(labels.get("component") or "") if isinstance(labels, dict) else ""
                pods.append((pod_name, label.strip()))

        found: dict[str, str] = {}
        claimed: set[str] = set()

        # Pass 1: exact ``component`` label match. On HA control planes a
        # component may have multiple replicas (e.g. 3 kube-apiservers);
        # we intentionally pick the first pod kubectl returns - one log
        # sample is enough for this check, and probing every replica
        # would multiply round-trips without changing pass/fail.
        for component in components:
            for pod_name, label in pods:
                if pod_name in claimed:
                    continue
                if label == component:
                    found[component] = pod_name
                    claimed.add(pod_name)
                    break

        # Pass 2: name-prefix fallback for components still missing
        # (distributions that do not set the ``component`` label). Match
        # longest component name first so e.g. ``kube-scheduler-extender``
        # claims its pod before ``kube-scheduler`` greedily picks it up.
        missing = [c for c in components if c not in found]
        for component in sorted(missing, key=len, reverse=True):
            for pod_name, _label in pods:
                if pod_name in claimed:
                    continue
                if pod_name == component or pod_name.startswith(f"{component}-"):
                    found[component] = pod_name
                    claimed.add(pod_name)
                    break

        return found, None


def _count_nonempty_lines(text: str) -> int:
    """Return the number of lines in ``text`` that contain non-whitespace."""
    if not text:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def _format_via(paths: set[str]) -> str:
    """Render the retrieval-path set as a human-readable ``via`` label."""
    if paths == {"kubectl"}:
        return "kubectl"
    if paths == {"command"}:
        return "command"
    return "kubectl+command"
