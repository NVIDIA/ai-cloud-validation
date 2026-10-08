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

"""Exercise networking workflows and cleanup at their kubectl/HTTP boundaries."""

import http.client
import importlib.util
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from isvctl.config.output_schemas import validate_output
from isvtest.validations.k8s_networking import K8sCidrRangesCheck, K8sDnsForwardingCheck, K8sLoadBalancerCheck

SPEC = importlib.util.spec_from_file_location(
    "networking_probe", Path(__file__).resolve().parents[2] / "isvctl/configs/providers/shared/k8s/networking_probe.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ORIGINAL = ".:53 {\n  kubernetes cluster.local\n  forward . /etc/resolv.conf\n  reload\n}\n"
SETTINGS = {
    "dns_forwarding": {},
    "load_balancer": {
        "services": {
            "external": {},
            "internal": {"annotations": {"private": "true"}},
            "static": {"loadBalancerIP": "1.1.1.1"},
        },
        "static_ips": ["1.1.1.1"],
    },
    "cidr_ranges": {"ranges": {"service": ["10.96.0.0/16"], "node": ["192.168.49.0/24"], "pod": ["10.244.0.0/16"]}},
}
CHECKS = {
    "load_balancer": K8sLoadBalancerCheck,
    "dns_forwarding": K8sDnsForwardingCheck,
    "cidr_ranges": K8sCidrRangesCheck,
}


class Cluster:
    """Model cluster objects, DNS and response failures without replacing probe decisions."""

    def __init__(self) -> None:
        """Initialize existing CoreDNS and network range inventory."""
        self.core = {"metadata": {"uid": "original-uid"}, "data": {"Corefile": ORIGINAL, "other": "preserve"}}
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.commands: list[list[str]] = []
        self.fault = ""
        self.response = ""
        self.http_errors: list[http.client.HTTPException] = []
        self.namespace_created = False
        self.deleted = False
        self.patches = 0

    def command(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Respond to actual command arguments, enforcing compare-and-swap Corefile updates."""
        self.commands.append(args)
        a = args[1:]
        stdout, stderr, code = "", "", 0
        if a[0] == "api-resources":
            stdout = "" if self.fault == "service-api" else "servicecidrs.networking.k8s.io\n"
        elif a[:2] == ["create", "namespace"]:
            if self.fault == "namespace-conflict":
                code, stderr = 1, "AlreadyExists"
            else:
                self.namespace_created = True
        elif a[:2] == ["create", "-f"]:
            obj = json.loads(kwargs["input"])
            kind, name = obj["kind"].lower(), obj["metadata"]["name"]
            if kind == "pod":
                obj["status"] = {"podIPs": [{"ip": "10.244.0.7"}]}
                if name == "backend":
                    self.response = obj["spec"]["containers"][0]["env"][0]["value"]
            if kind == "service":
                obj["spec"].setdefault("type", "ClusterIP")
                obj["spec"]["clusterIPs"] = ["10.96.1.7"]
                obj["spec"]["clusterIP"] = "10.96.1.7"
                if name in ("external", "internal", "static"):
                    obj["status"] = {
                        "loadBalancer": {
                            "ingress": [
                                {"ip": {"external": "8.8.8.8", "internal": "10.1.2.3", "static": "1.1.1.1"}[name]}
                            ]
                        }
                    }
                    if self.fault == "hostname":
                        obj["status"]["loadBalancer"]["ingress"] = [{"hostname": name + ".test"}]
            self.objects[(kind, name)] = obj
        elif a[0] == "get":
            kind, name = a[1], a[2] if not a[2].startswith("-") else ""
            if kind == "configmap" and name == "coredns":
                value = None if self.fault == "no-coredns" else self.core
            elif kind == "service" and name == "kubernetes":
                value = {"spec": {"clusterIPs": ["10.96.0.1"]}}
            elif kind == "nodes":
                value = {
                    "items": [
                        {
                            "spec": {"podCIDRs": [] if self.fault == "no-pod-cidr" else ["10.244.0.0/24"]},
                            "status": {"addresses": [{"type": "InternalIP", "address": "192.168.49.2"}]},
                        }
                    ]
                }
            elif kind == "servicecidrs.networking.k8s.io":
                value = {"items": [{"spec": {"cidrs": ["10.96.0.0/16"]}}]}
            else:
                value = self.objects[(kind, name)]
            stdout = json.dumps(value) if value is not None else ""
        elif a[0] == "wait":
            if self.fault == "pod-timeout":
                raise subprocess.TimeoutExpired(args, 180)
        elif a[0] == "exec":
            script = a[a.index("-c") + 1]
            if script == MODULE.RESOLVE:
                if a[-1] == "internal.test":
                    stdout = json.dumps(["10.1.2.3"])
                else:
                    stdout = json.dumps(["10.96.0.1"] if a[-1].startswith("kubernetes.") else ["198.18.0.42"])
            elif script == MODULE.HTTP_CLIENT:
                if self.fault == "internal-network":
                    code, stderr = 1, "Connection timed out"
                else:
                    stdout = json.dumps([{"address": ip, "body": self.response} for ip in json.loads(a[-1])])
            else:
                raise AssertionError(script)
        elif a[0] == "logs":
            corefile = self.objects[("configmap", "upstream")]["data"]["Corefile"]
            stdout = "" if self.fault == "no-forward" else corefile
        elif a[0] == "patch":
            patch = json.loads(a[a.index("-p") + 1])
            if self.patches and self.fault == "restore":
                code, stderr = 1, "API unavailable during restore"
            elif (
                patch[0]["value"] != self.core["metadata"]["uid"] or patch[1]["value"] != self.core["data"]["Corefile"]
            ):
                code, stderr = 1, "compare-and-swap test failed"
            else:
                self.patches += 1
                self.core["data"]["Corefile"] = patch[2]["value"]
                if self.patches == 1 and self.fault == "concurrent":
                    self.core["data"]["Corefile"] += "\n# concurrent operator edit\n"
                if self.patches == 1 and self.fault == "replacement":
                    self.core["metadata"]["uid"] = "replacement-uid"
        elif a[:2] == ["delete", "namespace"]:
            if self.fault == "cleanup":
                code, stderr = 1, "namespace finalizers timed out"
            else:
                self.deleted = True
        else:
            raise AssertionError(a)
        if self.fault == "forbidden" and a[0] == "get":
            code, stderr = 1, "Forbidden"
        return subprocess.CompletedProcess(args, code, stdout, stderr)


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> Cluster:
    """Replace only external commands, HTTP requests and waiting."""
    cluster = Cluster()
    monkeypatch.setattr(MODULE.subprocess, "run", cluster.command)
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.delenv("KUBECTL", raising=False)
    monkeypatch.setattr(MODULE.time, "sleep", lambda seconds: None)
    ticks = iter(range(0, 100000, 200))
    monkeypatch.setattr(MODULE.time, "monotonic", lambda: next(ticks))

    class Response:
        """Supply the actual fixture nonce through a modeled external HTTP connection."""

        def __enter__(self) -> "Response":
            """Open response."""
            return self

        def __exit__(self, *args: Any) -> None:
            """Close response."""

        def read(self, size: int) -> bytes:
            """Return the correct or deliberately incorrect backend body."""
            if cluster.http_errors:
                raise cluster.http_errors.pop(0)
            return ("wrong backend" if cluster.fault == "wrong-response" else cluster.response).encode()

    def open_url(url: str, **kwargs: Any) -> Response:
        """Model a public request failure independently of Kubernetes status."""
        if cluster.fault == "external-network":
            raise OSError("Connection timed out")
        if cluster.http_errors and isinstance(cluster.http_errors[0], http.client.BadStatusLine):
            raise cluster.http_errors.pop(0)
        return Response()

    monkeypatch.setattr(MODULE.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_url))
    monkeypatch.setattr(
        MODULE.socket,
        "getaddrinfo",
        lambda host, *args: [(2, 1, 6, "", ({"external.test": "8.8.8.8", "static.test": "1.1.1.1"}[host], 0))],
    )
    return cluster


def run(name: str) -> dict[str, Any]:
    """Execute the unchanged workflow and validate its JSON schema."""
    output = MODULE.run(name, deepcopy(SETTINGS[name]))
    valid, errors = validate_output(output, "k8s_networking")
    assert valid, errors
    return output


@pytest.mark.parametrize("name", CHECKS)
def test_live_workflows_collect_evidence_and_cleanup(cluster: Cluster, name: str) -> None:
    """Successful external operations produce evidence accepted by the real validator."""
    output = run(name)
    check = CHECKS[name](config={"step_output": output})
    check.run()
    assert check.passed, check.message
    assert cluster.deleted
    assert cluster.core["data"] == {"Corefile": ORIGINAL, "other": "preserve"}
    assert all("--kubeconfig" not in c for c in cluster.commands)


@pytest.mark.parametrize("name", CHECKS)
@pytest.mark.parametrize("fault", ["pod-timeout", "namespace-conflict", "cleanup"])
def test_execution_and_cleanup_errors_fail(cluster: Cluster, name: str, fault: str) -> None:
    """Failed execution is never a skip; namespace conflicts never delete existing resources."""
    cluster.fault = fault
    output = run(name)
    assert output["success"] is False and not output.get("skipped")
    assert cluster.deleted == (fault == "pod-timeout")


@pytest.mark.parametrize("fault", ["internal-network", "external-network"])
def test_assigned_ingress_without_connectivity_fails(cluster: Cluster, fault: str) -> None:
    """LoadBalancer status cannot substitute for completed HTTP requests."""
    cluster.fault = fault
    output = run("load_balancer")
    assert not output["success"] and cluster.deleted


@pytest.mark.parametrize(
    "error", [http.client.BadStatusLine("invalid status"), http.client.IncompleteRead(b"partial", 20)]
)
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_http_protocol_failure_preserves_cleanup(
    cluster: Cluster, error: http.client.HTTPException, cleanup_fails: bool
) -> None:
    """Opening or reading malformed HTTP responses yields failure JSON and cleanup evidence."""
    cluster.http_errors = [error]
    cluster.fault = "cleanup" if cleanup_fails else ""
    output = run("load_balancer")
    assert output["success"] is False and not output.get("skipped")
    assert "Timed out waiting for networking probe" in output["error"]
    assert str(error) in output["error"]
    assert bool(output.get("cleanup_errors")) == cleanup_fails
    assert cluster.deleted == (not cleanup_fails)


@pytest.mark.parametrize(
    "error", [http.client.BadStatusLine("invalid status"), http.client.IncompleteRead(b"partial", 20)]
)
def test_transient_http_protocol_failure_retries(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, error: http.client.HTTPException
) -> None:
    """An endpoint can recover from a malformed response within the convergence deadline."""
    ticks = iter(range(1000))
    monkeypatch.setattr(MODULE.time, "monotonic", lambda: next(ticks))
    cluster.http_errors = [error]
    output = run("load_balancer")
    check = K8sLoadBalancerCheck(config={"step_output": output})
    check.run()
    assert output["success"] and check.passed and cluster.deleted
    assert not cluster.http_errors


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_unretried_http_protocol_failure_preserves_json(
    cluster: Cluster, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    """The outer guard preserves failure and cleanup evidence for an unwrapped HTTP error."""

    def fail(probe: MODULE.Probe) -> None:
        """Raise outside the retry helper after registering cleanup."""
        probe.begin()
        raise http.client.BadStatusLine("invalid status")

    monkeypatch.setattr(MODULE.Probe, "load_balancer", fail)
    cluster.fault = "cleanup" if cleanup_fails else ""
    output = run("load_balancer")
    assert output["success"] is False and not output.get("skipped")
    assert output["error"] == "invalid status"
    assert bool(output.get("cleanup_errors")) == cleanup_fails
    assert cluster.deleted == (not cleanup_fails)


def test_wrong_backend_reaches_validator_and_fails(cluster: Cluster) -> None:
    """A working endpoint returning another application's body is not a successful test."""
    cluster.fault = "wrong-response"
    output = run("load_balancer")
    check = K8sLoadBalancerCheck(config={"step_output": output})
    check.run()
    assert not check.passed and cluster.deleted


def test_hostname_ingress_is_resolved_and_contacted(cluster: Cluster) -> None:
    """Cloud ingress hostnames require observed IPs and completed backend requests."""
    cluster.fault = "hostname"
    output = run("load_balancer")
    check = K8sLoadBalancerCheck(config={"step_output": output})
    check.run()
    assert check.passed, check.message
    assert any("internal.test" in command for command in cluster.commands if "exec" in command)


@pytest.mark.parametrize("fault", ["restore", "replacement"])
def test_failed_dns_restore_fails_but_still_removes_namespace(cluster: Cluster, fault: str) -> None:
    """Restore errors remain visible and never overwrite a recreated ConfigMap."""
    cluster.fault = fault
    output = run("dns_forwarding")
    assert not output["success"] and output["cleanup_errors"] and cluster.deleted
    if fault == "replacement":
        assert cluster.patches == 1


def test_dns_restore_preserves_concurrent_operator_edit(cluster: Cluster) -> None:
    """Cleanup removes only our unique zone block, not someone else's ConfigMap changes."""
    cluster.fault = "concurrent"
    output = run("dns_forwarding")
    assert output["success"] and output["rule_restored"]
    assert cluster.core["data"]["Corefile"] == ORIGINAL + "\n# concurrent operator edit\n"


def test_matching_dns_answer_without_upstream_query_fails(cluster: Cluster) -> None:
    """A matching cached or unrelated answer is not proof of conditional forwarding."""
    cluster.fault = "no-forward"
    output = run("dns_forwarding")
    check = K8sDnsForwardingCheck(config={"step_output": output})
    check.run()
    assert not check.passed and output["rule_restored"]


@pytest.mark.parametrize(
    "name,fault", [("dns_forwarding", "no-coredns"), ("cidr_ranges", "service-api"), ("cidr_ranges", "no-pod-cidr")]
)
def test_absent_prerequisite_skips_before_mutation(cluster: Cluster, name: str, fault: str) -> None:
    """Unsupported API/CNI and absent DNS components are clear prerequisite skips."""
    cluster.fault = fault
    output = run(name)
    assert output["skipped"] and not output["success"] and not cluster.namespace_created


def test_forbidden_api_is_failure_not_missing_component(cluster: Cluster) -> None:
    """Authorization failures must not masquerade as absent prerequisites."""
    cluster.fault = "forbidden"
    output = run("dns_forwarding")
    assert not output["success"] and not output.get("skipped")


@pytest.mark.parametrize("name", CHECKS)
def test_no_configuration_skips(cluster: Cluster, name: str) -> None:
    """Default invocation never mutates a cluster without explicit per-probe configuration."""
    output = MODULE.run(name, None)
    assert output["skipped"] and not cluster.commands


def test_missing_executable_skips_but_existing_127_fails(cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only absent executables are prerequisites; runtime exit 127 remains failure."""
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: None)
    assert MODULE.run("dns_forwarding", {})["skipped"]
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 127, "", "runtime helper absent"),
    )
    output = MODULE.run("dns_forwarding", {})
    assert not output["success"] and not output.get("skipped")


@pytest.mark.parametrize(
    "name,settings",
    [
        ("load_balancer", {"services": SETTINGS["load_balancer"]["services"], "static_ips": ["not-an-ip"]}),
        ("cidr_ranges", {"ranges": {"service": ["bad"], "node": ["10.0.0.0/8"], "pod": ["10.244.0.0/16"]}}),
        ("dns_forwarding", {"timeout": True}),
        ("dns_forwarding", "not-an-object"),
    ],
)
def test_invalid_settings_fail_before_mutation(cluster: Cluster, name: str, settings: Any) -> None:
    """Malformed explicit settings are errors, not skips or partially created fixtures."""
    output = MODULE.run(name, settings)
    assert not output["success"] and not output.get("skipped") and not cluster.namespace_created


def test_suite_and_provider_wire_all_three_checks() -> None:
    """Each canonical ID binds the matching executable mode and typed output contract."""
    root = Path(__file__).resolve().parents[2]
    suite = yaml.safe_load((root / "isvctl/configs/suites/k8s.yaml").read_text())
    provider = yaml.safe_load((root / "isvctl/configs/providers/k8s-networking.yaml").read_text())
    checks = suite["tests"]["validations"]["k8s_networking"]["checks"]
    steps = {s["name"]: s for s in provider["commands"]["kubernetes"]["steps"]}
    for mode, cls in CHECKS.items():
        config = checks[cls.__name__]
        step = steps[config["step"]]
        assert step["args"] == ["--check=" + mode]
        assert step["output_schema"] == "k8s_networking"
        assert step["continue_on_failure"] is True
        assert step["timeout"] >= {"load_balancer": 4000, "dns_forwarding": 2500, "cidr_ranges": 1500}[mode]
