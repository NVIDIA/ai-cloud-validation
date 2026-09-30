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

"""Issue #665: K8S29 (LoadBalancer), K8S30 (CoreDNS forwarding), K8S31 (CIDR ranges)."""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import socket
import subprocess
import time
import uuid
from typing import Any, ClassVar

import pytest
import yaml

from isvtest.config.settings import get_k8s_coredns_image, get_k8s_dns_probe_image, get_k8s_network_policy_image
from isvtest.core.k8s import (
    KubectlParseError,
    get_kubectl_base_shell,
    get_kubectl_command,
    is_resource_absent,
    kubectl_items_or_fail,
    parse_kubectl_json,
)
from isvtest.core.validation import BaseValidation

_BACKEND_POD_NAME = "lb-backend"
_BACKEND_LABEL = "isvtest-lb-backend"
_PROBE_ANSWER_IP = "203.0.113.55"  # RFC 5737 TEST-NET-3: reserved, never routable.
_RESOLVER_LABEL = "isvtest-coredns-resolver"
_ZONE_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*")


# ---------------------------------------------------------------------------
# K8S29 - LoadBalancer Service Support
# ---------------------------------------------------------------------------
class K8sLoadBalancerCheck(BaseValidation):
    """External / internal / static-IP LoadBalancer Service subtests.

    Config: probe_image, probe_port (8080), namespace_prefix, wait_timeout (300),
    poll_interval (5), annotations, internal_annotations (skips 'internal' if empty),
    static_ip (skips 'static_ip' if empty), static_ip_annotations.
    """

    description: ClassVar[str] = "Verify external, internal, and static-IP Kubernetes LoadBalancer Services."
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()
        probe_image = self.config.get("probe_image") or get_k8s_network_policy_image()
        probe_port = int(self.config.get("probe_port", 8080))
        namespace_prefix = self.config.get("namespace_prefix", "isvtest-lb")
        wait_timeout = int(self.config.get("wait_timeout", 300))
        poll_interval = max(1, int(self.config.get("poll_interval", 5)))

        try:
            base_annotations = _coerce_mapping(self.config.get("annotations"), "annotations")
            internal_annotations = _coerce_mapping(self.config.get("internal_annotations"), "internal_annotations")
            static_ip_annotations = _coerce_mapping(self.config.get("static_ip_annotations"), "static_ip_annotations")
        except ValueError as exc:
            self.set_failed(str(exc))
            return

        static_ip = str(self.config.get("static_ip") or "").strip()
        self._namespace = f"{namespace_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)
        ns_created = False
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            if not self._apply_backend_pod(probe_image, probe_port):
                return

            any_failed = False
            if not self._run_subtest(
                "external",
                "lb-external",
                probe_port,
                base_annotations,
                wait_timeout,
                poll_interval,
                expect_scope="public",
            ):
                any_failed = True

            if internal_annotations:
                if not self._run_subtest(
                    "internal",
                    "lb-internal",
                    probe_port,
                    {**base_annotations, **internal_annotations},
                    wait_timeout,
                    poll_interval,
                    expect_scope="private",
                ):
                    any_failed = True
            else:
                self.report_subtest(
                    "internal", passed=True, message="No internal-LB annotations; skipped", skipped=True
                )

            if static_ip:
                if not self._run_subtest(
                    "static_ip",
                    "lb-static-ip",
                    probe_port,
                    {**base_annotations, **static_ip_annotations},
                    wait_timeout,
                    poll_interval,
                    load_balancer_ip=static_ip,
                ):
                    any_failed = True
            else:
                self.report_subtest("static_ip", passed=True, message="No static_ip configured; skipped", skipped=True)

            if any_failed:
                self.set_failed("One or more LoadBalancer subtests failed; see subtest details")
            else:
                self.set_passed("LoadBalancer Service provisioning verified")
        finally:
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _apply_backend_pod(self, image: str, port: int) -> bool:
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": _BACKEND_POD_NAME, "namespace": self._namespace, "labels": {"app": _BACKEND_LABEL}},
            "spec": {
                "restartPolicy": "Never",
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 1000,
                    "runAsGroup": 1000,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "agnhost",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "args": ["netexec", f"--http-port={port}"],
                        "ports": [{"containerPort": port, "protocol": "TCP"}],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            },
        }
        if not self._apply(manifest, "backend pod"):
            return False
        wait_cmd = (
            f"{self._kubectl_base} wait --for=condition=Ready --timeout={self.timeout}s "
            f"-n {shlex.quote(self._namespace)} pod/{_BACKEND_POD_NAME}"
        )
        result = self.run_command(wait_cmd, timeout=self.timeout + 10)
        if result.exit_code != 0:
            self.set_failed(f"Backend pod did not become Ready: {result.stderr or result.stdout}")
            return False
        return True

    def _run_subtest(
        self,
        name: str,
        svc_name: str,
        port: int,
        annotations: dict[str, str],
        wait_timeout: int,
        poll_interval: int,
        load_balancer_ip: str | None = None,
        expect_scope: str | None = None,
    ) -> bool:
        spec: dict[str, Any] = {
            "type": "LoadBalancer",
            "selector": {"app": _BACKEND_LABEL},
            "ports": [{"port": 80, "targetPort": port, "protocol": "TCP"}],
        }
        if load_balancer_ip:
            spec["loadBalancerIP"] = load_balancer_ip
        manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": svc_name, "namespace": self._namespace, "annotations": dict(annotations)},
            "spec": spec,
        }
        if not self._apply(manifest, f"{name} Service"):
            self.report_subtest(name, passed=False, message=f"Failed to create Service {svc_name}")
            return False

        ingress = self._wait_for_ingress(svc_name, wait_timeout, poll_interval)
        if ingress is None:
            self.report_subtest(name, passed=False, message=f"ingress not populated within {wait_timeout}s")
            return False

        ip = ingress.get("ip")
        endpoint = ip or ingress.get("hostname") or "<empty>"
        if load_balancer_ip and ip != load_balancer_ip:
            self.report_subtest(name, passed=False, message=f"Requested {load_balancer_ip} but got {endpoint}")
            return False
        if expect_scope is not None:
            addresses = _ingress_addresses(ingress)
            if not addresses:
                self.report_subtest(
                    name,
                    passed=True,
                    skipped=True,
                    message=f"ingress={endpoint}; {expect_scope} address type not verified (no resolvable IP)",
                )
                return True
            for addr in addresses:
                try:
                    is_global = ipaddress.ip_address(addr).is_global
                except ValueError:
                    self.report_subtest(name, passed=False, message=f"Could not parse ingress address {addr!r}")
                    return False
                if is_global != (expect_scope == "public"):
                    self.report_subtest(
                        name,
                        passed=False,
                        message=f"Expected {expect_scope} ingress address but {addr} is "
                        f"{'public' if is_global else 'private'}",
                    )
                    return False
        self.report_subtest(name, passed=True, message=f"ingress={endpoint}")
        return True

    def _apply(self, manifest: dict[str, Any], label: str) -> bool:
        content = yaml.safe_dump(manifest, sort_keys=False)
        try:
            proc = subprocess.run(
                [*self._kubectl_parts, "apply", "-f", "-"],
                input=content,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except Exception as exc:
            self.set_failed(f"kubectl apply failed for {label}: {exc}")
            return False
        if proc.returncode != 0:
            self.set_failed(f"kubectl apply failed for {label}: {proc.stderr.strip() or proc.stdout.strip()}")
            return False
        return True

    def _wait_for_ingress(self, svc_name: str, wait_timeout: int, poll_interval: int) -> dict[str, Any] | None:
        cmd = f"{self._kubectl_base} get svc {shlex.quote(svc_name)} -n {shlex.quote(self._namespace)} -o json"
        deadline = time.time() + wait_timeout
        while True:
            result = self.run_command(cmd)
            if result.exit_code == 0:
                try:
                    payload = parse_kubectl_json(result, f"service {svc_name!r}")
                    ingress = ((payload.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
                    if ingress:
                        return ingress[0]
                except KubectlParseError as exc:
                    self.log.warning("Failed to parse Service %s: %s", svc_name, exc)
            if time.time() >= deadline:
                return None
            time.sleep(min(poll_interval, max(0.0, deadline - time.time())))


def _coerce_mapping(value: Any, field: str) -> dict[str, str]:
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


def _ingress_addresses(ingress: dict[str, Any]) -> list[str]:
    """Return the IPs behind a LoadBalancer ingress entry (resolves hostnames; [] if unresolvable)."""
    ip = ingress.get("ip")
    if ip:
        return [ip]
    hostname = ingress.get("hostname")
    if not hostname:
        return []
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(hostname, None)})
    except OSError:
        return []


# ---------------------------------------------------------------------------
# K8S30 - CoreDNS conditional forwarding
# ---------------------------------------------------------------------------
class K8sCoreDnsForwardingCheck(BaseValidation):
    """Patches the real coredns ConfigMap with a forwarding stub for a test zone,
    backed by a throwaway resolver, and verifies in-cluster DNS actually forwards.
    Skips (not fails) if there's no editable CoreDNS ConfigMap. Always restores it.
    """

    description: ClassVar[str] = "Verify CoreDNS conditional forwarding for a configured DNS zone."
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()
        self._coredns_namespace = self.config.get("coredns_namespace", "kube-system")
        self._coredns_configmap = self.config.get("coredns_configmap", "coredns")
        self._corefile_key = self.config.get("corefile_key", "Corefile")
        self._test_zone = str(self.config.get("test_zone") or "isvtest-forward.internal").strip(".")
        if not _ZONE_RE.fullmatch(self._test_zone):
            self.set_failed(f"test_zone is not a valid DNS name: {self._test_zone!r}")
            return
        namespace_prefix = self.config.get("namespace_prefix", "isvtest-coredns")
        resolver_image = self.config.get("resolver_image") or get_k8s_coredns_image()
        probe_image = self.config.get("probe_image") or get_k8s_dns_probe_image()
        propagation_timeout = int(self.config.get("propagation_timeout_s", 120))
        poll_interval = max(1, int(self.config.get("poll_interval_s", 5)))

        original = self._get_configmap()
        if original is None:
            return

        self._namespace = f"{namespace_prefix}-{uuid.uuid4().hex[:8]}"
        ns_quoted = shlex.quote(self._namespace)
        ns_created = False
        patched = False
        try:
            ns_result = self.run_command(f"{self._kubectl_base} create namespace {ns_quoted}")
            if ns_result.exit_code != 0:
                self.set_failed(f"Failed to create namespace {self._namespace}: {ns_result.stderr}")
                return
            ns_created = True

            resolver_ip = self._deploy_fake_resolver(resolver_image)
            if resolver_ip is None:
                return
            if not self._patch_forwarding(resolver_ip, original):
                return
            patched = True
            if not self._deploy_probe_pod(probe_image):
                return

            if self._wait_for_resolution(propagation_timeout, poll_interval):
                self.set_passed(f"Conditional forwarding verified via {resolver_ip} within {propagation_timeout}s")
            else:
                self.set_failed(f"probe.{self._test_zone} did not resolve within {propagation_timeout}s")
        finally:
            if patched:
                if not self._set_configmap(original):
                    self.log.warning("Failed to restore original CoreDNS Corefile - manual cleanup may be required")
            if ns_created:
                cleanup = self.run_command(
                    f"{self._kubectl_base} delete namespace {ns_quoted} --wait=false --ignore-not-found=true"
                )
                if cleanup.exit_code != 0:
                    self.log.warning("Namespace cleanup failed for %s: %s", self._namespace, cleanup.stderr)

    def _get_configmap(self) -> str | None:
        cmd = (
            f"{self._kubectl_base} get configmap {shlex.quote(self._coredns_configmap)} "
            f"-n {shlex.quote(self._coredns_namespace)} -o json"
        )
        result = self.run_command(cmd)
        if result.exit_code != 0:
            if is_resource_absent(result.stderr):
                pytest.skip("No editable CoreDNS ConfigMap found on this platform; K8S30 not assessable here")
            self.set_failed(f"Failed to read CoreDNS ConfigMap: {result.stderr}")
            return None
        try:
            payload = parse_kubectl_json(result, "coredns ConfigMap")
        except KubectlParseError as exc:
            self.set_failed(str(exc))
            return None
        corefile = (payload.get("data") or {}).get(self._corefile_key)
        if not isinstance(corefile, str):
            pytest.skip(f"CoreDNS ConfigMap has no '{self._corefile_key}' key; K8S30 not assessable here")
        return corefile

    def _deploy_fake_resolver(self, image: str) -> str | None:
        resolver_corefile = (
            f"{self._test_zone}:53 {{\n"
            f"    template IN A {self._test_zone} {{\n"
            f'        answer "{{{{ .Name }}}} 60 IN A {_PROBE_ANSWER_IP}"\n'
            "        fallthrough\n    }\n}\n"
        )
        docs = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "fake-resolver-corefile", "namespace": self._namespace},
                "data": {"Corefile": resolver_corefile},
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "fake-resolver",
                    "namespace": self._namespace,
                    "labels": {"app": _RESOLVER_LABEL},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "coredns",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "args": ["-conf", "/etc/coredns/Corefile"],
                            "ports": [{"containerPort": 53, "protocol": "UDP"}],
                            "volumeMounts": [{"name": "config", "mountPath": "/etc/coredns"}],
                        }
                    ],
                    "volumes": [{"name": "config", "configMap": {"name": "fake-resolver-corefile"}}],
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "fake-resolver", "namespace": self._namespace},
                "spec": {
                    "selector": {"app": _RESOLVER_LABEL},
                    "ports": [{"port": 53, "targetPort": 53, "protocol": "UDP"}],
                },
            },
        ]
        if not self._apply_yaml(yaml.safe_dump_all(docs, sort_keys=False), "fake resolver"):
            return None
        wait_cmd = (
            f"{self._kubectl_base} wait --for=condition=Ready --timeout={self.timeout}s "
            f"-n {shlex.quote(self._namespace)} pod/fake-resolver"
        )
        result = self.run_command(wait_cmd, timeout=self.timeout + 10)
        if result.exit_code != 0:
            self.set_failed(f"Fake resolver pod did not become Ready: {result.stderr or result.stdout}")
            return None
        svc_result = self.run_command(
            f"{self._kubectl_base} get svc fake-resolver -n {shlex.quote(self._namespace)} -o jsonpath={{.spec.clusterIP}}"
        )
        cluster_ip = (svc_result.stdout or "").strip()
        if svc_result.exit_code != 0 or not cluster_ip:
            self.set_failed(f"Failed to read fake resolver ClusterIP: {svc_result.stderr}")
            return None
        return cluster_ip

    def _patch_forwarding(self, resolver_ip: str, original: str) -> bool:
        new_corefile = (
            original + f"\n{self._test_zone}:53 {{\n    errors\n    cache 5\n    forward . {resolver_ip}\n}}\n"
        )
        return self._set_configmap(new_corefile)

    def _set_configmap(self, corefile: str) -> bool:
        patch = json.dumps({"data": {self._corefile_key: corefile}})
        cmd = (
            f"{self._kubectl_base} patch configmap {shlex.quote(self._coredns_configmap)} "
            f"-n {shlex.quote(self._coredns_namespace)} --type merge -p {shlex.quote(patch)}"
        )
        result = self.run_command(cmd)
        if result.exit_code != 0:
            self.set_failed(f"Failed to patch CoreDNS ConfigMap: {result.stderr}")
            return False
        return True

    def _deploy_probe_pod(self, image: str) -> bool:
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "dns-probe", "namespace": self._namespace},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {"name": "probe", "image": image, "imagePullPolicy": "IfNotPresent", "command": ["sleep", "3600"]}
                ],
            },
        }
        if not self._apply_yaml(yaml.safe_dump(manifest, sort_keys=False), "probe pod"):
            return False
        wait_cmd = (
            f"{self._kubectl_base} wait --for=condition=Ready --timeout={self.timeout}s "
            f"-n {shlex.quote(self._namespace)} pod/dns-probe"
        )
        result = self.run_command(wait_cmd, timeout=self.timeout + 10)
        if result.exit_code != 0:
            self.set_failed(f"Probe pod did not become Ready: {result.stderr or result.stdout}")
            return False
        return True

    def _wait_for_resolution(self, propagation_timeout: int, poll_interval: int) -> bool:
        target = f"probe.{self._test_zone}"
        cmd = f"{self._kubectl_base} exec -n {shlex.quote(self._namespace)} dns-probe -- nslookup {shlex.quote(target)}"
        deadline = time.time() + propagation_timeout
        while True:
            result = self.run_command(cmd, timeout=15)
            if result.exit_code == 0 and _PROBE_ANSWER_IP in result.stdout:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(min(poll_interval, max(0.0, deadline - time.time())))

    def _apply_yaml(self, content: str, label: str) -> bool:
        try:
            proc = subprocess.run(
                [*self._kubectl_parts, "apply", "-f", "-"],
                input=content,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except Exception as exc:
            self.set_failed(f"kubectl apply failed for {label}: {exc}")
            return False
        if proc.returncode != 0:
            self.set_failed(f"kubectl apply failed for {label}: {proc.stderr.strip() or proc.stdout.strip()}")
            return False
        return True


# ---------------------------------------------------------------------------
# K8S31 - Configurable CIDR ranges
# ---------------------------------------------------------------------------
class K8sCidrRangesCheck(BaseValidation):
    """Verifies live cluster state against expected_service_cidr / expected_pod_cidr /
    expected_node_cidr. Each is independently skippable when left as "" (default).
    """

    description: ClassVar[str] = "Verify configured service/pod/node CIDR ranges against live cluster state."
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        kubectl_base = get_kubectl_base_shell()
        try:
            expected_service_cidr = _parse_network(self.config.get("expected_service_cidr"), "expected_service_cidr")
            expected_pod_cidr = _parse_network(self.config.get("expected_pod_cidr"), "expected_pod_cidr")
            expected_node_cidr = _parse_network(self.config.get("expected_node_cidr"), "expected_node_cidr")
        except ValueError as exc:
            self.set_failed(str(exc))
            return

        if expected_service_cidr is None and expected_pod_cidr is None and expected_node_cidr is None:
            pytest.skip("No expected_service_cidr / expected_pod_cidr / expected_node_cidr configured")

        any_failed = False
        if expected_service_cidr is not None and not self._check_service_cidr(kubectl_base, expected_service_cidr):
            any_failed = True

        nodes: list[dict] | None = None
        if expected_pod_cidr is not None or expected_node_cidr is not None:
            nodes = self._get_nodes(kubectl_base)
            if nodes is None:
                return

        if expected_pod_cidr is not None and not self._check_pod_cidr(nodes, expected_pod_cidr):  # type: ignore[arg-type]
            any_failed = True
        if expected_node_cidr is not None and not self._check_node_cidr(nodes, expected_node_cidr):  # type: ignore[arg-type]
            any_failed = True

        if any_failed:
            self.set_failed("One or more configured CIDR ranges did not match live cluster state")
        else:
            self.set_passed("All configured CIDR ranges matched live cluster state")

    def _get_nodes(self, kubectl_base: str) -> list[dict] | None:
        result = self.run_command(f"{kubectl_base} get nodes -o json")
        items = kubectl_items_or_fail(self, result, "nodes")
        if items is None:
            return None
        if not items:
            self.set_failed("No nodes found in cluster")
            return None
        return items

    def _check_service_cidr(self, kubectl_base: str, expected: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
        result = self.run_command(f"{kubectl_base} get svc kubernetes -n default -o json")
        if result.exit_code != 0:
            self.report_subtest("service_cidr", passed=False, message=f"Failed to read Service: {result.stderr}")
            return False
        try:
            payload = parse_kubectl_json(result, "kubernetes Service")
        except KubectlParseError as exc:
            self.report_subtest("service_cidr", passed=False, message=str(exc))
            return False
        cluster_ip = (payload.get("spec") or {}).get("clusterIP")
        if not cluster_ip:
            self.report_subtest("service_cidr", passed=False, message="kubernetes Service has no clusterIP")
            return False
        try:
            in_range = ipaddress.ip_address(cluster_ip) in expected
        except ValueError:
            self.report_subtest("service_cidr", passed=False, message=f"Could not parse clusterIP {cluster_ip!r}")
            return False
        self.report_subtest(
            "service_cidr", passed=in_range, message=f"clusterIP={cluster_ip}, expected within {expected}"
        )
        return in_range

    def _check_pod_cidr(self, nodes: list[dict], expected: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
        any_failed = False
        for node in nodes:
            name = (node.get("metadata") or {}).get("name", "unknown")
            pod_cidr = (node.get("spec") or {}).get("podCIDR")
            if not pod_cidr:
                self.report_subtest(f"pod_cidr/{name}", passed=True, message="no podCIDR (CNI-managed)", skipped=True)
                continue
            try:
                network = ipaddress.ip_network(pod_cidr, strict=False)
                ok = network.version == expected.version and network.subnet_of(expected)
            except (ValueError, TypeError):
                ok = False
            if not ok:
                any_failed = True
            self.report_subtest(
                f"pod_cidr/{name}", passed=ok, message=f"podCIDR={pod_cidr}, expected within {expected}"
            )
        return not any_failed

    def _check_node_cidr(self, nodes: list[dict], expected: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
        any_failed = False
        for node in nodes:
            name = (node.get("metadata") or {}).get("name", "unknown")
            internal_ips = [
                a.get("address")
                for a in (node.get("status") or {}).get("addresses") or []
                if a.get("type") == "InternalIP"
            ]
            matched = next((ip for ip in internal_ips if _ip_in(ip, expected)), None)
            ok = matched is not None
            if not ok:
                any_failed = True
            msg = f"InternalIP={matched}, within {expected}" if ok else f"none of {internal_ips} within {expected}"
            self.report_subtest(f"node_cidr/{name}", passed=ok, message=msg)
        return not any_failed


def _ip_in(ip_str: str | None, network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    if not ip_str:
        return False
    try:
        return ipaddress.ip_address(ip_str) in network
    except ValueError:
        return False


def _parse_network(value: object, field: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid CIDR: {text!r} ({exc})") from exc
