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

"""Tests for the Firebird provider: network setup/teardown and the query steps.

Uses the fake-API harness in ``harness.py``. Script output is fed to the real
validation classes with the parameters the bare_metal suite wires, so a contract
drift fails here rather than on a live run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
from isvtest.core.validation import BaseValidation
from isvtest.validations.breakfix import (
    BmcKernelLogCheck,
    MaintenanceEventsCheck,
    NodeHealthAgentCheck,
    RepairHistoryCheck,
)
from isvtest.validations.governance import (
    FleetManagementApiCheck,
    GovernanceMetricsCheck,
    ResourceDiscoveryApiCheck,
)
from isvtest.validations.health import HostHealthCheck
from isvtest.validations.infiniband import IbTenantIsolationCheck
from isvtest.validations.storage_infra import StableStorageNodeIpCheck

from isvctl.config.merger import merge_yaml_files

from .harness import (
    FIREBIRD,
    ISVCTL_ROOT,
    PROJECT,
    FakeApi,
    FakeClock,
    Route,
    config_steps,
    render,
    suite_params,
    validate,
)
from .harness import load as _load
from .harness import operation as _operation
from .harness import run as _run

SHARED_HEALTH_AGENTS = ISVCTL_ROOT / "configs" / "providers" / "shared" / "breakfix" / "query_node_health_agents.py"


def _validate(check_cls: type[BaseValidation], output: dict[str, Any]) -> BaseValidation:
    """Run a validation class on step output with its bare_metal suite parameters."""
    return validate("bare_metal", check_cls, output)


def _suite_params(check_name: str) -> dict[str, Any]:
    """Return the parameters the bare_metal suite wires for ``check_name``."""
    return suite_params("bare_metal", check_name)


def _config_steps() -> dict[str, Any]:
    """Return the Firebird bare_metal steps by name, in order."""
    return config_steps("bare_metal", "bare_metal")


def _render(step_name: str, outputs: dict[str, dict[str, Any]]) -> list[str]:
    """Render one bare_metal step's args with the given upstream step outputs."""
    return render("bare_metal", "bare_metal", step_name, outputs)


def _flag(args: list[str], name: str) -> str:
    """Return the value of ``--name value`` or ``--name=value`` in rendered args."""
    for index, arg in enumerate(args):
        if arg == name:
            return args[index + 1]
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1]
    raise AssertionError(f"{name} not in {args}")


# ── Client helpers ────────────────────────────────────────────────────


def test_parse_timestamp_handles_protobuf_json_and_unset() -> None:
    """Nanosecond fractions parse to whole seconds; null and empty are unset."""
    fb = _load("health/query_host_health.py")._fb

    assert fb.parse_timestamp("2026-09-23T10:11:12.123456789Z") == datetime(2026, 9, 23, 10, 11, 12, tzinfo=UTC)
    assert fb.parse_timestamp(None) is None
    assert fb.parse_timestamp("") is None


def _client(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Route]) -> tuple[Any, FakeApi, ModuleType, FakeClock]:
    """Return a real FirebirdClient on the fake API with a fake clock, plus the api, client module, and clock."""
    module = _load("health/query_host_health.py")
    api = FakeApi(module, routes)
    clock = FakeClock()
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FIREBIRD_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(module._fb.FirebirdClient, "_send", api.send)
    monkeypatch.setattr(module._fb, "time", clock)
    return module._fb.FirebirdClient(), api, module._fb, clock


def test_paginate_follows_page_tokens_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every page is read, in order, each with the previous page's token."""
    pages = {
        "": {"items": [{"id": "a"}], "nextPageToken": "p2"},
        "p2": {"items": [{"id": "b"}], "nextPageToken": "p3"},
        "p3": {"items": [{"id": "c"}]},
    }
    client, api, _fb, _clock = _client(
        monkeypatch, {"GET /things": lambda _b, q: pages[(q.get("pageToken") or [""])[0]]}
    )

    assert [item["id"] for item in client.paginate("/things", "items")] == ["a", "b", "c"]
    assert [query.get("pageToken", [""])[0] for _m, _p, _b, query in api.calls] == ["", "p2", "p3"]


def test_paginate_stops_on_a_repeated_page_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that hands back an earlier token fails the call instead of looping forever."""

    def page(_b: Any, _q: Any) -> dict[str, Any]:
        if len(api.calls) > 5:
            raise AssertionError("paginate kept following a repeated token")
        return {"items": [{"id": "a"}], "nextPageToken": "same"}

    client, api, fb, _clock = _client(monkeypatch, {"GET /things": page})

    with pytest.raises(fb.FirebirdApiError, match="repeated page token"):
        client.paginate("/things", "items")
    assert len(api.calls) == 2


def test_wait_operation_without_an_operation_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response with no Operation raises at once, without polling /operations/ until the timeout."""
    client, api, fb, clock = _client(monkeypatch, {})
    start = clock.now

    with pytest.raises(fb.FirebirdApiError, match="no Operation"):
        client.wait_operation({}, 1800)
    assert api.calls == [] and clock.now == start
    assert client.wait_operation({"status": "COMPLETED"}, 1800) == {"status": "COMPLETED"}  # inline terminal
    assert api.calls == []


def test_wait_bm_aborts_early_on_a_degraded_bm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DEGRADED readback raises on the first poll instead of burning the timeout."""
    bm_path = f"GET /projects/{PROJECT}/compute/bms/bm.A"
    client, api, fb, clock = _client(monkeypatch, {bm_path: {"bm": {"id": "bm.A", "state": "DEGRADED"}}})
    start = clock.now

    with pytest.raises(fb.FirebirdApiError, match="DEGRADED"):
        client.wait_bm("bm.A", ("RUNNING",), 1800)
    assert api.paths() == [bm_path] and clock.now == start


# ── Network setup / teardown ──────────────────────────────────────────


def test_create_network_creates_vpc_then_subnet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No subnet supplied: create the VPC, then a subnet with its first host as gateway."""
    routes: dict[str, Route] = {
        f"POST /projects/{PROJECT}/network/vpcs": _operation("vpc.NEW", status="PENDING", op_id="operation.v"),
        "GET /operations/operation.v": _operation("vpc.NEW", op_id="operation.v"),
        f"POST /projects/{PROJECT}/network/vpcs/vpc.NEW/subnets": _operation("subnet.NEW"),
    }
    code, out, api = _run(
        monkeypatch,
        capsys,
        "network/create_network.py",
        routes,
        ["--name", "isv-bm-test", "--vpc-cidr", "172.16.240.0/24", "--subnet-cidr", "172.16.240.0/25"],
    )

    assert code == 0, out
    assert out["network_id"] == "vpc.NEW"
    assert out["subnets"] == [{"subnet_id": "subnet.NEW", "cidr": "172.16.240.0/25"}]
    assert out["created_vpc"] is True
    assert out["created_subnet_ids"] == ["subnet.NEW"]
    bodies = [body for method, _, body, _ in api.calls if method == "POST"]
    assert bodies == [
        {"name": "isv-bm-test-vpc", "cidr": "172.16.240.0/24"},
        {"name": "isv-bm-test-subnet-1", "cidr": "172.16.240.0/25", "gatewayIp": "172.16.240.1"},
    ]
    # The PENDING VPC operation was polled to completion before the subnet was created.
    assert api.paths().index("GET /operations/operation.v") < api.paths().index(
        f"POST /projects/{PROJECT}/network/vpcs/vpc.NEW/subnets"
    )


def test_create_network_passes_a_supplied_subnet_through_without_api_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A supplied subnet is used as-is: nothing is created, so nothing will be deleted."""
    code, out, api = _run(
        monkeypatch, capsys, "network/create_network.py", {}, ["--vpc-id=vpc.OLD", "--subnet-id=subnet.OLD"]
    )

    assert code == 0, out
    assert api.calls == []
    assert out["network_id"] == "vpc.OLD"
    assert out["subnets"] == [{"subnet_id": "subnet.OLD"}]
    assert out["created_vpc"] is False
    assert out["created_subnet_ids"] == []


def test_create_network_with_a_supplied_vpc_creates_only_the_subnet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A supplied VPC without a subnet gets a subnet; the VPC is not ours to delete."""
    routes: dict[str, Route] = {f"POST /projects/{PROJECT}/network/vpcs/vpc.OLD/subnets": _operation("subnet.NEW")}
    code, out, api = _run(
        monkeypatch, capsys, "network/create_network.py", routes, ["--vpc-id=vpc.OLD", "--subnet-id="]
    )

    assert code == 0, out
    assert api.paths("POST") == [f"POST /projects/{PROJECT}/network/vpcs/vpc.OLD/subnets"]
    assert out["created_vpc"] is False
    assert out["created_subnet_ids"] == ["subnet.NEW"]


def test_create_network_records_the_vpc_when_the_subnet_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A half-built network must still report the VPC so teardown can delete it."""
    routes: dict[str, Route] = {
        f"POST /projects/{PROJECT}/network/vpcs": _operation("vpc.NEW"),
        f"POST /projects/{PROJECT}/network/vpcs/vpc.NEW/subnets": 409,
    }
    code, out, _ = _run(monkeypatch, capsys, "network/create_network.py", routes)

    assert code == 1
    assert out["success"] is False
    assert out["created_vpc"] is True
    assert out["network_id"] == "vpc.NEW"
    assert out["created_subnet_ids"] == []


def test_create_network_records_a_subnet_whose_operation_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An accepted create exists server-side even when its Operation fails."""
    routes: dict[str, Route] = {
        f"POST /projects/{PROJECT}/network/vpcs": _operation("vpc.NEW"),
        f"POST /projects/{PROJECT}/network/vpcs/vpc.NEW/subnets": _operation("subnet.NEW", status="FAILED"),
    }
    code, out, _ = _run(monkeypatch, capsys, "network/create_network.py", routes)

    assert code == 1
    assert out["created_subnet_ids"] == ["subnet.NEW"]
    assert out["subnets"] == []


def test_create_network_requires_the_vpc_of_a_supplied_subnet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A subnet without its VPC cannot be passed through: fail before any call."""
    code, out, api = _run(monkeypatch, capsys, "network/create_network.py", {}, ["--vpc-id=", "--subnet-id=subnet.OLD"])

    assert code == 1
    assert "--vpc-id" in out["error"]
    assert api.calls == []


def test_create_network_refuses_a_fresh_network_for_a_reused_bm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reused BM keeps its subnet; creating another network for it would be wasted and misleading."""
    code, out, api = _run(monkeypatch, capsys, "network/create_network.py", {}, ["--reused-bm-id=bm.X"])

    assert code == 1
    assert "FIREBIRD_SUBNET_ID" in out["error"]
    assert api.calls == []


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"BM_INSTANCE_ID": "bm.X", "BM_KEY_FILE": "/tmp/key"}, "bm.X"),
        ({"BM_INSTANCE_ID": "bm.X"}, ""),  # launch_instance reuses only with both set
        ({}, ""),
    ],
)
def test_bare_metal_passes_the_reused_bm_to_create_network(
    env: dict[str, str], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reuse guard fires exactly when launch_instance will reuse a BM."""
    for name in ("BM_INSTANCE_ID", "BM_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert _flag(_render("create_network", {}), "--reused-bm-id") == expected


@pytest.mark.parametrize("cidr", ["172.16.240.1/25", "172.16.240.0/31"])
def test_create_network_rejects_a_subnet_cidr_before_any_call(
    cidr: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host-form or too-small subnet CIDR cannot yield a gateway; fail before creating anything."""
    code, out, api = _run(monkeypatch, capsys, "network/create_network.py", {}, ["--subnet-cidr", cidr])

    assert code == 1
    assert api.calls == []
    assert out["created_vpc"] is False


def test_teardown_network_deletes_subnets_before_the_vpc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Created subnets go first (a VPC with subnets cannot be deleted), then the VPC."""
    vpc = f"/projects/{PROJECT}/network/vpcs/vpc.NEW"
    routes: dict[str, Route] = {
        f"DELETE {vpc}/subnets/subnet.A": _operation("subnet.A"),
        f"DELETE {vpc}/subnets/subnet.B": 404,
        f"DELETE {vpc}": _operation("vpc.NEW"),
    }
    code, out, api = _run(
        monkeypatch,
        capsys,
        "network/teardown_network.py",
        routes,
        ["--vpc-id=vpc.NEW", "--subnet-ids=subnet.A,subnet.B", "--delete-vpc"],
    )

    assert code == 0, out
    assert api.paths() == [f"DELETE {vpc}/subnets/subnet.A", f"DELETE {vpc}/subnets/subnet.B", f"DELETE {vpc}"]
    assert out["resources_deleted"] == ["subnet:subnet.A", "subnet:subnet.B", "vpc:vpc.NEW"]


def test_teardown_network_keeps_a_supplied_vpc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --delete-vpc only the created subnet is deleted."""
    vpc = f"/projects/{PROJECT}/network/vpcs/vpc.OLD"
    routes: dict[str, Route] = {f"DELETE {vpc}/subnets/subnet.NEW": _operation("subnet.NEW")}
    code, out, api = _run(
        monkeypatch, capsys, "network/teardown_network.py", routes, ["--vpc-id=vpc.OLD", "--subnet-ids=subnet.NEW"]
    )

    assert code == 0, out
    assert api.paths() == [f"DELETE {vpc}/subnets/subnet.NEW"]


def test_teardown_network_stops_when_a_subnet_delete_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A subnet still in use (BM not detached) fails the step and leaves the VPC alone.

    The API refuses that delete as a validation error (HTTP 400), which must not
    be mistaken for "already gone".
    """
    vpc = f"/projects/{PROJECT}/network/vpcs/vpc.NEW"
    routes: dict[str, Route] = {f"DELETE {vpc}/subnets/subnet.A": 400}
    code, out, api = _run(
        monkeypatch,
        capsys,
        "network/teardown_network.py",
        routes,
        ["--vpc-id=vpc.NEW", "--subnet-ids=subnet.A", "--delete-vpc"],
    )

    assert code == 1
    assert "400" in out["error"]
    assert api.paths() == [f"DELETE {vpc}/subnets/subnet.A"]


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["--vpc-id=vpc.OLD", "--subnet-ids="], "Network was supplied"),
        (["--vpc-id=", "--subnet-ids="], "No network was created by this run"),
        (["--vpc-id=vpc.NEW", "--subnet-ids=subnet.A", "--delete-vpc", "--skip-destroy"], "BM_SKIP_TEARDOWN"),
    ],
    ids=["supplied-network", "nothing-created", "skip-teardown"],
)
def test_teardown_network_skips_without_api_calls(
    argv: list[str], reason: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A supplied network, a failed create, or BM_SKIP_TEARDOWN is a clean structured skip."""
    code, out, api = _run(monkeypatch, capsys, "network/teardown_network.py", {}, argv)

    assert code == 0
    assert out["skipped"] is True
    assert reason in out["skip_reason"]
    assert api.calls == []


def test_config_orders_network_around_the_bm_lifecycle() -> None:
    """create_network runs before launch; teardown_network runs after the BM is released."""
    names = list(_config_steps())

    assert names.index("create_network") < names.index("launch_instance")
    assert names.index("teardown") < names.index("teardown_network")
    assert names.index("verify_teardown") < names.index("teardown_network")


def test_cloud_init_check_runs_after_the_test_phase() -> None:
    """BOOT02-01 keeps its step and check but runs in the test phase, so its metadata subtest cannot gate the run."""
    merged = merge_yaml_files([FIREBIRD / "config" / "bare_metal.yaml"])
    cloud_init = merged["tests"]["validations"]["cloud_init"]

    assert cloud_init["phase"] == "test"
    assert cloud_init["step"] == "launch_instance"
    assert cloud_init["checks"]["BmCloudInitCheck"]["test_id"] == "BOOT02-01"


def test_launch_instance_uses_the_network_step_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """launch_instance reads the VPC/subnet from create_network, created or supplied."""
    monkeypatch.setenv("FIREBIRD_IMAGE_ID", "image.I")
    created = {"network_id": "vpc.NEW", "subnets": [{"subnet_id": "subnet.NEW", "cidr": "172.16.240.0/25"}]}

    args = _render("launch_instance", {"create_network": created})

    assert _flag(args, "--vpc-id") == "vpc.NEW"
    assert _flag(args, "--subnet-id") == "subnet.NEW"


SSH_STEPS = {
    "launch_instance": "--ssh-user",
    "start_instance": "--ssh-user",
    "reboot_instance": "--ssh-user",
    "power_cycle_instance": "--ssh-user",
    "describe_instance": "--ssh-user",
    "host_status_log": "--ssh-user",
    "query_node_health_agents": "--ssh-user",
    "reinstall_instance": "--ssh-user",
    "deploy_nim": "--user",
    "teardown_nim": "--user",
}


@pytest.mark.parametrize(("env", "user"), [(None, "ubuntu"), ("admin", "admin")])
def test_every_ssh_step_logs_in_as_firebird_ssh_user(
    monkeypatch: pytest.MonkeyPatch, env: str | None, user: str
) -> None:
    """FIREBIRD_SSH_USER (default ubuntu) reaches every bare_metal and image-registry step that opens SSH."""
    if env is None:
        monkeypatch.delenv("FIREBIRD_SSH_USER", raising=False)
    else:
        monkeypatch.setenv("FIREBIRD_SSH_USER", env)
    monkeypatch.setenv("FIREBIRD_IMAGE_ID", "image.I")
    outputs: dict[str, dict[str, Any]] = {
        "create_network": {"network_id": "vpc.V", "subnets": [{"subnet_id": "subnet.S"}]},
        "launch_instance": {"instance_id": "bm.A", "key_file": "/tmp/k"},
        "start_instance": {"public_ip": "10.0.0.1"},
        "describe_instance": {"public_ip": "10.0.0.1"},
        "power_cycle_instance": {"public_ip": "10.0.0.1", "key_file": "/tmp/k"},
    }
    config_steps = _config_steps()

    for step, flag in SSH_STEPS.items():
        assert step in config_steps
        assert _flag(_render(step, outputs), flag) == user, step
    install = render("image-registry", "image_registry", "install_image_bm", {})
    assert _flag(install, "--ssh-user") == user


@pytest.mark.parametrize("env", [{}, {"FIREBIRD_VPC_ID": "vpc.OLD", "FIREBIRD_SUBNET_ID": "subnet.OLD"}])
def test_create_network_args_render_supplied_ids_and_default_cidrs(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset IDs render empty (create); supplied IDs pass through; CIDRs have defaults."""
    for name in ("FIREBIRD_VPC_ID", "FIREBIRD_SUBNET_ID", "FIREBIRD_VPC_CIDR", "FIREBIRD_SUBNET_CIDR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    args = _render("create_network", {})

    assert _flag(args, "--vpc-id") == env.get("FIREBIRD_VPC_ID", "")
    assert _flag(args, "--subnet-id") == env.get("FIREBIRD_SUBNET_ID", "")
    assert _flag(args, "--vpc-cidr") == "172.16.240.0/24"
    assert _flag(args, "--subnet-cidr") == "172.16.240.0/25"


def test_teardown_network_round_trip_deletes_exactly_what_was_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """create_network output rendered into teardown_network deletes the created subnet, then VPC."""
    monkeypatch.delenv("BM_SKIP_TEARDOWN", raising=False)
    routes: dict[str, Route] = {
        f"POST /projects/{PROJECT}/network/vpcs": _operation("vpc.NEW"),
        f"POST /projects/{PROJECT}/network/vpcs/vpc.NEW/subnets": _operation("subnet.NEW"),
    }
    _, created, _ = _run(monkeypatch, capsys, "network/create_network.py", routes)

    args = _render("teardown_network", {"create_network": created})
    vpc = f"/projects/{PROJECT}/network/vpcs/vpc.NEW"
    routes = {f"DELETE {vpc}/subnets/subnet.NEW": _operation("subnet.NEW"), f"DELETE {vpc}": _operation("vpc.NEW")}
    code, out, api = _run(monkeypatch, capsys, "network/teardown_network.py", routes, args)

    assert code == 0, out
    assert api.paths() == [f"DELETE {vpc}/subnets/subnet.NEW", f"DELETE {vpc}"]


def test_teardown_network_round_trip_leaves_a_supplied_network_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A passed-through network renders no delete flags, so teardown_network skips."""
    monkeypatch.delenv("BM_SKIP_TEARDOWN", raising=False)
    _, supplied, _ = _run(
        monkeypatch, capsys, "network/create_network.py", {}, ["--vpc-id=vpc.OLD", "--subnet-id=subnet.OLD"]
    )

    args = _render("teardown_network", {"create_network": supplied})
    code, out, api = _run(monkeypatch, capsys, "network/teardown_network.py", {}, args)

    assert "--delete-vpc" not in args
    assert code == 0
    assert out["skipped"] is True
    assert api.calls == []


def test_teardown_phase_renders_after_a_failed_create_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only a failed create_network output, no teardown step hits a missing step reference.

    A MissingStepRefError would report the BM and NIM teardowns as failed when
    there was nothing to tear down; each must render and no-op instead.
    """
    monkeypatch.delenv("BM_SKIP_TEARDOWN", raising=False)
    failed = {
        "success": False,
        "platform": "network",
        "network_id": "",
        "subnets": [],
        "created_vpc": False,
        "created_subnet_ids": [],
        "error": "HTTP 400",
    }
    teardown_steps = [name for name, step in _config_steps().items() if step.phase == "teardown"]

    rendered = {name: _render(name, {"create_network": failed}) for name in teardown_steps}

    assert teardown_steps == ["teardown_nim", "teardown", "verify_teardown", "teardown_network"]
    assert "--skip" in rendered["teardown_nim"]
    assert rendered["teardown"] == ["--instance-id=", "--key-file="]
    assert rendered["teardown_network"] == ["--vpc-id=", "--subnet-ids="]


def test_teardown_nim_still_runs_when_deploy_nim_ran() -> None:
    """The skip defaults must not suppress NIM cleanup after a real deployment."""
    outputs = {
        "power_cycle_instance": {"public_ip": "172.16.240.10", "key_file": "/tmp/key"},
        "deploy_nim": {"success": False},
    }

    args = _render("teardown_nim", outputs)

    assert args == ["--host=172.16.240.10", "--key-file=/tmp/key", "--user=ubuntu"]


def test_teardown_network_renders_nothing_to_delete_when_setup_never_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standalone teardown without create_network output must not delete anything."""
    monkeypatch.delenv("BM_SKIP_TEARDOWN", raising=False)

    args = _render("teardown_network", {})

    assert args == ["--vpc-id=", "--subnet-ids="]


# ── Fleet-capacity report steps (CAP01-01, CAP02-01, CAP05-01) ────────

FLEET_PATH = "GET /reports/ncp/fleet-capacity"


def _row(**overrides: Any) -> dict[str, Any]:
    """Return a fleet-capacity row as the API emits it (unpopulated fields included)."""
    row: dict[str, Any] = {
        "healthState": "HEALTHY",
        "instanceId": "bm.A",
        "createdAt": "2026-01-01T00:00:00Z",
        "hardwareType": "H100",
        "gpuCount": 8,
        "cspAccount": "tenant.T",
        "inUse": True,
        "region": "region-1",
        "capacityState": "In Use",
        "healthObservedAt": (datetime.now(UTC) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z"),
        "nodeId": "bm.A",
        "projectId": PROJECT,
        "label": "",
    }
    row.update(overrides)
    return row


FLEET_ROWS = [
    _row(),
    _row(nodeId="bm.B", instanceId="bm.B", inUse=False, capacityState="Reserved", projectId="", gpuCount=4),
    _row(nodeId="bm.C", instanceId="bm.C", healthState="UNHEALTHY", inUse=False, capacityState="Reserved"),
]


def test_governance_metrics_roll_capacity_state_into_buckets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In Use -> active+reserved, Reserved -> reserved, healthy from healthState, all delivered."""
    code, out, _ = _run(
        monkeypatch, capsys, "governance/query_governance_metrics.py", {FLEET_PATH: {"rows": FLEET_ROWS}}
    )

    assert code == 0, out
    assert out["metrics"] == {
        "delivered": {"nodes": 3, "gpus": 20},
        "healthy": {"nodes": 2, "gpus": 12},
        "reserved": {"nodes": 3, "gpus": 20},
        "active": {"nodes": 1, "gpus": 8},
    }
    check = _validate(GovernanceMetricsCheck, out)
    assert check._passed is True, check._error


def test_governance_metrics_count_unattributed_capacity_states_as_delivered_only() -> None:
    """Healthy/Delivered capacity states are idle, unattributed nodes: not reserved or active."""
    module = _load("governance/query_governance_metrics.py")
    rows = [_row(capacityState="Healthy", inUse=False), _row(capacityState="Delivered", healthState="UNKNOWN")]

    metrics = module.aggregate_metrics(rows)

    assert metrics["delivered"]["nodes"] == 2
    assert metrics["healthy"]["nodes"] == 1
    assert metrics["reserved"]["nodes"] == 0
    assert metrics["active"]["nodes"] == 0


def test_fleet_inventory_maps_rows_onto_the_cap02_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every row field maps one to one; cspAccount is the account."""
    code, out, _ = _run(monkeypatch, capsys, "governance/query_fleet_inventory.py", {FLEET_PATH: {"rows": FLEET_ROWS}})

    assert code == 0, out
    assert out["nodes"][0] == {
        "node_id": "bm.A",
        "health_state": "healthy",
        "instance_id": "bm.A",
        "created_at": "2026-01-01T00:00:00Z",
        "hardware_type": "H100",
        "gpu_count": 8,
        "account_id": "tenant.T",
        "project_id": PROJECT,
        "in_use": True,
        "region": "region-1",
    }
    check = _validate(FleetManagementApiCheck, out)
    assert check._passed is True, check._error


def test_fleet_inventory_keeps_an_unknown_health_state_failing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An UNKNOWN classification is not guessed healthy; CAP02 fails on it."""
    rows = [_row(healthState="UNKNOWN", healthObservedAt=None)]
    _, out, _ = _run(monkeypatch, capsys, "governance/query_fleet_inventory.py", {FLEET_PATH: {"rows": rows}})

    check = _validate(FleetManagementApiCheck, out)
    assert check._passed is False
    assert "health_state" in check._error


def test_host_health_reports_classification_and_freshness(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A HEALTHY node has a report, no alerts, and an observation age; no probes are invented."""
    code, out, _ = _run(monkeypatch, capsys, "health/query_host_health.py", {FLEET_PATH: {"rows": [_row()]}})

    assert code == 0, out
    host = out["hosts"][0]
    assert host["health_present"] is True
    assert host["healthy"] is True
    assert host["alerts"] == []
    assert host["probe_ids"] == []
    assert 60 <= host["observed_age_seconds"] <= 600
    check = _validate(HostHealthCheck, out)
    assert check._passed is True, check._error


@pytest.mark.parametrize(("health_state", "reason"), [("UNHEALTHY", "alerts"), ("UNKNOWN", "no health report")])
def test_host_health_fails_unhealthy_and_unclassified_nodes(
    health_state: str, reason: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An UNHEALTHY node must fail (the check only fails on alerts); UNKNOWN is no report."""
    rows = [_row(healthState=health_state)]
    _, out, _ = _run(monkeypatch, capsys, "health/query_host_health.py", {FLEET_PATH: {"rows": rows}})

    check = _validate(HostHealthCheck, out)
    assert check._passed is False
    assert reason in check._error


FLEET_STEPS = [
    ("governance/query_governance_metrics.py", GovernanceMetricsCheck),
    ("governance/query_fleet_inventory.py", FleetManagementApiCheck),
    ("health/query_host_health.py", HostHealthCheck),
]


@pytest.mark.parametrize("status", [404, 501])
@pytest.mark.parametrize(("script", "check_cls"), FLEET_STEPS)
def test_fleet_steps_skip_when_the_report_is_not_served(
    script: str,
    check_cls: type[BaseValidation],
    status: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fleet-capacity route not served is a structured skip, never a pass."""
    code, out, _ = _run(monkeypatch, capsys, script, {FLEET_PATH: status})

    assert code == 0
    assert out["success"] is True
    assert out["skipped"] is True
    assert "fleet-capacity report" in out["skip_reason"]
    check = check_cls(config={"step_output": out, **_suite_params(check_cls.__name__)})
    with pytest.raises(pytest.skip.Exception):
        check.execute()


@pytest.mark.parametrize(("script", "check_cls"), FLEET_STEPS)
def test_fleet_steps_fail_when_the_report_errors(
    script: str, check_cls: type[BaseValidation], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A served report that errors is a failure, not a skip."""
    code, out, _ = _run(monkeypatch, capsys, script, {FLEET_PATH: 500})

    assert code == 1
    assert "skipped" not in out
    check = _validate(check_cls, out)
    assert check._passed is False


# ── Tenant BM pool steps (CAP03-01, STG03-01) ─────────────────────────


def _bm(bm_id: str, **overrides: Any) -> dict[str, Any]:
    """Return a tenant-pool BM."""
    bm: dict[str, Any] = {"id": bm_id, "state": "RUNNING", "subnetId": "subnet.S", "ipAddress": "172.16.240.10"}
    bm.update(overrides)
    return bm


def test_resource_discovery_polls_twice_and_reports_stable_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The index is polled --polls times; unregistered BMs are listed but not discovered."""
    pool = {"items": [_bm("bm.A"), _bm("bm.B", state="ALLOCATED")]}
    code, out, api = _run(
        monkeypatch, capsys, "governance/query_resource_discovery.py", {"GET /compute/bms": pool}, ["--polls", "2"]
    )

    assert code == 0, out
    assert api.paths() == ["GET /compute/bms", "GET /compute/bms"]
    assert out["polls"] == 2
    assert out["unstable_identifiers"] == []
    assert out["resources"] == [
        {"resource_id": "bm.A", "discovered": True},
        {"resource_id": "bm.B", "discovered": False},
    ]
    check = _validate(ResourceDiscoveryApiCheck, out)
    assert check._passed is True, check._error


def test_resource_discovery_flags_an_identifier_that_vanishes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ID in the first poll but not the last is unstable; a new one is not."""
    polls = iter([{"items": [_bm("bm.A"), _bm("bm.B")]}, {"items": [_bm("bm.A"), _bm("bm.C")]}])
    routes: dict[str, Route] = {"GET /compute/bms": lambda _body, _query: next(polls)}
    _, out, _ = _run(monkeypatch, capsys, "governance/query_resource_discovery.py", routes)

    assert out["unstable_identifiers"] == ["bm.B"]
    check = _validate(ResourceDiscoveryApiCheck, out)
    assert check._passed is False


def test_stable_ips_cover_provisioned_attached_bms(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Provisioned attached BMs report their IP; unattached or unprovisioned BMs are out of scope.

    A tenant pool can be attached to one subnet with only provisioned BMs holding
    an address, so an AVAILABLE BM without one is expected.
    """
    pool = {
        "items": [
            _bm("bm.A"),
            _bm("bm.S", state="STOPPED", ipAddress="172.16.240.11"),
            _bm("bm.B", subnetId="", ipAddress=""),
            _bm("bm.C", state="AVAILABLE", ipAddress=""),
        ]
    }
    code, out, _ = _run(monkeypatch, capsys, "storage/query_stable_ips.py", {"GET /compute/bms": pool})

    assert code == 0, out
    assert out["hosts"] == [
        {"host_id": "bm.A", "primary_ip_addresses": ["172.16.240.10"]},
        {"host_id": "bm.S", "primary_ip_addresses": ["172.16.240.11"]},
    ]
    check = _validate(StableStorageNodeIpCheck, out)
    assert check._passed is True, check._error


def test_stable_ips_fail_an_attached_bm_without_an_ip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provisioned attached BM with no IP is reported empty so the check fails on it."""
    pool = {"items": [_bm("bm.A", ipAddress="")]}
    _, out, _ = _run(monkeypatch, capsys, "storage/query_stable_ips.py", {"GET /compute/bms": pool})

    check = _validate(StableStorageNodeIpCheck, out)
    assert check._passed is False


# ── Break-fix steps (BFX02-01, BFX02-03, BFX03-03) ────────────────────

SERIAL_PATH = f"GET /projects/{PROJECT}/compute/bms/bm.A/serial-logs"


def test_bmc_kernel_logs_report_the_queried_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The window asked about is the window reported, with the entry count."""
    logs = {"logs": [{"timestamp": "2026-09-23T00:00:00Z", "content": "secret"}] * 3}
    code, out, api = _run(
        monkeypatch,
        capsys,
        "breakfix/query_bmc_kernel_logs.py",
        {SERIAL_PATH: logs},
        ["--instance-id", "bm.A", "--window-hours", "24"],
    )

    assert code == 0, out
    host = out["hosts"][0]
    query = api.calls[0][3]
    assert query["from"] == [host["window_start"]]
    assert query["to"] == [host["window_end"]]
    assert host["entries_returned"] == 3
    assert "secret" not in json.dumps(out)
    check = _validate(BmcKernelLogCheck, out)
    assert check._passed is True, check._error


def test_bmc_kernel_logs_fail_an_empty_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No entries over the window is a failure, not a pass."""
    _, out, _ = _run(
        monkeypatch, capsys, "breakfix/query_bmc_kernel_logs.py", {SERIAL_PATH: {"logs": []}}, ["--instance-id", "bm.A"]
    )

    check = _validate(BmcKernelLogCheck, out)
    assert check._passed is False


def _event(event_id: str, bm_id: str, status: str, created: str, **overrides: Any) -> dict[str, Any]:
    """Return a MaintenanceEvent."""
    event: dict[str, Any] = {
        "id": event_id,
        "bmId": bm_id,
        "status": status,
        "code": "GPU_XID",
        "comment": "GPU fault",
        "createdAt": created,
        "clearedAt": None,
    }
    event.update(overrides)
    return event


def test_maintenance_events_use_the_open_default_filter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No status parameter: the API's default OPEN filter (ACTIVE + REQUESTED) applies."""
    events = {"items": [_event("maintenance-event.1", "bm.A", "ACTIVE", "2026-09-01T00:00:00Z")]}
    code, out, api = _run(
        monkeypatch, capsys, "breakfix/query_maintenance_events.py", {"GET /maintenance-events": events}
    )

    assert code == 0, out
    assert "status" not in api.calls[0][3]
    assert "bmId" not in api.calls[0][3]
    assert out["events"] == [{"machine_id": "bm.A", "status": "ACTIVE", "message": "GPU fault"}]
    check = _validate(MaintenanceEventsCheck, out)
    assert check._passed is True, check._error


def test_maintenance_events_skip_when_none_are_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty list cannot demonstrate the API; the check skips rather than passes."""
    _, out, _ = _run(monkeypatch, capsys, "breakfix/query_maintenance_events.py", {"GET /maintenance-events": {}})

    with pytest.raises(pytest.skip.Exception):
        _validate(MaintenanceEventsCheck, out)


def test_repair_history_groups_all_events_per_bm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """status=ALL includes cleared events; entries are grouped per BM, oldest first."""
    events = {
        "items": [
            _event("maintenance-event.2", "bm.A", "ACTIVE", "2026-09-02T00:00:00Z"),
            _event("maintenance-event.1", "bm.A", "CLEARED", "2026-08-01T00:00:00Z", clearedAt="2026-08-02T00:00:00Z"),
            _event("maintenance-event.3", "bm.B", "CLEARED", "2026-07-01T00:00:00Z"),
        ]
    }
    code, out, api = _run(
        monkeypatch,
        capsys,
        "breakfix/query_repair_history.py",
        {"GET /maintenance-events": events},
        ["--instance-id", ""],
    )

    assert code == 0, out
    assert api.calls[0][3]["status"] == ["ALL"]
    records = {r["machine_id"]: r["entries"] for r in out["records"]}
    assert [e["event_id"] for e in records["bm.A"]] == ["maintenance-event.1", "maintenance-event.2"]
    assert records["bm.A"][0]["cleared_at"] == "2026-08-02T00:00:00Z"
    check = _validate(RepairHistoryCheck, out)
    assert check._passed is True, check._error


# ── InfiniBand (SDN04-04) ─────────────────────────────────────────────

IB_PATH = f"GET /projects/{PROJECT}/network/ib-partitions"


def test_ib_tenant_isolation_reports_project_partitions_honestly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Partitions carry the project's tenant; cross-tenant sharing is declared unobservable."""
    routes: dict[str, Route] = {
        IB_PATH: {"items": [{"id": "ib-partition.1", "name": "ib-a", "pkey": "0x0012", "state": "READY"}]},
        f"GET /projects/{PROJECT}": {"project": {"id": PROJECT, "tenantId": "tenant.T"}},
    }
    code, out, _ = _run(monkeypatch, capsys, "infiniband/query_ib_tenant_isolation.py", routes)

    assert code == 0, out
    assert out["cross_tenant_observable"] is False
    assert out["partitions"] == [
        {"name": "ib-a", "partition_key": "0x0012", "tenant_id": "tenant.T", "status": "READY"}
    ]
    check = _validate(IbTenantIsolationCheck, out)
    assert check._passed is True, check._error


def test_ib_tenant_isolation_fails_the_default_partition_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partition reusing the all-ports default P_Key is not isolated."""
    routes: dict[str, Route] = {
        IB_PATH: {"items": [{"id": "ib-partition.1", "name": "ib-a", "pkey": "0x7fff", "state": "READY"}]},
        f"GET /projects/{PROJECT}": {"project": {"tenantId": "tenant.T"}},
    }
    _, out, _ = _run(monkeypatch, capsys, "infiniband/query_ib_tenant_isolation.py", routes)

    check = _validate(IbTenantIsolationCheck, out)
    assert check._passed is False


@pytest.mark.parametrize("status", [404, 501])
def test_ib_tenant_isolation_skips_when_not_enabled(
    status: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the IB partition service the step skips and makes no further calls."""
    code, out, api = _run(monkeypatch, capsys, "infiniband/query_ib_tenant_isolation.py", {IB_PATH: status})

    assert code == 0
    assert out["skipped"] is True
    assert api.paths() == [IB_PATH]
    with pytest.raises(pytest.skip.Exception):
        _validate(IbTenantIsolationCheck, out)


# ── Node health agents (BFX04-01) ─────────────────────────────────────

LAUNCH = {"instance_id": "bm.A", "ssh_user": "ubuntu", "key_file": "/tmp/isv-bm-test-gpu-key"}
DESCRIBE = {"public_ip": "172.16.240.10"}


def _health_agent_args(monkeypatch: pytest.MonkeyPatch, opt_in: str | None, nodes: list[dict[str, Any]]) -> list[str]:
    """Render query_node_health_agents with the given opt-in and fleet inventory."""
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    if opt_in is None:
        monkeypatch.delenv("FIREBIRD_HEALTH_AGENT_CHECK", raising=False)
    else:
        monkeypatch.setenv("FIREBIRD_HEALTH_AGENT_CHECK", opt_in)
    outputs = {"launch_instance": LAUNCH, "describe_instance": DESCRIBE, "query_fleet_inventory": {"nodes": nodes}}
    return _render("query_node_health_agents", outputs)


def _inventory_node(project_id: str = PROJECT, in_use: bool = True, gpu_count: int = 8) -> dict[str, Any]:
    """Return a fleet-inventory node record."""
    return {"project_id": project_id, "in_use": in_use, "gpu_count": gpu_count}


def test_node_health_agents_skip_unless_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unconfigured renders no nodes, so the shared script emits its structured skip."""
    args = _health_agent_args(monkeypatch, None, [_inventory_node()])

    assert args[0] == "--nodes="


def test_node_health_agents_probe_the_provisioned_bm_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opted in, the BM is probed with the run's key; the fleet size comes from the inventory."""
    nodes = [
        _inventory_node(),
        _inventory_node(),  # a second running GPU node in the project: coverage must fail
        _inventory_node(project_id="project.OTHER"),
        _inventory_node(in_use=False),
        _inventory_node(gpu_count=0),
    ]
    args = _health_agent_args(monkeypatch, "True", nodes)

    assert args == [
        "--nodes=172.16.240.10",
        "--expected-nodes=2",
        "--ssh-user=ubuntu",
        "--key-file=/tmp/isv-bm-test-gpu-key",
        "--no-host-key-check",
    ]


def test_node_health_agents_fleet_size_is_zero_without_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skipped fleet inventory renders 0, which the shared script rejects rather than covers."""
    monkeypatch.setenv("FIREBIRD_HEALTH_AGENT_CHECK", "true")
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    outputs = {"launch_instance": LAUNCH, "describe_instance": DESCRIBE, "query_fleet_inventory": {"skipped": True}}

    args = _render("query_node_health_agents", outputs)

    assert args[1] == "--expected-nodes=0"


def _load_shared_health_agents() -> ModuleType:
    """Load the shared BFX04-01 reference script."""
    spec = importlib.util.spec_from_file_location("test_firebird_shared_health_agents", SHARED_HEALTH_AGENTS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shared_health_agent_probe_uses_the_run_key_end_to_end(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rendered args reach ssh as -l/-i before ``--``, and the output passes BFX04-01."""
    module = _load_shared_health_agents()
    args = _health_agent_args(monkeypatch, "true", [_inventory_node()])
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> Any:
        """Report the first agent unit active."""
        calls.append(command)
        states = "".join("active\n" if i == 0 else "inactive\n" for i in range(len(module.AGENT_UNITS)))
        return module.subprocess.CompletedProcess(command, 0, stdout=states, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["query_node_health_agents.py", *args])

    assert module.main() == 0
    out = json.loads(capsys.readouterr().out)
    command = calls[0]
    separator = command.index("--")
    assert command[separator + 1] == "172.16.240.10"
    options = command[:separator]
    assert options[options.index("-l") + 1] == "ubuntu"
    assert options[options.index("-i") + 1] == "/tmp/isv-bm-test-gpu-key"
    assert "StrictHostKeyChecking=no" in options
    check = NodeHealthAgentCheck(config={"step_output": out})
    check.run()
    assert check._passed is True, check._error
