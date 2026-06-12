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

"""Tests for AWS storage reference scripts (DATASVC-XX-02/03/04)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_STORAGE_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "storage"

EXPECTED_CONTENT = "isv-ncp-validate-storage-deadbeef"


def _load_script(script_name: str) -> ModuleType:
    """Load an AWS storage script as a module for direct testing."""
    script_path = AWS_STORAGE_SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(f"test_storage_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_error(code: str) -> ClientError:
    """Build a ClientError with the given AWS error code."""
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class FakeWaiter:
    """No-op boto3 waiter."""

    def wait(self, **kwargs: Any) -> None:
        """Return immediately - nothing to wait for in tests."""


class FakeEc2:
    """Minimal fake EC2 client covering the raw calls the scripts make."""

    def __init__(
        self,
        *,
        availability_zone: str = "us-west-2a",
        public_ip: str | None = "203.0.113.10",
        volume_size: int = 10,
        attached_instance: str = "i-fixture",
    ) -> None:
        """Seed instance/volume describe responses used by the scripts."""
        self.availability_zone = availability_zone
        self.public_ip = public_ip
        self.size = volume_size
        self.attached_instance = attached_instance
        self.stopped: list[str] = []
        self.started: list[str] = []

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """Return a single instance with AZ + public IP."""
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "Placement": {"AvailabilityZone": self.availability_zone},
                            "PublicIpAddress": self.public_ip,
                        }
                    ]
                }
            ]
        }

    def describe_volumes(self, VolumeIds: list[str]) -> dict[str, Any]:
        """Return the (mutable) size and an in-use attachment."""
        return {
            "Volumes": [
                {
                    "Size": self.size,
                    "State": "in-use",
                    "Attachments": [{"InstanceId": self.attached_instance}],
                }
            ]
        }

    def modify_volume(self, VolumeId: str, Size: int) -> dict[str, Any]:
        """Apply the new size so a later describe reflects the grow."""
        self.size = Size
        return {"VolumeModification": {"ModificationState": "modifying"}}

    def stop_instances(self, InstanceIds: list[str]) -> None:
        """Record the stop call."""
        self.stopped.extend(InstanceIds)

    def start_instances(self, InstanceIds: list[str]) -> None:
        """Record the start call."""
        self.started.extend(InstanceIds)

    def get_waiter(self, name: str) -> FakeWaiter:
        """Return a no-op waiter."""
        return FakeWaiter()


def _patch_ebs_volume_ops(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> None:
    """Patch the ebs boto3 wrappers to fast, side-effect-free fakes."""
    monkeypatch.setattr(module.ebs, "create_volume", lambda *a, **k: "vol-fixture")
    monkeypatch.setattr(module.ebs, "create_volume_from_snapshot", lambda *a, **k: "vol-restore")
    monkeypatch.setattr(module.ebs, "attach_volume", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "wait_for_volume_available", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "wait_for_volume_in_use", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "wait_for_attachment_device", lambda *a, **k: True)


# ---------------------------------------------------------------------------
# common/ebs.py pure helpers
# ---------------------------------------------------------------------------


def test_nvme_serial_and_by_id_path_drop_dash() -> None:
    """The Nitro NVMe serial / by-id path embed the volume ID minus the dash."""
    module = _load_script("create_volume.py")
    assert module.ebs.nvme_serial_for_volume("vol-0123456789abcdef0") == "vol0123456789abcdef0"
    assert module.ebs.guest_by_id_path("vol-0123456789abcdef0") == (
        "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0"
    )


def test_detach_and_delete_volume_treats_missing_as_gone() -> None:
    """A volume that is already gone is a successful cleanup (returns None)."""
    module = _load_script("teardown_volume.py")

    class GoneEc2:
        def detach_volume(self, **kwargs: Any) -> None:
            raise _client_error("InvalidVolume.NotFound")

    assert module.ebs.detach_and_delete_volume(GoneEc2(), "vol-x") is None


def test_wait_for_modification_complete_returns_usable_state() -> None:
    """The modification wait returns once the new size is usable (optimizing)."""
    module = _load_script("volume_resize.py")

    class ModEc2:
        def describe_volumes_modifications(self, VolumeIds: list[str]) -> dict[str, Any]:
            return {"VolumesModifications": [{"ModificationState": "optimizing"}]}

    assert module.ebs.wait_for_modification_complete(ModEc2(), "vol-x") == "optimizing"


def test_wait_for_modification_complete_raises_on_failure() -> None:
    """A failed modification raises rather than looping until timeout."""
    module = _load_script("volume_resize.py")

    class FailedEc2:
        def describe_volumes_modifications(self, VolumeIds: list[str]) -> dict[str, Any]:
            return {"VolumesModifications": [{"ModificationState": "failed", "StatusMessage": "nope"}]}

    with pytest.raises(RuntimeError, match="modification failed"):
        module.ebs.wait_for_modification_complete(FailedEc2(), "vol-x")


# ---------------------------------------------------------------------------
# create_volume.py
# ---------------------------------------------------------------------------


def test_create_volume_happy_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A clean create/attach/format/mount/seed flow passes every operation."""
    module = _load_script("create_volume.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2())
    _patch_ebs_volume_ops(monkeypatch, module)
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)
    monkeypatch.setattr(module, "ssh_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(sys, "argv", ["create_volume.py", "--instance-id", "i-fixture", "--key-file", "/tmp/k.pem"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["volume_id"] == "vol-fixture"
    assert all(op["passed"] for op in payload["operations"].values())


def test_create_volume_guest_setup_failure_fails_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero in-guest setup makes the fixture step fail."""
    module = _load_script("create_volume.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2())
    _patch_ebs_volume_ops(monkeypatch, module)
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)
    monkeypatch.setattr(module, "ssh_run", lambda *a, **k: (1, "", "mkfs failed"))
    monkeypatch.setattr(sys, "argv", ["create_volume.py", "--instance-id", "i-fixture", "--key-file", "/tmp/k.pem"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["operations"]["format"]["passed"] is False


# ---------------------------------------------------------------------------
# snapshot_lifecycle.py
# ---------------------------------------------------------------------------


def test_snapshot_lifecycle_happy_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A matching restored sentinel passes the snapshot round-trip."""
    module = _load_script("snapshot_lifecycle.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2())
    _patch_ebs_volume_ops(monkeypatch, module)
    monkeypatch.setattr(module.ebs, "create_snapshot", lambda *a, **k: "snap-1")
    monkeypatch.setattr(module.ebs, "wait_for_snapshot_completed", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "mount_and_read_sentinel", lambda *a, **k: (0, EXPECTED_CONTENT, ""))
    monkeypatch.setattr(module.ebs, "detach_and_delete_volume", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "delete_snapshot_best_effort", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)
    monkeypatch.setattr(module, "ssh_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "snapshot_lifecycle.py",
            "--instance-id",
            "i-fixture",
            "--volume-id",
            "vol-fixture",
            "--key-file",
            "/tmp/k.pem",
            "--expected-content",
            EXPECTED_CONTENT,
        ],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["snapshot_id"] == "snap-1"
    assert payload["operations"]["verify_data"]["content_matches"] is True


def test_snapshot_lifecycle_content_mismatch_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupted restore (sentinel mismatch) fails verify_data."""
    module = _load_script("snapshot_lifecycle.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2())
    _patch_ebs_volume_ops(monkeypatch, module)
    monkeypatch.setattr(module.ebs, "create_snapshot", lambda *a, **k: "snap-1")
    monkeypatch.setattr(module.ebs, "wait_for_snapshot_completed", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "mount_and_read_sentinel", lambda *a, **k: (0, "CORRUPT", ""))
    monkeypatch.setattr(module.ebs, "detach_and_delete_volume", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "delete_snapshot_best_effort", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)
    monkeypatch.setattr(module, "ssh_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "snapshot_lifecycle.py",
            "--instance-id",
            "i-fixture",
            "--volume-id",
            "vol-fixture",
            "--key-file",
            "/tmp/k.pem",
            "--expected-content",
            EXPECTED_CONTENT,
        ],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["operations"]["verify_data"]["content_matches"] is False


def test_snapshot_lifecycle_cleanup_error_fails_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A leaked restore volume / snapshot makes the snapshot step fail."""
    module = _load_script("snapshot_lifecycle.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2())
    _patch_ebs_volume_ops(monkeypatch, module)
    monkeypatch.setattr(module.ebs, "create_snapshot", lambda *a, **k: "snap-1")
    monkeypatch.setattr(module.ebs, "wait_for_snapshot_completed", lambda *a, **k: None)
    monkeypatch.setattr(module.ebs, "mount_and_read_sentinel", lambda *a, **k: (0, EXPECTED_CONTENT, ""))
    monkeypatch.setattr(module.ebs, "detach_and_delete_volume", lambda *a, **k: "DeleteVolume failed")
    monkeypatch.setattr(module.ebs, "delete_snapshot_best_effort", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)
    monkeypatch.setattr(module, "ssh_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "snapshot_lifecycle.py",
            "--instance-id",
            "i-fixture",
            "--volume-id",
            "vol-fixture",
            "--key-file",
            "/tmp/k.pem",
            "--expected-content",
            EXPECTED_CONTENT,
        ],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["cleanup_errors"] == ["DeleteVolume failed"]


# ---------------------------------------------------------------------------
# volume_resize.py
# ---------------------------------------------------------------------------


def test_volume_resize_happy_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """ModifyVolume + growpart + resize2fs + a larger filesystem all pass."""
    module = _load_script("volume_resize.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2(volume_size=10))
    monkeypatch.setattr(module.ebs, "wait_for_modification_complete", lambda *a, **k: "completed")
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)

    sizes = iter(["10000000000", "15000000000"])

    def fake_ssh(host: str, user: str, key: str, script: str, **kwargs: Any) -> tuple[int, str, str]:
        if "df -B1" in script:
            return (0, next(sizes), "")
        return (0, "", "")

    monkeypatch.setattr(module, "ssh_run", fake_ssh)
    monkeypatch.setattr(
        sys,
        "argv",
        ["volume_resize.py", "--instance-id", "i-fixture", "--volume-id", "vol-fixture", "--key-file", "/tmp/k.pem"],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["new_size_gib"] == 15
    assert all(op["passed"] for op in payload["operations"].values())


def test_volume_resize_growpart_failure_fails_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing growpart fails the resize and skips resize2fs."""
    module = _load_script("volume_resize.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2(volume_size=10))
    monkeypatch.setattr(module.ebs, "wait_for_modification_complete", lambda *a, **k: "completed")
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)

    def fake_ssh(host: str, user: str, key: str, script: str, **kwargs: Any) -> tuple[int, str, str]:
        if "df -B1" in script:
            return (0, "10000000000", "")
        if "growpart" in script:
            return (1, "", "growpart boom")
        return (0, "", "")

    monkeypatch.setattr(module, "ssh_run", fake_ssh)
    monkeypatch.setattr(
        sys,
        "argv",
        ["volume_resize.py", "--instance-id", "i-fixture", "--volume-id", "vol-fixture", "--key-file", "/tmp/k.pem"],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["operations"]["grow_partition"]["passed"] is False
    assert payload["operations"]["resize_filesystem"]["passed"] is False


# ---------------------------------------------------------------------------
# volume_persistence.py
# ---------------------------------------------------------------------------


def test_volume_persistence_happy_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Stop/start with the volume still attached and data intact passes."""
    module = _load_script("volume_persistence.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2(attached_instance="i-fixture"))
    monkeypatch.setattr(module.ebs, "is_volume_attached_to", lambda *a, **k: True)
    monkeypatch.setattr(module.ebs, "mount_and_read_sentinel", lambda *a, **k: (0, EXPECTED_CONTENT, ""))
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)
    monkeypatch.setattr(module, "wait_for_public_ip", lambda *a, **k: "203.0.113.10")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "volume_persistence.py",
            "--instance-id",
            "i-fixture",
            "--volume-id",
            "vol-fixture",
            "--key-file",
            "/tmp/k.pem",
            "--expected-content",
            EXPECTED_CONTENT,
        ],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert all(op["passed"] for op in payload["operations"].values())


def test_volume_persistence_detached_after_restart_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A volume that does not reattach after restart fails verify_attached."""
    module = _load_script("volume_persistence.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2())
    monkeypatch.setattr(module.ebs, "is_volume_attached_to", lambda *a, **k: False)
    monkeypatch.setattr(module.ebs, "mount_and_read_sentinel", lambda *a, **k: (0, EXPECTED_CONTENT, ""))
    monkeypatch.setattr(module, "wait_for_ssh", lambda *a, **k: True)
    monkeypatch.setattr(module, "wait_for_public_ip", lambda *a, **k: "203.0.113.10")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "volume_persistence.py",
            "--instance-id",
            "i-fixture",
            "--volume-id",
            "vol-fixture",
            "--key-file",
            "/tmp/k.pem",
            "--expected-content",
            EXPECTED_CONTENT,
        ],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["operations"]["verify_attached"]["passed"] is False


# ---------------------------------------------------------------------------
# teardown_volume.py
# ---------------------------------------------------------------------------


def test_teardown_volume_deletes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A successful detach + delete reports the volume as deleted."""
    module = _load_script("teardown_volume.py")
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: FakeEc2())
    monkeypatch.setattr(module.ebs, "detach_and_delete_volume", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["teardown_volume.py", "--volume-id", "vol-fixture"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["resources_deleted"] == ["vol-fixture"]


def test_teardown_volume_skip_destroy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """--skip-destroy preserves the volume and short-circuits cleanup."""
    module = _load_script("teardown_volume.py")
    monkeypatch.setattr(sys, "argv", ["teardown_volume.py", "--volume-id", "vol-fixture", "--skip-destroy"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["resources_deleted"] == []
