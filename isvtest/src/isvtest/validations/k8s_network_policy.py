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

"""NetworkPolicy enforcement and dual-stack node validations (K8S22).

This module provides two independent ``BaseValidation`` subclasses:

* ``K8sNetworkPolicyCheck`` - applies a pair of NetworkPolicies in an
  ephemeral namespace and verifies that ingress/egress are enforced as
  expected against both IPv4 and (when available) IPv6 pod addresses.
* ``K8sDualStackNodeCheck`` - inspects every node's ``InternalIP``
  addresses and verifies that the cluster is dual-stack (IPv4 + IPv6) when
  configuration requires it.

The classes are kept split so clusters that only support one stack can still
exercise the NetworkPolicy validation.
"""

from __future__ import annotations

import ipaddress
import json
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

from isvtest.config.settings import (
    get_k8s_network_policy_image,
    get_k8s_require_dual_stack,
)
from isvtest.core.k8s import KubectlParseError, get_kubectl_base_shell, get_kubectl_command, parse_kubectl_json
from isvtest.core.validation import BaseValidation

_MANIFEST_DIR = Path(__file__).parent / "manifests" / "k8s"
_PODS_MANIFEST = _MANIFEST_DIR / "network_policy_test_pods.yaml"
_INGRESS_MANIFEST = _MANIFEST_DIR / "network_policy_ingress.yaml"
_EGRESS_MANIFEST = _MANIFEST_DIR / "network_policy_egress.yaml"


class K8sNetworkPolicyCheck(BaseValidation):
    """Verify that Kubernetes NetworkPolicy enforces ingress and egress.

    The validation stands up four agnhost pods in an ephemeral namespace and
    probes TCP connectivity between them both before and after applying a pair
    of NetworkPolicies. Each probe is reported as a subtest so that individual
    ingress/egress failures are visible without masking the others.

    Config keys (with defaults):
        image: Agnhost image providing ``connect`` / ``netexec``
            (default from ``get_k8s_network_policy_image``).
        probe_port: TCP port exposed by the server pods (default: 8080).
        probe_timeout_s: ``agnhost connect`` timeout per probe (default: 10).
        settle_timeout_s: Max time to wait after applying the policies before
            starting real probes (default: 30).
        namespace_prefix: Prefix for the ephemeral namespace name
            (default: ``isvtest-netpol``).
        timeout: Overall class-level timeout for each ``run_command`` call
            (default: 300).
        test_egress: Whether to run the egress subtests (default: True).
    """

    description: ClassVar[str] = (
        "Apply a NetworkPolicy and verify pod connectivity is restricted (ingress + egress) on IPv4 and IPv6."
    )
    timeout: ClassVar[int] = 300

    def run(self) -> None:
        """Apply NetworkPolicy manifests and probe allowed/denied paths, recording subtests and a pass/fail outcome."""
        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()
        self._image = self.config.get("image") or get_k8s_network_policy_image()
        self._probe_port = int(self.config.get("probe_port", 8080))
        self._probe_timeout = int(self.config.get("probe_timeout_s", 10))
        settle_timeout = int(self.config.get("settle_timeout_s", 30))
        namespace_prefix = self.config.get("namespace_prefix", "isvtest-netpol")
        test_egress = bool(self.config.get("test_egress", True))
        self._namespace = f"{namespace_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)

        ns_created = False
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            if not self._apply_manifest(_PODS_MANIFEST):
                return

            wait_cmd = (
                f"{self._kubectl_base} wait --for=condition=Ready "
                f"--timeout={self.timeout}s -n {ns_quoted} "
                f"pod/server pod/other-server pod/allowed-client pod/denied-client"
            )
            wait_result = self.run_command(wait_cmd)
            if wait_result.exit_code != 0:
                self.set_failed(f"Probe pods did not become Ready: {wait_result.stderr or wait_result.stdout}")
                return

            server_ips = self._get_pod_ips("server")
            other_ips = self._get_pod_ips("other-server")
            if not server_ips:
                self.set_failed("Failed to read server pod IPs")
                return
            if test_egress and not other_ips:
                self.set_failed("Failed to read other-server pod IPs")
                return

            server_ipv4 = [ip for ip in server_ips if _is_ipv4(ip)]
            server_ipv6 = [ip for ip in server_ips if _is_ipv6(ip)]
            other_ipv4 = [ip for ip in other_ips if _is_ipv4(ip)]
            other_ipv6 = [ip for ip in other_ips if _is_ipv6(ip)]

            for client in ("allowed-client", "denied-client"):
                for ip in server_ips:
                    if not self._probe(client, ip):
                        self.set_failed(
                            f"Baseline connectivity broken - CNI issue? {client} could not reach server at {ip}"
                        )
                        return

            # Baseline allowed-client -> other-server before egress policy lands,
            # so a pre-existing CNI failure isn't misattributed to _EGRESS_MANIFEST.
            if test_egress:
                for ip in other_ips:
                    if not self._probe("allowed-client", ip):
                        self.set_failed(
                            f"Baseline connectivity broken - CNI issue? allowed-client could not reach other-server at {ip}"
                        )
                        return

            if not self._apply_manifest(_INGRESS_MANIFEST):
                return
            if test_egress and not self._apply_manifest(_EGRESS_MANIFEST):
                return

            # Poll every deny path that will later be asserted so IPv6 and
            # egress policies don't get probed before enforcement has landed.
            deny_probes: list[tuple[str, str, str]] = []
            if server_ipv4:
                deny_probes.append(("denied-client", "server", server_ipv4[0]))
            if server_ipv6:
                deny_probes.append(("denied-client", "server", server_ipv6[0]))
            if test_egress:
                if other_ipv4:
                    deny_probes.append(("allowed-client", "other-server", other_ipv4[0]))
                if other_ipv6:
                    deny_probes.append(("allowed-client", "other-server", other_ipv6[0]))

            for client, target_label, ip in deny_probes:
                if not self._wait_for_policy_enforcement(client, ip, settle_timeout):
                    self.set_failed(
                        f"NetworkPolicy did not take effect within {settle_timeout}s "
                        f"({client} still reaches {target_label} at {ip})"
                    )
                    return

            # Families are driven by server presence alone so an IPv6-capable
            # server still gets ingress coverage even if other-server lacks IPv6.
            families: list[tuple[str, list[str], list[str]]] = []
            if server_ipv4:
                families.append(("IPv4", server_ipv4, other_ipv4))
            if server_ipv6:
                families.append(("IPv6", server_ipv6, other_ipv6))

            any_failed = False
            for family, server_family_ips, other_family_ips in families:
                server_ip = server_family_ips[0]

                # A single allow[family] subtest exercises both the server-side
                # ingress rule and the client-side egress rule; they can't be
                # attributed independently from this probe alone.
                allow_ok = self._probe("allowed-client", server_ip)
                if not self._report_probe(
                    f"allow[{family}]", allow_ok, "allowed-client", "server", server_ip, expect_success=True
                ):
                    any_failed = True

                denied_ok = self._probe("denied-client", server_ip)
                if not self._report_probe(
                    f"ingress-deny[{family}]", denied_ok, "denied-client", "server", server_ip, expect_success=False
                ):
                    any_failed = True

                if test_egress:
                    if other_family_ips:
                        other_ip = other_family_ips[0]
                        egress_ok = self._probe("allowed-client", other_ip)
                        if not self._report_probe(
                            f"egress-deny[{family}]",
                            egress_ok,
                            "allowed-client",
                            "other-server",
                            other_ip,
                            expect_success=False,
                        ):
                            any_failed = True
                    else:
                        self.report_subtest(
                            f"egress-deny[{family}]",
                            passed=True,
                            message=f"other-server has no {family} address; egress check skipped",
                            skipped=True,
                        )

            if any_failed:
                self.set_failed("One or more NetworkPolicy subtests failed; see subtest details")
            else:
                covered = [name for name, _, _ in families]
                suffix = f" ({covered[0]} only)" if len(covered) == 1 else f" ({' + '.join(covered)})"
                self.set_passed(f"NetworkPolicy enforcement verified{suffix}")
        finally:
            # Cleanup must never overwrite pass/fail outcome set above.
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _apply_manifest(self, manifest_path: Path) -> bool:
        """Apply a manifest after substituting namespace/image/port placeholders.

        Returns True on success; sets the validation to failed and returns
        False on error.
        """
        if not manifest_path.exists():
            self.set_failed(f"Manifest not found: {manifest_path}")
            return False

        content = (
            manifest_path.read_text()
            .replace("__NAMESPACE__", self._namespace)
            .replace("__IMAGE__", self._image)
            .replace("__PORT__", str(self._probe_port))
        )

        try:
            proc = subprocess.run(
                self._kubectl_parts + ["apply", "-f", "-"],
                input=content,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            self.set_failed(f"kubectl apply timed out for {manifest_path.name}")
            return False
        except Exception as exc:
            self.set_failed(f"kubectl apply failed for {manifest_path.name}: {exc}")
            return False

        if proc.returncode != 0:
            self.set_failed(
                f"kubectl apply failed for {manifest_path.name}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
            return False
        return True

    def _get_pod_ips(self, pod: str) -> list[str]:
        """Return every IP assigned to ``pod`` via ``status.podIPs``."""
        cmd = f"{self._kubectl_base} get pod {shlex.quote(pod)} -n {shlex.quote(self._namespace)} -o json"
        result = self.run_command(cmd)
        if result.exit_code != 0:
            self.log.error("Failed to read pod IPs for %s: %s", pod, result.stderr)
            return []
        try:
            payload = parse_kubectl_json(result, f"pod {pod!r}")
        except KubectlParseError as exc:
            self.log.error("Failed to read pod IPs for %s: %s", pod, exc)
            return []
        pod_ips = (payload.get("status") or {}).get("podIPs") or []
        return [str(entry["ip"]) for entry in pod_ips if isinstance(entry, dict) and entry.get("ip")]

    def _probe(self, client: str, host: str) -> bool:
        """Run an ``agnhost connect`` probe from ``client`` to ``host:port``.

        IPv6 literals are wrapped in brackets. Returns True iff the probe
        succeeded (exit code 0).
        """
        host_literal = f"[{host}]" if _is_ipv6(host) else host
        target = f"{host_literal}:{self._probe_port}"
        cmd = (
            f"{self._kubectl_base} exec -n {shlex.quote(self._namespace)} {shlex.quote(client)} "
            f"-- /agnhost connect {shlex.quote(target)} --timeout={self._probe_timeout}s"
        )
        # Slack over the probe timeout so a real network timeout surfaces as
        # probe failure rather than the exec itself being killed.
        result = self.run_command(cmd, timeout=max(self._probe_timeout + 10, 30))
        return result.exit_code == 0

    def _report_probe(
        self,
        name: str,
        probe_succeeded: bool,
        client: str,
        target_label: str,
        host: str,
        *,
        expect_success: bool,
    ) -> bool:
        """Report a probe outcome as a subtest. Returns True iff outcome matched expectation."""
        matched = probe_succeeded == expect_success
        expected = "success" if expect_success else "timeout"
        actual = "success" if probe_succeeded else "timeout"
        self.report_subtest(
            name,
            passed=matched,
            message=f"{client} -> {target_label}@{host}:{self._probe_port} (expected {expected}, got {actual})",
        )
        return matched

    def _wait_for_policy_enforcement(self, client: str, host: str, settle_timeout: int) -> bool:
        """Poll ``client``'s probe to ``host`` until it starts timing out (deny enforced)."""
        deadline = time.time() + settle_timeout
        while time.time() < deadline:
            if not self._probe(client, host):
                return True
            time.sleep(1.0)
        return False


class K8sDualStackNodeCheck(BaseValidation):
    """Verify that cluster nodes have both IPv4 and IPv6 InternalIP addresses.

    Config keys (with defaults):
        require_dual_stack: One of ``True``, ``False``, or ``"auto"``. Defaults
            to the value returned by
            ``isvtest.config.settings.get_k8s_require_dual_stack``
            (``"auto"`` unless ``K8S_REQUIRE_DUAL_STACK`` is set).

    Decision matrix:
        * ``True`` - any node missing either family fails the validation.
        * ``False`` - always passes; per-node summary is still emitted.
        * ``"auto"`` - if at least one node has both families the cluster is
          treated as dual-stack and every node must be; if no node has both
          the check skips.
    """

    description: ClassVar[str] = "Verify IPv4 and IPv6 addresses on dual-stack nodes."
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        """List cluster nodes and apply the ``require_dual_stack`` decision matrix, setting the validation pass/fail state."""
        require_dual_stack = self.config.get("require_dual_stack", get_k8s_require_dual_stack())
        try:
            normalized = _normalize_require_dual_stack(require_dual_stack)
        except ValueError:
            self.set_failed(
                f"Invalid require_dual_stack value: {require_dual_stack!r} (expected True, False, or 'auto')"
            )
            return

        result = self.run_command(f"{get_kubectl_base_shell()} get nodes -o json")
        if result.exit_code != 0:
            self.set_failed(f"Failed to list nodes: {result.stderr}")
            return

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.set_failed(f"Failed to parse kubectl JSON output: {exc}")
            return

        nodes = payload.get("items", [])
        if not nodes:
            self.set_passed("No nodes found in cluster")
            return

        node_families: list[tuple[str, bool, bool]] = []
        cluster_has_dual_stack_hint = False
        for node in nodes:
            name = node.get("metadata", {}).get("name", "unknown")
            has_v4, has_v6 = _classify_node(node)
            node_families.append((name, has_v4, has_v6))
            # Cluster-level hint combines InternalIP and podCIDR evidence so
            # auto mode still detects a dual-stack cluster when a node carries
            # only one InternalIP family but advertises both pod CIDR families.
            cidr_v4, cidr_v6 = _node_podcidr_families(node)
            if (has_v4 or cidr_v4) and (has_v6 or cidr_v6):
                cluster_has_dual_stack_hint = True

        if normalized == "auto" and not cluster_has_dual_stack_hint:
            # Still emit per-node subtests for visibility, then skip.
            for name, has_v4, has_v6 in node_families:
                self.report_subtest(
                    f"node/{name}",
                    passed=True,
                    message=f"single-stack cluster (auto mode); node has {_family_summary(has_v4, has_v6)}",
                    skipped=True,
                )
            self.set_passed("Skipped: cluster is single-stack (auto mode)")
            return

        require_both = normalized is True or (normalized == "auto" and cluster_has_dual_stack_hint)
        failures: list[str] = []

        for name, has_v4, has_v6 in node_families:
            summary = _family_summary(has_v4, has_v6)
            if require_both:
                node_ok = has_v4 and has_v6
                self.report_subtest(f"node/{name}", passed=node_ok, message=summary)
                if not node_ok:
                    failures.append(f"{name} ({summary})")
            else:
                self.report_subtest(f"node/{name}", passed=True, message=summary)

        if failures:
            self.set_failed(f"{len(failures)} node(s) missing required address family: {', '.join(failures)}")
            return

        if require_both:
            self.set_passed(f"All {len(node_families)} nodes have IPv4 and IPv6 InternalIPs")
        else:
            self.set_passed(
                f"Informational: per-node IPv4/IPv6 summary recorded for "
                f"{len(node_families)} node(s) (require_dual_stack=False)"
            )


def _normalize_require_dual_stack(value: object) -> bool | str:
    """Normalize a ``require_dual_stack`` config value.

    Returns ``True``, ``False``, or ``"auto"``. Raises ``ValueError`` for
    anything else.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
        if lowered == "auto":
            return "auto"
    raise ValueError(f"unrecognized require_dual_stack value: {value!r}")


def _is_ipv4(addr: str) -> bool:
    """Return True iff ``addr`` parses as an IPv4 address."""
    try:
        return isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address)
    except ValueError:
        return False


def _is_ipv6(addr: str) -> bool:
    """Return True iff ``addr`` parses as an IPv6 address."""
    try:
        return isinstance(ipaddress.ip_address(addr), ipaddress.IPv6Address)
    except ValueError:
        return False


def _classify_node(node: dict[str, Any]) -> tuple[bool, bool]:
    """Return ``(has_ipv4, has_ipv6)`` based on the node's InternalIP addresses.

    podCIDRs are deliberately ignored here - a node that advertises both pod CIDR
    families but only one InternalIP family is not dual-stack at the node level.
    Use ``_node_podcidr_families`` for cluster-level auto-detection hints.
    """
    has_v4 = False
    has_v6 = False

    for addr in node.get("status", {}).get("addresses", []) or []:
        if addr.get("type") != "InternalIP":
            continue
        ip_str = addr.get("address", "")
        if _is_ipv4(ip_str):
            has_v4 = True
        elif _is_ipv6(ip_str):
            has_v6 = True

    return has_v4, has_v6


def _node_podcidr_families(node: dict[str, Any]) -> tuple[bool, bool]:
    """Return ``(has_ipv4, has_ipv6)`` for the node's ``spec.podCIDRs``."""
    has_v4 = False
    has_v6 = False
    for cidr in node.get("spec", {}).get("podCIDRs", []) or []:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network):
            has_v4 = True
        elif isinstance(network, ipaddress.IPv6Network):
            has_v6 = True
    return has_v4, has_v6


def _family_summary(has_v4: bool, has_v6: bool) -> str:
    """Human-readable per-node family summary."""
    families = []
    if has_v4:
        families.append("IPv4")
    if has_v6:
        families.append("IPv6")
    return f"families=[{', '.join(families) or 'none'}]"
