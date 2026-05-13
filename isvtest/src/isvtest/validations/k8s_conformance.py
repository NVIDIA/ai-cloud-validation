# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""CNCF Kubernetes conformance validation via the upstream e2e Pod.

Runs the ``registry.k8s.io/conformance:<server-version>`` image directly as a
Pod, which is functionally what Sonobuoy's e2e plugin does internally. The
image pins ``ginkgo`` + ``e2e.test`` to a Kubernetes release, invokes them
through ``run_e2e.sh``, and writes a JUnit report to the results directory.

The image's default entrypoint exits when the suite finishes, which would let
the Pod transition to ``Succeeded`` - after which ``kubectl exec`` can no
longer enter the container to retrieve artifacts. To avoid that, we override
the container command with a small shell wrapper that runs ``run_e2e.sh``,
writes the exit code to ``/tmp/results/done``, and then ``sleep infinity`` so
the container stays Running long enough for the harness to ``cat`` the JUnit.
"""

from __future__ import annotations

import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import yaml

from isvtest.core.k8s import (
    TERMINAL_WAITING_REASONS,
    TRANSIENT_WAITING_REASONS,
    get_kubectl_base_shell,
    is_k8s_available,
    parse_server_version,
    pod_state_from_result,
)
from isvtest.core.validation import BaseValidation


@dataclass
class _TestCase:
    name: str
    passed: bool
    skipped: bool = False
    message: str = ""
    duration: float | None = None


@dataclass
class _Summary:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    cases: list[_TestCase] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped


class K8sCncfConformanceCheck(BaseValidation):
    """Verify CNCF Kubernetes conformance by running the e2e Pod directly."""

    description = "Verify CNCF Kubernetes conformance by running the registry.k8s.io/conformance Pod."
    markers: ClassVar[list[str]] = ["kubernetes", "l2", "slow"]
    timeout: ClassVar[int] = 120  # default for auxiliary commands

    _DEFAULT_TIMEOUT = 7200
    _DEFAULT_STARTUP_TIMEOUT = 600
    _AUX_CMD_TIMEOUT = 60
    _EXEC_CAT_TIMEOUT = 300
    _POLL_INTERVAL = 10.0
    _PROGRESS_INTERVAL = 60.0

    # Resource defaults chosen to keep the pod in Burstable QoS with real
    # ephemeral-storage accounting - a BestEffort pod is the first kubelet
    # evicts under node pressure, which kills long conformance runs. CPU limit
    # is intentionally omitted: CFS throttling on e2e.test causes spurious
    # test timeouts. Memory limit is generous because ginkgo + e2e.test can
    # spike well above steady-state during heavy SIG suites.
    _DEFAULT_RESOURCES: ClassVar[dict[str, str]] = {
        "cpu_request": "500m",
        "memory_request": "1Gi",
        "memory_limit": "4Gi",
        "ephemeral_storage_request": "2Gi",
        "ephemeral_storage_limit": "10Gi",
    }

    _VALID_MODES: ClassVar[set[str]] = {
        "certified-conformance",
        "non-disruptive-conformance",
        "quick",
    }
    _MODE_PRESETS: ClassVar[dict[str, dict[str, str]]] = {
        "certified-conformance": {
            "focus": r"\[Conformance\]",
            "skip": "",
            "parallel": "false",
        },
        "non-disruptive-conformance": {
            "focus": r"\[Conformance\]",
            "skip": r"\[Disruptive\]|\[Serial\]",
            "parallel": "false",
        },
        "quick": {
            # Minimal focus for smoke-testing the harness end-to-end.
            "focus": r"\[Conformance\]\[sig-api-machinery\].*configmap",
            "skip": "",
            "parallel": "false",
        },
    }

    _RESULTS_DIR = "/tmp/results"
    _DONE_MARKER = f"{_RESULTS_DIR}/done"
    _JUNIT_PATH = f"{_RESULTS_DIR}/junit_01.xml"
    _POD_NAME = "e2e-conformance"
    _MANIFEST_TEMPLATE = Path(__file__).parent / "manifests" / "k8s" / "k8s_conformance.yaml"

    def run(self) -> None:
        if not is_k8s_available():
            self.set_failed("Kubernetes cluster is not available")
            return

        mode = self.config.get("mode", "certified-conformance")
        if mode not in self._VALID_MODES:
            self.set_failed(f"Invalid mode {mode!r} (expected one of {sorted(self._VALID_MODES)})")
            return

        version = self.config.get("kubernetes_version") or self._detect_cluster_version()
        if not version:
            self.set_failed("Could not detect Kubernetes server version; set kubernetes_version in config")
            return

        image = self.config.get("conformance_image") or f"registry.k8s.io/conformance:{version}"
        namespace = self.config.get("namespace") or f"conformance-{uuid.uuid4().hex[:8]}"
        conformance_timeout = int(self.config.get("timeout", self._DEFAULT_TIMEOUT))
        startup_timeout = int(self.config.get("startup_timeout", self._DEFAULT_STARTUP_TIMEOUT))
        cleanup_namespace = bool(self.config.get("cleanup_namespace", True))
        report_individual = bool(self.config.get("report_individual_tests", True))

        preset = self._MODE_PRESETS[mode]
        env_vars = {
            "E2E_FOCUS": self.config.get("e2e_focus", preset["focus"]),
            "E2E_SKIP": self.config.get("e2e_skip", preset["skip"]),
            "E2E_PARALLEL": str(self.config.get("e2e_parallel", preset["parallel"])),
            "E2E_PROVIDER": self.config.get("e2e_provider", "skeleton"),
            "RESULTS_DIR": self._RESULTS_DIR,
        }

        resources = {key: str(self.config.get(key, default)) for key, default in self._DEFAULT_RESOURCES.items()}

        self.log.info(
            f"Starting conformance: image={image} namespace={namespace} mode={mode} timeout={conformance_timeout}s"
        )

        manifest = self._render_manifest(
            namespace=namespace,
            pod_name=self._POD_NAME,
            image=image,
            env_vars=env_vars,
            resources=resources,
        )

        applied = False
        try:
            applied = self._apply_manifest(manifest)
            if not applied:
                self.set_failed("Failed to apply conformance manifest to the cluster")
                return

            startup_error = self._wait_for_pod_running(namespace, self._POD_NAME, startup_timeout)
            if startup_error is not None:
                logs = self._get_pod_logs(namespace, self._POD_NAME)
                self.set_failed(startup_error, output=logs[-4000:])
                return

            completion_error = self._wait_for_completion(namespace, self._POD_NAME, conformance_timeout)
            if completion_error is not None:
                # Logs may be empty if the pod is gone - fall back to events so
                # the report surfaces evictions / taints.
                output = self._get_pod_logs(namespace, self._POD_NAME)
                if not output:
                    output = self._get_recent_events(namespace)
                self.set_failed(completion_error, output=output[-4000:])
                return

            ok, junit_xml = self._exec_cat(namespace, self._POD_NAME, self._JUNIT_PATH)
            if not ok or not junit_xml:
                logs = self._get_pod_logs(namespace, self._POD_NAME)
                self.set_failed(
                    f"Could not retrieve {self._JUNIT_PATH} from conformance pod",
                    output=logs[-4000:],
                )
                return

            summary = self._parse_junit(junit_xml)

            if report_individual:
                for case in summary.cases:
                    self.report_subtest(
                        case.name,
                        case.passed,
                        message=case.message,
                        skipped=case.skipped,
                        duration=case.duration,
                    )

            msg = (
                f"Conformance ({mode}, {version}): "
                f"{summary.passed}/{summary.total} passed, "
                f"{summary.failed} failed, {summary.skipped} skipped"
            )
            if summary.total == 0:
                self.set_failed(f"{msg} - no testcases parsed from JUnit output")
            elif summary.failed > 0:
                failed_names = [c.name for c in summary.cases if not c.passed and not c.skipped][:10]
                self.set_failed(msg, output="First failures:\n" + "\n".join(failed_names))
            else:
                self.set_passed(msg)

        finally:
            if applied and cleanup_namespace:
                self._cleanup(namespace)

    def _detect_cluster_version(self) -> str | None:
        cmd = get_kubectl_base_shell("version", "-o", "json")
        result = self.run_command(cmd, timeout=self._AUX_CMD_TIMEOUT)
        if result.exit_code != 0 or not result.stdout:
            self.log.warning(f"kubectl version failed: {result.stderr.strip()}")
            return None
        version = parse_server_version(result.stdout)
        if version is None:
            self.log.warning(f"Could not parse server version from kubectl output: {result.stdout!r}")
        return version

    def _render_manifest(
        self,
        namespace: str,
        pod_name: str,
        image: str,
        env_vars: dict[str, str],
        resources: dict[str, str],
    ) -> str:
        docs = list(yaml.safe_load_all(self._MANIFEST_TEMPLATE.read_text()))
        by_kind = {d["kind"]: d for d in docs}

        by_kind["Namespace"]["metadata"]["name"] = namespace
        by_kind["ServiceAccount"]["metadata"]["namespace"] = namespace

        crb = by_kind["ClusterRoleBinding"]
        crb["metadata"]["name"] = f"conformance-{namespace}"
        crb["subjects"][0]["namespace"] = namespace

        pod = by_kind["Pod"]
        pod["metadata"]["name"] = pod_name
        pod["metadata"]["namespace"] = namespace

        container = pod["spec"]["containers"][0]
        container["image"] = image
        container["env"] = [{"name": k, "value": str(v)} for k, v in env_vars.items()]
        container["resources"] = {
            "requests": {
                "cpu": resources["cpu_request"],
                "memory": resources["memory_request"],
                "ephemeral-storage": resources["ephemeral_storage_request"],
            },
            "limits": {
                "memory": resources["memory_limit"],
                "ephemeral-storage": resources["ephemeral_storage_limit"],
            },
        }

        return yaml.safe_dump_all(docs, sort_keys=False)

    def _apply_manifest(self, manifest: str) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(manifest)
            path = fh.name
        try:
            cmd = get_kubectl_base_shell("apply", "-f", path)
            result = self.run_command(cmd, timeout=self._AUX_CMD_TIMEOUT)
            if result.exit_code != 0:
                self.log.error(f"kubectl apply failed: {result.stderr.strip()}")
                return False
            self.log.info(f"Applied conformance manifest: {result.stdout.strip()}")
            return True
        finally:
            Path(path).unlink(missing_ok=True)

    def _wait_for_pod_running(self, namespace: str, pod_name: str, timeout: int) -> str | None:
        """Poll until the pod is Running. Return None on success, or a failure
        message explaining why startup is doomed (image pull errors, Failed
        phase, deletion, or timeout) - the caller surfaces it via set_failed."""
        deadline = time.time() + timeout
        last_progress = time.time()
        prev_transient_reason = ""
        while time.time() < deadline:
            phase, reason, message = self._get_pod_state(namespace, pod_name)
            if phase == "Running":
                return None
            if phase == "Succeeded":
                # Container finished before we observed Running (very short runtime).
                return None
            if phase == "Failed":
                self.log.error(f"Pod {pod_name} entered Failed phase during startup")
                return f"Conformance pod {pod_name} entered Failed phase during startup"
            if phase == "NotFound":
                self.log.error(f"Pod {pod_name} disappeared during startup")
                return f"Conformance pod {pod_name} was deleted during startup (likely evicted)"

            if reason in TERMINAL_WAITING_REASONS:
                detail = f"{reason}: {message}" if message else reason
                self.log.error(f"Pod {pod_name} container stuck in {detail}")
                return f"Conformance pod {pod_name} cannot start ({detail})"
            if reason in TRANSIENT_WAITING_REASONS:
                if prev_transient_reason == reason:
                    detail = f"{reason}: {message}" if message else reason
                    self.log.error(f"Pod {pod_name} container persistently {detail}")
                    return f"Conformance pod {pod_name} cannot start ({detail})"
                prev_transient_reason = reason
            else:
                prev_transient_reason = ""

            now = time.time()
            if now - last_progress >= self._PROGRESS_INTERVAL:
                self.log.info(f"Waiting for pod {pod_name} to start (phase={phase})")
                last_progress = now
            time.sleep(self._POLL_INTERVAL)
        return f"Conformance pod did not reach Running state within {timeout}s"

    def _wait_for_completion(self, namespace: str, pod_name: str, timeout: int) -> str | None:
        """Poll until the done marker appears. Return None on success, or a
        failure message (Failed phase, pod deleted, or timeout)."""
        start = time.time()
        deadline = start + timeout
        last_progress = start
        while time.time() < deadline:
            phase, _, _ = self._get_pod_state(namespace, pod_name)
            if phase == "Failed":
                self.log.error(f"Pod {pod_name} entered Failed phase before completion marker")
                return f"Conformance pod {pod_name} entered Failed phase before completion marker appeared"
            if phase == "NotFound":
                self.log.error(f"Pod {pod_name} disappeared before completion marker")
                return f"Conformance pod {pod_name} was deleted before completion marker appeared (likely evicted)"

            done, _ = self._exec_cat(namespace, pod_name, self._DONE_MARKER, timeout=self._AUX_CMD_TIMEOUT, quiet=True)
            if done:
                self.log.info(f"Conformance completed (elapsed={int(time.time() - start)}s)")
                return None

            now = time.time()
            if now - last_progress >= self._PROGRESS_INTERVAL:
                elapsed = int(now - start)
                remaining = int(deadline - now)
                self.log.info(f"Conformance still running (elapsed={elapsed}s, remaining<={remaining}s, phase={phase})")
                last_progress = now
            time.sleep(self._POLL_INTERVAL)
        return f"Conformance run did not finish within {timeout}s"

    def _get_pod_state(self, namespace: str, pod_name: str) -> tuple[str, str, str]:
        """Fetch ``(phase, waiting_reason, waiting_message)`` in one kubectl call."""
        cmd = get_kubectl_base_shell("get", "pod", pod_name, "-n", namespace, "-o", "json")
        return pod_state_from_result(self.run_command(cmd, timeout=self._AUX_CMD_TIMEOUT))

    def _exec_cat(
        self,
        namespace: str,
        pod_name: str,
        path: str,
        timeout: int | None = None,
        quiet: bool = False,
    ) -> tuple[bool, str]:
        """Cat ``path`` inside the pod. Returns ``(ok, content)``.

        ``quiet=True`` suppresses the warning log on failure - used for
        existence-polling where failure is the expected steady state until the
        file appears.
        """
        cmd = get_kubectl_base_shell("exec", "-n", namespace, pod_name, "--", "cat", path)
        result = self.run_command(cmd, timeout=timeout if timeout is not None else self._EXEC_CAT_TIMEOUT)
        if result.exit_code != 0:
            if not quiet:
                self.log.warning(f"kubectl exec cat {path} failed: {result.stderr.strip()}")
            return False, ""
        return True, result.stdout

    def _get_pod_logs(self, namespace: str, pod_name: str) -> str:
        """Return the last 200 lines of pod logs, or ``""`` on failure."""
        cmd = get_kubectl_base_shell("logs", "-n", namespace, pod_name, "--tail=200")
        result = self.run_command(cmd, timeout=self._AUX_CMD_TIMEOUT)
        if result.exit_code != 0:
            return ""
        return result.stdout

    def _get_recent_events(self, namespace: str) -> str:
        """Return namespace events sorted by ``lastTimestamp``, or ``""`` on failure."""
        cmd = get_kubectl_base_shell("get", "events", "-n", namespace, "--sort-by=.lastTimestamp")
        result = self.run_command(cmd, timeout=self._AUX_CMD_TIMEOUT)
        if result.exit_code != 0:
            return ""
        return result.stdout

    def _cleanup(self, namespace: str) -> None:
        """Delete the run's namespace and its cluster-scoped ClusterRoleBinding; failures are logged, not raised."""
        ns_cmd = get_kubectl_base_shell("delete", "namespace", namespace, "--ignore-not-found=true", "--wait=false")
        ns_result = self.run_command(ns_cmd, timeout=self._AUX_CMD_TIMEOUT)
        if ns_result.exit_code != 0:
            self.log.warning(f"Failed to delete namespace {namespace}: {ns_result.stderr.strip()}")

        # ClusterRoleBinding is cluster-scoped and outlives the namespace deletion.
        crb_cmd = get_kubectl_base_shell(
            "delete", "clusterrolebinding", f"conformance-{namespace}", "--ignore-not-found=true"
        )
        crb_result = self.run_command(crb_cmd, timeout=self._AUX_CMD_TIMEOUT)
        if crb_result.exit_code != 0:
            self.log.warning(f"Failed to delete clusterrolebinding: {crb_result.stderr.strip()}")

    def _parse_junit(self, xml_str: str) -> _Summary:
        summary = _Summary()
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as exc:
            self.log.error(f"Failed to parse JUnit XML: {exc}")
            return summary

        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))

        for suite in suites:
            for case in suite.iter("testcase"):
                name = (case.get("name") or "").strip() or "(unnamed)"
                duration = _parse_duration(case.get("time"))

                failure = case.find("failure")
                error = case.find("error")
                skipped = case.find("skipped")

                if skipped is not None:
                    summary.skipped += 1
                    msg = (skipped.get("message") or skipped.text or "").strip()
                    summary.cases.append(
                        _TestCase(name=name, passed=False, skipped=True, message=msg, duration=duration)
                    )
                elif failure is not None or error is not None:
                    summary.failed += 1
                    node = failure if failure is not None else error
                    msg = (node.get("message") or node.text or "").strip() if node is not None else ""
                    summary.cases.append(
                        _TestCase(name=name, passed=False, skipped=False, message=msg, duration=duration)
                    )
                else:
                    summary.passed += 1
                    summary.cases.append(_TestCase(name=name, passed=True, skipped=False, duration=duration))

        return summary


def _parse_duration(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
