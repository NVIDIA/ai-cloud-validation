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

"""Tests for Kubernetes utility functions (KUBECTL override)."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from isvtest.core.k8s import (
    TERMINAL_WAITING_REASONS,
    TRANSIENT_WAITING_REASONS,
    KubectlParseError,
    ensure_incluster_kubeconfig,
    get_k8s_provider,
    get_kubectl_base_shell,
    get_kubectl_command,
    is_k8s_available,
    kubectl_items_or_fail,
    node_is_ready,
    parse_kubectl_json,
    parse_kubectl_json_items,
    parse_pod_state,
    parse_server_version,
    pod_is_ready,
    pod_kubectl_status,
    pod_state_from_result,
    pod_status_reason,
    wait_for_multiple_pods_completion,
)
from isvtest.core.runners import CommandResult


def _command_result(stdout: str, *, exit_code: int = 0, stderr: str = "") -> CommandResult:
    """Return a command result for parser tests."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


class _StubValidation:
    """Minimal stand-in exposing ``set_failed`` for parser-helper tests."""

    def __init__(self) -> None:
        """Initialize the object with its configured dependencies."""
        self.error: str | None = None

    def set_failed(self, error: str, output: str = "") -> None:
        """Handle set failed."""
        self.error = error


class TestGetKubectlCommandOverride:
    """Tests for KUBECTL environment variable override in get_kubectl_command()."""

    def test_unset_defaults_to_provider_detection(self) -> None:
        """Unset KUBECTL falls through to K8S_PROVIDER / auto-detection."""
        env = {"K8S_PROVIDER": "kubectl"}
        with patch.dict(os.environ, env, clear=True):
            get_k8s_provider.cache_clear()
            result = get_kubectl_command()
        assert result == ["kubectl"]

    def test_simple_override(self) -> None:
        """KUBECTL=oc returns ["oc"]."""
        with (
            patch.dict(os.environ, {"KUBECTL": "oc"}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/usr/bin/oc"),
        ):
            result = get_kubectl_command()
        assert result == ["oc"]

    def test_override_with_leading_trailing_whitespace(self) -> None:
        """KUBECTL="  oc  " strips whitespace and returns ["oc"]."""
        with (
            patch.dict(os.environ, {"KUBECTL": "  oc  "}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/usr/bin/oc"),
        ):
            result = get_kubectl_command()
        assert result == ["oc"]

    def test_multi_token_prefix(self) -> None:
        """KUBECTL="microk8s kubectl" returns ["microk8s", "kubectl"]."""
        with (
            patch.dict(os.environ, {"KUBECTL": "microk8s kubectl"}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/snap/bin/microk8s"),
        ):
            result = get_kubectl_command()
        assert result == ["microk8s", "kubectl"]

    def test_quoted_path_with_spaces(self) -> None:
        """KUBECTL with a quoted path containing spaces is handled by shlex."""
        with (
            patch.dict(os.environ, {"KUBECTL": '"/tmp/with space/oc"'}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/tmp/with space/oc"),
        ):
            result = get_kubectl_command()
        assert result == ["/tmp/with space/oc"]

    def test_precedence_over_k8s_provider(self) -> None:
        """KUBECTL takes precedence over K8S_PROVIDER."""
        env = {"KUBECTL": "oc", "K8S_PROVIDER": "microk8s"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/usr/bin/oc"),
        ):
            get_k8s_provider.cache_clear()
            result = get_kubectl_command()
        assert result == ["oc"]

    def test_empty_string_falls_through(self) -> None:
        """KUBECTL="" falls through to K8S_PROVIDER detection."""
        env = {"KUBECTL": "", "K8S_PROVIDER": "kubectl"}
        with patch.dict(os.environ, env, clear=True):
            get_k8s_provider.cache_clear()
            result = get_kubectl_command()
        assert result == ["kubectl"]

    def test_whitespace_only_falls_through(self) -> None:
        """KUBECTL="   \\t  " falls through to K8S_PROVIDER detection."""
        env = {"KUBECTL": "   \t  ", "K8S_PROVIDER": "kubectl"}
        with patch.dict(os.environ, env, clear=True):
            get_k8s_provider.cache_clear()
            result = get_kubectl_command()
        assert result == ["kubectl"]

    def test_empty_quoted_value_falls_through(self) -> None:
        """KUBECTL='""' yields [""] from shlex.split; treated as invalid, falls through."""
        env = {"KUBECTL": '""', "K8S_PROVIDER": "kubectl"}
        with patch.dict(os.environ, env, clear=True):
            get_k8s_provider.cache_clear()
            result = get_kubectl_command()
        assert result == ["kubectl"]

    def test_get_kubectl_base_shell_round_trip(self) -> None:
        """get_kubectl_base_shell() returns shell-safe string from KUBECTL override."""
        with (
            patch.dict(os.environ, {"KUBECTL": "microk8s kubectl"}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/snap/bin/microk8s"),
        ):
            result = get_kubectl_base_shell()
        assert result == "microk8s kubectl"

    def test_binary_not_on_path_raises(self) -> None:
        """KUBECTL=nonexistent raises FileNotFoundError with clear message."""
        with (
            patch.dict(os.environ, {"KUBECTL": "nonexistent"}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value=None),
        ):
            with pytest.raises(FileNotFoundError, match="not found on PATH"):
                get_kubectl_command()


class TestEnsureInclusterKubeconfig:
    """Tests for the in-cluster kubeconfig bootstrap."""

    @staticmethod
    def _sa_dir(tmp_path: Path) -> Path:
        """Build a stand-in for the projected ServiceAccount mount."""
        sa = tmp_path / "serviceaccount"
        sa.mkdir()
        (sa / "token").write_text("sa-token-value")
        (sa / "ca.crt").write_text("ca-pem")
        (sa / "namespace").write_text("storage-system\n")
        return sa

    def _run(self, sa_dir: Path, env: dict[str, str], home: Path) -> str | None:
        """Invoke the bootstrap with a patched SA mount and HOME."""
        ensure_incluster_kubeconfig.cache_clear()
        with (
            patch.dict(os.environ, {**env, "HOME": str(home)}, clear=True),
            patch("isvtest.core.k8s._INCLUSTER_SA_DIR", sa_dir),
        ):
            path = ensure_incluster_kubeconfig()
            self._kubeconfig_env = os.environ.get("KUBECONFIG")
        return path

    def test_generates_kubeconfig_in_pod(self, tmp_path: Path) -> None:
        """In a pod with no kubeconfig, one is written and KUBECONFIG points at it."""
        sa = self._sa_dir(tmp_path)
        path = self._run(
            sa,
            {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443"},
            tmp_path / "home",
        )

        assert path is not None
        assert self._kubeconfig_env == path
        cfg = yaml.safe_load(Path(path).read_text())
        assert cfg["clusters"][0]["cluster"]["server"] == "https://10.0.0.1:443"
        assert cfg["clusters"][0]["cluster"]["certificate-authority"] == str(sa / "ca.crt")
        assert cfg["contexts"][0]["context"]["namespace"] == "storage-system"

    def test_references_token_by_path_not_value(self, tmp_path: Path) -> None:
        """The token is referenced via tokenFile so it neither leaks nor goes stale."""
        sa = self._sa_dir(tmp_path)
        path = self._run(sa, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, tmp_path / "home")

        assert path is not None
        raw = Path(path).read_text()
        assert "sa-token-value" not in raw
        cfg = yaml.safe_load(raw)
        assert cfg["users"][0]["user"]["tokenFile"] == str(sa / "token")

    def test_defaults_port_when_unset(self, tmp_path: Path) -> None:
        """A pod exposing only KUBERNETES_SERVICE_HOST still gets a usable server URL."""
        sa = self._sa_dir(tmp_path)
        path = self._run(sa, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, tmp_path / "home")

        assert path is not None
        cfg = yaml.safe_load(Path(path).read_text())
        assert cfg["clusters"][0]["cluster"]["server"] == "https://10.0.0.1:443"

    def test_brackets_ipv6_host(self, tmp_path: Path) -> None:
        """On an IPv6 cluster the bare literal must be bracketed to form a valid URL."""
        sa = self._sa_dir(tmp_path)
        path = self._run(
            sa,
            {"KUBERNETES_SERVICE_HOST": "fd00::1", "KUBERNETES_SERVICE_PORT": "443"},
            tmp_path / "home",
        )

        assert path is not None
        cfg = yaml.safe_load(Path(path).read_text())
        assert cfg["clusters"][0]["cluster"]["server"] == "https://[fd00::1]:443"

    def test_existing_kubeconfig_env_wins(self, tmp_path: Path) -> None:
        """An explicit KUBECONFIG is never overwritten."""
        sa = self._sa_dir(tmp_path)
        path = self._run(
            sa,
            {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBECONFIG": "/explicit/config"},
            tmp_path / "home",
        )

        assert path is None
        assert self._kubeconfig_env == "/explicit/config"

    def test_existing_home_kubeconfig_wins(self, tmp_path: Path) -> None:
        """A ~/.kube/config on disk takes precedence over the generated one."""
        home = tmp_path / "home"
        (home / ".kube").mkdir(parents=True)
        (home / ".kube" / "config").write_text("apiVersion: v1\n")
        sa = self._sa_dir(tmp_path)

        path = self._run(sa, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, home)

        assert path is None
        assert self._kubeconfig_env is None

    def test_noop_outside_cluster(self, tmp_path: Path) -> None:
        """Off-cluster (no KUBERNETES_SERVICE_HOST) the bootstrap does nothing."""
        sa = self._sa_dir(tmp_path)
        path = self._run(sa, {}, tmp_path / "home")

        assert path is None
        assert self._kubeconfig_env is None

    def test_noop_without_serviceaccount_mount(self, tmp_path: Path) -> None:
        """The env vars alone are not enough; the token/CA must actually be mounted."""
        path = self._run(tmp_path / "absent", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, tmp_path / "home")

        assert path is None
        assert self._kubeconfig_env is None


class TestIsK8sAvailable:
    """Tests for the cluster-reachability probe."""

    @staticmethod
    def _completed(returncode: int) -> subprocess.CompletedProcess[str]:
        """Build a minimal CompletedProcess for kubectl probe tests."""
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")

    def test_probes_version_endpoint_not_cluster_info(self) -> None:
        """Reachability must use ``/version``, not ``cluster-info`` (RBAC)."""
        with patch("isvtest.core.k8s.run_kubectl", return_value=self._completed(0)) as run:
            assert is_k8s_available() is True
        args = run.call_args[0][0]
        assert args == ["get", "--raw", "/version"]
        assert "cluster-info" not in args

    def test_false_when_unreachable(self) -> None:
        """Verify false when unreachable."""
        with patch("isvtest.core.k8s.run_kubectl", return_value=self._completed(1)):
            assert is_k8s_available() is False

    def test_false_when_kubectl_missing(self) -> None:
        """Verify false when kubectl missing."""
        with patch("isvtest.core.k8s.run_kubectl", side_effect=FileNotFoundError):
            assert is_k8s_available() is False

    def test_false_on_timeout(self) -> None:
        """Verify false on timeout."""
        with patch("isvtest.core.k8s.run_kubectl", side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=10)):
            assert is_k8s_available() is False


class TestGetKubectlBaseShellArgs:
    """Tests for get_kubectl_base_shell() args composition."""

    def test_composes_args_with_quoting(self) -> None:
        """Verify composes args with quoting."""
        with (
            patch.dict(os.environ, {"KUBECTL": "kubectl"}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/usr/bin/kubectl"),
        ):
            result = get_kubectl_base_shell("get", "pod", "my-pod", "-n", "default")
        assert result == "kubectl get pod my-pod -n default"

    def test_quotes_arg_with_spaces(self) -> None:
        """Verify quotes arg with spaces."""
        with (
            patch.dict(os.environ, {"KUBECTL": "kubectl"}, clear=True),
            patch("isvtest.core.k8s.shutil.which", return_value="/usr/bin/kubectl"),
        ):
            result = get_kubectl_base_shell("label", "node", "n1", "app=foo bar")
        # The value with a space must be quoted so the shell sees it as one token.
        assert "'app=foo bar'" in result


class TestKubectlJsonParsers:
    """Tests for structured kubectl JSON parser helpers."""

    def test_parse_kubectl_json_object(self) -> None:
        """Verify parse kubectl json object."""
        payload = parse_kubectl_json(_command_result(json.dumps({"kind": "Pod"})), "pod")
        assert payload == {"kind": "Pod"}

    def test_parse_kubectl_json_reports_invalid_json(self) -> None:
        """Verify parse kubectl json reports invalid json."""
        with pytest.raises(KubectlParseError, match="Failed to parse pod"):
            parse_kubectl_json(_command_result("not-json"), "pod")

    def test_parse_kubectl_json_items_extracts_items(self) -> None:
        """Verify parse kubectl json items extracts items."""
        payload = json.dumps({"items": [{"metadata": {"name": "n1"}}]})
        items = parse_kubectl_json_items(_command_result(payload), "node list")
        assert items == [{"metadata": {"name": "n1"}}]

    def test_parse_kubectl_json_items_requires_items_list(self) -> None:
        """Verify parse kubectl json items requires items list."""
        with pytest.raises(KubectlParseError, match="expected 'items' list"):
            parse_kubectl_json_items(_command_result(json.dumps({"items": {}})), "node list")


class TestKubectlItemsOrFail:
    """Tests for the validation-aware ``kubectl_items_or_fail`` helper."""

    def test_returns_items_on_success(self) -> None:
        """Verify returns items on success."""
        validation = _StubValidation()
        payload = json.dumps({"items": [{"metadata": {"name": "n1"}}]})
        items = kubectl_items_or_fail(validation, _command_result(payload), "node list")
        assert items == [{"metadata": {"name": "n1"}}]
        assert validation.error is None

    def test_routes_exec_failure_to_set_failed(self) -> None:
        """Verify routes exec failure to set failed."""
        validation = _StubValidation()
        result = _command_result("", exit_code=1, stderr="cluster unavailable")
        items = kubectl_items_or_fail(validation, result, "node list")
        assert items is None
        assert validation.error == "Failed to get node list: cluster unavailable"

    def test_routes_parse_failure_to_set_failed(self) -> None:
        """Verify routes parse failure to set failed."""
        validation = _StubValidation()
        items = kubectl_items_or_fail(validation, _command_result("not-json"), "node list")
        assert items is None
        assert validation.error is not None
        assert "Failed to parse node list" in validation.error


class TestPodStatusReason:
    """Tests for ``pod_status_reason`` kubectl STATUS-column emulation."""

    def test_container_waiting_reason_wins_over_phase(self) -> None:
        """Verify container waiting reason wins over phase."""
        pod = {
            "status": {
                "phase": "Pending",
                "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
            }
        }
        assert pod_status_reason(pod) == "ImagePullBackOff"

    def test_pod_level_reason_overrides_phase_when_no_container_state(self) -> None:
        # Regression: evicted pods carry ``status.reason: Evicted`` but no
        # informative container state; kubectl shows "Evicted" in STATUS so
        # ``error_states: [Evicted]`` configs must still match.
        """Verify pod level reason overrides phase when no container state."""
        pod = {"status": {"phase": "Failed", "reason": "Evicted"}}
        assert pod_status_reason(pod) == "Evicted"

    def test_falls_back_to_phase_when_reason_absent(self) -> None:
        """Verify falls back to phase when reason absent."""
        pod = {"status": {"phase": "Running"}}
        assert pod_status_reason(pod) == "Running"

    def test_returns_unknown_when_phase_missing(self) -> None:
        """Verify returns unknown when phase missing."""
        assert pod_status_reason({}) == "Unknown"


class TestPodKubectlStatus:
    """Tests for ``pod_kubectl_status`` Init:/Terminating/Unknown wording."""

    def test_prefixes_init_container_failures(self) -> None:
        """Verify an init-container failure is prefixed with ``Init:``."""
        pod = {
            "status": {
                "phase": "Pending",
                "initContainerStatuses": [
                    {"state": {"terminated": {"reason": "Completed"}}},
                    {"state": {"waiting": {"reason": "CrashLoopBackOff"}}},
                ],
            }
        }
        assert pod_kubectl_status(pod) == "Init:CrashLoopBackOff"

    def test_main_container_reason_is_unprefixed(self) -> None:
        """Verify a main-container reason is returned as-is once init containers completed."""
        pod = {
            "status": {
                "phase": "Running",
                "initContainerStatuses": [{"state": {"terminated": {"reason": "Completed"}}}],
                "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}],
            }
        }
        assert pod_kubectl_status(pod) == "CrashLoopBackOff"

    @pytest.mark.parametrize(("status_reason", "expected"), [("NodeLost", "Unknown"), (None, "Terminating")])
    def test_deleted_pods_report_terminating_or_unknown(self, status_reason: str | None, expected: str) -> None:
        """Verify a pod marked for deletion is not labelled by its stale container state."""
        status: dict[str, Any] = {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}
        if status_reason:
            status["reason"] = status_reason
        pod = {"metadata": {"deletionTimestamp": "2026-08-13T00:00:00Z"}, "status": status}
        assert pod_kubectl_status(pod) == expected


class TestPodIsReady:
    """Tests for ``pod_is_ready``."""

    @pytest.mark.parametrize(
        ("conditions", "expected"),
        [
            ([{"type": "Ready", "status": "True"}], True),
            ([{"type": "Ready", "status": "False"}], False),
            ([{"type": "PodScheduled", "status": "True"}], False),
            ([], False),
        ],
    )
    def test_reads_ready_condition(self, conditions: list[dict[str, str]], expected: bool) -> None:
        """Verify only a ``Ready=True`` condition counts as ready."""
        assert pod_is_ready({"status": {"conditions": conditions}}) is expected

    def test_missing_status_is_not_ready(self) -> None:
        """Verify a pod without status is not ready."""
        assert pod_is_ready({}) is False


class TestNodeIsReady:
    """Tests for ``node_is_ready``."""

    @pytest.mark.parametrize(
        ("conditions", "expected"),
        [
            ([{"type": "Ready", "status": "True"}], True),
            ([{"type": "Ready", "status": "False"}], False),
            ([{"type": "Ready", "status": "Unknown"}], False),
            ([], False),
        ],
    )
    def test_reads_ready_condition(self, conditions: list[dict[str, str]], expected: bool) -> None:
        """Verify only a ``Ready=True`` condition counts as ready."""
        assert node_is_ready({"status": {"conditions": conditions}}) is expected

    def test_missing_status_is_not_ready(self) -> None:
        """Verify a node without status is not ready."""
        assert node_is_ready({}) is False


class TestPodStateFromResult:
    """Tests for the result-aware ``pod_state_from_result`` wrapper."""

    def test_parses_command_result_stdout_on_success(self) -> None:
        """Verify parses command result stdout on success."""
        payload = json.dumps({"status": {"phase": "Running"}})
        assert pod_state_from_result(_command_result(payload)) == ("Running", "", "")

    def test_inspects_stderr_on_command_result_failure(self) -> None:
        """Verify inspects stderr on command result failure."""
        result = _command_result("", exit_code=1, stderr='Error from server (NotFound): pods "x" not found')
        assert pod_state_from_result(result) == ("NotFound", "", "")

    def test_accepts_completed_process(self) -> None:
        """Verify accepts completed process."""
        payload = json.dumps({"status": {"phase": "Succeeded"}})
        completed = subprocess.CompletedProcess(args=["kubectl"], returncode=0, stdout=payload, stderr="")
        assert pod_state_from_result(completed) == ("Succeeded", "", "")

    def test_completed_process_failure_uses_stderr(self) -> None:
        """Verify completed process failure uses stderr."""
        completed = subprocess.CompletedProcess(args=["kubectl"], returncode=1, stdout="", stderr="boom")
        assert pod_state_from_result(completed) == ("Unknown", "", "")


class TestWaitForMultiplePodsCompletion:
    """Tests for WaitForMultiplePodsCompletion."""

    def test_duplicate_targets_complete_after_unique_pods_finish(self) -> None:
        """Verify duplicate targets complete after unique pods finish."""
        payload = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "worker-0"},
                        "status": {"phase": "Succeeded"},
                    }
                ]
            }
        )
        completed = subprocess.CompletedProcess(args=["kubectl"], returncode=0, stdout=payload, stderr="")

        with (
            patch("isvtest.core.k8s.run_kubectl", return_value=completed),
            patch("isvtest.core.k8s.time.time", side_effect=[0.0, 0.1]),
            patch("isvtest.core.k8s.time.sleep") as sleep,
        ):
            result = wait_for_multiple_pods_completion(["worker-0", "worker-0"], "default", timeout=10)

        assert result == {"worker-0": (True, "Succeeded")}
        sleep.assert_not_called()


class TestParsePodState:
    """Tests for ParsePodState."""

    def test_running_pod(self) -> None:
        """Verify running pod."""
        payload = json.dumps({"status": {"phase": "Running"}})
        assert parse_pod_state(payload, "") == ("Running", "", "")

    def test_pending_with_waiting_reason(self) -> None:
        """Verify pending with waiting reason."""
        payload = json.dumps(
            {
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [
                        {"state": {"waiting": {"reason": "ImagePullBackOff", "message": "back-off"}}}
                    ],
                }
            }
        )
        phase, reason, msg = parse_pod_state(payload, "")
        assert phase == "Pending"
        assert reason == "ImagePullBackOff"
        assert msg == "back-off"

    def test_notfound_from_stderr(self) -> None:
        """Verify notfound from stderr."""
        stderr = 'Error from server (NotFound): pods "my-pod" not found'
        assert parse_pod_state("", stderr) == ("NotFound", "", "")

    def test_unknown_on_generic_failure(self) -> None:
        """Verify unknown on generic failure."""
        assert parse_pod_state("", "connection refused") == ("Unknown", "", "")

    def test_unknown_on_malformed_json(self) -> None:
        """Verify unknown on malformed json."""
        assert parse_pod_state("not json", "") == ("Unknown", "", "")

    def test_missing_container_statuses(self) -> None:
        """Verify missing container statuses."""
        payload = json.dumps({"status": {"phase": "Pending"}})
        assert parse_pod_state(payload, "") == ("Pending", "", "")


class TestParseServerVersion:
    """Tests for ParseServerVersion."""

    def test_strips_build_metadata(self) -> None:
        """Verify strips build metadata."""
        assert parse_server_version(json.dumps({"serverVersion": {"gitVersion": "v1.30.2+abc"}})) == "v1.30.2"

    def test_plain_git_version(self) -> None:
        """Verify plain git version."""
        assert parse_server_version(json.dumps({"serverVersion": {"gitVersion": "v1.31.3"}})) == "v1.31.3"

    def test_missing_server_version(self) -> None:
        """Verify missing server version."""
        assert parse_server_version(json.dumps({})) is None

    def test_malformed_json(self) -> None:
        """Verify malformed json."""
        assert parse_server_version("not json") is None

    def test_unexpected_format(self) -> None:
        """Verify unexpected format."""
        assert parse_server_version(json.dumps({"serverVersion": {"gitVersion": "1.x.y"}})) is None


class TestWaitingReasonConstants:
    """Tests for WaitingReasonConstants."""

    def test_terminal_reasons_are_frozen(self) -> None:
        """Verify terminal reasons are frozen."""
        assert "ImagePullBackOff" in TERMINAL_WAITING_REASONS
        assert isinstance(TERMINAL_WAITING_REASONS, frozenset)

    def test_transient_reasons_are_frozen(self) -> None:
        """Verify transient reasons are frozen."""
        assert "ErrImagePull" in TRANSIENT_WAITING_REASONS
        assert isinstance(TRANSIENT_WAITING_REASONS, frozenset)

    def test_terminal_and_transient_are_disjoint(self) -> None:
        """Verify terminal and transient are disjoint."""
        assert TERMINAL_WAITING_REASONS.isdisjoint(TRANSIENT_WAITING_REASONS)
