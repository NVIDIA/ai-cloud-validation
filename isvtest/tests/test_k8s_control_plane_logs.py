# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Unit tests for ``isvtest.validations.k8s_control_plane_logs``."""

from __future__ import annotations

import json
from typing import Any

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_control_plane_logs import (
    K8sControlPlaneLogsCheck,
    _count_nonempty_lines,
)


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult`` (exit code 0) with the given output streams."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failing ``CommandResult`` with the given non-zero exit code and output streams."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _pod_list(*pairs: tuple[str, str]) -> str:
    """Simulate ``kubectl get pods -o json`` output for pod/component pairs."""
    items = []
    for pod, label in pairs:
        labels = {"component": label} if label else {}
        items.append({"metadata": {"name": pod, "labels": labels}})
    return json.dumps({"items": items})


class TestCountNonemptyLines:
    """Unit tests for the ``_count_nonempty_lines`` helper."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("\n", 0),
            ("   \n   ", 0),
            ("one", 1),
            ("one\ntwo", 2),
            ("one\n\nthree\n", 2),
            ("  one  \n\n  two  ", 2),
        ],
    )
    def test_counts(self, text: str, expected: int) -> None:
        assert _count_nonempty_lines(text) == expected


class TestInputValidation:
    """Config-level input validation: reject malformed ``mode``, ``components``, ``commands``, ``tail`` and ``min_log_lines`` up front with clear errors."""

    def test_invalid_mode_fails(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "bogus"})
        check.run()
        assert not check.passed
        assert "Invalid mode" in check.message

    def test_empty_components_fails(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"components": []})
        check.run()
        assert not check.passed
        assert "components list is empty" in check.message

    def test_scalar_components_rejected_with_friendly_error(self) -> None:
        """A YAML scalar (e.g. `components: kube-apiserver`) must not be
        silently iterated character-by-character."""
        check = K8sControlPlaneLogsCheck(config={"components": "kube-apiserver"})
        check.run()
        assert not check.passed
        assert "`components` must be a YAML list" in check.message

    def test_non_dict_commands_rejected_with_friendly_error(self) -> None:
        """`commands` as a scalar must be rejected up front rather than
        crashing downstream on `.get()`."""
        check = K8sControlPlaneLogsCheck(
            config={"mode": "command", "components": ["kube-apiserver"], "commands": "echo hi"}
        )
        check.run()
        assert not check.passed
        assert "`commands` must be a mapping" in check.message

    def test_non_integer_tail_rejected_with_friendly_error(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"tail": "twenty"})
        check.run()
        assert not check.passed
        assert "`tail` must be an integer" in check.message

    def test_non_integer_min_log_lines_rejected_with_friendly_error(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"min_log_lines": "one"})
        check.run()
        assert not check.passed
        assert "`min_log_lines` must be an integer" in check.message

    def test_min_log_lines_over_tail_rejected_for_kubectl_modes(self) -> None:
        """Structurally unsatisfiable kubectl config must fail up front."""
        check = K8sControlPlaneLogsCheck(config={"mode": "kubectl", "tail": 5, "min_log_lines": 10})
        check.run()
        assert not check.passed
        assert "cannot exceed `tail`" in check.message

    def test_min_log_lines_over_tail_allowed_for_command_mode(self) -> None:
        """`tail` is kubectl-only; command mode must not enforce the bound."""
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "command",
                "components": ["kube-apiserver"],
                "commands": {"kube-apiserver": "echo hi"},
                "tail": 5,
                "min_log_lines": 100,
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            return _ok(stdout="\n".join(f"line{i}" for i in range(200)) + "\n")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message


class TestAutoDispatch:
    """``mode: auto`` dispatch: route each component to kubectl or command based on pod discovery, and surface actionable errors when neither path can serve it."""

    def test_auto_hard_fails_when_no_pods_and_no_commands(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "auto", "components": ["kube-apiserver"]})

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _ok(stdout=_pod_list())
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "no `commands` entries configured for component(s)" in check.message
        assert "kube-apiserver" in check.message

    def test_auto_surfaces_kubectl_error_when_probe_fails(self) -> None:
        """When ``kubectl get pods`` itself errors, the failure must blame
        cluster access rather than a missing ``commands`` mapping."""
        check = K8sControlPlaneLogsCheck(config={"mode": "auto", "components": ["kube-apiserver"]})

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _fail(stderr="Unable to connect to the server: dial tcp: i/o timeout")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "Unable to list pods" in check.message
        assert "i/o timeout" in check.message
        assert "no `commands` entries configured" not in check.message

    def test_auto_surfaces_invalid_pod_json(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "auto", "components": ["kube-apiserver"]})

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _ok(stdout="not-json")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "Failed to parse pod list" in check.message

    def test_auto_surfaces_kubectl_error_even_when_commands_cover_all_components(self) -> None:
        """A probe failure (kubeconfig/RBAC/context broken) must not be
        masked by falling through to commands for every component - the
        operator needs to know cluster access is broken."""
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "auto",
                "components": ["kube-apiserver"],
                "commands": {"kube-apiserver": "echo hi"},
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _fail(stderr="Unable to connect to the server: dial tcp: i/o timeout")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "Unable to list pods" in check.message
        assert "i/o timeout" in check.message

    def test_auto_picks_kubectl_when_pods_present(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "auto", "components": ["kube-apiserver"]})
        run_commands: list[str] = []

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            run_commands.append(cmd)
            if "get pods" in cmd:
                return _ok(stdout=_pod_list(("kube-apiserver-node1", "kube-apiserver")))
            if "logs " in cmd:
                return _ok(stdout="apiserver started\nserving HTTPS\nready\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert any("logs " in c for c in run_commands)
        # Pod discovery must be a single kubectl round-trip, and auto
        # dispatch must not re-run it after picking kubectl.
        assert sum(1 for c in run_commands if "get pods" in c) == 1

    def test_auto_falls_through_to_command_when_no_pods(self) -> None:
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "auto",
                "components": ["kube-apiserver"],
                "commands": {"kube-apiserver": "echo 'one\\ntwo\\nthree'"},
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _ok(stdout=_pod_list())
            if cmd.startswith("echo"):
                return _ok(stdout="line1\nline2\nline3\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert "command" in check.message

    def test_auto_resolves_each_component_independently(self) -> None:
        """Hybrid cluster: kube-apiserver is a static pod (kubectl), but
        kube-scheduler is externally managed (command). Auto must route
        each component separately instead of forcing one path for all."""
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "auto",
                "components": ["kube-apiserver", "kube-scheduler"],
                "commands": {"kube-scheduler": "echo scheduler-log"},
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                # Only the apiserver is visible via kubectl.
                return _ok(stdout=_pod_list(("kube-apiserver-node1", "kube-apiserver")))
            if "logs kube-apiserver-node1" in cmd:
                return _ok(stdout="apiserver-line\n")
            if cmd == "echo scheduler-log":
                return _ok(stdout="scheduler-line\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert "kubectl+command" in check.message
        assert "kube-apiserver=1 lines" in check.message
        assert "kube-scheduler=1 lines" in check.message

    def test_auto_partial_unresolved_component_fails_with_guidance(self) -> None:
        """When only some components resolve and no command is configured
        for the rest, the failure names the missing component(s) and tells
        the user how to fix it."""
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "auto",
                "components": ["kube-apiserver", "kube-scheduler"],
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _ok(stdout=_pod_list(("kube-apiserver-node1", "kube-apiserver")))
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "kube-scheduler" in check.message
        assert "commands[kube-scheduler]" in check.message


class TestKubectlPath:
    """``mode: kubectl`` path: pod discovery (label then name-prefix fallback), ``--tail``/``--since`` forwarding, and failure handling when logs are missing, too short, or error out."""

    def test_kubectl_path_passes_when_each_component_returns_logs(self) -> None:
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "kubectl",
                "components": ["kube-apiserver", "kube-scheduler"],
                "tail": 5,
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _ok(
                    stdout=_pod_list(
                        ("kube-apiserver-node1", "kube-apiserver"),
                        ("kube-scheduler-node1", "kube-scheduler"),
                    )
                )
            if "logs kube-apiserver-node1" in cmd:
                return _ok(stdout="line1\nline2\n")
            if "logs kube-scheduler-node1" in cmd:
                return _ok(stdout="entryA\nentryB\nentryC\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert "kube-apiserver=2 lines" in check.message
        assert "kube-scheduler=3 lines" in check.message

    def test_kubectl_path_fails_when_component_pod_missing(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "kubectl", "components": ["kube-apiserver", "kube-scheduler"]})

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                # Apiserver resolvable by label; nothing that can match scheduler.
                return _ok(
                    stdout=_pod_list(
                        ("kube-apiserver-node1", "kube-apiserver"),
                        ("coredns-abc", "coredns"),
                    )
                )
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "kube-scheduler" in check.message

    def test_kubectl_path_fallback_matches_by_name_prefix(self) -> None:
        """When no pod carries the ``component`` label, fall back to
        matching by pod-name prefix."""
        check = K8sControlPlaneLogsCheck(config={"mode": "kubectl", "components": ["kube-apiserver"]})

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                # No component labels set - fallback must kick in.
                return _ok(stdout=_pod_list(("kube-apiserver-node1", ""), ("coredns-abc", "")))
            if "logs kube-apiserver-node1" in cmd:
                return _ok(stdout="logged\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message

    def test_kubectl_path_forwards_tail_to_logs_command(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "kubectl", "components": ["kube-apiserver"], "tail": 42})
        run_commands: list[str] = []

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            run_commands.append(cmd)
            if "get pods" in cmd:
                return _ok(stdout=_pod_list(("kube-apiserver-node1", "kube-apiserver")))
            if "logs " in cmd:
                return _ok(stdout="line1\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        logs_cmds = [c for c in run_commands if "logs " in c]
        assert len(logs_cmds) == 1
        assert "--tail=42" in logs_cmds[0]

    def test_kubectl_path_forwards_since_to_logs_command(self) -> None:
        """`since` must be shell-quoted and appended as `--since=<value>`."""
        check = K8sControlPlaneLogsCheck(config={"mode": "kubectl", "components": ["kube-apiserver"], "since": "5m"})
        run_commands: list[str] = []

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            run_commands.append(cmd)
            if "get pods" in cmd:
                return _ok(stdout=_pod_list(("kube-apiserver-node1", "kube-apiserver")))
            if "logs " in cmd:
                return _ok(stdout="line1\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        logs_cmd = next(c for c in run_commands if "logs " in c)
        assert "--since=5m" in logs_cmd

    def test_kubectl_path_fallback_prefers_longest_component_name(self) -> None:
        """``kube-scheduler-extender`` must claim its pod before ``kube-scheduler``
        greedily picks it up via the ``startswith`` fallback."""
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "kubectl",
                "components": ["kube-scheduler", "kube-scheduler-extender"],
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                # No component labels - forces pure name-prefix fallback.
                return _ok(
                    stdout=_pod_list(
                        ("kube-scheduler-extender-abc", ""),
                        ("kube-scheduler-node1", ""),
                    )
                )
            if "logs kube-scheduler-extender-abc" in cmd:
                return _ok(stdout="extender-log\n")
            if "logs kube-scheduler-node1" in cmd:
                return _ok(stdout="scheduler-log\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message

    def test_kubectl_path_fails_when_logs_return_too_few_lines(self) -> None:
        check = K8sControlPlaneLogsCheck(
            config={"mode": "kubectl", "components": ["kube-apiserver"], "min_log_lines": 3}
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _ok(stdout=_pod_list(("kube-apiserver-node1", "kube-apiserver")))
            if "logs " in cmd:
                return _ok(stdout="just-one-line\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "retrieved 1 lines" in check.message

    def test_kubectl_path_fails_when_logs_command_errors(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "kubectl", "components": ["kube-apiserver"]})

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if "get pods" in cmd:
                return _ok(stdout=_pod_list(("kube-apiserver-node1", "kube-apiserver")))
            if "logs " in cmd:
                return _fail(stderr="Error from server (Forbidden)")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "Forbidden" in check.message


class TestCommandPath:
    """``mode: command`` path: run the per-component command, enforce line-count thresholds, and report which command failed and why."""

    def test_command_path_passes_when_each_command_returns_logs(self) -> None:
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "command",
                "components": ["kube-apiserver", "kube-scheduler"],
                "commands": {
                    "kube-apiserver": "echo apiserver-line",
                    "kube-scheduler": "echo scheduler-line",
                },
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            if cmd == "echo apiserver-line":
                return _ok(stdout="apiserver-line\n")
            if cmd == "echo scheduler-line":
                return _ok(stdout="scheduler-line\n")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert "kube-apiserver=1 lines" in check.message
        assert "kube-scheduler=1 lines" in check.message

    def test_command_path_fails_when_command_missing_for_component(self) -> None:
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "command",
                "components": ["kube-apiserver", "kube-scheduler"],
                "commands": {"kube-apiserver": "echo a"},
            }
        )
        check.run()
        assert not check.passed
        assert "kube-scheduler" in check.message

    def test_command_path_fails_when_commands_config_absent(self) -> None:
        check = K8sControlPlaneLogsCheck(config={"mode": "command", "components": ["kube-apiserver", "kube-scheduler"]})
        check.run()
        assert not check.passed
        assert "kube-apiserver" in check.message
        assert "kube-scheduler" in check.message
        assert "no command configured" in check.message

    def test_command_path_fails_when_command_returns_no_lines(self) -> None:
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "command",
                "components": ["kube-apiserver"],
                "commands": {"kube-apiserver": "true"},
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            return _ok(stdout="")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "0 lines" in check.message

    def test_command_path_fails_when_command_exits_nonzero(self) -> None:
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "command",
                "components": ["kube-apiserver"],
                "commands": {"kube-apiserver": "false"},
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            return _fail(stderr="denied")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "exited" in check.message

    def test_command_path_failure_includes_command_snippet(self) -> None:
        """On failure, users need to see *which* command blew up so they
        can distinguish credentials errors from syntax typos without
        having to grep the logger output."""
        check = K8sControlPlaneLogsCheck(
            config={
                "mode": "command",
                "components": ["kube-apiserver"],
                "commands": {"kube-apiserver": "aws logs tail /aws/eks/demo --since 5m"},
            }
        )

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            return _fail(stderr="AccessDeniedException")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert not check.passed
        assert "aws logs tail" in check.message
