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

"""Tests for the Firebird network config and scripts.

Uses the fake-API harness in ``harness.py``; SSH probes are replaced in each
script's namespace. Script output is fed to the real validation classes with
the parameters the network suite wires (composite checks run member by member).
"""

from __future__ import annotations

import ipaddress
import itertools
import re
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from isvtest.core.validation import BaseValidation
from isvtest.validations import generic
from isvtest.validations import network as network_checks
from isvtest.validations.network import (
    BackendSwitchFabricCheck,
    DhcpIpManagementCheck,
    NetworkConnectivityCheck,
    NetworkProvisionedCheck,
    SdnFilterAuditTrailCheck,
    SgNodeScopingCheck,
    SgPolicyPropagationTimingCheck,
    SgSubnetScopingCheck,
    StableEgressIpCheck,
    StablePrivateIpCheck,
    TrafficFlowCheck,
)

from .harness import PROJECT, SCRIPTS, SUITES, FakeApi, Route, config_steps, load, operation, render, run, validate

VPCS = f"/projects/{PROJECT}/network/vpcs"
BM_ARGS = ["--instance-id", "bm.A", "--key-file", "/tmp/a"]
PAIR_ARGS = [*BM_ARGS, "--peer-id", "bm.B", "--peer-key-file", "/tmp/b"]


def _check(check_cls: type[BaseValidation], output: dict[str, Any]) -> BaseValidation:
    """Run a network-suite validation class on step output."""
    return validate("network", check_cls, output)


def _composite(name: str, output: dict[str, Any]) -> list[str]:
    """Run a composite network check member by member; return the failures."""
    for group in yaml.safe_load((SUITES / "network.yaml").read_text())["tests"]["validations"].values():
        if name in (group.get("checks") or {}):
            compose = group["checks"][name]["compose"]
            break
    else:
        raise AssertionError(f"{name} not in the network suite")
    failures = []
    for entry in compose:
        member, params = (entry, {}) if isinstance(entry, str) else next(iter(entry.items()))
        cls = getattr(generic, member, None) or getattr(network_checks, member)
        check = cls(config={"step_output": output, **(params or {})})
        check.run()
        if not check._passed:
            failures.append(f"{member}: {check._error}")
    return failures


def _bm(bm_id: str, ip: str, subnet: str = "subnet.S", state: str = "RUNNING") -> dict[str, Any]:
    """Return a BM GET response."""
    return {"bm": {"id": bm_id, "state": state, "powerState": "ON", "ipAddress": ip, "subnetId": subnet}}


def _vpcs_with_subnets(*subnets: tuple[str, str]) -> dict[str, Any]:
    """Return one VPC holding ``(subnet_id, cidr)`` subnets."""
    return {"items": [{"vpc": {"id": "vpc.V"}, "subnets": [{"id": s, "cidr": c} for s, c in subnets]}]}


def _pair_routes(peer_subnet: str = "subnet.S") -> dict[str, Route]:
    """Return routes for a RUNNING BM pair in one VPC."""
    return {
        f"GET /projects/{PROJECT}/compute/bms/bm.A": _bm("bm.A", "172.16.240.10"),
        f"GET /projects/{PROJECT}/compute/bms/bm.B": _bm("bm.B", "172.16.240.200", subnet=peer_subnet),
        f"GET /projects/{PROJECT}/network/vpcs-with-subnets": _vpcs_with_subnets(
            ("subnet.S", "172.16.240.0/25"), ("subnet.T", "172.16.240.128/25")
        ),
    }


class Rules:
    """Stateful fake of one VPC's firewall-rule routes."""

    def __init__(self, vpc_id: str = "vpc.V") -> None:
        """Start with no rules."""
        self.base = f"{VPCS}/{vpc_id}/firewall-rules"
        self.rules: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def routes(self, *rule_ids: str) -> dict[str, Route]:
        """Return routes for creating and managing rules that will get ``rule_ids``."""
        ids = iter(rule_ids)

        def create(body: dict[str, Any] | None, _q: Any) -> dict[str, Any]:
            rule_id = next(ids)
            self.rules[rule_id] = {"id": rule_id, "state": "READY", **(body or {})}
            self.created.append(body or {})
            return operation(rule_id)

        routes: dict[str, Route] = {
            f"POST {self.base}": create,
            f"GET {self.base}": lambda _b, _q: {"items": list(self.rules.values())},
        }
        for rule_id in rule_ids:
            routes[f"GET {self.base}/{rule_id}"] = self._get(rule_id)
            routes[f"PUT {self.base}/{rule_id}"] = self._put(rule_id)
            routes[f"DELETE {self.base}/{rule_id}"] = self._delete(rule_id)
        return routes

    def _get(self, rule_id: str) -> Route:
        def get(_b: Any, _q: Any) -> dict[str, Any]:
            if rule_id not in self.rules:
                raise NotFound()
            return {"firewallRule": self.rules[rule_id]}

        return get

    def _put(self, rule_id: str) -> Route:
        def put(body: dict[str, Any] | None, _q: Any) -> dict[str, Any]:
            self.rules[rule_id].update(body or {})
            return operation(rule_id)

        return put

    def _delete(self, rule_id: str) -> Route:
        def delete(_b: Any, _q: Any) -> dict[str, Any]:
            self.rules.pop(rule_id, None)
            self.deleted.append(rule_id)
            return operation(rule_id)

        return delete


class NotFound(Exception):
    """Raised by a fake route to stand for HTTP 404; translated by ``_with_404``."""


def _with_404(routes: dict[str, Route], api_module: Any) -> dict[str, Route]:
    """Wrap callable routes so a ``NotFound`` becomes the client's 404 error."""
    wrapped: dict[str, Route] = {}
    for key, route in routes.items():
        if callable(route):

            def call(body: Any, query: Any, route: Any = route, key: str = key) -> dict[str, Any]:
                try:
                    return route(body, query)
                except NotFound:
                    raise api_module.FirebirdApiError(f"{key}: HTTP 404", status=404) from None

            wrapped[key] = call
        else:
            wrapped[key] = route
    return wrapped


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: str,
    routes: dict[str, Route],
    argv: list[str] | None = None,
    patches: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], Any]:
    """Run a network script, translating ``NotFound`` and applying module patches."""

    def prepare(module: Any) -> None:
        for name, value in (patches or {}).items():
            monkeypatch.setattr(module, name, value)

    return run(monkeypatch, capsys, script, routes, argv, prepare=prepare, wrap=_with_404)


# ── Config ────────────────────────────────────────────────────────────


def test_config_wires_suite_step_names_and_orders_teardown_last() -> None:
    """Every configured step is a network-suite step; setup first, teardown last."""
    suite_steps = set()
    for group in yaml.safe_load((SUITES / "network.yaml").read_text())["tests"]["validations"].values():
        suite_steps.add(group.get("step"))
        suite_steps.update(check.get("step") for check in (group.get("checks") or {}).values())
    steps = config_steps("network", "network")

    assert set(steps) <= suite_steps
    names = list(steps)
    assert names[0] == "create_network"
    assert names[-1] == "teardown"
    assert all(step.phase == "test" for name, step in steps.items() if name not in ("create_network", "teardown"))


def test_config_carries_no_labels() -> None:
    """Labels live only in suites (AGENTS.md)."""
    text = (SUITES.parent / "providers" / "firebird" / "config" / "network.yaml").read_text()

    assert "labels:" not in text


def test_bm_steps_render_empty_without_configured_bms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset BM variables render empty values, so the scripts take their skip path."""
    for name in ("BM_INSTANCE_ID", "BM_KEY_FILE", "FIREBIRD_PEER_BM_ID", "FIREBIRD_PEER_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)

    assert render("network", "network", "traffic_validation", {})[:4] == [
        "--instance-id=",
        "--key-file=",
        "--peer-id=",
        "--peer-key-file=",
    ]


def test_teardown_deletes_what_create_network_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """The network teardown renders the created subnets and the VPC flag, and deletes unless kept."""
    monkeypatch.delenv("NETWORK_SKIP_TEARDOWN", raising=False)
    created = {"network_id": "vpc.N", "created_vpc": True, "created_subnet_ids": ["subnet.1", "subnet.2"]}
    args = render("network", "network", "teardown", {"create_network": created})

    assert args[:3] == ["--vpc-id=vpc.N", "--subnet-ids=subnet.1,subnet.2", "--delete-vpc"]
    assert "--skip-destroy" not in args
    assert render("network", "network", "teardown", {})[:3] == [
        "--vpc-id=",
        "--subnet-ids=",
        "--keep-var=NETWORK_SKIP_TEARDOWN",
    ]


@pytest.mark.parametrize("step", ["sg_crud", "sg_policy_propagation", "sdn_filter_audit_trail"])
def test_probe_rule_steps_target_the_run_network_subnet_a(step: str) -> None:
    """Probe rules need a prefix inside a subnet, so the rule steps get the run's VPC and subnet A."""
    created = {"network_id": "vpc.N", "subnets": [{"subnet_id": "subnet.1"}, {"subnet_id": "subnet.2"}]}
    args = render("network", "network", step, {"create_network": created})

    assert _flag_values(args, "--vpc-id") == ["vpc.N"]
    assert _flag_values(args, "--subnet-id") == ["subnet.1"]


def test_create_network_passes_a_supplied_network_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """FIREBIRD_NETWORK_VPC_ID + _SUBNET_ID_A/_B reach create_network, so a kept network is reused."""
    monkeypatch.setenv("FIREBIRD_NETWORK_VPC_ID", "vpc.K")
    monkeypatch.setenv("FIREBIRD_NETWORK_SUBNET_ID_A", "subnet.KA")
    monkeypatch.setenv("FIREBIRD_NETWORK_SUBNET_ID_B", "subnet.KB")
    args = render("network", "network", "create_network", {})

    assert "--vpc-id=vpc.K" in args
    assert [a for a in args if a.startswith("--subnet-id=")] == ["--subnet-id=subnet.KA", "--subnet-id=subnet.KB"]


def test_network_teardown_keeps_the_network_on_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """NETWORK_SKIP_TEARDOWN=true keeps a created network and names the variables that reuse it."""
    created = {"network_id": "vpc.N", "created_vpc": True, "created_subnet_ids": ["subnet.1", "subnet.2"]}
    monkeypatch.setenv("NETWORK_SKIP_TEARDOWN", "true")
    args = render("network", "network", "teardown", {"create_network": created})

    assert "--skip-destroy" in args
    assert "--keep-var=NETWORK_SKIP_TEARDOWN" in args


def test_supplied_network_is_neither_created_nor_deleted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A supplied VPC and two subnets pass through with no API call, and teardown then skips."""
    code, out, api = _run(
        monkeypatch,
        capsys,
        "network/create_network.py",
        {},
        ["--vpc-id=vpc.K", "--subnet-id=subnet.KA", "--subnet-id=subnet.KB"],
    )

    assert code == 0, out
    assert out["subnets"] == [{"subnet_id": "subnet.KA"}, {"subnet_id": "subnet.KB"}]
    assert out["created_subnet_ids"] == [] and out["created_vpc"] is False
    assert api.calls == []
    assert render("network", "network", "teardown", {"create_network": out})[:2] == [
        "--vpc-id=vpc.K",
        "--subnet-ids=",
    ]


def test_kept_network_hint_names_the_network_suite_variables(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The kept-network hint prints the IDs under the variables the network config reads."""
    argv = [
        "--vpc-id=vpc.N",
        "--subnet-ids=subnet.1,subnet.2",
        "--delete-vpc",
        "--skip-destroy",
        "--keep-var=NETWORK_SKIP_TEARDOWN",
        "--reuse-vars=FIREBIRD_NETWORK_VPC_ID,FIREBIRD_NETWORK_SUBNET_ID_A,FIREBIRD_NETWORK_SUBNET_ID_B",
    ]
    code, out, api = _run(monkeypatch, capsys, "network/teardown_network.py", {}, argv)

    assert code == 0 and api.calls == []
    assert "NETWORK_SKIP_TEARDOWN=true" in out["skip_reason"]
    assert (
        "FIREBIRD_NETWORK_VPC_ID=vpc.N FIREBIRD_NETWORK_SUBNET_ID_A=subnet.1 FIREBIRD_NETWORK_SUBNET_ID_B=subnet.2"
        in out["skip_reason"]
    )


CIDR_ENV = (
    "FIREBIRD_VPC_CIDR",
    "FIREBIRD_SUBNET_CIDR",
    "FIREBIRD_NETWORK_VPC_CIDR",
    "FIREBIRD_NETWORK_SUBNET_CIDR_A",
    "FIREBIRD_NETWORK_SUBNET_CIDR_B",
    "FIREBIRD_CRUD_VPC_CIDR",
    "FIREBIRD_IR_VPC_CIDR",
    "FIREBIRD_IR_SUBNET_CIDR",
)


def _flag_values(args: list[str], name: str) -> list[str]:
    """Return every value passed as ``name value`` in rendered args."""
    return [args[i + 1] for i, arg in enumerate(args) if arg == name]


def _script_default(script: str, flag: str) -> str:
    """Return the argparse default of ``flag`` in a network script."""
    source = (SCRIPTS / "network" / script).read_text()
    match = re.search(rf'add_argument\("{flag}", default="([^"]+)"', source)
    assert match, f"{flag} default not found in {script}"
    return match.group(1)


def test_default_cidrs_never_overlap_across_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API rejects overlapping VPC or subnet CIDRs project-wide.

    Each config and CRUD script creates its own network, and a bare_metal run
    kept with BM_SKIP_TEARDOWN leaves its network in place, so every default VPC
    must be disjoint from the others, and every subnet from every other subnet.
    """
    for name in CIDR_ENV:
        monkeypatch.delenv(name, raising=False)
    bm = render("bare_metal", "bare_metal", "create_network", {})
    net = render("network", "network", "create_network", {})
    crud = render("network", "network", "vpc_crud", {})
    image_registry = render("image-registry", "image_registry", "create_network", {})
    vpcs = {
        "bare_metal": (_flag_values(bm, "--vpc-cidr")[0], _flag_values(bm, "--subnet-cidr")),
        "image_registry": (
            _flag_values(image_registry, "--vpc-cidr")[0],
            _flag_values(image_registry, "--subnet-cidr"),
        ),
        "network": (_flag_values(net, "--vpc-cidr")[0], _flag_values(net, "--subnet-cidr")),
        "vpc_crud": (_flag_values(crud, "--cidr")[0], []),
    }
    # The CRUD scripts' own defaults are the same networks as the config's.
    assert _script_default("vpc_crud_test.py", "--cidr") == vpcs["vpc_crud"][0]
    assert len(vpcs["network"][1]) == 2

    networks = {name: ipaddress.ip_network(vpc) for name, (vpc, _) in vpcs.items()}
    for (a, net_a), (b, net_b) in itertools.combinations(networks.items(), 2):
        assert not net_a.overlaps(net_b), f"{a} VPC {net_a} overlaps {b} VPC {net_b}"
    subnets = [(name, ipaddress.ip_network(s)) for name, (_, subs) in vpcs.items() for s in subs]
    for name, subnet in subnets:
        assert subnet.subnet_of(networks[name]), f"{name} subnet {subnet} is outside its VPC"
    for (a, sub_a), (b, sub_b) in itertools.combinations(subnets, 2):
        assert not sub_a.overlaps(sub_b), f"{a} subnet {sub_a} overlaps {b} subnet {sub_b}"


# ── Shared helpers ────────────────────────────────────────────────────


def test_echo_request_rule_matches_requests_only_and_statelessly() -> None:
    """Replies must not match: stateful or any-ICMP would block the uncovered direction too."""
    network = load("network/traffic_test.py")._common["network"]

    rule = network.echo_request_rule("isv-x", "DENY", "10.0.0.1/32", "0.0.0.0/0")

    assert rule["protocol"] == "ICMP"
    assert rule["icmpType"] == 8
    assert rule["stateful"] is False
    assert (rule["srcPrefix"], rule["dstPrefix"], rule["action"]) == ("10.0.0.1/32", "0.0.0.0/0", "DENY")


def test_tcp_rule_always_carries_the_full_source_port_range() -> None:
    """Some deployments reject a TCP rule without a source port range, so the builder always sends 1-65535."""
    network = load("network/sg_crud_test.py")._common["network"]

    rule = network.tcp_rule("isv-x", "DENY", "192.0.2.1/32", "172.16.243.126/32", 9)

    assert (rule["srcPortFrom"], rule["srcPortTo"]) == (1, 65535)
    assert (rule["dstPortFrom"], rule["dstPortTo"]) == (9, 9)
    assert (rule["protocol"], rule["action"]) == ("TCP", "DENY")
    assert (rule["srcPrefix"], rule["dstPrefix"]) == ("192.0.2.1/32", "172.16.243.126/32")


@pytest.mark.parametrize(
    ("cidr", "host"),
    [
        ("172.16.243.0/25", "172.16.243.126/32"),
        ("172.16.243.128/25", "172.16.243.254/32"),
        ("10.0.0.0/30", "10.0.0.2/32"),
    ],
)
def test_probe_host_is_a_host_inside_the_subnet(cidr: str, host: str) -> None:
    """The in-deployment side of a probe rule is one usable host of the subnet, never the gateway."""
    network = load("network/sg_crud_test.py")._common["network"]

    assert network.probe_host(cidr) == host
    assert ipaddress.ip_network(host).subnet_of(ipaddress.ip_network(cidr))
    assert host != f"{network.gateway_ip(cidr)}/32"


def test_rule_guard_deletes_a_rule_whose_create_operation_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepted create exists server-side even when its Operation fails, so cleanup deletes it."""
    module = load("network/traffic_test.py")
    network, fb = module._common["network"], module._fb
    rules = Rules()
    routes = rules.routes("firewall-rule.F")
    create = routes[f"POST {rules.base}"]
    routes[f"POST {rules.base}"] = lambda b, q: {"operation": {**create(b, q)["operation"], "status": "FAILED"}}
    api = FakeApi(module, _with_404(routes, fb))
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FIREBIRD_BEARER_TOKEN", "t")
    monkeypatch.setattr(fb.FirebirdClient, "_send", api.send)
    guard = network.RuleGuard(fb.FirebirdClient(), "vpc.V")

    with pytest.raises(fb.FirebirdApiError, match="failed"):
        guard.create(network.echo_request_rule("isv-x", "DENY", "0.0.0.0/0", "10.0.0.1/32"), 60)

    assert guard.rule_ids == ["firewall-rule.F"]
    assert guard.cleanup(60) == []
    assert rules.deleted == ["firewall-rule.F"]
    assert guard.rule_ids == []


PING_OK = (
    "PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.\n"
    "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
    "rtt min/avg/max/mdev = 0.101/0.250/0.402/0.050 ms\n"
)
HOST = SimpleNamespace(bm_id="bm.A", ip="10.0.0.1", user="ubuntu", key_file="/tmp/a")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ((0, PING_OK, ""), (True, 0.25)),
        ((1, "3 packets transmitted, 0 received, 100% packet loss\n", ""), (False, None)),
        ((124, "", "TimeoutExpired"), (False, None)),
        ((255, "", "ssh: connect to host 10.0.0.1 port 22: No route to host"), (False, None)),
    ],
    ids=["reply", "no-reply", "ssh-timeout", "ssh-unreachable"],
)
def test_ping_reads_the_exit_code_and_average_rtt(
    answer: tuple[int, str, str], expected: tuple[bool, float | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only exit 0 is a reply; the average RTT comes from ping's summary line."""
    probes = load("network/traffic_test.py")._common["probes"]
    commands: list[str] = []

    def fake_ssh(host: str, user: str, key: str, command: str, *, timeout: int) -> tuple[int, str, str]:
        commands.append(command)
        assert (host, user, key) == ("10.0.0.1", "ubuntu", "/tmp/a")
        return answer

    monkeypatch.setattr(probes, "ssh_run", fake_ssh)

    assert probes.ping(HOST, "10.0.0.2") == expected
    assert commands == ["ping -c 3 -W 2 10.0.0.2"]


def test_ping_quotes_its_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The target reaches a remote shell, so it is quoted."""
    probes = load("network/traffic_test.py")._common["probes"]
    commands: list[str] = []
    monkeypatch.setattr(probes, "ssh_run", lambda *a, command=None, **k: commands.append(a[3]) or (1, "", ""))

    probes.ping(HOST, "10.0.0.2; reboot")

    assert commands == ["ping -c 3 -W 2 '10.0.0.2; reboot'"]


def _clocked(monkeypatch: pytest.MonkeyPatch, probes: Any) -> list[float]:
    """Replace probes.time with a fake clock that sleep() advances; return the sleep log."""
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(probes, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return sleeps


def test_wait_ping_polls_until_the_wanted_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforcement lags the Operation: keep probing until the ping stops answering."""
    probes = load("network/traffic_test.py")._common["probes"]
    answers = iter([(0, PING_OK, ""), (0, PING_OK, ""), (1, "", "")])
    monkeypatch.setattr(probes, "ssh_run", lambda *a, **k: next(answers))
    sleeps = _clocked(monkeypatch, probes)

    assert probes.wait_ping(HOST, "10.0.0.2", reachable=False, timeout=60, interval=5) is True
    assert sleeps == [5, 5]


def test_wait_ping_gives_up_at_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rule that never takes effect is reported as such, not waited on forever."""
    probes = load("network/traffic_test.py")._common["probes"]
    monkeypatch.setattr(probes, "ssh_run", lambda *a, **k: (0, PING_OK, ""))
    sleeps = _clocked(monkeypatch, probes)

    assert probes.wait_ping(HOST, "10.0.0.2", reachable=False, timeout=12, interval=5) is False
    assert sum(sleeps) >= 12


# ── Setup and inventory ───────────────────────────────────────────────


def test_create_network_builds_two_subnets(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The network step creates the VPC and both subnets, as NetworkProvisionedCheck expects."""
    subnet_ids = iter(["subnet.1", "subnet.2"])
    routes: dict[str, Route] = {
        f"POST {VPCS}": operation("vpc.N"),
        f"POST {VPCS}/vpc.N/subnets": lambda _b, _q: operation(next(subnet_ids)),
    }
    args = render("network", "network", "create_network", {})
    code, out, _ = _run(monkeypatch, capsys, "network/create_network.py", routes, args)

    assert code == 0, out
    assert [s["cidr"] for s in out["subnets"]] == ["172.16.243.0/25", "172.16.243.128/25"]
    assert _check(NetworkProvisionedCheck, out)._passed is True


def test_inventory_steps_pass_their_composites(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """list_vpcs / get_vpc / subnet_assignment satisfy VpcListed / VpcReadFromInventory / VpcContains."""
    vpc = {"id": "vpc.N", "name": "isv-net-test-vpc", "cidr": "172.16.240.0/24", "state": "READY"}
    routes: dict[str, Route] = {
        f"GET {VPCS}": {"items": [vpc]},
        f"GET {VPCS}/vpc.N": {"vpc": vpc},
        f"GET {VPCS}/vpc.N/subnets": {"items": [{"id": "subnet.1", "vpcId": "vpc.N"}]},
    }
    _, listed, _ = _run(monkeypatch, capsys, "network/list_vpcs.py", routes, ["--vpc-id", "vpc.N"])
    _, read, _ = _run(monkeypatch, capsys, "network/get_vpc.py", routes, ["--vpc-id", "vpc.N"])
    _, assigned, _ = _run(
        monkeypatch, capsys, "network/subnet_assignment.py", routes, ["--vpc-id", "vpc.N", "--subnet-id", "subnet.1"]
    )

    assert _composite("VpcListedCheck", listed) == []
    assert _composite("VpcReadFromInventoryCheck", read) == []
    assert _composite("VpcContainsExpectedSubnetCheck", assigned) == []


def test_subnet_assignment_fails_a_subnet_outside_the_vpc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A subnet missing from the VPC's list fails the composite."""
    routes: dict[str, Route] = {f"GET {VPCS}/vpc.N/subnets": {"items": [{"id": "subnet.9", "vpcId": "vpc.N"}]}}
    code, out, _ = _run(
        monkeypatch, capsys, "network/subnet_assignment.py", routes, ["--vpc-id", "vpc.N", "--subnet-id", "subnet.1"]
    )

    assert code == 1
    assert _composite("VpcContainsExpectedSubnetCheck", out)


# ── VPC CRUD ──────────────────────────────────────────────────────────


def _vpc_crud_routes(state: dict[str, Any]) -> dict[str, Route]:
    """Return VPC routes for vpc_crud that record the VPC's existence in ``state``."""

    def create(body: dict[str, Any] | None, _q: Any) -> dict[str, Any]:
        state["vpc"] = {"id": "vpc.C", **(body or {})}
        return operation("vpc.C")

    def get(_b: Any, _q: Any) -> dict[str, Any]:
        if "vpc" not in state:
            raise NotFound()
        return {"vpc": state["vpc"]}

    def delete(_b: Any, _q: Any) -> dict[str, Any]:
        state.pop("vpc", None)
        state["deletes"] = state.get("deletes", 0) + 1
        return operation("vpc.C")

    return {f"POST {VPCS}": create, f"GET {VPCS}/vpc.C": get, f"DELETE {VPCS}/vpc.C": delete}


def test_vpc_crud_passes_create_read_delete_and_fails_updates_as_unsupported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Create/read/delete pass; the tag and DNS updates fail as not supported, never pass."""
    state: dict[str, Any] = {}
    code, out, _ = _run(
        monkeypatch, capsys, "network/vpc_crud_test.py", _vpc_crud_routes(state), ["--cidr", "172.16.241.0/24"]
    )

    assert code == 0, out
    for name in ("VpcCreatedCheck", "VpcReadCheck", "VpcDeletedCheck"):
        assert _composite(name, out) == [], name
    failures = _composite("VpcUpdatedCheck", out)
    assert failures and "not supported" in failures[0]
    assert "vpc" not in state


def test_vpc_crud_deletes_the_vpc_when_a_later_step_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A created VPC is deleted even when the read fails."""
    state: dict[str, Any] = {}
    routes = _vpc_crud_routes(state)
    routes[f"GET {VPCS}/vpc.C"] = 500
    code, out, _ = _run(monkeypatch, capsys, "network/vpc_crud_test.py", routes)

    assert code == 1
    assert state.get("deletes") == 1
    assert out["tests"]["delete_vpc"]["passed"] is False


# ── Security group CRUD (firewall rules) ─────────────────────────────

RUN_SUBNET = "172.16.243.0/25"  # subnet A of the run's network
RULE_ARGS = ["--vpc-id", "vpc.V", "--subnet-id", "subnet.A"]


def _run_network() -> dict[str, Route]:
    """Return the subnet lookup route for the run's VPC and its subnets A and B."""
    return {
        f"GET /projects/{PROJECT}/network/vpcs-with-subnets": _vpcs_with_subnets(
            ("subnet.A", RUN_SUBNET), ("subnet.B", "172.16.243.128/25")
        )
    }


def _sg_routes(rules: Rules, vpc_state: str = "READY") -> dict[str, Route]:
    """Return the run network's VPC, subnet lookup, and firewall-rule routes for sg_crud."""
    return {
        **rules.routes("firewall-rule.1", "firewall-rule.2"),
        **_run_network(),
        f"GET {VPCS}/vpc.V": {"vpc": {"id": "vpc.V", "state": vpc_state}},
    }


def _assert_probe_rule_shape(body: dict[str, Any], subnet_cidr: str) -> None:
    """A probe rule sends the full source port range and targets a /32 inside ``subnet_cidr`` from TEST-NET-1."""
    assert (body["srcPortFrom"], body["srcPortTo"]) == (1, 65535)
    assert body["protocol"] == "TCP"
    assert ipaddress.ip_network(body["srcPrefix"]).subnet_of(ipaddress.ip_network("192.0.2.0/24"))
    dst = ipaddress.ip_network(body["dstPrefix"])
    assert dst.prefixlen == 32 and dst.subnet_of(ipaddress.ip_network(subnet_cidr))


def _assert_no_network_mutation(api: Any) -> None:
    """The rule steps only create, change, and delete firewall rules, never a VPC or subnet."""
    for method in ("POST", "PUT", "DELETE"):
        assert all("/firewall-rules" in path for path in api.paths(method)), api.paths(method)


def test_sg_crud_maps_the_lifecycle_onto_firewall_rules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """All eight SG operations pass on the rule lifecycle in the run's VPC, which is left in place."""
    rules = Rules()
    code, out, api = _run(monkeypatch, capsys, "network/sg_crud_test.py", _sg_routes(rules), RULE_ARGS)

    assert code == 0, out
    for name in (
        "SecurityGroupCreatedCheck",
        "SecurityGroupReadCheck",
        "SecurityGroupUpdatedCheck",
        "SecurityGroupDeletedCheck",
    ):
        assert _composite(name, out) == [], name
    assert [body["dstPortFrom"] for body in rules.created] == [8443, 9443]
    for body in rules.created:
        _assert_probe_rule_shape(body, RUN_SUBNET)
    put = next(body for method, path, body, _ in api.calls if method == "PUT")
    assert put == {"dstPortFrom": 8444, "dstPortTo": 8444}
    assert rules.deleted == ["firewall-rule.2", "firewall-rule.1"]
    _assert_no_network_mutation(api)


def test_sg_crud_leaves_rules_it_did_not_create(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run's VPC may already hold rules: the SG is only this step's rules, and the others stay."""
    rules = Rules()
    rules.rules["firewall-rule.OLD"] = {"id": "firewall-rule.OLD", "state": "READY"}
    code, out, api = _run(monkeypatch, capsys, "network/sg_crud_test.py", _sg_routes(rules), RULE_ARGS)

    assert code == 0, out
    assert _composite("SecurityGroupDeletedCheck", out) == []
    assert list(rules.rules) == ["firewall-rule.OLD"]
    assert f"DELETE {rules.base}/firewall-rule.OLD" not in api.paths()


def test_sg_crud_read_back_compares_the_source_port_range(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rule that reads back without the source range it was created with fails read_sg."""
    rules = Rules()
    routes = _sg_routes(rules)
    get = routes[f"GET {rules.base}/firewall-rule.1"]

    def without_src_ports(b: Any, q: Any) -> dict[str, Any]:
        rule = dict(get(b, q)["firewallRule"])
        rule.pop("srcPortFrom", None)
        return {"firewallRule": rule}

    routes[f"GET {rules.base}/firewall-rule.1"] = without_src_ports
    code, out, _ = _run(monkeypatch, capsys, "network/sg_crud_test.py", routes, RULE_ARGS)

    assert code == 1
    assert out["tests"]["read_sg"]["passed"] is False
    assert "srcPortFrom" in out["tests"]["read_sg"]["error"]


def test_sg_crud_fails_create_vpc_when_the_run_vpc_is_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The container check reads the run's VPC; a VPC that is not READY fails SecurityGroupCreatedCheck."""
    rules = Rules()
    code, out, _ = _run(monkeypatch, capsys, "network/sg_crud_test.py", _sg_routes(rules, "ERROR"), RULE_ARGS)

    assert code == 1
    assert "ERROR" in out["tests"]["create_vpc"]["error"]
    assert _composite("SecurityGroupCreatedCheck", out)
    assert rules.rules == {}


def test_sg_crud_deletes_every_rule_it_created_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed modify still leaves none of the step's rules behind, and the network untouched."""
    rules = Rules()
    routes = _sg_routes(rules)
    routes[f"PUT {rules.base}/firewall-rule.1"] = 500
    code, out, api = _run(monkeypatch, capsys, "network/sg_crud_test.py", routes, RULE_ARGS)

    assert code == 1
    assert rules.rules == {}
    assert sorted(rules.deleted) == ["firewall-rule.1", "firewall-rule.2"]
    assert "cleanup_errors" not in out
    assert _composite("SecurityGroupUpdatedCheck", out)
    _assert_no_network_mutation(api)


def test_sg_crud_deletes_a_rule_whose_create_operation_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An accepted rule whose Operation fails (as operation-time rejections do) is still deleted."""
    rules = Rules()
    routes = _sg_routes(rules)
    create = routes[f"POST {rules.base}"]
    routes[f"POST {rules.base}"] = lambda b, q: {"operation": {**create(b, q)["operation"], "status": "FAILED"}}
    code, out, _ = _run(monkeypatch, capsys, "network/sg_crud_test.py", routes, RULE_ARGS)

    assert code == 1
    assert rules.deleted == ["firewall-rule.1"]
    assert rules.rules == {}
    assert _composite("SecurityGroupCreatedCheck", out)


# ── Propagation and audit ─────────────────────────────────────────────


def test_policy_propagation_times_add_and_remove(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both timings are reported and the check passes under the threshold."""
    rules = Rules()
    code, out, api = _run(
        monkeypatch,
        capsys,
        "network/sg_policy_propagation_test.py",
        {**rules.routes("firewall-rule.P"), **_run_network()},
        RULE_ARGS,
    )

    assert code == 0, out
    assert out["add_observed_seconds"] >= 0 and out["remove_observed_seconds"] >= 0
    assert rules.rules == {}
    _assert_probe_rule_shape(rules.created[0], RUN_SUBNET)
    _assert_no_network_mutation(api)
    assert _check(SgPolicyPropagationTimingCheck, out)._passed is True


def test_policy_propagation_deletes_the_probe_rule_when_its_operation_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed create Operation still gets the accepted rule deleted."""
    rules = Rules()
    routes = {**rules.routes("firewall-rule.P"), **_run_network()}
    create = routes[f"POST {rules.base}"]
    routes[f"POST {rules.base}"] = lambda b, q: {"operation": {**create(b, q)["operation"], "status": "FAILED"}}
    code, out, _ = _run(monkeypatch, capsys, "network/sg_policy_propagation_test.py", routes, RULE_ARGS)

    assert code == 1
    assert rules.deleted == ["firewall-rule.P"]
    assert out["tests"]["cleanup"]["passed"] is True
    assert _check(SgPolicyPropagationTimingCheck, out)._passed is False


@pytest.mark.parametrize(
    "script",
    ["network/sg_crud_test.py", "network/sg_policy_propagation_test.py", "network/sdn_filter_audit_trail_test.py"],
)
def test_rule_steps_refuse_a_subnet_outside_the_vpc(
    script: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A subnet of another VPC cannot hold the rule's in-deployment prefix: fail before creating anything."""
    rules = Rules()
    routes = {
        **rules.routes("firewall-rule.X"),
        f"GET /projects/{PROJECT}/network/vpcs-with-subnets": {
            "items": [{"vpc": {"id": "vpc.OTHER"}, "subnets": [{"id": "subnet.A", "cidr": RUN_SUBNET}]}]
        },
        "GET /audit": {"items": []},
        f"GET {VPCS}/vpc.V": {"vpc": {"id": "vpc.V", "state": "READY"}},
    }
    code, out, _ = _run(monkeypatch, capsys, script, routes, RULE_ARGS)

    assert code == 1
    assert "vpc.OTHER" in out["error"]
    assert rules.created == []


def _audit_route(actions: list[str]) -> Route:
    """Return a GET /audit route answering with events for ``actions``."""

    def audit(_b: Any, query: dict[str, list[str]]) -> dict[str, Any]:
        if query.get("pageSize") == ["1"] and "targetId" not in query:
            return {"items": []}
        assert query["targetKind"] == ["FIREWALL_RULE"]
        return {
            "items": [
                {
                    "operationAction": a,
                    "actorId": "user.U",
                    "ts": "2026-09-23T00:00:00Z",
                    "targetId": query["targetId"][0],
                }
                for a in actions
            ]
        }

    return audit


def test_audit_trail_finds_create_modify_delete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each rule change is found in GET /audit with actor, time, and action."""
    rules = Rules()
    routes = {
        **rules.routes("firewall-rule.A"),
        **_run_network(),
        "GET /audit": _audit_route(["CREATE", "UPDATE", "DELETE"]),
    }
    code, out, api = _run(monkeypatch, capsys, "network/sdn_filter_audit_trail_test.py", routes, RULE_ARGS)

    assert code == 0, out
    assert out["target_rule_id"] == "firewall-rule.A"
    assert rules.rules == {}
    _assert_probe_rule_shape(rules.created[0], RUN_SUBNET)
    _assert_no_network_mutation(api)
    assert _check(SdnFilterAuditTrailCheck, out)._passed is True


def test_audit_trail_fails_a_missing_event(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A change absent from the trail fails its subtest once the wait expires."""
    rules = Rules()
    routes = {**rules.routes("firewall-rule.A"), **_run_network(), "GET /audit": _audit_route(["CREATE", "UPDATE"])}
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/sdn_filter_audit_trail_test.py",
        routes,
        [*RULE_ARGS, "--audit-timeout", "0"],
    )

    assert code == 1
    assert out["tests"]["delete_rule_logged"]["passed"] is False
    assert _check(SdnFilterAuditTrailCheck, out)._passed is False


def test_audit_trail_fails_an_event_without_an_actor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An event that does not name who made the change is not an audit trail."""
    rules = Rules()
    route = _audit_route(["CREATE", "UPDATE", "DELETE"])

    def without_actor(body: Any, query: dict[str, list[str]]) -> dict[str, Any]:
        page = route(body, query)
        for event in page["items"]:
            if event.get("operationAction") == "UPDATE":
                event["actorId"] = ""
        return page

    routes = {**rules.routes("firewall-rule.A"), **_run_network(), "GET /audit": without_actor}
    code, out, _ = _run(monkeypatch, capsys, "network/sdn_filter_audit_trail_test.py", routes, RULE_ARGS)

    assert code == 1
    assert out["tests"]["audit_event_has_required_fields"]["passed"] is False
    assert "UPDATE:actorId" in out["tests"]["audit_event_has_required_fields"]["error"]
    assert _check(SdnFilterAuditTrailCheck, out)._passed is False


@pytest.mark.parametrize("status", [404, 501])
def test_audit_trail_skips_when_audit_is_disabled(
    status: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the audit API the step skips before creating any rule."""
    code, out, api = _run(
        monkeypatch, capsys, "network/sdn_filter_audit_trail_test.py", {"GET /audit": status}, RULE_ARGS
    )

    assert code == 0
    assert out["skipped"] is True
    assert api.paths() == ["GET /audit"]
    with pytest.raises(pytest.skip.Exception):
        SdnFilterAuditTrailCheck(config={"step_output": out}).execute()


# ── Backend switch fabric ─────────────────────────────────────────────


def _topology(node_id: str, tiers: tuple[str, ...] = ("leaf", "spine", "core")) -> dict[str, Any]:
    """Return a /topology/nodes/{node} response."""
    return {"node_id": node_id, "topology_status": "complete", "tiers": {t: {"id": f"sw.{t}"} for t in tiers}}


def test_backend_fabric_reads_the_flat_topology_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The topology route is at the API root, not under /api/v1."""
    code, out, api = _run(
        monkeypatch,
        capsys,
        "network/backend_switch_fabric.py",
        {"GET /topology/nodes/bm.A": _topology("bm.A")},
        ["--node-id=bm.A"],
    )

    assert code == 0, out
    assert api.raw_paths == ["/topology/nodes/bm.A"]
    assert out["fabric"] == {
        "leaf_switch_ids": ["sw.leaf"],
        "spine_switch_ids": ["sw.spine"],
        "core_switch_ids": ["sw.core"],
    }
    assert _check(BackendSwitchFabricCheck, out)._passed is True


def test_backend_fabric_falls_back_to_a_listed_bm_and_fails_a_missing_tier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --node-id the first listed BM is used; a missing core tier fails."""
    routes: dict[str, Route] = {
        "GET /topology/nodes": {"nodes": [{"node_id": "bm.Z"}]},
        "GET /topology/nodes/bm.Z": _topology("bm.Z", ("leaf", "spine")),
    }
    code, out, api = _run(monkeypatch, capsys, "network/backend_switch_fabric.py", routes, ["--node-id="])

    assert code == 1
    assert api.calls[0][3]["node_kind"] == ["bm"]
    assert out["node_id"] == "bm.Z"
    assert out["tests"]["core_switch_ids_present"]["passed"] is False
    assert _check(BackendSwitchFabricCheck, out)._passed is False


# ── Single-BM checks ──────────────────────────────────────────────────

BM_SCRIPTS = ["network/dhcp_ip_test.py", "network/stable_ip_test.py", "network/stable_egress_ip_test.py"]


@pytest.mark.parametrize("script", BM_SCRIPTS)
def test_single_bm_steps_skip_without_a_provisioned_bm(
    script: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No BM configured is a structured skip naming the variables, with no API call."""
    code, out, api = _run(monkeypatch, capsys, script, {}, ["--instance-id=", "--key-file="])

    assert code == 0
    assert out["skipped"] is True
    assert "BM_INSTANCE_ID" in out["skip_reason"]
    assert api.calls == []


def test_dhcp_step_feeds_the_ssh_check(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The real DHCP check reads the API-reported IP from the output and compares it on the host."""
    routes: dict[str, Route] = {f"GET /projects/{PROJECT}/compute/bms/bm.A": _bm("bm.A", "172.16.240.10")}
    code, out, _ = _run(monkeypatch, capsys, "network/dhcp_ip_test.py", routes, BM_ARGS)
    assert code == 0, out

    answers = {
        "---DHCP_PROC---": "---DHCP_PROC---\n123 systemd-networkd\n---DHCP_LEASE---\nADDRESS=172.16.240.10\n",
        "ip -4 addr": "172.16.240.10\n",
        "---RESOLV---": "---RESOLV---\nnameserver 172.16.240.1\n---DHCP_OPTS---\nDONE\n",
    }
    seen: dict[str, Any] = {}

    def fake_client(host: str, user: str, key: str) -> Any:
        seen.update(host=host, user=user, key=key)
        return SimpleNamespace(close=lambda: None)

    def fake_command(_ssh: Any, command: str) -> tuple[int, str, str]:
        return 0, next(v for k, v in answers.items() if k in command), ""

    monkeypatch.setattr(network_checks, "get_ssh_client", fake_client)
    monkeypatch.setattr(network_checks, "run_ssh_command", fake_command)
    check = _check(DhcpIpManagementCheck, out)

    assert check._passed is True, check._error
    assert seen == {"host": "172.16.240.10", "user": "ubuntu", "key": "/tmp/a"}


def _power_routes(ips: list[str], calls: list[str]) -> dict[str, Route]:
    """Return BM routes whose power actions flip state and whose IP follows ``ips``."""
    state = {"state": "RUNNING", "powerState": "ON", "ip": ips[0]}
    after = iter(ips[1:])

    def get(_b: Any, _q: Any) -> dict[str, Any]:
        return {
            "bm": {"id": "bm.A", "state": state["state"], "powerState": state["powerState"], "ipAddress": state["ip"]}
        }

    def action(name: str) -> Route:
        def call(_b: Any, _q: Any) -> dict[str, Any]:
            calls.append(name)
            if name == "power-off":
                state.update(state="STOPPED", powerState="OFF")
            else:
                state.update(state="RUNNING", powerState="ON", ip=next(after, state["ip"]))
            return operation("bm.A")

        return call

    base = f"/projects/{PROJECT}/compute/bms/bm.A"
    return {
        f"GET {base}": get,
        f"POST {base}/power-off": action("power-off"),
        f"POST {base}/power-on": action("power-on"),
    }


@pytest.mark.parametrize(
    ("ips", "stable"), [(["172.16.240.10", "172.16.240.10"], True), (["172.16.240.10", "172.16.240.99"], False)]
)
def test_stable_ip_compares_the_ip_across_power_off_and_on(
    ips: list[str], stable: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The IP before power-off is compared with the IP after power-on."""
    calls: list[str] = []
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/stable_ip_test.py",
        _power_routes(ips, calls),
        BM_ARGS,
        patches={"wait_for_ssh": lambda *a, **k: True},
    )

    assert calls == ["power-off", "power-on"]
    assert out["tests"]["ip_unchanged"]["ip_after"] == ips[1]
    assert (code == 0) is stable
    assert (_check(StablePrivateIpCheck, out)._passed is True) is stable


def test_stable_ip_powers_the_bm_back_on_after_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed power-on is retried on the way out so the BM is not left off."""
    calls: list[str] = []
    routes = _power_routes(["172.16.240.10"], calls)
    power_on = routes[f"POST /projects/{PROJECT}/compute/bms/bm.A/power-on"]
    attempts = iter([500])

    def flaky(body: Any, query: Any) -> dict[str, Any]:
        if next(attempts, None):
            calls.append("power-on-failed")
            raise NotFound()  # any API error; the first power-on fails
        return power_on(body, query)

    routes[f"POST /projects/{PROJECT}/compute/bms/bm.A/power-on"] = flaky
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/stable_ip_test.py",
        routes,
        BM_ARGS,
        patches={"wait_for_ssh": lambda *a, **k: True},
    )

    assert code == 1
    assert calls == ["power-off", "power-on-failed", "power-on"]
    assert "cleanup_errors" not in out


def _ssh_answers(answers: list[tuple[int, str]]) -> Any:
    """Return a ``run(host, command)`` stub answering in order."""
    queue = iter(answers)
    return lambda _host, _command, timeout=40: next(queue)


def test_stable_egress_ip_reports_a_stable_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three identical probes pass DMS05-01."""
    routes: dict[str, Route] = {f"GET /projects/{PROJECT}/compute/bms/bm.A": _bm("bm.A", "172.16.240.10")}
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/stable_egress_ip_test.py",
        routes,
        [*BM_ARGS, "--interval-seconds", "0"],
        patches={"run": _ssh_answers([(0, "203.0.113.7\n")] * 3)},
    )

    assert code == 0, out
    assert _check(StableEgressIpCheck, out)._passed is True


def test_stable_egress_ip_fails_without_egress(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed probe (no internet from the subnet) fails the check rather than skipping."""
    routes: dict[str, Route] = {f"GET /projects/{PROJECT}/compute/bms/bm.A": _bm("bm.A", "172.16.240.10")}
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/stable_egress_ip_test.py",
        routes,
        BM_ARGS,
        patches={"run": _ssh_answers([(28, "")])},
    )

    assert code == 1
    assert "skipped" not in out
    assert "egress" in out["tests"]["probe_egress_ip"]["error"]
    assert _check(StableEgressIpCheck, out)._passed is False


# ── Two-BM checks ─────────────────────────────────────────────────────

PAIR_SCRIPTS = [
    ("network/connectivity_test.py", []),
    ("network/traffic_test.py", []),
    ("network/sg_scoping_test.py", ["--scope", "node"]),
    ("network/sg_scoping_test.py", ["--scope", "subnet"]),
]


@pytest.mark.parametrize(("script", "extra"), PAIR_SCRIPTS)
def test_two_bm_steps_skip_without_a_peer(
    script: str, extra: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With only one BM configured the step skips, naming the missing peer, with no API call."""
    code, out, api = _run(monkeypatch, capsys, script, {}, [*extra, *BM_ARGS, "--peer-id=", "--peer-key-file="])

    assert code == 0
    assert out["skipped"] is True
    assert "FIREBIRD_PEER_BM_ID" in out["skip_reason"]
    assert api.calls == []


def _pings(reachable: dict[tuple[str, str], bool]) -> Any:
    """Return a ``ping(host, target)`` stub keyed by (source BM, target IP)."""
    return lambda host, target, count=3: (reachable.get((host.bm_id, target), True), 0.3)


def test_connectivity_pings_both_ways(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Both BMs are listed with their IPs and ping each other."""
    code, out, _ = _run(
        monkeypatch, capsys, "network/connectivity_test.py", _pair_routes(), PAIR_ARGS, patches={"ping": _pings({})}
    )

    assert code == 0, out
    assert [i["private_ip"] for i in out["instances"]] == ["172.16.240.10", "172.16.240.200"]
    assert _check(NetworkConnectivityCheck, out)._passed is True


def test_connectivity_fails_when_bms_are_in_different_vpcs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Firewall rules and private routing are per VPC, so a cross-VPC pair is a config error."""
    routes = _pair_routes(peer_subnet="subnet.X")
    routes[f"GET /projects/{PROJECT}/network/vpcs-with-subnets"] = {
        "items": [
            {"vpc": {"id": "vpc.V"}, "subnets": [{"id": "subnet.S"}]},
            {"vpc": {"id": "vpc.W"}, "subnets": [{"id": "subnet.X"}]},
        ]
    }
    code, out, _ = _run(
        monkeypatch, capsys, "network/connectivity_test.py", routes, PAIR_ARGS, patches={"ping": _pings({})}
    )

    assert code == 1
    assert "different VPCs" in out["error"]


def test_traffic_blocks_with_a_scoped_deny_rule_and_deletes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The DENY rule targets the pair's /32s, is observed blocking, and is deleted."""
    rules = Rules()
    routes = {**_pair_routes(), **rules.routes("firewall-rule.T")}
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/traffic_test.py",
        routes,
        PAIR_ARGS,
        patches={
            "ping": _pings({}),
            "wait_ping": lambda host, target, reachable, timeout: True,
            "run": _ssh_answers([(0, "")]),
        },
    )

    assert code == 0, out
    assert rules.created[0]["srcPrefix"] == "172.16.240.10/32"
    assert rules.created[0]["dstPrefix"] == "172.16.240.200/32"
    assert rules.created[0]["icmpType"] == 8
    assert rules.deleted == ["firewall-rule.T"]
    assert _check(TrafficFlowCheck, out)._passed is True


def test_traffic_deletes_the_rule_when_a_probe_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A probe failing mid-test still leaves no rule behind, and fails the check."""
    rules = Rules()
    calls = itertools.count()

    def broken(*_a: Any, **_k: Any) -> bool:
        if next(calls) == 0:  # the baseline answers; enforcement and the removal wait raise
            return True
        raise subprocess.TimeoutExpired("ssh", 40)

    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/traffic_test.py",
        {**_pair_routes(), **rules.routes("firewall-rule.T")},
        PAIR_ARGS,
        patches={"ping": _pings({}), "wait_ping": broken},
    )

    assert code == 1
    assert rules.rules == {}
    assert out["tests"]["cleanup"]["passed"] is True
    assert out["cleanup_propagated"] is False
    assert _check(TrafficFlowCheck, out)._passed is False


class Fabric:
    """Fake data plane for the BM pair, answering ``ssh_run`` pings from the fake VPC's rules.

    A ping answers unless a live DENY rule covers it. Before any rule is created
    the first ``draining`` pings fail (a previous check's rule still draining);
    after a covering rule is deleted, ``lingering`` more pings of that direction
    fail (its removal propagating). ``None`` means forever.
    """

    def __init__(self, rules: Rules, *, draining: int | None = 0, lingering: int | None = 0) -> None:
        """Model the pair's pings on ``rules``."""
        self.rules = rules
        self.draining = draining
        self.lingering = lingering
        self.pings: list[tuple[str, str, bool]] = []

    def ssh_run(self, ip: str, _user: str, _key: str, command: str, *, timeout: int = 40) -> tuple[int, str, str]:
        """Answer a ping from ``ip``; any other command succeeds."""
        if not command.startswith("ping "):
            return 0, "", ""
        target = command.split()[-1]
        answered = self._answers(ip, target)
        self.pings.append((ip, target, answered))
        return (0, PING_OK, "") if answered else (1, "", "")

    @staticmethod
    def _covers(rule: dict[str, Any], src: str, dst: str) -> bool:
        return ipaddress.ip_address(src) in ipaddress.ip_network(rule["srcPrefix"]) and ipaddress.ip_address(
            dst
        ) in ipaddress.ip_network(rule["dstPrefix"])

    def _spend(self, budget: str) -> bool:
        """Return whether a ping still fails under ``budget``, spending one ping of it."""
        left = getattr(self, budget)
        if left is None:
            return True
        if left > 0:
            setattr(self, budget, left - 1)
            return True
        return False

    def _answers(self, src: str, dst: str) -> bool:
        if not self.rules.created:
            return not self._spend("draining")
        if any(self._covers(rule, src, dst) for rule in self.rules.rules.values()):
            return False
        if any(self._covers(rule, src, dst) for rule in self.rules.created):
            return not self._spend("lingering")
        return True


# (script, extra args, peer subnet, check, direction the rule blocks as (source IP, target IP))
RULE_CHECKS = [
    pytest.param(
        "network/traffic_test.py",
        [],
        "subnet.S",
        TrafficFlowCheck,
        ("172.16.240.10", "172.16.240.200"),
        id="traffic",
    ),
    pytest.param(
        "network/sg_scoping_test.py",
        ["--scope", "node"],
        "subnet.S",
        SgNodeScopingCheck,
        ("172.16.240.200", "172.16.240.10"),
        id="node",
    ),
    pytest.param(
        "network/sg_scoping_test.py",
        ["--scope", "subnet"],
        "subnet.T",
        SgSubnetScopingCheck,
        ("172.16.240.200", "172.16.240.10"),
        id="subnet",
    ),
]


def _run_on_fabric(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: str,
    argv: list[str],
    peer_subnet: str,
    **fabric: int | None,
) -> tuple[int, dict[str, Any], Rules, Fabric, list[float]]:
    """Run a rule check with real ping polling against a ``Fabric`` and a fake clock."""
    rules = Rules()
    net = Fabric(rules, **fabric)
    clock: list[list[float]] = []

    def prepare(module: Any) -> None:
        probes = module._common["probes"]
        monkeypatch.setattr(probes, "ssh_run", net.ssh_run)
        clock.append(_clocked(monkeypatch, probes))

    code, out, _ = run(
        monkeypatch,
        capsys,
        script,
        {**_pair_routes(peer_subnet=peer_subnet), **rules.routes("firewall-rule.R")},
        argv,
        prepare=prepare,
        wrap=_with_404,
    )
    return code, out, rules, net, clock[0]


@pytest.mark.parametrize(("script", "extra", "peer_subnet", "check", "blocked"), RULE_CHECKS)
def test_baseline_waits_for_a_previous_rule_to_drain(
    script: str,
    extra: list[str],
    peer_subnet: str,
    check: type[BaseValidation],
    blocked: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A check that starts while an earlier rule's removal propagates waits for the ping instead of failing."""
    code, out, rules, net, sleeps = _run_on_fabric(
        monkeypatch, capsys, script, [*extra, *PAIR_ARGS], peer_subnet, draining=3
    )

    assert code == 0, out
    assert [answered for *_, answered in net.pings[:4]] == [False, False, False, True]
    assert sleeps[:3] == [5, 5, 5]
    assert len(rules.created) == 1
    assert _check(check, out)._passed is True


@pytest.mark.parametrize(("script", "extra", "peer_subnet", "check", "blocked"), RULE_CHECKS)
def test_baseline_fails_before_any_rule_when_the_pair_never_answers(
    script: str,
    extra: list[str],
    peer_subnet: str,
    check: type[BaseValidation],
    blocked: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Blocking cannot be shown for traffic that never flowed: after the window, fail with no rule created."""
    code, out, rules, _, sleeps = _run_on_fabric(
        monkeypatch, capsys, script, [*extra, *PAIR_ARGS, "--removal-timeout", "20"], peer_subnet, draining=None
    )

    assert code == 1
    assert "before any rule (waited 20s)" in out["error"]
    assert sum(sleeps) >= 20
    assert rules.created == []
    assert "cleanup_propagated" not in out
    assert _check(check, out)._passed is False


@pytest.mark.parametrize(("script", "extra", "peer_subnet", "check", "blocked"), RULE_CHECKS)
def test_cleanup_waits_for_the_rule_removal_to_propagate(
    script: str,
    extra: list[str],
    peer_subnet: str,
    check: type[BaseValidation],
    blocked: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After deleting its rule the check polls the blocked direction until it answers again."""
    code, out, rules, net, _ = _run_on_fabric(
        monkeypatch, capsys, script, [*extra, *PAIR_ARGS], peer_subnet, lingering=4
    )

    assert code == 0, out
    assert rules.deleted == ["firewall-rule.R"]
    assert out["cleanup_propagated"] is True
    assert "cleanup_warning" not in out
    # The last probes are the removal wait: four still blocked, then an answer.
    tail = [answered for src, dst, answered in net.pings if (src, dst) == blocked][-5:]
    assert tail == [False, False, False, False, True]
    assert _check(check, out)._passed is True


@pytest.mark.parametrize(("script", "extra", "peer_subnet", "check", "blocked"), RULE_CHECKS)
def test_cleanup_records_a_removal_that_never_propagates_without_failing(
    script: str,
    extra: list[str],
    peer_subnet: str,
    check: type[BaseValidation],
    blocked: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rule is gone from the API, so a slow removal is a warning, not a failed check."""
    code, out, rules, _, _ = _run_on_fabric(
        monkeypatch, capsys, script, [*extra, *PAIR_ARGS, "--removal-timeout", "30"], peer_subnet, lingering=None
    )

    assert code == 0, out
    assert out["success"] is True
    assert rules.deleted == ["firewall-rule.R"]
    assert out["cleanup_propagated"] is False
    assert "30s after the rule was deleted" in out["cleanup_warning"]
    assert out["tests"]["cleanup"]["passed"] is True
    assert _check(check, out)._passed is True


@pytest.mark.parametrize(
    ("script", "extra"),
    [("network/traffic_test.py", []), ("network/sg_scoping_test.py", ["--scope", "node"])],
)
def test_rule_cleanup_keeps_its_own_budget_after_the_deadline(
    script: str, extra: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the step's deadline spent, deleting the rule still gets the cleanup budget."""
    rules = Rules()
    waits: list[tuple[str, int]] = []

    def broken(*_a: Any, reachable: bool, **_k: Any) -> bool:
        if reachable:
            return True
        raise subprocess.TimeoutExpired("ssh", 40)

    def prepare(module: Any) -> None:
        real_wait = module._fb.FirebirdClient.wait_operation

        def recording(self: Any, op: dict[str, Any], timeout: int, interval: float | None = None) -> Any:
            waits.append((op.get("resourceId", ""), timeout))
            return real_wait(self, op, timeout, interval)

        monkeypatch.setattr(module._fb.FirebirdClient, "wait_operation", recording)
        monkeypatch.setattr(module, "ping", _pings({}))
        monkeypatch.setattr(module, "wait_ping", broken)

    code, _, _ = run(
        monkeypatch,
        capsys,
        script,
        {**_pair_routes(), **rules.routes("firewall-rule.C")},
        [*extra, *PAIR_ARGS, "--timeout", "0"],
        prepare=prepare,
        wrap=_with_404,
    )

    assert code == 1
    assert rules.deleted == ["firewall-rule.C"]
    create_wait, delete_wait = waits
    assert create_wait[1] == 1  # the spent step deadline
    assert delete_wait[1] >= 120


def test_node_scoping_denies_requests_to_one_bm_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The node rule covers the BM's /32; the peer is blocked towards it, the BM still reaches the peer."""
    rules = Rules()
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/sg_scoping_test.py",
        {**_pair_routes(), **rules.routes("firewall-rule.N")},
        ["--scope", "node", *PAIR_ARGS],
        patches={
            "ping": _pings({}),
            "wait_ping": lambda host, target, reachable, timeout: host.bm_id == "bm.B" or reachable,
        },
    )

    assert code == 0, out
    assert rules.created[0]["dstPrefix"] == "172.16.240.10/32"
    assert rules.deleted == ["firewall-rule.N"]
    # The inverted (deny) mapping is visible in the output.
    assert out["rule_polarity"] == "deny"
    assert out["tests"]["other_node_blocked"]["probe"].startswith("bm.B -> bm.A, covered by the DENY rule")
    assert out["tests"]["target_node_allowed"]["probe"] == "bm.A -> bm.B, not covered by the DENY rule"
    assert _check(SgNodeScopingCheck, out)._passed is True


def test_node_scoping_fails_a_rule_that_blocks_both_ways(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the uncovered direction is blocked too, the rule is not node-scoped."""
    rules = Rules()
    reachable = iter([False])  # the BM->peer probe after the rule
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/sg_scoping_test.py",
        {**_pair_routes(), **rules.routes("firewall-rule.N")},
        ["--scope", "node", *PAIR_ARGS],
        patches={
            "ping": lambda host, target, count=3: (next(reachable), 0.3),
            "wait_ping": lambda host, target, reachable, timeout: True,
        },
    )

    assert code == 1
    assert out["tests"]["target_node_allowed"]["passed"] is False
    assert rules.rules == {}
    assert _check(SgNodeScopingCheck, out)._passed is False


def test_subnet_scoping_needs_two_subnets(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """BMs on one subnet cannot show subnet scoping: a structured skip, no rule created."""
    rules = Rules()
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/sg_scoping_test.py",
        {**_pair_routes(), **rules.routes("firewall-rule.S")},
        ["--scope", "subnet", *PAIR_ARGS],
        patches={"ping": _pings({})},
    )

    assert code == 0
    assert out["skipped"] is True
    assert rules.created == []


def test_subnet_scoping_denies_requests_from_the_peer_subnet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On two subnets the rule matches the peer's subnet CIDR as source."""
    rules = Rules()
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "network/sg_scoping_test.py",
        {**_pair_routes(peer_subnet="subnet.T"), **rules.routes("firewall-rule.S")},
        ["--scope", "subnet", *PAIR_ARGS],
        patches={"ping": _pings({}), "wait_ping": lambda host, target, reachable, timeout: True},
    )

    assert code == 0, out
    assert rules.created[0]["srcPrefix"] == "172.16.240.128/25"
    assert rules.deleted == ["firewall-rule.S"]
    assert _check(SgSubnetScopingCheck, out)._passed is True
