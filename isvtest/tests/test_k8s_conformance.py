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

"""Tests for K8sCncfConformanceCheck (direct-Pod conformance harness)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
import yaml

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_conformance import (
    K8sCncfConformanceCheck,
    _parse_duration,
)

_DEFAULT_ENV_VARS = {
    "E2E_FOCUS": r"\[Conformance\]",
    "E2E_SKIP": "",
    "E2E_PARALLEL": "false",
    "E2E_PROVIDER": "skeleton",
    "RESULTS_DIR": "/tmp/results",
}

VERSION_JSON = json.dumps({"serverVersion": {"gitVersion": "v1.31.3"}})


def pod_state_json(phase: str, reason: str = "", message: str = "") -> str:
    """Build the JSON payload returned by `kubectl get pod -o json` for tests."""
    state: dict = {}
    if reason or message:
        waiting: dict = {}
        if reason:
            waiting["reason"] = reason
        if message:
            waiting["message"] = message
        state["waiting"] = waiting
    payload: dict = {"status": {"phase": phase}}
    if state:
        payload["status"]["containerStatuses"] = [{"state": state}]
    return json.dumps(payload)


PASSING_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Kubernetes e2e suite" tests="3" failures="0" skipped="1" time="120.5">
  <testcase name="[sig-api-machinery] ConfigMap create" classname="k8s.io/e2e" time="1.2"/>
  <testcase name="[sig-node] Pods basic" classname="k8s.io/e2e" time="3.4"/>
  <testcase name="[sig-storage] Disruptive CSI" classname="k8s.io/e2e" time="0">
    <skipped message="skipped: [Disruptive]"/>
  </testcase>
</testsuite>
"""

FAILING_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Kubernetes e2e suite" tests="2" failures="1" skipped="0" time="5.0">
  <testcase name="[sig-api-machinery] OK" time="1.0"/>
  <testcase name="[sig-node] FAILS" time="4.0">
    <failure message="expected 200 got 500">stack trace</failure>
  </testcase>
</testsuite>
"""

WRAPPED_JUNIT = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="A" tests="1" failures="0" skipped="0">
    <testcase name="[A] one" time="0.5"/>
  </testsuite>
  <testsuite name="B" tests="1" failures="0" skipped="1">
    <testcase name="[B] two" time="0">
      <skipped/>
    </testcase>
  </testsuite>
</testsuites>
"""


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.01)


def fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.01)


class CommandRouter:
    """Route runner.run calls to CommandResults by substring matching.

    Results can be a single CommandResult or a list. Lists are consumed in
    order; the last element is repeated once exhausted. First matching
    substring wins - put more specific patterns before general ones.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[str, CommandResult | list[CommandResult]]] = []
        self._list_index: dict[int, int] = {}
        self.seen: list[str] = []

    def add(self, substring: str, result: CommandResult | list[CommandResult]) -> None:
        self._routes.append((substring, result))

    def __call__(self, cmd: str, timeout: int | None = None) -> CommandResult:
        self.seen.append(cmd)
        for idx, (sub, res) in enumerate(self._routes):
            if sub in cmd:
                if isinstance(res, list):
                    i = self._list_index.get(idx, 0)
                    out = res[min(i, len(res) - 1)]
                    self._list_index[idx] = i + 1
                    return out
                return res
        msg = f"Unexpected command in router: {cmd}"
        raise AssertionError(msg)


class _VirtualClock:
    """Deterministic clock: time.time() returns a monotonically increasing counter
    advanced only by time.sleep() (patched to this clock's tick)."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


@pytest.fixture(autouse=True)
def _virtual_clock() -> Iterator[_VirtualClock]:
    clock = _VirtualClock()
    with (
        patch("isvtest.validations.k8s_conformance.time.time", clock.time),
        patch("isvtest.validations.k8s_conformance.time.sleep", clock.sleep),
    ):
        yield clock


@pytest.fixture(autouse=True)
def _kubectl_stub() -> Iterator[None]:
    with patch(
        "isvtest.core.k8s.get_kubectl_command",
        return_value=["kubectl"],
    ):
        yield


def _make_check(runner: MagicMock, config: dict | None = None) -> K8sCncfConformanceCheck:
    cfg = dict(config or {})
    cfg.setdefault("namespace", "conformance-test")
    cfg.setdefault("startup_timeout", 60)
    cfg.setdefault("timeout", 120)
    return K8sCncfConformanceCheck(runner=runner, config=cfg)


def _happy_router(
    overrides: dict[str, CommandResult | list[CommandResult]] | None = None,
) -> CommandRouter:
    """Build the happy-path router, optionally with per-substring overrides.

    Overrides are registered first; CommandRouter uses first-match-wins, so
    an overridden substring takes priority over the default happy route.
    """
    router = CommandRouter()
    for substring, result in (overrides or {}).items():
        router.add(substring, result)
    router.add("version -o json", ok(VERSION_JSON))
    router.add("apply", ok("pod/e2e-conformance created"))
    router.add("get pod", ok(pod_state_json("Running")))
    router.add("cat /tmp/results/done", ok("0"))
    router.add("cat /tmp/results/junit", ok(PASSING_JUNIT))
    router.add("delete namespace", ok())
    router.add("delete clusterrolebinding", ok())
    router.add("logs", ok("log tail"))
    router.add("get events", ok(""))
    return router


def _run_check(router: CommandRouter, config: dict | None = None) -> K8sCncfConformanceCheck:
    runner = MagicMock()
    runner.run.side_effect = router
    with patch("isvtest.validations.k8s_conformance.is_k8s_available", return_value=True):
        check = _make_check(runner, config)
        check.run()
    return check


class TestGuards:
    def test_cluster_unavailable(self) -> None:
        runner = MagicMock()
        with patch("isvtest.validations.k8s_conformance.is_k8s_available", return_value=False):
            check = _make_check(runner)
            check.run()
        assert not check.passed
        assert "Kubernetes cluster is not available" in check.message
        runner.run.assert_not_called()

    def test_invalid_mode(self) -> None:
        runner = MagicMock()
        with patch("isvtest.validations.k8s_conformance.is_k8s_available", return_value=True):
            check = _make_check(runner, {"mode": "bogus"})
            check.run()
        assert not check.passed
        assert "Invalid mode" in check.message


class TestVersionDetection:
    def _check_with_version_response(self, response: CommandResult) -> K8sCncfConformanceCheck:
        router = CommandRouter()
        router.add("version -o json", response)
        runner = MagicMock()
        runner.run.side_effect = router
        return _make_check(runner)

    def test_detects_from_kubectl_version(self) -> None:
        check = self._check_with_version_response(ok(VERSION_JSON))
        assert check._detect_cluster_version() == "v1.31.3"

    def test_strips_build_metadata(self) -> None:
        payload = json.dumps({"serverVersion": {"gitVersion": "v1.30.2+abc123"}})
        check = self._check_with_version_response(ok(payload))
        assert check._detect_cluster_version() == "v1.30.2"

    def test_returns_none_on_nonzero_exit(self) -> None:
        check = self._check_with_version_response(fail(stderr="boom"))
        assert check._detect_cluster_version() is None

    def test_returns_none_on_malformed_json(self) -> None:
        check = self._check_with_version_response(ok("not json"))
        assert check._detect_cluster_version() is None

    def test_returns_none_on_missing_gitversion(self) -> None:
        check = self._check_with_version_response(ok(json.dumps({"serverVersion": {}})))
        assert check._detect_cluster_version() is None

    def test_config_override_skips_detection(self) -> None:
        router = _happy_router()
        runner = MagicMock()
        runner.run.side_effect = router
        with patch("isvtest.validations.k8s_conformance.is_k8s_available", return_value=True):
            check = _make_check(runner, {"kubernetes_version": "v1.29.0"})
            check.run()
        assert check.passed
        assert not any("version -o json" in cmd for cmd in router.seen)

    def test_run_fails_when_version_undetectable(self) -> None:
        router = CommandRouter()
        router.add("version -o json", fail(stderr="no server"))
        runner = MagicMock()
        runner.run.side_effect = router
        with patch("isvtest.validations.k8s_conformance.is_k8s_available", return_value=True):
            check = _make_check(runner)
            check.run()
        assert not check.passed
        assert "Could not detect Kubernetes server version" in check.message


class TestRun:
    def test_happy_path(self) -> None:
        router = _happy_router()
        check = _run_check(router)

        assert check.passed
        assert "2/3 passed" in check.message  # 2 passed, 1 skipped, 0 failed
        # Cleanup fired
        assert any("delete namespace" in cmd for cmd in router.seen)
        assert any("delete clusterrolebinding" in cmd for cmd in router.seen)
        # Version was auto-detected and image pinned to server version
        assert any("version -o json" in cmd for cmd in router.seen)

    def test_failing_junit_sets_failed(self) -> None:
        router = _happy_router({"cat /tmp/results": ok(FAILING_JUNIT)})
        check = _run_check(router)

        assert not check.passed
        assert "1/2 passed" in check.message
        assert "1 failed" in check.message
        assert "[sig-node] FAILS" in check._output

    def test_apply_failure_skips_cleanup(self) -> None:
        router = _happy_router({"apply": fail(stderr="forbidden")})
        check = _run_check(router)

        assert not check.passed
        assert "Failed to apply conformance manifest" in check.message
        assert not any("delete " in cmd for cmd in router.seen)

    def test_pod_stuck_pending_times_out(self) -> None:
        router = _happy_router({"get pod": ok(pod_state_json("Pending"))})
        check = _run_check(router, {"startup_timeout": 30})

        assert not check.passed
        assert "did not reach Running state within 30s" in check.message
        # Cleanup still fired
        assert any("delete namespace" in cmd for cmd in router.seen)

    def test_image_pull_backoff_fails_fast(self) -> None:
        """ImagePullBackOff is terminal - fail without burning startup_timeout."""
        router = _happy_router(
            {
                "get pod": ok(
                    pod_state_json(
                        "Pending",
                        reason="ImagePullBackOff",
                        message="Back-off pulling image 'registry.k8s.io/conformance:v1.31.3'",
                    )
                ),
            }
        )
        check = _run_check(router, {"startup_timeout": 600})

        assert not check.passed
        assert "ImagePullBackOff" in check.message
        assert "Back-off pulling image" in check.message
        # Should not have waited for the full startup timeout.
        phase_polls = [cmd for cmd in router.seen if "get pod" in cmd]
        assert len(phase_polls) <= 2, f"Expected fast-fail, but polled phase {len(phase_polls)} times"

    def test_invalid_image_name_fails_fast(self) -> None:
        router = _happy_router(
            {
                "get pod": ok(
                    pod_state_json(
                        "Pending",
                        reason="InvalidImageName",
                        message="couldn't parse image reference",
                    )
                ),
            }
        )
        check = _run_check(router)

        assert not check.passed
        assert "InvalidImageName" in check.message

    def test_err_image_pull_transient_then_recovers(self) -> None:
        """A single ErrImagePull poll shouldn't fail - kubelet may succeed next attempt."""
        router = _happy_router(
            {
                # First poll: Pending with transient ErrImagePull. Second poll: Running.
                "get pod": [
                    ok(pod_state_json("Pending", reason="ErrImagePull", message="network blip")),
                    ok(pod_state_json("Running")),
                ],
            }
        )
        check = _run_check(router)

        assert check.passed

    def test_err_image_pull_persistent_fails_fast(self) -> None:
        """ErrImagePull across two consecutive polls should fail without waiting for timeout."""
        router = _happy_router(
            {
                "get pod": ok(pod_state_json("Pending", reason="ErrImagePull", message="manifest unknown")),
            }
        )
        check = _run_check(router, {"startup_timeout": 600})

        assert not check.passed
        assert "ErrImagePull" in check.message
        phase_polls = [cmd for cmd in router.seen if "get pod" in cmd]
        # Requires at least 2 polls to confirm persistence, but far fewer than timeout/poll_interval.
        assert 2 <= len(phase_polls) <= 3

    def test_pod_failed_during_startup(self) -> None:
        router = _happy_router({"get pod": ok(pod_state_json("Failed"))})
        check = _run_check(router)

        assert not check.passed
        assert "entered Failed phase during startup" in check.message

    def test_completion_timeout(self) -> None:
        router = _happy_router({"cat /tmp/results/done": fail()})  # done marker never appears
        check = _run_check(router, {"timeout": 90})

        assert not check.passed
        assert "did not finish within 90s" in check.message

    def test_pod_failed_during_completion(self) -> None:
        router = _happy_router(
            {
                "get pod": [ok(pod_state_json("Running")), ok(pod_state_json("Failed"))],
                "cat /tmp/results/done": fail(),
            }
        )
        check = _run_check(router)

        assert not check.passed
        assert "entered Failed phase before completion marker" in check.message

    def test_pod_deleted_during_completion_fails_fast(self) -> None:
        """If the pod is evicted/deleted mid-run, the harness must bail instead
        of polling until `timeout` elapses. Regression for the case where
        TaintManagerEviction removed the Pod and kubectl get returned NotFound,
        which was previously mapped to "Unknown" and silently retried."""
        router = _happy_router(
            {
                "get pod": [
                    ok(pod_state_json("Running")),
                    fail(stderr='Error from server (NotFound): pods "e2e-conformance" not found'),
                ],
                "cat /tmp/results/done": fail(),
                "logs": fail(stderr="pod not found"),
                "get events": ok("10m  Warning  TaintManagerEviction  pod/e2e-conformance  Marking for deletion"),
            }
        )
        check = _run_check(router, {"timeout": 7200})

        assert not check.passed
        assert "deleted before completion marker" in check.message
        # Must not have polled phase hundreds of times waiting out the timeout.
        phase_polls = [cmd for cmd in router.seen if "get pod" in cmd]
        assert len(phase_polls) <= 3, f"Expected fast-fail, but polled phase {len(phase_polls)} times"
        # Events fallback populated the output so evictions are visible in the report.
        assert "TaintManagerEviction" in check._output

    def test_pod_deleted_during_startup_fails_fast(self) -> None:
        router = _happy_router(
            {"get pod": fail(stderr='Error from server (NotFound): pods "e2e-conformance" not found')}
        )
        check = _run_check(router, {"startup_timeout": 600})

        assert not check.passed
        assert "deleted during startup" in check.message
        phase_polls = [cmd for cmd in router.seen if "get pod" in cmd]
        assert len(phase_polls) <= 2

    def test_junit_retrieval_failure(self) -> None:
        router = _happy_router({"cat /tmp/results/junit": fail(stderr="no such file")})
        check = _run_check(router)

        assert not check.passed
        assert "Could not retrieve" in check.message

    def test_malformed_junit_sets_failed(self) -> None:
        router = _happy_router({"cat /tmp/results/junit": ok("<<<not xml")})
        check = _run_check(router)

        assert not check.passed
        assert "no testcases parsed" in check.message

    def test_cleanup_disabled(self) -> None:
        router = _happy_router()
        check = _run_check(router, {"cleanup_namespace": False})

        assert check.passed
        assert not any("delete namespace" in cmd for cmd in router.seen)
        assert not any("delete clusterrolebinding" in cmd for cmd in router.seen)

    def test_report_individual_tests(self) -> None:
        check = _run_check(_happy_router())

        names = [r["name"] for r in check._subtest_results]
        assert "[sig-api-machinery] ConfigMap create" in names
        assert "[sig-node] Pods basic" in names
        assert "[sig-storage] Disruptive CSI" in names
        skipped = [r for r in check._subtest_results if r["skipped"]]
        assert len(skipped) == 1

    def test_report_individual_tests_disabled(self) -> None:
        check = _run_check(_happy_router(), {"report_individual_tests": False})

        assert check.passed
        assert check._subtest_results == []


def _render(
    namespace: str = "n",
    pod_name: str = "p",
    image: str = "img",
    env_vars: dict[str, str] | None = None,
    resources: dict[str, str] | None = None,
) -> str:
    check = K8sCncfConformanceCheck(runner=MagicMock(), config={})
    return check._render_manifest(
        namespace=namespace,
        pod_name=pod_name,
        image=image,
        env_vars=env_vars if env_vars is not None else _DEFAULT_ENV_VARS,
        resources=resources if resources is not None else K8sCncfConformanceCheck._DEFAULT_RESOURCES,
    )


class TestRenderManifest:
    def test_contains_namespace_and_image(self) -> None:
        manifest = _render(namespace="myns", pod_name="e2e-conformance", image="registry.k8s.io/conformance:v1.31.0")
        docs = {d["kind"]: d for d in yaml.safe_load_all(manifest)}
        assert docs["Namespace"]["metadata"]["name"] == "myns"
        assert docs["Pod"]["spec"]["containers"][0]["image"] == "registry.k8s.io/conformance:v1.31.0"
        assert docs["Pod"]["spec"]["serviceAccountName"] == "conformance"
        crb = docs["ClusterRoleBinding"]
        assert crb["metadata"]["name"] == "conformance-myns"
        assert crb["roleRef"]["name"] == "cluster-admin"

    def test_env_vars_parse_under_container(self) -> None:
        """Regression: env list must nest under the container, not be sibling containers.

        A previous rendering used 4-space indentation for env entries, which made
        the YAML parser treat them as additional entries in the outer `containers`
        list - causing kubectl to reject the Pod with
        `unknown field "spec.containers[1].value"`.
        """
        docs = list(yaml.safe_load_all(_render()))
        pod = next(d for d in docs if d.get("kind") == "Pod")
        containers = pod["spec"]["containers"]
        assert len(containers) == 1, f"Expected 1 container, got {len(containers)}: {containers}"
        env = {e["name"]: e["value"] for e in containers[0]["env"]}
        assert env["E2E_FOCUS"] == r"\[Conformance\]"
        assert env["E2E_PROVIDER"] == "skeleton"

    def test_wrapper_command_keeps_pod_alive_after_e2e(self) -> None:
        """The image entrypoint exits when the suite finishes; the Pod would
        then transition to Succeeded and kubectl exec could no longer retrieve
        the JUnit. Verify the container command wraps run_e2e.sh so the done
        marker is written and the container sleeps after completion.
        """
        docs = list(yaml.safe_load_all(_render()))
        pod = next(d for d in docs if d.get("kind") == "Pod")
        container = pod["spec"]["containers"][0]
        assert container["command"] == ["/bin/sh", "-c"]
        script = container["args"][0]
        assert "/run_e2e.sh" in script
        assert "/tmp/results/done" in script
        assert "sleep infinity" in script

    def test_pod_has_resource_requests_and_limits(self) -> None:
        """A BestEffort pod (no requests) is the first kubelet evicts under
        node pressure, which kills long conformance runs. Verify defaults
        render a Burstable-QoS pod with ephemeral-storage accounting.
        """
        docs = list(yaml.safe_load_all(_render()))
        pod = next(d for d in docs if d.get("kind") == "Pod")
        resources = pod["spec"]["containers"][0]["resources"]
        assert resources["requests"]["cpu"] == "500m"
        assert resources["requests"]["memory"] == "1Gi"
        assert resources["requests"]["ephemeral-storage"] == "2Gi"
        assert resources["limits"]["memory"] == "4Gi"
        assert resources["limits"]["ephemeral-storage"] == "10Gi"
        # CPU limit intentionally absent - CFS throttling causes e2e test timeouts.
        assert "cpu" not in resources["limits"]

    def test_pod_tolerates_e2e_evict_taint(self) -> None:
        """The k8s e2e suite applies ``kubernetes.io/e2e-evict-taint-key`` during
        eviction/NodeLifecycle tests - without this toleration the conformance
        pod itself can be evicted by the suite it's running. Matches sonobuoy.
        """
        docs = list(yaml.safe_load_all(_render()))
        pod = next(d for d in docs if d.get("kind") == "Pod")
        toleration_keys = {t["key"] for t in pod["spec"]["tolerations"]}
        assert "kubernetes.io/e2e-evict-taint-key" in toleration_keys
        assert "CriticalAddonsOnly" in toleration_keys
        # Both the legacy `master` key and the post-1.24 `control-plane` key -
        # running this against clusters spanning that rename must not pin the
        # pod onto a control-plane node either way.
        assert "node-role.kubernetes.io/control-plane" in toleration_keys
        assert pod["spec"]["nodeSelector"] == {"kubernetes.io/os": "linux"}

    def test_resources_are_configurable(self) -> None:
        manifest = _render(
            resources={
                "cpu_request": "1",
                "memory_request": "2Gi",
                "memory_limit": "8Gi",
                "ephemeral_storage_request": "4Gi",
                "ephemeral_storage_limit": "20Gi",
            },
        )
        docs = list(yaml.safe_load_all(manifest))
        pod = next(d for d in docs if d.get("kind") == "Pod")
        resources = pod["spec"]["containers"][0]["resources"]
        assert resources["requests"]["cpu"] == "1"
        assert resources["requests"]["memory"] == "2Gi"
        assert resources["requests"]["ephemeral-storage"] == "4Gi"
        assert resources["limits"]["memory"] == "8Gi"
        assert resources["limits"]["ephemeral-storage"] == "20Gi"

    def test_yaml_special_chars_in_env_values_round_trip(self) -> None:
        # Focus/skip regexes contain backslashes, brackets, pipes, and quotes -
        # all YAML-special. Verify the serializer quotes them such that parsing
        # yields the original string verbatim.
        tricky = {
            "E2E_FOCUS": r"\[Conformance\]",
            "E2E_SKIP": r'\[Disruptive\]|\[Serial\]|has "quotes"',
            "E2E_PROVIDER": "skeleton",
        }
        docs = list(yaml.safe_load_all(_render(env_vars=tricky)))
        pod = next(d for d in docs if d.get("kind") == "Pod")
        env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
        assert env == tricky


class TestParseJunit:
    def _check(self) -> K8sCncfConformanceCheck:
        return K8sCncfConformanceCheck(runner=MagicMock(), config={})

    def test_single_testsuite_passing(self) -> None:
        summary = self._check()._parse_junit(PASSING_JUNIT)
        assert summary.total == 3
        assert summary.passed == 2
        assert summary.failed == 0
        assert summary.skipped == 1
        assert summary.cases[0].duration == 1.2

    def test_single_testsuite_with_failure(self) -> None:
        summary = self._check()._parse_junit(FAILING_JUNIT)
        assert summary.failed == 1
        assert summary.passed == 1
        assert summary.cases[1].message == "expected 200 got 500"

    def test_testsuites_wrapper(self) -> None:
        summary = self._check()._parse_junit(WRAPPED_JUNIT)
        assert summary.total == 2
        assert summary.passed == 1
        assert summary.skipped == 1

    def test_error_element_treated_as_failure(self) -> None:
        xml = """<testsuite>
          <testcase name="x"><error message="boom"/></testcase>
        </testsuite>"""
        summary = self._check()._parse_junit(xml)
        assert summary.failed == 1
        assert summary.cases[0].message == "boom"

    def test_malformed_returns_empty_summary(self) -> None:
        summary = self._check()._parse_junit("<<<")
        assert summary.total == 0
        assert summary.cases == []

    def test_unnamed_testcase(self) -> None:
        xml = "<testsuite><testcase/></testsuite>"
        summary = self._check()._parse_junit(xml)
        assert summary.cases[0].name == "(unnamed)"


class TestParseDuration:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1.5", 1.5),
            ("0", 0.0),
            ("", None),
            (None, None),
            ("not-a-number", None),
        ],
    )
    def test_parses_expected_values(self, value: str | None, expected: float | None) -> None:
        assert _parse_duration(value) == expected
