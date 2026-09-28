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

"""Tests for the Firebird storage config, scripts, and StorageProvider shim.

Filesystems are faked statefully (``FakeStorage``): one fake carries a
filesystem from create through resize to delete, tracks the tenant organization's
allocation separately from the API readback, and answers every per-filesystem
GET with a credential blob that must never reach any output. The tenant's own
filesystems (``team-home``, ``shared-data``) exist in every fake and must survive
every script.
"""

from __future__ import annotations

import base64
import itertools
import json
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from isvtest.core.storage import load_provider_registry
from isvtest.validations.hss import (
    HssLiveExpansionCheck,
    HssMultipleFilesystemsCheck,
    HssStorageProvisioningCheck,
)
from isvtest.validations.storage_provider import StorageProviderApiCheck

from .harness import (
    FIREBIRD,
    PROJECT,
    SCRIPTS,
    SUITES,
    FakeApi,
    FakeClock,
    HttpError,
    Route,
    config_steps,
    load,
    operation,
    render,
    run,
    schema_errors,
    validate,
)

FS = f"/projects/{PROJECT}/storage/fs"
TENANT = "tenant.T"
RUN_ID = "a1b2c3"
# Stands in for authCredentialsBase64: a base64 client auth-token.json whose values must never leak.
TOKEN = {
    "access_token": "ACCESS-SECRET-7f3e",
    "refresh_token": "REFRESH-SECRET-9c1a",
    "token_type": "Bearer",
    "expires_in": 300,
    "password_change_required": False,
}
SECRET = base64.b64encode(json.dumps(TOKEN).encode()).decode()
ENDPOINT = "http://weka-backend.example:14000"
DEV = {"filesystem.TEAMHOME": "team-home", "filesystem.SHARED": "shared-data"}
NO_BM_REASON = "no BM configured for mount checks"
NFS_REASON = "NFS is not enabled on the filesystem cluster"
API_STEPS = ("provision_storage", "multiple_filesystems", "live_expansion")
# Mount-dependent step -> its script.
MOUNT_STEPS = {
    "provision_parallel_fs": "parallel_fs_test.py",
    "qos_throughput": "qos_throughput_test.py",
    "quota_enforcement": "quota_enforcement_test.py",
    "root_squash": "root_squash_test.py",
    "flock_mount": "flock_mount_test.py",
    "changelog_audit": "changelog_audit_test.py",
    "multipath": "multipath_test.py",
    "home_directory_storage": "home_directory_storage_test.py",
}
GAP_STEPS = (
    "launch_instance",
    "create_volume",
    "snapshot_lifecycle",
    "volume_resize",
    "volume_persistence",
    "teardown_volume",
    "non_disruptive_upgrade",
    "rdma_memory_protection",
    "setup_cluster",
)


def _ts(hours_ago: float) -> str:
    """Return an API timestamp ``hours_ago`` hours before now."""
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456Z")


class FakeStorage:
    """Stateful Filesystem API: create, read, resize, delete, and the tenant organization's status.

    ``backend`` is what the backend allocated per filesystem; a resize changes it only
    when ``backend_resizes`` (the API readback always shows the requested size).
    """

    def __init__(
        self,
        *,
        backend_resizes: bool = True,
        create_status: str = "COMPLETED",
        readback_gib: int | None = None,
        resize_state: str = "READY",
        fail_create_at: int | None = None,
        delete_errors: list[int] | None = None,
        create_lands_late: bool = False,
        delete_sticks: bool = False,
        stale_allocations: list[int] | None = None,
    ) -> None:
        """Start with the tenant's two dev filesystems.

        ``create_lands_late``: a new filesystem's backend allocation stays 0 until its
        resize request. ``delete_sticks``: a delete is accepted but the filesystem
        stays readable. ``stale_allocations``: the first status reads return these
        (an earlier delete's release still pending) before the live sum.
        """
        self.stale_allocations = list(stale_allocations or [])
        self.create_lands_late = create_lands_late
        self.delete_sticks = delete_sticks
        self.pending: dict[str, int] = {}
        self.backend_resizes = backend_resizes
        self.create_status = create_status
        self.readback_gib = readback_gib
        self.resize_state = resize_state
        self.fail_create_at = fail_create_at
        self.delete_errors = list(delete_errors or [])
        self.fs: dict[str, dict[str, Any]] = {}
        self.backend: dict[str, int] = {}
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.delete_attempts: list[str] = []
        self._ids = itertools.count(1)
        self.add("filesystem.TEAMHOME", "team-home", 10240, _ts(24 * 20))
        self.add("filesystem.SHARED", "shared-data", 6144, _ts(24 * 18))

    def add(self, fs_id: str, name: str, gib: int, created_at: str, state: str = "READY") -> None:
        """Seed an existing filesystem."""
        self.fs[fs_id] = {
            "id": fs_id,
            "name": name,
            "state": state,
            "capacity": {"size": gib, "sizeUnit": "GiB"},
            "createdAt": created_at,
        }
        self.backend[fs_id] = gib

    def _create(self, body: Any, _q: Any) -> dict[str, Any]:
        n = next(self._ids)
        self.bodies.append(("POST", body))
        if n == self.fail_create_at:
            raise HttpError(429)
        fs_id = f"filesystem.{n}"
        size = body["capacity"]["size"]
        self.add(fs_id, body["name"], self.readback_gib or size, "2026-09-28T10:00:00Z")
        self.backend[fs_id] = size
        if self.create_lands_late:
            self.backend[fs_id] = 0
            self.pending[fs_id] = size
        return operation(fs_id, self.create_status, op_id=f"operation.c{n}")

    def _get(self, fs_id: str) -> Any:
        def get(_b: Any, _q: Any) -> dict[str, Any]:
            if fs_id not in self.fs:
                raise HttpError(404)
            return {"filesystem": dict(self.fs[fs_id]), "authCredentialsBase64": SECRET, "storageEndpoint": ENDPOINT}

        return get

    def _put(self, fs_id: str) -> Any:
        def put(body: Any, _q: Any) -> dict[str, Any]:
            self.bodies.append(("PUT", body))
            self.fs[fs_id]["capacity"] = dict(body["capacity"])
            self.fs[fs_id]["state"] = self.resize_state
            if fs_id in self.pending:
                self.backend[fs_id] = self.pending.pop(fs_id)
            if self.backend_resizes:
                self.backend[fs_id] = body["capacity"]["size"]
            return operation(fs_id, op_id="operation.u")

        return put

    def _delete(self, fs_id: str) -> Any:
        def delete(_b: Any, _q: Any) -> dict[str, Any]:
            if self.delete_errors:
                raise HttpError(self.delete_errors.pop(0))
            if fs_id not in self.fs:
                raise HttpError(404)
            self.delete_attempts.append(fs_id)
            if self.delete_sticks:
                return operation(fs_id, op_id="operation.d")
            del self.fs[fs_id]
            del self.backend[fs_id]
            self.deleted.append(fs_id)
            return operation(fs_id, op_id="operation.d")

        return delete

    def _status(self, _b: Any, _q: Any) -> dict[str, Any]:
        allocated = self.stale_allocations.pop(0) if self.stale_allocations else sum(self.backend.values())
        return {"totalQuotaGb": 93132, "allocatedGb": allocated}

    def routes(self) -> dict[str, Route]:
        """Return the fake's routes, covering the seeded filesystems and the next few created ones."""
        routes: dict[str, Route] = {
            f"GET {FS}": lambda _b, _q: {"items": [dict(fs) for fs in self.fs.values()]},
            f"POST {FS}": self._create,
            f"GET {FS}/status": self._status,
            f"GET /projects/{PROJECT}": {"project": {"id": PROJECT, "tenantId": TENANT}},
        }
        for fs_id in [*self.fs, *(f"filesystem.{n}" for n in range(1, 8))]:
            routes[f"GET {FS}/{fs_id}"] = self._get(fs_id)
            routes[f"PUT {FS}/{fs_id}"] = self._put(fs_id)
            routes[f"DELETE {FS}/{fs_id}"] = self._delete(fs_id)
        return routes

    def names(self) -> list[str]:
        """Return the names of the filesystems that still exist."""
        return sorted(fs["name"] for fs in self.fs.values())


def _assert_dev_untouched(storage: FakeStorage, api: FakeApi) -> None:
    """The tenant's own filesystems still exist and no mutation named them."""
    assert set(DEV) <= set(storage.fs)
    assert not [p for p in api.paths() if not p.startswith("GET ") and any(d in p for d in DEV)]


def _assert_secret_never_leaks(out: dict[str, Any], api: FakeApi) -> None:
    """Neither the mount credential blob nor any token value inside it reaches stdout or stderr."""
    for secret in (SECRET, TOKEN["access_token"], TOKEN["refresh_token"]):
        assert secret not in repr(out)
        assert secret not in api.stderr


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: str,
    storage: FakeStorage,
    argv: list[str] | None = None,
    clock: FakeClock | None = None,
) -> tuple[int, dict[str, Any], FakeApi]:
    """Run a storage script against ``storage``; waits run on a fake clock, so none can hang."""

    def prepare(module: Any) -> None:
        (clock or FakeClock()).install(monkeypatch, module)

    return run(monkeypatch, capsys, f"storage/{script}", storage.routes(), argv, prepare=prepare)


# ── Config ────────────────────────────────────────────────────────────


def _suite_steps() -> set[str]:
    """Return every step name the storage suite binds a validation to."""
    steps = set()
    for group in yaml.safe_load((SUITES / "storage.yaml").read_text())["tests"]["validations"].values():
        steps.add(group.get("step"))
        steps.update(check.get("step") for check in (group.get("checks") or {}).values())
    return steps - {None}


def test_config_wires_suite_steps_setup_first_teardown_last() -> None:
    """Every step is a suite step (or setup/sweep/mount/teardown); setup runs first, teardown last."""
    steps = config_steps("storage", "storage")
    phases = [step.phase for step in steps.values()]

    # nfs_home_directory: DIR02-01, rebound by the provider (see the home_directory override).
    assert set(steps) <= _suite_steps() | {"setup", "sweep_leftovers", "setup_mount", "nfs_home_directory", "teardown"}
    assert list(steps)[:3] == ["setup", "sweep_leftovers", "setup_mount"]
    assert list(steps)[-1] == "teardown"
    assert phases == sorted(phases, key=["setup", "test", "teardown"].index)
    assert "labels:" not in (FIREBIRD / "config" / "storage.yaml").read_text()


def test_config_wires_api_and_mount_checks_to_scripts_nfs_to_a_skip_and_leaves_gaps_unwired() -> None:
    """API and mount checks run scripts, DIR02-01 skips on its own step, and the gaps have no step."""
    steps = config_steps("storage", "storage")

    for name in API_STEPS:
        assert steps[name].command.endswith(f"storage/{name}_test.py"), name
    for name, script in MOUNT_STEPS.items():
        assert steps[name].command.endswith(f"storage/{script}"), name
        assert steps[name].continue_on_failure, name
    assert steps["setup_mount"].continue_on_failure
    assert steps["nfs_home_directory"].command.endswith("storage/skip_check.py")
    assert render("storage", "storage", "nfs_home_directory", {}) == [
        "--test-name",
        "nfs_home_directory",
        "--reason",
        NFS_REASON,
    ]
    assert not set(GAP_STEPS) & set(steps)


def test_config_binds_dir01_to_the_home_directory_step_and_dir02_to_the_nfs_skip() -> None:
    """The provider splits the suite's home_directory group so DIR02-01 can skip on its own."""
    from isvtest.core.resolution import parse_validations

    from isvctl.config.merger import merge_yaml_files

    config = merge_yaml_files([FIREBIRD / "config" / "storage.yaml"])
    steps = {
        e.name: e.step for e in parse_validations(config["tests"]["validations"]) if e.category == "home_directory"
    }
    assert steps == {
        "DirectoryFilesystemQuotaCheck": "home_directory_storage",
        "DirectoryUsageAccountingCheck": "home_directory_storage",
        "DirectoryNfsAvailabilityCheck": "nfs_home_directory",
    }


@pytest.mark.parametrize("name", ["sweep_leftovers", "setup_mount", "live_expansion", *MOUNT_STEPS, "teardown"])
def test_config_hands_the_bm_from_the_environment_to_every_step_that_touches_it(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """BM_INSTANCE_ID / BM_KEY_FILE reach each BM step, and the mount steps get setup_mount's mount."""
    monkeypatch.setenv("BM_INSTANCE_ID", "bm.B")
    monkeypatch.setenv("BM_KEY_FILE", "/k/key")
    outputs = {
        "setup": {"run_id": RUN_ID},
        "setup_mount": {"mount_point": f"/mnt/isv-{RUN_ID}", "fs_id": "filesystem.1", "client_version": "5.1"},
    }

    args = render("storage", "storage", name, outputs)

    assert "--instance-id=bm.B" in args and "--key-file=/k/key" in args and "--ssh-user=ubuntu" in args
    if name in MOUNT_STEPS:
        assert f"--mount-point=/mnt/isv-{RUN_ID}" in args and "--mount-error=" in args
    if name in ("setup_mount", "live_expansion", "home_directory_storage"):
        assert "--mount-options=net=udp" in args


def test_config_leaves_the_mount_checks_without_a_mount_when_setup_mount_skipped() -> None:
    """Without a BM, setup_mount emits no mount point, and the mount steps render empty ones."""
    args = render(
        "storage", "storage", "qos_throughput", {"setup": {"run_id": RUN_ID}, "setup_mount": {"skipped": True}}
    )
    assert "--instance-id=" in args and "--mount-point=" in args


def test_config_hands_the_run_id_to_every_creating_step_and_teardown() -> None:
    """Scripts that create filesystems, the shim's check, and teardown all see the setup step's run ID."""
    setup = {"run_id": RUN_ID, "storage": {"manifest_path": "/m.yaml"}}

    for name in API_STEPS:
        args = render("storage", "storage", name, {"setup": setup})
        assert args[:2] == ["--run-id", RUN_ID], name
    assert render("storage", "storage", "teardown", {"setup": setup})[0] == f"--run-id={RUN_ID}"
    assert render("storage", "storage", "teardown", {})[0] == "--run-id="
    check = yaml.safe_load((FIREBIRD / "config" / "storage.yaml").read_text())["tests"]["validations"]
    assert "steps.setup.run_id" in check["storage_provider_api"]["checks"]["StorageProviderApiCheck"]["run_id"]


def test_config_keeps_filesystems_at_the_1_gib_minimum() -> None:
    """Every filesystem the config creates is 1 GiB, grown only to 2 GiB by live_expansion."""
    setup = {"setup": {"run_id": RUN_ID}}

    assert render("storage", "storage", "provision_storage", setup)[2:4] == ["--capacity-gib", "1"]
    assert "--capacity-gib" in (args := render("storage", "storage", "multiple_filesystems", setup))
    assert args[args.index("--capacity-gib") + 1] == "1"
    assert render("storage", "storage", "live_expansion", setup)[2:6] == ["--from-gib", "1", "--to-gib", "2"]


def _timeout_arg(args: list[str]) -> int:
    """Return the ``--timeout`` value from rendered step args (``--timeout N`` or ``--timeout=N``)."""
    for i, arg in enumerate(args):
        if arg == "--timeout":
            return int(args[i + 1])
        if arg.startswith("--timeout="):
            return int(arg.split("=", 1)[1])
    raise AssertionError(f"no --timeout in {args}")


@pytest.mark.parametrize("name", [*API_STEPS, "sweep_leftovers", "teardown"])
def test_config_keeps_every_deleting_script_inside_its_step_timeout(name: str) -> None:
    """A script's own --timeout (its work plus every delete) ends before the orchestrator kills the step."""
    steps = config_steps("storage", "storage")
    budget = _timeout_arg(render("storage", "storage", name, {"setup": {"run_id": RUN_ID}}))

    assert budget < steps[name].timeout, name
    if name in API_STEPS:
        assert budget > load(f"storage/{name}_test.py").CLEANUP_SECONDS, name


# ── setup and the mount-step skips ────────────────────────────────────


def test_setup_mints_a_run_id_and_points_at_the_manifest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """setup emits a 6-hex run ID, its prefix, and the existing manifest path; it calls no API."""
    code, out, api = run(monkeypatch, capsys, "storage/setup.py", {})

    assert code == 0 and out["success"]
    assert len(out["run_id"]) == 6 and int(out["run_id"], 16) >= 0
    assert out["fs_prefix"] == f"isv-fs-{out['run_id']}-"
    assert Path(out["storage"]["manifest_path"]) == FIREBIRD / "config" / "storage-provider-manifest.yaml"
    assert api.calls == []


@pytest.mark.parametrize("step", [*MOUNT_STEPS, "setup_mount"])
def test_mount_steps_skip_without_a_bm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    """Without a BM every mount step is a structured skip; no API call, no SSH (the harness fails both)."""
    script = MOUNT_STEPS.get(step, "setup_mount.py")
    argv = ["--run-id", RUN_ID] if step in ("setup_mount", "home_directory_storage") else []
    code, out, api = run(monkeypatch, capsys, f"storage/{script}", {}, argv)

    assert code == 0
    assert out["success"] is True and out["skipped"] is True
    assert out["skip_reason"] == NO_BM_REASON
    assert api.calls == []


def test_nfs_step_skips_with_the_reason_it_is_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """skip_check.py is a structured skip with the given reason; no API call."""
    code, out, api = run(
        monkeypatch, capsys, "storage/skip_check.py", {}, ["--test-name", "nfs_home_directory", "--reason", NFS_REASON]
    )

    assert code == 0
    assert out == {
        "success": True,
        "platform": "storage",
        "test_name": "nfs_home_directory",
        "skipped": True,
        "skip_reason": NFS_REASON,
    }
    assert api.calls == []


# ── provision_storage (HSS01-01) ──────────────────────────────────────


def test_provision_storage_passes_and_deletes_its_filesystem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 1 GiB filesystem is created READY with the requested capacity, then deleted."""
    storage = FakeStorage()
    code, out, api = _run(monkeypatch, capsys, "provision_storage_test.py", storage, ["--run-id", RUN_ID])

    assert code == 0, out
    check = validate("storage", HssStorageProvisioningCheck, out)
    assert check._passed, check._error
    assert out["tests"]["capacity_matches"] == {"passed": True, "capacity_gib": 1}
    assert storage.bodies == [("POST", {"name": f"isv-fs-{RUN_ID}-hss01", "capacity": {"size": 1, "sizeUnit": "GiB"}})]
    assert storage.deleted == ["filesystem.1"]
    assert storage.names() == ["shared-data", "team-home"]
    _assert_dev_untouched(storage, api)
    _assert_secret_never_leaks(out, api)


def test_provision_storage_fails_a_capacity_mismatch_and_still_deletes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A readback capacity other than the request fails capacity_matches; the filesystem is deleted."""
    storage = FakeStorage(readback_gib=2)
    code, out, _ = _run(monkeypatch, capsys, "provision_storage_test.py", storage, ["--run-id", RUN_ID])

    assert code == 1
    assert out["tests"]["capacity_matches"]["passed"] is False
    assert "read back 2" in out["tests"]["capacity_matches"]["error"]
    assert not validate("storage", HssStorageProvisioningCheck, out)._passed
    assert storage.deleted == ["filesystem.1"]


def test_provision_storage_deletes_a_filesystem_whose_create_operation_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ID is recorded when the API accepts the create, so a failed Operation still leaves nothing behind."""
    storage = FakeStorage(create_status="FAILED")
    code, out, _ = _run(monkeypatch, capsys, "provision_storage_test.py", storage, ["--run-id", RUN_ID])

    assert code == 1
    assert out["tests"]["api_available"]["passed"] is True
    assert out["tests"]["provisioned"]["passed"] is False
    assert storage.deleted == ["filesystem.1"]


def test_provision_storage_fails_the_step_when_its_delete_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed delete is reported and fails the step even though every subtest passed."""
    storage = FakeStorage(delete_errors=[500])
    code, out, _ = _run(monkeypatch, capsys, "provision_storage_test.py", storage, ["--run-id", RUN_ID])

    assert code == 1 and out["success"] is False
    assert all(t["passed"] for t in out["tests"].values())
    assert out["cleanup_errors"] and "filesystem.1" in out["cleanup_errors"][0]


def test_provision_storage_reports_an_unserved_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the Filesystem API nothing is created and every subtest fails with the error."""
    code, out, api = run(
        monkeypatch, capsys, "storage/provision_storage_test.py", {f"GET {FS}": 404}, ["--run-id", RUN_ID]
    )

    assert code == 1
    assert {t["passed"] for t in out["tests"].values()} == {False}
    assert "HTTP 404" in out["error"]
    assert api.paths("POST") == []


# ── multiple_filesystems (HSS09-01) ───────────────────────────────────


def test_multiple_filesystems_passes_and_deletes_both(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two 1 GiB filesystems are READY together within the quota; the smallest is 0.001 TiB."""
    storage = FakeStorage()
    code, out, api = _run(monkeypatch, capsys, "multiple_filesystems_test.py", storage, ["--run-id", RUN_ID])

    assert code == 0, out
    check = validate("storage", HssMultipleFilesystemsCheck, out)
    assert check._passed, check._error
    assert out["tests"]["multiple_filesystems"] == {"passed": True, "filesystem_count": 2}
    assert out["tests"]["within_total_capacity"] == {
        "passed": True,
        "total_quota_gib": 93132,
        "allocated_gib": 16384 + 2,
    }
    assert out["tests"]["min_fs_size"] == {"passed": True, "min_size_tib": 0.001}
    assert [body["name"] for _, body in storage.bodies] == [f"isv-fs-{RUN_ID}-hss09-1", f"isv-fs-{RUN_ID}-hss09-2"]
    assert storage.deleted == ["filesystem.1", "filesystem.2"]
    _assert_dev_untouched(storage, api)
    _assert_secret_never_leaks(out, api)


def test_multiple_filesystems_deletes_the_first_when_the_second_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A quota refusal of the second create fails every subtest and still deletes the first."""
    storage = FakeStorage(fail_create_at=2)
    code, out, _ = _run(monkeypatch, capsys, "multiple_filesystems_test.py", storage, ["--run-id", RUN_ID])

    assert code == 1
    assert {t["passed"] for t in out["tests"].values()} == {False}
    assert "HTTP 429" in out["error"]
    assert storage.deleted == ["filesystem.1"]
    assert not validate("storage", HssMultipleFilesystemsCheck, out)._passed


@pytest.mark.parametrize("status", [{"totalQuotaGb": 16000, "allocatedGb": 16386}, {"allocatedGb": 16386}])
def test_multiple_filesystems_fails_an_allocation_outside_the_quota(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: dict[str, int]
) -> None:
    """An allocation above the quota, or no quota at all, fails within_total_capacity."""
    storage = FakeStorage()
    routes = {**storage.routes(), f"GET {FS}/status": status}
    code, out, _ = run(monkeypatch, capsys, "storage/multiple_filesystems_test.py", routes, ["--run-id", RUN_ID])

    assert code == 1
    assert out["tests"]["within_total_capacity"]["passed"] is False
    assert out["tests"]["multiple_filesystems"]["passed"] is True
    assert storage.deleted == ["filesystem.1", "filesystem.2"]


# ── live_expansion (HSS10-01) ─────────────────────────────────────────


def test_live_expansion_passes_the_api_subtests_and_fails_the_mount_only_ones(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resize the backend applies passes capacity and metadata; inodes and I/O fail as not supported."""
    storage = FakeStorage()
    code, out, api = _run(monkeypatch, capsys, "live_expansion_test.py", storage, ["--run-id", RUN_ID])

    assert code == 0 and out["success"], out
    tests = out["tests"]
    assert tests["capacity_expanded"] == {
        "passed": True,
        "from_gib": 1,
        "to_gib": 2,
        "backend_allocation_delta_gib": 2,
        "baseline_allocated_gib": 16384,
    }
    assert tests["metadata_consistent"] == {"passed": True, "evidence": "control_plane"}
    for key in ("inodes_expanded", "io_uninterrupted"):
        assert tests[key]["passed"] is False and tests[key]["supported"] is False
        assert NO_BM_REASON in tests[key]["error"]
    check = validate("storage", HssLiveExpansionCheck, out)
    assert not check._passed
    assert "capacity_expanded" not in check._error and "inodes_expanded" in check._error
    assert ("PUT", {"name": f"isv-fs-{RUN_ID}-hss10", "capacity": {"size": 2, "sizeUnit": "GiB"}}) in storage.bodies
    assert storage.deleted == ["filesystem.1"]
    _assert_dev_untouched(storage, api)
    _assert_secret_never_leaks(out, api)


def test_live_expansion_fails_a_resize_the_backend_never_applied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The API readback shows the new size but the backend allocation stays put: capacity_expanded fails."""
    storage = FakeStorage(backend_resizes=False)
    code, out, _ = _run(monkeypatch, capsys, "live_expansion_test.py", storage, ["--run-id", RUN_ID], clock=FakeClock())

    assert code == 1
    expanded = out["tests"]["capacity_expanded"]
    assert expanded["passed"] is False
    assert expanded["to_gib"] == 2 and expanded["backend_allocation_delta_gib"] == 1
    assert "is not reflected in /storage/fs/status" in expanded["error"]
    assert storage.deleted == ["filesystem.1"]


def test_live_expansion_is_not_fooled_by_a_create_allocation_that_lands_late(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The create's 1 GiB reaches the backend only after the resize, which never does: capacity_expanded still fails.

    Measured from after the create, the late 1 GiB would equal the resize's growth
    and pass; the baseline is read before the create, so it needs the full 2 GiB.
    """
    storage = FakeStorage(backend_resizes=False, create_lands_late=True)
    code, out, _ = _run(monkeypatch, capsys, "live_expansion_test.py", storage, ["--run-id", RUN_ID], clock=FakeClock())

    assert code == 1
    expanded = out["tests"]["capacity_expanded"]
    assert expanded["passed"] is False and expanded["backend_allocation_delta_gib"] == 1
    assert "is not reflected in /storage/fs/status" in expanded["error"]
    assert storage.deleted == ["filesystem.1"]


def test_live_expansion_waits_out_a_pending_release_before_its_baseline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An earlier step's 2 GiB still counted at the first read is not taken as the baseline."""
    storage = FakeStorage(stale_allocations=[16386])
    code, out, _ = _run(monkeypatch, capsys, "live_expansion_test.py", storage, ["--run-id", RUN_ID])

    assert code == 0, out
    expanded = out["tests"]["capacity_expanded"]
    assert expanded["baseline_allocated_gib"] == 16384 and expanded["backend_allocation_delta_gib"] == 2


def test_live_expansion_passes_when_both_allocations_land_late(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A create allocation that lands late does not fail a resize that did reach the backend."""
    storage = FakeStorage(create_lands_late=True)
    code, out, _ = _run(monkeypatch, capsys, "live_expansion_test.py", storage, ["--run-id", RUN_ID])

    assert code == 0, out
    assert out["tests"]["capacity_expanded"]["backend_allocation_delta_gib"] == 2


def test_live_expansion_fails_metadata_when_the_readback_never_settles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filesystem stuck UPDATING after the resize fails metadata_consistent, bounded by --settle-timeout."""
    storage = FakeStorage(resize_state="UPDATING")
    clock = FakeClock()
    code, out, _ = _run(
        monkeypatch,
        capsys,
        "live_expansion_test.py",
        storage,
        ["--run-id", RUN_ID, "--settle-timeout", "60"],
        clock=clock,
    )

    assert code == 1
    assert out["tests"]["metadata_consistent"]["passed"] is False
    assert "UPDATING" in out["tests"]["metadata_consistent"]["error"]
    assert clock.now < 1000 + 300
    assert storage.deleted == ["filesystem.1"]


# ── Leftover sweep, teardown, and the delete guard ────────────────────


def test_sweep_deletes_only_old_run_filesystems(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Old isv-fs-<hex>-* filesystems go; young ones, other names, and the dev filesystems stay."""
    storage = FakeStorage()
    storage.add("filesystem.OLD", "isv-fs-0a0b0c-hss01", 1, _ts(30))
    storage.add("filesystem.SHIM", "isv-fs-0123456789ab-firebird-fs", 1, _ts(30))
    storage.add("filesystem.YOUNG", "isv-fs-d1e2f3-hss09-1", 1, _ts(1))
    storage.add("filesystem.MANUAL", "isv-fs-manual", 1, _ts(30))
    storage.add("filesystem.OTHER", "isv-data", 1, _ts(30))
    storage.add("filesystem.NOTIME", "isv-fs-a1a1a1-hss10", 1, "")
    code, out, api = _run(monkeypatch, capsys, "sweep_leftovers.py", storage)

    assert code == 0 and out["success"]
    assert sorted(storage.deleted) == ["filesystem.OLD", "filesystem.SHIM"]
    assert out["resources_deleted"] == [
        "filesystem:filesystem.OLD (isv-fs-0a0b0c-hss01)",
        "filesystem:filesystem.SHIM (isv-fs-0123456789ab-firebird-fs)",
    ]
    assert schema_errors("teardown", out) == []
    _assert_dev_untouched(storage, api)


def test_sweep_skips_when_filesystems_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused list is a structured skip that never fails setup."""
    code, out, _ = run(monkeypatch, capsys, "storage/sweep_leftovers.py", {f"GET {FS}": 403})

    assert code == 0 and out["success"] and out["skipped"]
    assert "not listed" in out["skip_reason"]


def test_sweep_reports_a_failed_delete_without_failing_setup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A delete the API refuses is listed under resources_failed; the step still succeeds."""
    storage = FakeStorage(delete_errors=[500])
    storage.add("filesystem.OLD", "isv-fs-0a0b0c-hss01", 1, _ts(30))
    code, out, _ = _run(monkeypatch, capsys, "sweep_leftovers.py", storage)

    assert code == 0 and out["success"]
    assert out["resources_failed"] and "filesystem.OLD" in out["resources_failed"][0]


def test_teardown_deletes_exactly_the_runs_filesystems(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Teardown deletes the run's isv-fs-<run_id>-* filesystems (the shim's too), nothing else."""
    storage = FakeStorage()
    storage.add("filesystem.MINE", f"isv-fs-{RUN_ID}-hss10", 1, _ts(0.1))
    storage.add("filesystem.SHIM", f"isv-fs-{RUN_ID}-firebird-fs", 1, _ts(0.1))
    storage.add("filesystem.OTHERRUN", "isv-fs-d1e2f3-hss01", 1, _ts(0.1))
    code, out, api = _run(monkeypatch, capsys, "teardown.py", storage, ["--run-id", RUN_ID])

    assert code == 0 and out["success"], out
    assert sorted(storage.deleted) == ["filesystem.MINE", "filesystem.SHIM"]
    assert "filesystem.OTHERRUN" in storage.fs
    assert schema_errors("teardown", out) == []
    _assert_dev_untouched(storage, api)


def test_teardown_without_a_run_id_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the setup step's run ID teardown makes no API call."""
    code, out, api = run(monkeypatch, capsys, "storage/teardown.py", {}, ["--run-id="])

    assert code == 0 and out["success"]
    assert api.calls == []


def test_teardown_fails_when_a_delete_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filesystem teardown could not delete fails the step and is named."""
    storage = FakeStorage(delete_errors=[500])
    storage.add("filesystem.MINE", f"isv-fs-{RUN_ID}-hss10", 1, _ts(0.1))
    code, out, _ = _run(monkeypatch, capsys, "teardown.py", storage, ["--run-id", RUN_ID])

    assert code == 1 and out["success"] is False
    assert "filesystem.MINE" in out["resources_failed"][0]


@pytest.mark.parametrize(("script", "names"), [("teardown.py", RUN_ID), ("sweep_leftovers.py", "0a0b0c")])
def test_teardown_and_sweep_share_one_deadline_across_their_deletes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], script: str, names: str
) -> None:
    """Three filesystems that never go away cost one --timeout in total, not one each."""
    storage = FakeStorage(delete_sticks=True)
    for n in (1, 2, 3):
        storage.add(f"filesystem.{n}", f"isv-fs-{names}-hss09-{n}", 1, _ts(30))
    clock = FakeClock()
    argv = ["--run-id", RUN_ID, "--timeout", "600"] if script == "teardown.py" else ["--timeout", "600"]
    _, out, _ = _run(monkeypatch, capsys, script, storage, argv, clock=clock)

    assert len(out["resources_failed"]) == 3, out
    assert storage.delete_attempts == ["filesystem.1", "filesystem.2", "filesystem.3"]
    assert clock.now - 1000 < 600 + 60


def _client(monkeypatch: pytest.MonkeyPatch, storage: FakeStorage) -> tuple[Any, Any, FakeApi]:
    """Return (filesystems module, client, api) for direct helper calls against ``storage``."""
    module = load("storage/teardown.py")
    api = FakeApi(module, storage.routes())
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FIREBIRD_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(module._fb.FirebirdClient, "_send", api.send)
    monkeypatch.setattr(socket.socket, "connect", lambda *_a: pytest.fail("real socket"))
    FakeClock().install(monkeypatch, module)
    return module._common["filesystems"], module._fb.FirebirdClient(), api


@pytest.mark.parametrize("fs_id", list(DEV))
def test_delete_refuses_a_filesystem_not_named_isv_fs(monkeypatch: pytest.MonkeyPatch, fs_id: str) -> None:
    """Even by ID, the tenant's own filesystems are never deleted: the guard reads the name first."""
    storage = FakeStorage()
    filesystems, client, api = _client(monkeypatch, storage)

    with pytest.raises(filesystems.RefusedDeleteError, match="not named isv-fs-"):
        filesystems.delete_filesystem(client, fs_id, 60)
    assert api.paths("DELETE") == []
    assert fs_id in storage.fs


def test_delete_retries_while_another_operation_holds_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 409 (an unfinished create) is retried until the delete is accepted."""
    storage = FakeStorage(delete_errors=[409, 409])
    storage.add("filesystem.MINE", f"isv-fs-{RUN_ID}-hss01", 1, _ts(0.1), state="PROVISIONING")
    filesystems, client, api = _client(monkeypatch, storage)

    assert filesystems.delete_filesystem(client, "filesystem.MINE", 600) is True
    assert api.paths("DELETE").count(f"DELETE {FS}/filesystem.MINE") == 3
    assert storage.deleted == ["filesystem.MINE"]


def test_delete_all_shares_one_deadline_across_every_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three deletes that never finish together stay within the one timeout, and each is still attempted."""
    storage = FakeStorage(delete_sticks=True)
    for n in (1, 2, 3):
        storage.add(f"filesystem.{n}", f"isv-fs-{RUN_ID}-hss09-{n}", 1, _ts(0.1))
    filesystems, client, _ = _client(monkeypatch, storage)
    clock = filesystems.time
    start = clock.now

    errors = filesystems.delete_all(client, ["filesystem.1", "filesystem.2", "filesystem.3"], 600)

    assert len(errors) == 3 and all("still readable" in e or "after" in e for e in errors), errors
    assert storage.delete_attempts == ["filesystem.1", "filesystem.2", "filesystem.3"]
    # One shared 600 s deadline plus a few poll intervals, not 3 x 600 s.
    assert clock.now - start < 600 + 3 * 2 * filesystems.POLL_SECONDS + 30


@pytest.mark.parametrize("fs_id", list(DEV))
def test_resize_refuses_a_filesystem_not_named_isv_fs(monkeypatch: pytest.MonkeyPatch, fs_id: str) -> None:
    """The tenant's own filesystems are never resized: the guard checks the name before the PUT."""
    storage = FakeStorage()
    filesystems, client, api = _client(monkeypatch, storage)

    with pytest.raises(filesystems.RefusedResizeError, match="not named isv-fs-"):
        filesystems.submit_resize(client, filesystems.get_filesystem(client, fs_id), 99999)
    assert api.paths("PUT") == []
    assert storage.fs[fs_id]["capacity"]["size"] == {"filesystem.TEAMHOME": 10240, "filesystem.SHARED": 6144}[fs_id]


def test_get_filesystem_drops_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-filesystem response's mount token never leaves the helper."""
    filesystems, client, _ = _client(monkeypatch, FakeStorage())

    fs = filesystems.get_filesystem(client, "filesystem.TEAMHOME")
    assert fs["name"] == "team-home" and SECRET not in repr(fs)
    assert filesystems.get_filesystem(client, "filesystem.7") is None


@pytest.mark.parametrize(
    ("fs", "gib"),
    [
        ({"capacity": {"size": 3, "sizeUnit": "GiB"}}, 3),
        ({"capacity": {"size": 2, "sizeUnit": "TiB"}}, 2048),
        ({"capacity": {"size": 5, "sizeUnit": 1}}, 5),
        ({"totalCapacityGb": "7"}, 7),
        ({}, None),
    ],
)
def test_capacity_reads_every_unit_and_the_legacy_field(fs: dict[str, Any], gib: int | None) -> None:
    """Capacity is normalized to GiB from the structured field or the deprecated one."""
    assert load("storage/teardown.py")._common["filesystems"].capacity_gib(fs) == gib


# ── StorageProvider shim ──────────────────────────────────────────────

SHIM = SCRIPTS / "storage" / "firebird" / "api.py"
MANIFEST = FIREBIRD / "config" / "storage-provider-manifest.yaml"


@pytest.fixture
def shim_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a function that fakes the shim's HTTP with a ``FakeStorage``; no real socket is allowed."""
    monkeypatch.setenv("FIREBIRD_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FIREBIRD_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(socket.socket, "connect", lambda *_a: pytest.fail("real socket"))
    for name in ("_firebird_storage_shim_client", "_firebird_storage_shim_filesystems"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    def install(storage: FakeStorage, overrides: dict[str, Route] | None = None) -> FakeApi:
        registry = load_provider_registry({"manifest_path": str(MANIFEST)})  # loads the shim's helpers
        assert [p.name for p in registry] == ["firebird-fs"]
        client_module = sys.modules["_firebird_storage_shim_client"]
        api = FakeApi(SimpleNamespace(_fb=client_module), {**storage.routes(), **(overrides or {})})
        monkeypatch.setattr(client_module.FirebirdClient, "_send", api.send)
        clock = FakeClock()
        for module in (client_module, sys.modules["_firebird_storage_shim_filesystems"]):
            monkeypatch.setattr(module, "time", clock)
        return api

    return install


def _shim_api() -> Any:
    """Return the served shim from the manifest (after ``shim_env`` installed the fake)."""
    return load_provider_registry({"manifest_path": str(MANIFEST)})[0].api


def test_storage_provider_api_check_passes_against_the_shim(shim_env: Any) -> None:
    """The real check passes every subtest; its filesystem carries the run ID and is deleted."""
    storage = FakeStorage()
    api = shim_env(storage)
    check = StorageProviderApiCheck(
        config={"manifest_path": str(MANIFEST), "run_id": RUN_ID, "volume_size_bytes": 1 << 30}
    )
    check.run()

    assert check._passed, check._error
    subtests = {s["name"]: s for s in check._subtest_results}
    assert {s["passed"] for s in subtests.values()} == {True}
    assert not any(s.get("skipped") for s in subtests.values())
    assert "hard_limit_bytes=" + str(93132 << 30) in subtests["tenant-quota[firebird-fs]"]["message"]
    assert storage.bodies == [
        ("POST", {"name": f"isv-fs-{RUN_ID}-firebird-fs", "capacity": {"size": 1, "sizeUnit": "GiB"}})
    ]
    assert storage.deleted == ["filesystem.1"]
    _assert_dev_untouched(storage, api)


def test_shim_maps_a_refused_token_to_authentication_error(shim_env: Any) -> None:
    """A 401 from the filesystem list is an AuthenticationError, so the check skips the volume subtests."""
    from isvtest.core.storage_provider import AuthenticationError

    shim_env(FakeStorage(), {f"GET {FS}": 401})
    with pytest.raises(AuthenticationError):
        _shim_api().health_check()


def test_shim_refuses_to_delete_the_tenants_filesystems(shim_env: Any) -> None:
    """delete_volume on team-home raises ValidationError and issues no DELETE."""
    from isvtest.core.storage_provider import DeleteVolumeRequest, ValidationError

    storage = FakeStorage()
    fake = shim_env(storage)
    with pytest.raises(ValidationError, match="not named isv-fs-"):
        _shim_api().delete_volume(DeleteVolumeRequest(volume_id="filesystem.TEAMHOME"))
    assert fake.paths("DELETE") == []
    assert "filesystem.TEAMHOME" in storage.fs


def test_shim_deletes_a_filesystem_whose_create_failed(shim_env: Any) -> None:
    """A create whose Operation failed is deleted before the error is raised."""
    from isvtest.core.storage_provider import CreateVolumeRequest, StorageApiError

    storage = FakeStorage(create_status="FAILED")
    shim_env(storage)
    with pytest.raises(StorageApiError):
        _shim_api().create_volume(CreateVolumeRequest(size_bytes=1, volume_type="file", name=f"isvtest-{RUN_ID}-x"))
    assert storage.deleted == ["filesystem.1"]


def test_shim_lists_filesystems_as_volumes_without_reading_credentials(shim_env: Any) -> None:
    """list/get read only the collection, never the per-filesystem response that carries the token."""
    from isvtest.core.storage_provider import GetVolumeRequest

    storage = FakeStorage()
    fake = shim_env(storage)
    volume = _shim_api().get_volume(GetVolumeRequest(volume_id="filesystem.TEAMHOME"))

    assert (volume.name, volume.size_bytes, volume.state, volume.tenant_id) == (
        "team-home",
        10240 << 30,
        "available",
        TENANT,
    )
    assert not [p for p in fake.paths("GET") if p.startswith(f"GET {FS}/filesystem.")]


@pytest.mark.parametrize(
    ("requested", "name"),
    [
        (f"isvtest-{RUN_ID}-firebird-fs", f"isv-fs-{RUN_ID}-firebird-fs"),
        ("isvtest-0123456789ab-firebird-fs", "isv-fs-0123456789ab-firebird-fs"),
        ("my volume!", "isv-fs-my-volume"),
    ],
)
def test_shim_volume_names_keep_the_provider_prefix(shim_env: Any, requested: str, name: str) -> None:
    """Every shim filesystem is named isv-fs-*, within the API's 32-character cap."""
    shim_env(FakeStorage())
    module = sys.modules["_firebird_storage_shim_filesystems"]
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("_shim_under_test", SHIM)
    assert spec and spec.loader
    shim = module_from_spec(spec)
    spec.loader.exec_module(shim)

    assert shim.volume_name(requested) == name
    assert shim.volume_name(None).startswith(module.FS_PREFIX) and len(shim.volume_name(None)) <= 32
    assert len(shim.volume_name("x" * 80)) == 32
