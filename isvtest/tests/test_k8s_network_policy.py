# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Unit tests for ``isvtest.validations.k8s_network_policy``."""

from __future__ import annotations

import itertools
import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_network_policy import (
    K8sDualStackNodeCheck,
    K8sNetworkPolicyCheck,
    _classify_node,
    _family_summary,
    _is_ipv4,
    _is_ipv6,
    _node_podcidr_families,
    _normalize_require_dual_stack,
)


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult`` with the given stdout/stderr."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failing ``CommandResult`` with the given output and exit code."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


class TestNormalizeRequireDualStack:
    """Tests for ``_normalize_require_dual_stack``."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("true", True),
            ("FALSE", False),
            ("Auto", "auto"),
            ("  auto  ", "auto"),
            ("yes", True),
            ("no", False),
            ("1", True),
            ("0", False),
        ],
    )
    def test_valid_values(self, value: Any, expected: Any) -> None:
        assert _normalize_require_dual_stack(value) == expected

    @pytest.mark.parametrize("value", ["maybe", "", None, 42, object()])
    def test_invalid_values(self, value: Any) -> None:
        with pytest.raises(ValueError):
            _normalize_require_dual_stack(value)


class TestIpClassification:
    """Tests for ``_is_ipv4`` / ``_is_ipv6``."""

    def test_ipv4(self) -> None:
        assert _is_ipv4("10.0.0.1")
        assert not _is_ipv6("10.0.0.1")

    def test_ipv6(self) -> None:
        assert _is_ipv6("fd00::1")
        assert not _is_ipv4("fd00::1")

    def test_invalid(self) -> None:
        assert not _is_ipv4("not-an-ip")
        assert not _is_ipv6("not-an-ip")
        assert not _is_ipv4("")


class TestClassifyNode:
    """Tests for ``_classify_node``."""

    def test_dual_stack_node(self) -> None:
        node = {
            "metadata": {"name": "n1"},
            "status": {
                "addresses": [
                    {"type": "InternalIP", "address": "10.0.0.1"},
                    {"type": "InternalIP", "address": "fd00::1"},
                    {"type": "ExternalIP", "address": "1.2.3.4"},
                ]
            },
        }
        assert _classify_node(node) == (True, True)

    def test_ipv4_only(self) -> None:
        node = {
            "status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.1"}]},
        }
        assert _classify_node(node) == (True, False)

    def test_ipv6_only(self) -> None:
        node = {
            "status": {"addresses": [{"type": "InternalIP", "address": "fd00::1"}]},
        }
        assert _classify_node(node) == (False, True)

    def test_pod_cidrs_do_not_supplement_internal_ips(self) -> None:
        # Per-node classification must ignore podCIDRs: a node whose control
        # plane advertises only an IPv4 InternalIP is not dual-stack at the
        # node level, even when its pod CIDRs span both families.
        node = {
            "status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.1"}]},
            "spec": {"podCIDRs": ["10.244.0.0/24", "fd00::/64"]},
        }
        assert _classify_node(node) == (True, False)

    def test_no_addresses(self) -> None:
        assert _classify_node({}) == (False, False)


class TestNodePodCidrFamilies:
    """Tests for ``_node_podcidr_families``."""

    def test_dual_family(self) -> None:
        node = {"spec": {"podCIDRs": ["10.244.0.0/24", "fd00::/64"]}}
        assert _node_podcidr_families(node) == (True, True)

    def test_ipv4_only(self) -> None:
        assert _node_podcidr_families({"spec": {"podCIDRs": ["10.244.0.0/24"]}}) == (True, False)

    def test_ipv6_only(self) -> None:
        assert _node_podcidr_families({"spec": {"podCIDRs": ["fd00::/64"]}}) == (False, True)

    def test_missing_and_invalid(self) -> None:
        assert _node_podcidr_families({}) == (False, False)
        assert _node_podcidr_families({"spec": {"podCIDRs": ["not-a-cidr"]}}) == (False, False)


class TestFamilySummary:
    """Tests for ``_family_summary``, which formats detected IP families for display."""

    def test_both(self) -> None:
        assert _family_summary(True, True) == "families=[IPv4, IPv6]"

    def test_none(self) -> None:
        assert _family_summary(False, False) == "families=[none]"


def _nodes_json(
    node_addrs: list[list[tuple[str, str]]],
    pod_cidrs: list[list[str]] | None = None,
) -> str:
    """Build a ``kubectl get nodes -o json`` payload.

    ``node_addrs`` is a list of per-node address lists, each element a
    ``(type, address)`` tuple. ``pod_cidrs`` is an optional per-node list of
    CIDR strings set as ``spec.podCIDRs``.
    """
    items = []
    for i, addrs in enumerate(node_addrs):
        node: dict[str, Any] = {
            "metadata": {"name": f"node-{i}"},
            "status": {"addresses": [{"type": t, "address": a} for t, a in addrs]},
        }
        if pod_cidrs and i < len(pod_cidrs) and pod_cidrs[i]:
            node["spec"] = {"podCIDRs": pod_cidrs[i]}
        items.append(node)
    return json.dumps({"items": items})


class TestDualStackNodeCheck:
    """Tests for ``K8sDualStackNodeCheck``."""

    def _make(self, config: dict[str, Any] | None = None) -> K8sDualStackNodeCheck:
        check = K8sDualStackNodeCheck(config=config or {})
        return check

    def test_invalid_require_dual_stack_fails(self) -> None:
        check = self._make({"require_dual_stack": "maybe"})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert not check.passed
        assert "Invalid require_dual_stack" in check._error

    def test_kubectl_failure_sets_failed(self) -> None:
        check = self._make({"require_dual_stack": True})
        with patch.object(check, "run_command", return_value=_fail(stderr="boom")):
            check.run()
        assert not check.passed
        assert "Failed to list nodes" in check._error

    def test_bad_json_sets_failed(self) -> None:
        check = self._make({"require_dual_stack": True})
        with patch.object(check, "run_command", return_value=_ok(stdout="not-json")):
            check.run()
        assert not check.passed
        assert "parse kubectl JSON" in check._error

    def test_no_nodes_passes(self) -> None:
        check = self._make({"require_dual_stack": True})
        with patch.object(check, "run_command", return_value=_ok(stdout=json.dumps({"items": []}))):
            check.run()
        assert check.passed
        assert "No nodes" in check._output

    def test_require_true_fails_on_single_stack_node(self) -> None:
        payload = _nodes_json(
            [
                [("InternalIP", "10.0.0.1"), ("InternalIP", "fd00::1")],
                [("InternalIP", "10.0.0.2")],  # IPv4 only
            ]
        )
        check = self._make({"require_dual_stack": True})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert not check.passed
        assert "node-1" in check._error

    def test_require_true_passes_when_all_dual_stack(self) -> None:
        payload = _nodes_json(
            [
                [("InternalIP", "10.0.0.1"), ("InternalIP", "fd00::1")],
                [("InternalIP", "10.0.0.2"), ("InternalIP", "fd00::2")],
            ]
        )
        check = self._make({"require_dual_stack": True})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert check.passed
        assert "All 2 nodes" in check._output

    def test_require_false_always_passes(self) -> None:
        payload = _nodes_json([[("InternalIP", "10.0.0.1")]])
        check = self._make({"require_dual_stack": False})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert check.passed
        assert "Informational" in check._output

    def test_auto_skips_when_no_node_dual_stack(self) -> None:
        payload = _nodes_json(
            [
                [("InternalIP", "10.0.0.1")],
                [("InternalIP", "10.0.0.2")],
            ]
        )
        check = self._make({"require_dual_stack": "auto"})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert check.passed
        assert "single-stack" in check._output

    def test_auto_requires_all_when_any_node_dual_stack(self) -> None:
        payload = _nodes_json(
            [
                [("InternalIP", "10.0.0.1"), ("InternalIP", "fd00::1")],
                [("InternalIP", "10.0.0.2")],  # Missing IPv6
            ]
        )
        check = self._make({"require_dual_stack": "auto"})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert not check.passed
        assert "node-1" in check._error

    def test_auto_passes_when_all_nodes_dual_stack(self) -> None:
        payload = _nodes_json(
            [
                [("InternalIP", "10.0.0.1"), ("InternalIP", "fd00::1")],
                [("InternalIP", "10.0.0.2"), ("InternalIP", "fd00::2")],
            ]
        )
        check = self._make({"require_dual_stack": "auto"})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert check.passed
        assert "All 2 nodes" in check._output

    def test_auto_pod_cidrs_hint_requires_internal_ip_per_node(self) -> None:
        # No node has both InternalIP families, but podCIDRs span both: the
        # cluster-level auto hint fires, and the per-node check then fails the
        # node that is missing the IPv6 InternalIP.
        payload = _nodes_json(
            [[("InternalIP", "10.0.0.1")]],
            pod_cidrs=[["10.244.0.0/24", "fd00::/64"]],
        )
        check = self._make({"require_dual_stack": "auto"})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert not check.passed
        assert "node-0" in check._error


def _primed_check(config: dict[str, Any] | None = None, *, probe_timeout: int = 5) -> K8sNetworkPolicyCheck:
    """Return a check with the invariants ``run()`` would normally set."""
    check = K8sNetworkPolicyCheck(config=config or {})
    check._kubectl_base = "kubectl"
    check._kubectl_parts = ["kubectl"]
    check._namespace = "ns"
    check._image = "test-image"
    check._probe_port = 8080
    check._probe_timeout = probe_timeout
    return check


class TestNetworkPolicyCheckBehavior:
    """Unit tests for ``K8sNetworkPolicyCheck`` that don't need a cluster."""

    def test_namespace_create_failure_sets_failed(self) -> None:
        check = K8sNetworkPolicyCheck(config={})
        with patch.object(check, "run_command", return_value=_fail(stderr="forbidden")):
            check.run()
        assert not check.passed
        assert "Failed to create namespace" in check._error

    def test_get_pod_ips_parses_multiple_families(self) -> None:
        check = _primed_check()
        payload = json.dumps({"status": {"podIPs": [{"ip": "10.0.0.1"}, {"ip": "fd00::1"}]}})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            ips = check._get_pod_ips("server")
        assert ips == ["10.0.0.1", "fd00::1"]

    def test_get_pod_ips_ignores_malformed_entries(self) -> None:
        check = _primed_check()
        payload = json.dumps({"status": {"podIPs": [{"ip": "10.0.0.1"}, "bad", None, {"name": "missing-ip"}]}})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            ips = check._get_pod_ips("server")
        assert ips == ["10.0.0.1"]

    def test_get_pod_ips_returns_empty_on_invalid_json(self) -> None:
        check = _primed_check()
        with patch.object(check, "run_command", return_value=_ok(stdout="not-json")):
            ips = check._get_pod_ips("server")
        assert ips == []

    def test_get_pod_ips_returns_empty_on_error(self) -> None:
        check = _primed_check()
        with patch.object(check, "run_command", return_value=_fail(stderr="boom")):
            ips = check._get_pod_ips("server")
        assert ips == []

    def test_probe_success(self) -> None:
        check = _primed_check()
        with patch.object(check, "run_command", return_value=_ok()) as mock_run:
            ok = check._probe("allowed-client", "10.0.0.1")
        assert ok
        cmd_arg = mock_run.call_args[0][0]
        assert "allowed-client" in cmd_arg
        assert "10.0.0.1:8080" in cmd_arg
        assert "--timeout=5s" in cmd_arg

    def test_probe_failure(self) -> None:
        check = _primed_check()
        with patch.object(check, "run_command", return_value=_fail(exit_code=1)):
            ok = check._probe("denied-client", "10.0.0.1")
        assert not ok

    def test_probe_brackets_ipv6_literal(self) -> None:
        check = _primed_check()
        with patch.object(check, "run_command", return_value=_ok()) as mock_run:
            check._probe("allowed-client", "fd00::1")
        cmd_arg = mock_run.call_args[0][0]
        assert "[fd00::1]:8080" in cmd_arg

    def test_wait_for_policy_enforcement_succeeds_when_probe_stops(self) -> None:
        check = _primed_check()
        # First probe succeeds (reachable), second times out (enforced).
        calls = [_ok(), _fail(exit_code=1)]

        def fake_run(cmd: str, timeout: int | None = None, display_cmd: str | None = None) -> CommandResult:
            return calls.pop(0)

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_network_policy.time.sleep"),
        ):
            ok = check._wait_for_policy_enforcement("denied-client", "10.0.0.1", settle_timeout=10)
        assert ok

    def test_wait_for_policy_enforcement_times_out(self) -> None:
        check = _primed_check()
        # Sequence: deadline init (0.0), loop check #1 (0.0), loop check #2 (11.0 >= deadline).
        with (
            patch.object(check, "run_command", return_value=_ok()),
            patch("isvtest.validations.k8s_network_policy.time.sleep"),
            patch(
                "isvtest.validations.k8s_network_policy.time.time",
                side_effect=[0.0, 0.0, 11.0, 11.0, 11.0],
            ),
        ):
            ok = check._wait_for_policy_enforcement("denied-client", "10.0.0.1", settle_timeout=10)
        assert not ok


class _NetPolStub:
    """Scripted stub for the full ``K8sNetworkPolicyCheck.run()`` flow.

    Dispatches ``run_command`` calls by command content and tracks
    ``subprocess.run`` calls for manifest applies. Probe outcomes depend on
    whether any NetworkPolicy manifest has been applied yet (``_policy_applied``).
    """

    def __init__(
        self,
        *,
        server_ips: tuple[str, ...] = ("10.0.0.1",),
        other_ips: tuple[str, ...] = ("10.2.2.2",),
        baseline_blocks: set[str] | None = None,
        enforcement_probes_until: int | None = 1,
        allow_after: bool = True,
        egress_blocks_after: bool = True,
    ) -> None:
        self.server_ips = list(server_ips)
        self.other_ips = list(other_ips)
        self.baseline_blocks: set[str] = set(baseline_blocks or ())
        self.enforcement_probes_until = enforcement_probes_until
        self.allow_after = allow_after
        self.egress_blocks_after = egress_blocks_after
        self._policy_applied = False
        self._denied_probe_count = 0
        self.applied_manifests: list[str] = []
        self.run_commands: list[str] = []

    def run_command(self, cmd: str, timeout: int | None = None, display_cmd: str | None = None) -> CommandResult:
        """Dispatch a scripted ``CommandResult`` based on the shape of ``cmd``.

        Records ``cmd`` in ``self.run_commands`` and returns the canned
        response for namespace ops, readiness waits, pod IP lookups, or probes.
        Raises ``AssertionError`` if ``cmd`` is not a recognized shape.
        """
        self.run_commands.append(cmd)
        if "create namespace" in cmd or "delete namespace" in cmd:
            return _ok()
        if "wait --for=condition=Ready" in cmd:
            return _ok()
        if "get pod" in cmd and "-o json" in cmd:
            ips = self.other_ips if "other-server" in cmd else self.server_ips
            return _ok(stdout=json.dumps({"status": {"podIPs": [{"ip": ip} for ip in ips]}}))
        if "/agnhost connect" in cmd:
            return self._probe(cmd)
        raise AssertionError(f"No scripted response for command: {cmd}")

    def _probe(self, cmd: str) -> CommandResult:
        """Return a probe ``CommandResult`` modeling pre/post-policy behavior.

        Before any policy is applied, returns success unless the client is in
        ``baseline_blocks``. After policy application, outcomes depend on
        client/target pair and the ``allow_after``/``egress_blocks_after``/
        ``enforcement_probes_until`` knobs.
        """
        client = "allowed-client" if "allowed-client" in cmd else "denied-client"
        is_server_target = any(ip in cmd for ip in self.server_ips)
        is_other_target = any(ip in cmd for ip in self.other_ips)

        if not self._policy_applied:
            if client in self.baseline_blocks:
                return _fail(exit_code=1)
            return _ok()

        if client == "denied-client" and is_server_target:
            self._denied_probe_count += 1
            if self.enforcement_probes_until is None:
                return _ok()
            if self._denied_probe_count >= self.enforcement_probes_until:
                return _fail(exit_code=1)
            return _ok()
        if client == "allowed-client" and is_server_target:
            return _ok() if self.allow_after else _fail(exit_code=1)
        if client == "allowed-client" and is_other_target:
            return _fail(exit_code=1) if self.egress_blocks_after else _ok()
        raise AssertionError(f"unexpected probe combination: {cmd}")

    def subprocess_run(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        """Record a manifest apply and flip ``_policy_applied`` when relevant.

        Inspects the piped manifest from ``kwargs['input']``, appends a label
        (``"pods"``, ``"ingress"``, ``"egress"``, or ``"netpol-other"``) to
        ``applied_manifests``, and returns a successful ``CompletedProcess``.
        """
        content = kwargs.get("input", "")
        if "kind: NetworkPolicy" in content:
            self._policy_applied = True
            if "isvtest-netpol-ingress" in content:
                self.applied_manifests.append("ingress")
            elif "isvtest-netpol-egress" in content:
                self.applied_manifests.append("egress")
            else:
                self.applied_manifests.append("netpol-other")
        else:
            self.applied_manifests.append("pods")
        return subprocess.CompletedProcess(args=args[0] if args else [], returncode=0, stdout="", stderr="")


class TestNetworkPolicyCheckEndToEnd:
    """Integration-style tests driving the full ``K8sNetworkPolicyCheck.run()`` path."""

    def _make(self, config: dict[str, Any] | None = None) -> K8sNetworkPolicyCheck:
        return K8sNetworkPolicyCheck(config=config or {})

    def test_happy_path_ipv4_only(self) -> None:
        check = self._make({"probe_timeout_s": 1, "settle_timeout_s": 5})
        stub = _NetPolStub()
        with (
            patch.object(check, "run_command", side_effect=stub.run_command),
            patch("isvtest.validations.k8s_network_policy.subprocess.run", side_effect=stub.subprocess_run),
            patch("isvtest.validations.k8s_network_policy.time.sleep"),
        ):
            check.run()
        assert check.passed, check._error
        assert "IPv4 only" in check._output
        assert stub.applied_manifests == ["pods", "ingress", "egress"]
        assert any("delete namespace" in c for c in stub.run_commands)

    def test_baseline_failure_sets_failed_and_cleans_up(self) -> None:
        check = self._make({"probe_timeout_s": 1})
        stub = _NetPolStub(baseline_blocks={"allowed-client"})
        with (
            patch.object(check, "run_command", side_effect=stub.run_command),
            patch("isvtest.validations.k8s_network_policy.subprocess.run", side_effect=stub.subprocess_run),
        ):
            check.run()
        assert not check.passed
        assert "Baseline connectivity broken" in check._error
        assert any("delete namespace" in c for c in stub.run_commands)
        # Policy manifests must not have been applied after baseline failure.
        assert "ingress" not in stub.applied_manifests
        assert "egress" not in stub.applied_manifests

    def test_policy_never_enforced_sets_failed(self) -> None:
        check = self._make({"probe_timeout_s": 1, "settle_timeout_s": 2})
        stub = _NetPolStub(enforcement_probes_until=None)
        with (
            patch.object(check, "run_command", side_effect=stub.run_command),
            patch("isvtest.validations.k8s_network_policy.subprocess.run", side_effect=stub.subprocess_run),
            patch("isvtest.validations.k8s_network_policy.time.sleep"),
            # First call establishes deadline; subsequent calls always exceed it.
            patch(
                "isvtest.validations.k8s_network_policy.time.time",
                side_effect=itertools.chain([0.0], itertools.repeat(1000.0)),
            ),
        ):
            check.run()
        assert not check.passed
        assert "NetworkPolicy did not take effect" in check._error
        # Namespace cleanup still runs.
        assert any("delete namespace" in c for c in stub.run_commands)

    def test_test_egress_false_skips_egress_manifest_and_probes(self) -> None:
        check = self._make({"probe_timeout_s": 1, "settle_timeout_s": 5, "test_egress": False})
        stub = _NetPolStub()
        with (
            patch.object(check, "run_command", side_effect=stub.run_command),
            patch("isvtest.validations.k8s_network_policy.subprocess.run", side_effect=stub.subprocess_run),
            patch("isvtest.validations.k8s_network_policy.time.sleep"),
        ):
            check.run()
        assert check.passed, check._error
        assert stub.applied_manifests == ["pods", "ingress"]
        # allowed-client must never be probed against the other-server IP.
        assert not any("10.2.2.2" in c and "allowed-client" in c for c in stub.run_commands)

    def test_wait_for_ready_failure_cleans_up_namespace(self) -> None:
        check = self._make({"probe_timeout_s": 1})
        run_commands: list[str] = []

        def run_command(cmd: str, timeout: int | None = None, display_cmd: str | None = None) -> CommandResult:
            run_commands.append(cmd)
            if "create namespace" in cmd or "delete namespace" in cmd:
                return _ok()
            if "wait --for=condition=Ready" in cmd:
                return _fail(stderr="pods not ready")
            raise AssertionError(f"unexpected: {cmd}")

        def subproc_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args=args[0] if args else [], returncode=0, stdout="", stderr="")

        with (
            patch.object(check, "run_command", side_effect=run_command),
            patch("isvtest.validations.k8s_network_policy.subprocess.run", side_effect=subproc_run),
        ):
            check.run()
        assert not check.passed
        assert "did not become Ready" in check._error
        assert any("delete namespace" in c for c in run_commands)

    def test_single_subtest_per_allow_family(self) -> None:
        """The allow probe should produce exactly one subtest per address family."""
        check = self._make({"probe_timeout_s": 1, "settle_timeout_s": 5})
        stub = _NetPolStub()
        with (
            patch.object(check, "run_command", side_effect=stub.run_command),
            patch("isvtest.validations.k8s_network_policy.subprocess.run", side_effect=stub.subprocess_run),
            patch("isvtest.validations.k8s_network_policy.time.sleep"),
        ):
            check.run()
        allow_subtests = [r for r in check._subtest_results if r["name"].startswith("allow[")]
        assert len(allow_subtests) == 1
        assert allow_subtests[0]["name"] == "allow[IPv4]"
