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

"""Tests for BM provisioning flags, SSH keys and waits, BM teardown, and the leftover sweep.

``common.provision.provision_bm`` sets the flags teardown relies on; these
tests pin its guards, flags, key handling, and call order through
``launch_instance``, and that every SSH wait ends before its step timeout. The
leftover sweep must delete only this provider's own, old ``isv-*`` accounts and
projects, and never fail setup.
"""

from __future__ import annotations

import base64
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from .harness import (
    PROJECT,
    SCRIPTS,
    FakeClock,
    HttpError,
    Route,
    config_steps,
    fake_keygen,
    load,
    operation,
    render,
    run,
)
from .iam_fake import FakeIam

BM = "bm.A"
BM_PATH = f"/projects/{PROJECT}/compute/bms/{BM}"
# The BM is pinned: automatic selection only ever picks AVAILABLE BMs, so the guards matter for a pinned one.
LAUNCH_ARGS = ["--bm-id", BM, "--vpc-id", "vpc.V", "--subnet-id", "subnet.S", "--image-id", "image.I"]
AUTO_ARGS = LAUNCH_ARGS[2:]  # no --bm-id: automatic selection


# ── provision_bm ──────────────────────────────────────────────────────


def _launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pool_bm: dict[str, Any],
    *,
    others: list[dict[str, Any]] | None = None,
    argv: list[str] | None = None,
    provision_route: Route | None = None,
    readback_lag: int = 0,
    ssh_ready: bool = True,
    clock: FakeClock | None = None,
    cloud_init: list[str] | None = None,
) -> tuple[int, dict[str, Any], Any]:
    """Run launch_instance (provision_bm) with ``bm.A`` in the pool; the BM's readback follows the calls made.

    ``cloud_init`` is the stdout of successive ``cloud-init status`` calls (the
    last one repeats); without it they answer with empty output.

    ``others`` are listed before ``bm.A`` and have no routes, so selecting one fails
    the run. ``readback_lag`` GETs after attach-subnet still show the old subnet,
    and provision is refused (409) unless the last readback showed the new one.
    """
    state = {
        "projectId": pool_bm.get("projectId", ""),
        "subnetId": pool_bm.get("subnetId", ""),
        "provisioned": False,
        "lag": 0,
        "seen_subnet": pool_bm.get("subnetId", ""),
    }
    provisions: list[dict[str, Any]] = []

    def attach(_b: Any, _q: Any) -> dict[str, Any]:
        state["projectId"] = PROJECT
        return {}

    def attach_subnet(body: Any, _q: Any) -> dict[str, Any]:
        state["subnetId"] = body["subnetId"]
        state["lag"] = readback_lag
        return operation(BM)

    def provision(body: Any, _q: Any) -> dict[str, Any]:
        if state["seen_subnet"] != "subnet.S":
            raise HttpError(409)
        provisions.append(body)
        state["provisioned"] = True
        return operation(BM)

    def get(_b: Any, _q: Any) -> dict[str, Any]:
        running = state["provisioned"]
        subnet = pool_bm.get("subnetId", "") if state["lag"] else state["subnetId"]
        state["lag"] = max(0, state["lag"] - 1)
        state["seen_subnet"] = subnet
        return {
            "bm": {
                "id": BM,
                "state": "RUNNING" if running else pool_bm["state"],
                "powerState": "ON" if running else "OFF",
                "ipAddress": "172.16.240.10" if running else "",
                "subnetId": subnet,
            }
        }

    routes: dict[str, Route] = {
        "GET /compute/bms": {"items": [*(others or []), {"id": BM, **pool_bm}]},
        f"POST {BM_PATH}/attach": attach,
        f"POST {BM_PATH}/attach-subnet": attach_subnet,
        f"POST {BM_PATH}/provision": provision if provision_route is None else provision_route,
        f"GET {BM_PATH}": get,
    }

    def prepare(module: Any) -> None:
        fake_keygen(monkeypatch, module, tmp_path)
        if clock:
            clock.install(monkeypatch, module)
        ssh_utils = module._common["ssh_utils"]

        answers = list(cloud_init or [])

        def ssh_run(*a: Any, **_k: Any) -> tuple[int, str, str]:
            if clock:
                clock.now += ssh_utils.SSH_ATTEMPT_TIMEOUT  # worst case: every attempt runs to its timeout
            if not ssh_ready:
                return 255, "", "Connection timed out"
            if "cloud-init status" in a[3] and answers:
                return 0, answers.pop(0) if len(answers) > 1 else answers[0], ""
            return 0, "", ""

        monkeypatch.setattr(ssh_utils, "ssh_run", ssh_run)

    code, out, api = run(
        monkeypatch, capsys, "bare_metal/launch_instance.py", routes, argv or LAUNCH_ARGS, prepare=prepare
    )
    api.provisions = provisions
    return code, out, api


def _injected_key(body: dict[str, Any]) -> str:
    """Return the public key the provision request's cloud-init user-data authorizes."""
    return base64.b64decode(body["userDataB64"]).decode().splitlines()[-1].strip().removeprefix("- ")


def test_provision_attaches_a_fresh_bm_then_provisions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A BM outside the project gets /attach, then attach-subnet, then provision; every flag is set."""
    code, out, api = _launch(monkeypatch, capsys, tmp_path, {"state": "AVAILABLE", "projectId": "", "subnetId": ""})

    assert code == 0, out
    assert api.paths("POST") == [
        f"POST {BM_PATH}/attach",
        f"POST {BM_PATH}/attach-subnet",
        f"POST {BM_PATH}/provision",
    ]
    assert (out["attached_project"], out["attached_subnet"], out["owned"]) == (True, True, True)
    assert out["state"] == "running"


def test_provision_refuses_a_bm_that_is_not_available(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A RUNNING BM (someone else's install) is rejected before any mutation; teardown must not touch it."""
    code, out, api = _launch(
        monkeypatch, capsys, tmp_path, {"state": "RUNNING", "projectId": PROJECT, "subnetId": "subnet.S"}
    )

    assert code == 1
    assert "expected AVAILABLE" in out["error"]
    assert api.paths("POST") == []
    assert (out["attached_project"], out["attached_subnet"], out["owned"]) == (False, False, False)
    assert out["generated_key"] is False and list(tmp_path.iterdir()) == []  # no key for a rejected BM


def test_provision_refuses_a_bm_on_another_subnet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A BM attached to a different subnet is rejected before any mutation."""
    code, out, api = _launch(
        monkeypatch, capsys, tmp_path, {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.OTHER"}
    )

    assert code == 1
    assert "subnet.OTHER" in out["error"]
    assert api.paths("POST") == []
    assert (out["attached_project"], out["attached_subnet"], out["owned"]) == (False, False, False)


@pytest.mark.parametrize(
    "other",
    [
        {"id": "bm.theirs", "state": "AVAILABLE", "projectId": "project.OTHER", "subnetId": ""},
        {"id": "bm.elsewhere", "state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.OTHER"},
    ],
    ids=["other-project", "other-subnet"],
)
def test_auto_selection_skips_bms_in_another_project_or_subnet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, other: dict[str, Any]
) -> None:
    """An AVAILABLE BM in another project, or on another subnet, is passed over for a free one."""
    code, out, _ = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": "", "subnetId": ""},
        others=[other],
        argv=AUTO_ARGS,
    )

    assert code == 0, out
    assert out["instance_id"] == BM


def test_provision_waits_for_the_subnet_readback_after_attach(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Provision is sent only once the BM readback shows the attached subnet, not when the Operation completes."""
    code, out, api = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": ""},
        readback_lag=2,
    )

    assert code == 0, out
    assert len(api.provisions) == 1


@pytest.mark.parametrize(
    ("failure", "owned"),
    [
        (HttpError(409), False),  # refused: another run or tenant took the BM
        (HttpError(403), False),
        (HttpError(503), True),  # server error: outcome unknown
        (HttpError(0), True),  # transport error (FirebirdApiError without a status)
        (TimeoutError("read timed out"), True),  # socket timeout outside urllib's URLError
    ],
    ids=["409", "403", "503", "transport", "timeout"],
)
def test_provision_owns_the_bm_unless_the_request_was_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    failure: Exception,
    owned: bool,
) -> None:
    """A 4xx provision answer leaves the BM alone at teardown; an unknown outcome is deprovisioned."""

    def provision(_b: Any, _q: Any) -> dict[str, Any]:
        raise failure

    code, out, _ = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.S"},
        provision_route=provision,
    )

    assert code == 1
    assert out["owned"] is owned
    assert out["instance_id"] == BM


def test_provision_owns_the_bm_once_the_request_is_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A 2xx provision whose Operation then fails is still this run's to deprovision."""
    code, out, _ = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.S"},
        provision_route=operation(BM, status="FAILED"),
    )

    assert code == 1 and "failed" in out["error"]
    assert out["owned"] is True


# ── SSH wait vs the step timeout ──────────────────────────────────────


@pytest.mark.parametrize("budget", [0, 19, 20, 35, 36, 300, 3900])
def test_wait_for_ssh_ends_before_its_deadline(monkeypatch: pytest.MonkeyPatch, budget: int) -> None:
    """With every attempt running to its timeout, the wait returns False no later than the deadline."""
    ssh_utils = load("bare_metal/launch_instance.py")._common["ssh_utils"]
    clock = FakeClock()
    attempts: list[float] = []

    def ssh_run(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        clock.now += ssh_utils.SSH_ATTEMPT_TIMEOUT
        attempts.append(clock.now)
        return 255, "", "Connection timed out"

    monkeypatch.setattr(ssh_utils, "time", clock)
    monkeypatch.setattr(ssh_utils, "ssh_run", ssh_run)
    deadline = clock.now + budget

    assert ssh_utils.wait_for_ssh("10.0.0.1", "ubuntu", "/k", deadline) is False
    assert clock.now <= deadline
    assert bool(attempts) is (budget >= ssh_utils.SSH_ATTEMPT_TIMEOUT)
    if budget >= 300:
        # It keeps trying until the deadline leaves no room for another attempt.
        assert deadline - clock.now < 15 + ssh_utils.SSH_ATTEMPT_TIMEOUT


def test_wait_for_ssh_returns_once_ssh_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that answers on the third attempt is ready."""
    ssh_utils = load("bare_metal/launch_instance.py")._common["ssh_utils"]
    clock = FakeClock()
    answers = iter([255, 255, 0])
    monkeypatch.setattr(ssh_utils, "time", clock)
    monkeypatch.setattr(ssh_utils, "ssh_run", lambda *_a, **_k: (next(answers), "", ""))

    assert ssh_utils.wait_for_ssh("10.0.0.1", "ubuntu", "/k", clock.now + 300) is True


def test_wait_for_cloud_init_returns_once_cloud_init_leaves_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSH answering is not enough: the wait polls until cloud-init reports a final status."""
    ssh_utils = load("bare_metal/launch_instance.py")._common["ssh_utils"]
    clock = FakeClock()
    answers = iter(
        [(255, "", "refused"), (0, "status: running\n", ""), (0, "status: running\n", ""), (0, "status: done\n", "")]
    )
    calls: list[str] = []

    def ssh_run(*a: Any, **_k: Any) -> tuple[int, str, str]:
        calls.append(a[3])
        return next(answers)

    monkeypatch.setattr(ssh_utils, "time", clock)
    monkeypatch.setattr(ssh_utils, "ssh_run", ssh_run)

    assert ssh_utils.wait_for_cloud_init("10.0.0.1", "ubuntu", "/k", clock.now + 300) == "done"
    assert len(calls) == 4 and all("cloud-init status" in c for c in calls)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("status: error\n", "error"),
        ("status: degraded done\n", "degraded done"),
        ("not_found\n", "not_found"),
        ("garbage\n", "unknown"),
    ],
)
def test_wait_for_cloud_init_reports_a_final_status_at_once(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str
) -> None:
    """A final, missing, or unreadable status ends the wait on the first poll."""
    ssh_utils = load("bare_metal/launch_instance.py")._common["ssh_utils"]
    clock = FakeClock()
    calls: list[str] = []
    monkeypatch.setattr(ssh_utils, "time", clock)
    monkeypatch.setattr(ssh_utils, "ssh_run", lambda *a, **_k: (calls.append(a[3]), (0, stdout, ""))[1])

    assert ssh_utils.wait_for_cloud_init("10.0.0.1", "ubuntu", "/k", clock.now + 300) == expected
    assert len(calls) == 1


@pytest.mark.parametrize("budget", [0, 19, 35, 300])
def test_wait_for_cloud_init_ends_before_its_deadline(monkeypatch: pytest.MonkeyPatch, budget: int) -> None:
    """cloud-init that never finishes ends the wait as ``timeout`` no later than the deadline."""
    ssh_utils = load("bare_metal/launch_instance.py")._common["ssh_utils"]
    clock = FakeClock()

    def ssh_run(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        clock.now += ssh_utils.SSH_ATTEMPT_TIMEOUT
        return 0, "status: running\n", ""

    monkeypatch.setattr(ssh_utils, "time", clock)
    monkeypatch.setattr(ssh_utils, "ssh_run", ssh_run)
    deadline = clock.now + budget

    assert ssh_utils.wait_for_cloud_init("10.0.0.1", "ubuntu", "/k", deadline) == "timeout"
    assert clock.now <= deadline


def test_launch_waits_for_cloud_init_and_reports_its_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Launch returns only after cloud-init finishes, and reports the status it saw."""
    code, out, _ = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.S"},
        clock=FakeClock(),
        cloud_init=["status: running\n", "status: running\n", "status: done\n"],
    )

    assert code == 0, out
    assert out["cloud_init_status"] == "done"


def test_launch_reports_its_bm_when_ssh_never_comes_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Unreachable SSH ends the launch within --timeout, with the IDs and flags teardown needs in its JSON."""
    clock = FakeClock()
    start = clock.now
    code, out, _ = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": "", "subnetId": ""},
        argv=[*LAUNCH_ARGS, "--timeout", "3900"],
        ssh_ready=False,
        clock=clock,
    )

    assert code == 1 and "SSH" in out["error"]
    assert clock.now <= start + 3900  # before the 4200 s step timeout kills the step and loses stdout
    assert out["instance_id"] == BM
    assert (out["owned"], out["attached_project"], out["attached_subnet"]) == (True, True, True)
    assert out["generated_key"] is True and Path(out["key_file"]).exists()


def test_host_status_log_reports_before_its_step_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The SSH wait leaves room for the sampling, so the JSON is printed within --timeout."""
    clock = FakeClock()
    start = clock.now

    def prepare(module: Any) -> None:
        clock.install(monkeypatch, module)
        ssh_utils = module._common["ssh_utils"]

        def ssh_run(*_a: Any, **_k: Any) -> tuple[int, str, str]:
            clock.now += ssh_utils.SSH_ATTEMPT_TIMEOUT
            return 255, "", "Connection timed out"

        monkeypatch.setattr(ssh_utils, "ssh_run", ssh_run)

    code, out, _ = run(
        monkeypatch,
        capsys,
        "bare_metal/host_status_log.py",
        {},
        ["--key-file=/k", "--public-ip=10.0.0.1"],
        prepare=prepare,
    )
    steps = config_steps("bare_metal", "bare_metal")

    assert code == 1 and "SSH did not become ready" in out["error"]
    assert clock.now <= start + 270 - 90  # the sampling's worst case still fits in --timeout
    assert 270 < steps["host_status_log"].timeout


# ── SSH key ownership ─────────────────────────────────────────────────


def test_launch_never_reuses_a_key_found_at_a_predictable_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A key planted at the old default path is ignored: a new key in a private directory is injected."""
    planted = tmp_path / "isv-bm-test-gpu-key"
    planted.write_text("PLANTED PRIVATE KEY\n")
    Path(f"{planted}.pub").write_text("ssh-ed25519 AAAA planted\n")

    code, out, api = _launch(
        monkeypatch, capsys, tmp_path, {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.S"}
    )

    key_file = Path(out["key_file"])
    assert code == 0, out
    assert key_file != planted and key_file.parent.parent == tmp_path
    assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
    assert out["generated_key"] is True and out["key_name"] == key_file.name
    assert _injected_key(api.provisions[0]) == f"ssh-ed25519 AAAA generated:{key_file}"


def test_every_launch_generates_its_own_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Two runs never share a key file, so one run's teardown cannot delete another's key."""
    bm = {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.S"}
    _, first, _ = _launch(monkeypatch, capsys, tmp_path, bm)
    _, second, _ = _launch(monkeypatch, capsys, tmp_path, bm)

    assert first["key_file"] != second["key_file"]
    assert Path(first["key_file"]).exists() and Path(second["key_file"]).exists()


def test_launch_reuses_an_explicitly_supplied_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--key-file reuses that key (no generation) and marks it not generated, so teardown keeps it."""
    supplied = tmp_path / "mine"
    supplied.write_text("MY PRIVATE KEY\n")
    Path(f"{supplied}.pub").write_text("ssh-ed25519 AAAA mine\n")

    code, out, api = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.S"},
        argv=[*LAUNCH_ARGS, f"--key-file={supplied}"],
    )

    assert code == 0, out
    assert out["key_file"] == str(supplied) and out["generated_key"] is False
    assert _injected_key(api.provisions[0]) == "ssh-ed25519 AAAA mine"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mine", "mine.pub"]  # nothing generated


def test_launch_refuses_a_supplied_key_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A missing --key-file is an error before any mutation, not a key generated at that path."""
    missing = tmp_path / "missing"
    code, out, api = _launch(
        monkeypatch,
        capsys,
        tmp_path,
        {"state": "AVAILABLE", "projectId": "", "subnetId": ""},
        argv=[*LAUNCH_ARGS, f"--key-file={missing}"],
    )

    assert code == 1 and "does not exist" in out["error"]
    assert api.paths("POST") == [] and not missing.exists()
    assert (out["owned"], out["generated_key"]) == (False, False)


def test_reuse_mode_reports_the_supplied_key_as_not_generated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BM_INSTANCE_ID + BM_KEY_FILE reuse that key; teardown must not delete it."""

    def prepare(_module: Any) -> None:
        monkeypatch.setenv("BM_INSTANCE_ID", BM)
        monkeypatch.setenv("BM_KEY_FILE", "/keys/kept-key")

    routes: dict[str, Route] = {f"GET {BM_PATH}": {"bm": {"id": BM, "state": "RUNNING", "ipAddress": "10.0.0.1"}}}
    code, out, _ = run(monkeypatch, capsys, "bare_metal/launch_instance.py", routes, LAUNCH_ARGS, prepare=prepare)

    assert code == 0, out
    assert out["key_file"] == "/keys/kept-key" and out["generated_key"] is False


@pytest.mark.parametrize(
    ("config", "platform", "teardown", "setup"),
    [
        ("bare_metal", "bare_metal", "teardown", "launch_instance"),
        ("image-registry", "image_registry", "teardown_bm", "install_image_bm"),
    ],
)
@pytest.mark.parametrize("generated", [True, False])
def test_teardown_deletes_only_a_generated_key(
    config: str, platform: str, teardown: str, setup: str, generated: bool
) -> None:
    """--delete-key-pair follows generated_key, not ownership of the BM."""
    output = {"instance_id": BM, "owned": True, "key_file": "/tmp/run/key", "generated_key": generated}

    args = render(config, platform, teardown, {setup: output})

    assert ("--delete-key-pair" in args) is generated
    if config == "bare_metal":
        verify = render(config, platform, "verify_teardown", {setup: output})
        assert ("--key-file=/tmp/run/key" in verify) is generated


def test_teardown_keeps_the_key_when_launch_reported_none() -> None:
    """A launch output without generated_key (killed step, older output) deletes nothing."""
    args = render("bare_metal", "bare_metal", "teardown", {"launch_instance": {"instance_id": BM, "owned": True}})

    assert "--delete-key-pair" not in args


def test_teardown_removes_the_generated_key_and_its_directory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The key pair and its private run directory go; nothing outside it is touched."""
    run_dir = tmp_path / "isv-bm-test-gpu-abc123"
    run_dir.mkdir(mode=0o700)
    key = run_dir / "isv-bm-test-gpu-key"
    key.write_text("K")
    Path(f"{key}.pub").write_text("P")
    neighbour = tmp_path / "unrelated"
    neighbour.write_text("keep")

    code, out, api = run(monkeypatch, capsys, "bare_metal/teardown.py", {}, ["--delete-key-pair", f"--key-file={key}"])

    assert code == 0, out
    assert not run_dir.exists() and neighbour.exists()
    assert api.calls == []


def test_teardown_keeps_a_supplied_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A --key-file without --delete-key-pair (a user's BM_KEY_FILE) survives teardown, pair and directory."""
    key = tmp_path / "my-key"
    key.write_text("K")
    Path(f"{key}.pub").write_text("P")

    code, out, api = run(monkeypatch, capsys, "bare_metal/teardown.py", {}, [f"--key-file={key}"])

    assert code == 0, out
    assert key.exists() and Path(f"{key}.pub").exists()
    assert not any(item.startswith("key_pair:") for item in out["resources_deleted"])
    assert api.calls == []


# ── teardown ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["AVAILABLE", "ALLOCATED"])
def test_teardown_does_not_deprovision_a_bm_without_an_os(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    """A BM that is already AVAILABLE or ALLOCATED gets no deprovision request."""
    routes: dict[str, Route] = {f"GET {BM_PATH}": {"bm": {"id": BM, "state": state, "subnetId": ""}}}

    code, out, api = run(
        monkeypatch, capsys, "bare_metal/teardown.py", routes, [f"--instance-id={BM}", "--deprovision"]
    )

    assert code == 0, out
    assert api.paths("POST") == []


def test_teardown_detaches_the_project_only_after_the_subnet_readback_clears(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Project detach waits for the BM readback to show no subnet, not just for the Operation."""
    state = {"subnetId": "subnet.S", "lag": 0, "seen": "subnet.S"}

    def detach_subnet(_b: Any, _q: Any) -> dict[str, Any]:
        state["subnetId"] = ""
        state["lag"] = 2
        return operation(BM)

    def get(_b: Any, _q: Any) -> dict[str, Any]:
        subnet = "subnet.S" if state["lag"] else state["subnetId"]
        state["lag"] = max(0, state["lag"] - 1)
        state["seen"] = subnet
        return {"bm": {"id": BM, "state": "AVAILABLE", "subnetId": subnet}}

    def detach(_b: Any, _q: Any) -> dict[str, Any]:
        if state["seen"]:
            raise HttpError(409)
        return {}

    routes: dict[str, Route] = {
        f"GET {BM_PATH}": get,
        f"POST {BM_PATH}/detach-subnet": detach_subnet,
        f"POST {BM_PATH}/detach": detach,
    }
    argv = [f"--instance-id={BM}", "--detach-subnet", "--detach-project"]

    code, out, api = run(monkeypatch, capsys, "bare_metal/teardown.py", routes, argv)

    assert code == 0, out
    assert api.paths("POST") == [f"POST {BM_PATH}/detach-subnet", f"POST {BM_PATH}/detach"]


# ── Leftover sweep ────────────────────────────────────────────────────


def _ts(hours_ago: float) -> str:
    """Return an API timestamp ``hours_ago`` hours in the past."""
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sa(sa_id: str, name: str, hours_ago: float | None) -> dict[str, Any]:
    """Return a listed service account."""
    item = {"id": sa_id, "displayName": name}
    return item if hours_ago is None else {**item, "createdAt": _ts(hours_ago)}


ACCOUNTS = [
    _sa("service-account.old-cp", "isv-cp-a1b2c3", 30),  # deleted
    _sa("service-account.old-audit", "isv-sec-audit-0f0f0f", 7),  # deleted
    _sa("service-account.young", "isv-iam-d4e5f6", 1),  # a concurrent run's
    _sa("service-account.prod", "prod-deploy-bot", 999),  # not ours
    _sa("service-account.lookalike", "isv-cp-a1b2c3-extra", 999),  # not the exact pattern
    _sa("service-account.uppercase", "isv-cp-A1B2C3", 999),
    _sa("service-account.undated", "isv-cp-ffffff", None),  # age unknown
]
PROJECTS = [
    {"id": "project.oldB", "name": "isv-lp-abcdef", "createdAt": _ts(48)},  # deleted
    {"id": "project.youngB", "name": "isv-lp-123456", "createdAt": _ts(2)},
    {"id": "project.prod", "name": "production", "createdAt": _ts(999)},
    {"id": "project.lookalike", "name": "isv-lp-abcdef-keep", "createdAt": _ts(999)},  # not the exact pattern
    {"id": PROJECT, "name": "isv-lp-999999", "createdAt": _ts(999)},  # the run's own project
]


def _sweep_routes(**overrides: Route) -> dict[str, Route]:
    """Return list and read routes plus deletes for exactly the resources that must go."""
    return {
        "GET /service-accounts": {"serviceAccounts": ACCOUNTS},
        "GET /projects": {"projects": PROJECTS},
        **{f"GET /service-accounts/{a['id']}": a for a in ACCOUNTS},
        **{f"GET /projects/{p['id']}": {"project": p} for p in PROJECTS},
        "DELETE /service-accounts/service-account.old-cp": {},
        "DELETE /service-accounts/service-account.old-audit": {},
        "DELETE /projects/project.oldB": {},
        **overrides,
    }


def test_sweep_deletes_only_old_exact_isv_resources(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Young, non-isv, look-alike, undated resources and the run's own project are untouched."""
    monkeypatch.delenv("FIREBIRD_SWEEP_MIN_AGE_HOURS", raising=False)
    code, out, api = run(monkeypatch, capsys, "iam/sweep_leftovers.py", _sweep_routes())

    assert code == 0 and out["success"] is True
    assert api.paths("DELETE") == [
        "DELETE /service-accounts/service-account.old-cp",
        "DELETE /service-accounts/service-account.old-audit",
        "DELETE /projects/project.oldB",
    ]
    assert out["resources_failed"] == []
    assert out["min_age_hours"] == 6.0
    assert "swept service_account:service-account.old-cp" in api.stderr


def test_sweep_min_age_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FIREBIRD_SWEEP_MIN_AGE_HOURS sets the threshold: at 1.5h the 2h-old project goes, the 1h-old account stays."""

    def prepare(_module: Any) -> None:
        monkeypatch.setenv("FIREBIRD_SWEEP_MIN_AGE_HOURS", "1.5")

    routes = _sweep_routes(**{"DELETE /projects/project.youngB": {}})
    code, out, api = run(monkeypatch, capsys, "iam/sweep_leftovers.py", routes, prepare=prepare)

    assert code == 0
    assert out["min_age_hours"] == 1.5
    assert "DELETE /projects/project.youngB" in api.paths("DELETE")
    assert "DELETE /service-accounts/service-account.young" not in api.paths("DELETE")


def test_sweep_delete_errors_do_not_fail_setup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed delete is reported; the others still run and the step succeeds."""
    routes = _sweep_routes(**{"DELETE /service-accounts/service-account.old-cp": 500})

    code, out, _ = run(monkeypatch, capsys, "iam/sweep_leftovers.py", routes)

    assert code == 0 and out["success"] is True
    assert out["resources_failed"][0].startswith("service_account:service-account.old-cp")
    assert len(out["resources_deleted"]) == 2


@pytest.mark.parametrize(
    ("overrides", "skipped"),
    [
        ({"GET /service-accounts": 403, "GET /projects": 403}, True),
        ({"GET /service-accounts": 501}, False),
    ],
)
def test_sweep_skips_what_it_cannot_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], overrides: dict[str, Route], skipped: bool
) -> None:
    """Nothing listable is a clean skip; one refused listing still sweeps the other kind."""
    code, out, api = run(monkeypatch, capsys, "iam/sweep_leftovers.py", _sweep_routes(**overrides))

    assert code == 0 and out["success"] is True
    assert out.get("skipped", False) is skipped
    assert api.paths("DELETE") == ([] if skipped else ["DELETE /projects/project.oldB"])


def test_sweep_patterns_cover_every_name_the_scripts_create() -> None:
    """Each isv-* account or project name a script creates is one the sweep recognizes, and nothing broader."""
    sweep = load("iam/sweep_leftovers.py")
    sources = "\n".join(path.read_text() for path in SCRIPTS.rglob("*.py"))
    configs = "\n".join(p.read_text() for p in (SCRIPTS.parent / "config").glob("*.yaml"))
    account_prefixes = set(re.findall(r'service_accounts\.create\([^,]+, "(isv-[a-z-]+)"\)', sources))
    account_prefixes |= set(re.findall(r'"--name-prefix"\n\s+- "(isv-[a-z-]+)"', configs))
    project_prefixes = set(re.findall(r'unique_name\("(isv-lp)"\)', sources))

    assert account_prefixes == {"isv-cp", "isv-iam", "isv-sec-sa", "isv-sec-lp", "isv-sec-audit"}
    assert project_prefixes == {"isv-lp"}
    for prefix in account_prefixes:
        assert sweep.SA_NAME.match(f"{prefix}-0a1b2c")
    assert sweep.PROJECT_NAME.match("isv-lp-0a1b2c")
    for name in ("isv-lp-0a1b2c", "isv-crud-0a1b2c", "isv-cp-0a1b2c-x", "xisv-cp-0a1b2c", "isv-cp-0a1b2"):
        assert not sweep.SA_NAME.match(name), name


@pytest.mark.parametrize(
    ("config", "platform"), [("control-plane", "control_plane"), ("iam", "iam"), ("security", "security")]
)
def test_sweep_is_the_first_setup_step(config: str, platform: str) -> None:
    """Every config that creates accounts or projects sweeps leftovers first."""
    steps = config_steps(config, platform)

    assert next(iter(steps)) == "sweep_leftovers"
    assert steps["sweep_leftovers"].phase == "setup"


def test_created_accounts_and_projects_are_logged_when_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IDs reach stderr the moment they exist, so a killed step still leaves them on record."""
    iam = FakeIam()
    routes = {**iam.routes("project.B"), "POST /projects": {"project": {"id": "project.B"}}, "POST /auth/token": 401}

    _, _, api = run(monkeypatch, capsys, "security/least_privilege_test.py", routes, ["--retries", "1"])

    assert "created project project.B" in api.stderr
    assert "created service account service-account.1" in api.stderr


# ── Power-on whose Operation fails while the platform still applies it ─────

REFUSED_POWER_ON = {
    "operation": {
        "id": "operation.on",
        "action": "POWER_ON",
        "status": "FAILED",
        "resourceId": BM,
        "error": {"code": "TASK_FAILED", "message": "ResetType On conflicts: host still shutting down"},
    }
}


def _start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    power_on: Route,
    comes_up_after: int | None,
) -> tuple[int, dict[str, Any]]:
    """Run start_instance on a STOPPED ``bm.A`` that reads RUNNING after ``comes_up_after`` GETs (None: never)."""
    gets = {"n": 0}

    def get(_b: Any, _q: Any) -> dict[str, Any]:
        gets["n"] += 1
        up = comes_up_after is not None and gets["n"] > comes_up_after
        return {
            "bm": {
                "id": BM,
                "state": "RUNNING" if up else "STOPPED",
                "powerState": "ON" if up else "OFF",
                "ipAddress": "172.16.240.10",
            }
        }

    clock = FakeClock()

    def prepare(module: Any) -> None:
        clock.install(monkeypatch, module)
        monkeypatch.setattr(module, "wait_for_ssh", lambda *_a, **_k: True)

    routes: dict[str, Route] = {f"GET {BM_PATH}": get, f"POST {BM_PATH}/power-on": power_on}
    argv = [f"--instance-id={BM}", "--key-file=/k", "--timeout=900"]
    code, out, _ = run(monkeypatch, capsys, "bare_metal/start_instance.py", routes, argv, prepare=prepare)
    return code, out


def test_start_succeeds_when_the_bm_comes_up_after_a_failed_power_on_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The BMC refused the first attempt, the platform applied it later: started, with the failure on record."""
    code, out = _start(monkeypatch, capsys, REFUSED_POWER_ON, comes_up_after=3)

    assert code == 0, out
    assert out["start_initiated"] is True and out["ssh_ready"] is True
    assert "ResetType On conflicts" in out["power_on_operation_error"]


def test_start_fails_with_both_errors_when_the_bm_stays_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed power-on that the platform never applies fails the step, naming the Operation's error."""
    code, out = _start(monkeypatch, capsys, REFUSED_POWER_ON, comes_up_after=None)

    assert code == 1
    assert out["start_initiated"] is True
    assert "ResetType On conflicts" in out["error"] and "did not reach RUNNING" in out["error"]


def test_start_fails_at_once_when_the_power_on_request_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An HTTP refusal means nothing was started: no waiting, start_initiated stays false."""

    def refuse(_b: Any, _q: Any) -> dict[str, Any]:
        raise HttpError(409)

    code, out = _start(monkeypatch, capsys, refuse, comes_up_after=1)

    assert code == 1
    assert out["start_initiated"] is False
    assert "power_on_operation_error" not in out
