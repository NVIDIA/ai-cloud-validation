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

"""Tests for AWS bare-metal reinstall identity output."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError, WaiterError

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_BM_REINSTALL_SCRIPT = (
    ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "bare_metal" / "reinstall_instance.py"
)


def _client_error(operation_name: str, code: str, message: str) -> ClientError:
    """Create a botocore ClientError for fake AWS client failures."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation_name)


def _load_reinstall_script() -> ModuleType:
    """Load the AWS bare-metal reinstall script as a module."""
    spec = importlib.util.spec_from_file_location("test_aws_bm_reinstall_instance", AWS_BM_REINSTALL_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NoopWaiter:
    """Fake waiter that accepts wait calls."""

    def wait(self, **_kwargs: Any) -> None:
        """Accept a waiter invocation."""


class _FakeReinstallEc2:
    """Small fake EC2 client for the successful reinstall path."""

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """Return a running instance with one root volume."""
        assert InstanceIds == ["i-original"]
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-original",
                            "ImageId": "ami-source",
                            "State": {"Name": "running"},
                            "Placement": {"AvailabilityZone": "us-west-2a"},
                            "SubnetId": "subnet-source",
                            "SecurityGroups": [{"GroupId": "sg-source"}],
                            "KeyName": "key-source",
                            "RootDeviceName": "/dev/sda1",
                            "PublicIpAddress": "203.0.113.30",
                            "PrivateIpAddress": "10.0.0.30",
                            "BlockDeviceMappings": [
                                {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-old"}},
                            ],
                        }
                    ]
                }
            ]
        }

    def describe_images(self, ImageIds: list[str]) -> dict[str, Any]:
        """Return an AMI root snapshot."""
        assert ImageIds == ["ami-source"]
        return {
            "Images": [
                {
                    "RootDeviceName": "/dev/sda1",
                    "BlockDeviceMappings": [
                        {"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": "snap-source"}},
                    ],
                    "Architecture": "x86_64",
                }
            ]
        }

    def get_waiter(self, _name: str) -> _NoopWaiter:
        """Return a no-op waiter."""
        return _NoopWaiter()

    def stop_instances(self, InstanceIds: list[str]) -> None:
        """Accept stop requests."""
        assert InstanceIds == ["i-original"]

    def detach_volume(self, **kwargs: Any) -> None:
        """Accept detach requests."""
        assert kwargs == {"VolumeId": "vol-old", "InstanceId": "i-original", "Force": True}

    def create_volume(self, **kwargs: Any) -> dict[str, str]:
        """Return a new root volume."""
        assert kwargs["SnapshotId"] == "snap-source"
        return {"VolumeId": "vol-new"}

    def attach_volume(self, **kwargs: Any) -> None:
        """Accept attach requests."""
        assert kwargs == {"VolumeId": "vol-new", "InstanceId": "i-original", "Device": "/dev/sda1"}

    def start_instances(self, InstanceIds: list[str]) -> None:
        """Accept start requests."""
        assert InstanceIds == ["i-original"]

    def delete_volume(self, VolumeId: str) -> None:
        """Accept old root cleanup."""
        assert VolumeId == "vol-old"


class _FakeInaccessibleSnapshotEc2(_FakeReinstallEc2):
    """Fake EC2 client where the AMI snapshot cannot be used with CreateVolume."""

    def __init__(self) -> None:
        """Initialize call event tracking."""
        self.events: list[str] = []

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """Return target or temporary donor instance details."""
        if InstanceIds == ["i-donor"]:
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-donor",
                                "State": {"Name": "stopped"},
                                "RootDeviceName": "/dev/sda1",
                                "BlockDeviceMappings": [
                                    {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-new"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        return super().describe_instances(InstanceIds)

    def create_volume(self, **kwargs: Any) -> dict[str, str]:
        """Fail when creating from the AMI snapshot directly."""
        assert kwargs["SnapshotId"] == "snap-source"
        self.events.append("create_volume")
        raise _client_error("CreateVolume", "InvalidSnapshot.NotFound", "Snapshot does not exist")

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        """Materialize a replacement root volume through a donor instance."""
        self.events.append("run_donor")
        assert kwargs["ImageId"] == "ami-source"
        assert kwargs["InstanceType"] == "t3.micro"
        assert kwargs["SubnetId"] == "subnet-source"
        assert kwargs["SecurityGroupIds"] == ["sg-source"]
        return {"Instances": [{"InstanceId": "i-donor"}]}

    def stop_instances(self, InstanceIds: list[str]) -> None:
        """Track target and donor stop requests."""
        if InstanceIds == ["i-donor"]:
            self.events.append("stop_donor")
            return
        self.events.append("stop_target")
        super().stop_instances(InstanceIds)

    def detach_volume(self, **kwargs: Any) -> None:
        """Track donor and target root detach requests."""
        if kwargs["VolumeId"] == "vol-new":
            self.events.append("detach_donor")
            assert kwargs == {"VolumeId": "vol-new", "InstanceId": "i-donor", "Force": True}
            return
        self.events.append("detach_target")
        super().detach_volume(**kwargs)

    def terminate_instances(self, InstanceIds: list[str]) -> None:
        """Track donor termination."""
        assert InstanceIds == ["i-donor"]
        self.events.append("terminate_donor")


class _FailingDonorVolumeWaiterEc2:
    """Fake EC2 client where donor detach is requested but never becomes available."""

    def __init__(self) -> None:
        """Initialize call event tracking."""
        self.events: list[str] = []
        self.donor_delete_on_termination: bool | None = None

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        """Record donor launch settings."""
        self.events.append("run_donor")
        self.donor_delete_on_termination = kwargs["BlockDeviceMappings"][0]["Ebs"]["DeleteOnTermination"]
        return {"Instances": [{"InstanceId": "i-donor"}]}

    def get_waiter(self, name: str) -> _NoopWaiter:
        """Fail only after the donor root volume detach is requested."""
        if name == "volume_available":
            return _FailingWaiter()
        return _NoopWaiter()

    def stop_instances(self, InstanceIds: list[str]) -> None:
        """Track donor stop requests."""
        assert InstanceIds == ["i-donor"]
        self.events.append("stop_donor")

    def describe_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """Return the temporary donor instance root volume."""
        assert InstanceIds == ["i-donor"]
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-donor",
                            "State": {"Name": "stopped"},
                            "RootDeviceName": "/dev/sda1",
                            "BlockDeviceMappings": [
                                {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-new"}},
                            ],
                        }
                    ]
                }
            ]
        }

    def detach_volume(self, **kwargs: Any) -> None:
        """Track donor root detach requests."""
        assert kwargs == {"VolumeId": "vol-new", "InstanceId": "i-donor", "Force": True}
        self.events.append("detach_donor")

    def terminate_instances(self, InstanceIds: list[str]) -> None:
        """Track donor termination."""
        assert InstanceIds == ["i-donor"]
        self.events.append("terminate_donor")

    def delete_volume(self, VolumeId: str) -> None:
        """Track orphan donor volume cleanup."""
        assert VolumeId == "vol-new"
        self.events.append("delete_donor_volume")


class _FailingWaiter:
    """Fake waiter that raises a timeout-like WaiterError."""

    def wait(self, **_kwargs: Any) -> None:
        """Raise a waiter failure."""
        raise WaiterError(name="VolumeAvailable", reason="timed out", last_response={})


def test_reinstall_outputs_observed_node_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """StableIdentifierCheck must compare launch ID with post-reinstall node identity."""
    module = _load_reinstall_script()
    ec2 = _FakeReinstallEc2()
    ssh_calls: list[tuple[str, str, str, str]] = []

    def fake_ssh_run(host: str, user: str, key_file: str, command: str) -> tuple[int, str, str]:
        ssh_calls.append((host, user, key_file, command))
        return 0, "i-observed\n", ""

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: ec2)
    monkeypatch.setattr(module, "wait_for_ssh", lambda host, user, key_file: True)
    monkeypatch.setattr(module, "ssh_run", fake_ssh_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reinstall_instance.py",
            "--instance-id",
            "i-original",
            "--region",
            "us-west-2",
            "--key-file",
            "/tmp/key.pem",
        ],
    )

    exit_code = module.main()
    result: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["success"] is True
    assert result["instance_id"] == "i-observed"
    assert result["state"] == "running"
    assert result["public_ip"] == "203.0.113.30"
    assert result["private_ip"] == "10.0.0.30"
    assert result["key_file"] == "/tmp/key.pem"
    assert result["ssh_user"] == "ubuntu"
    assert result["ssh_ready"] is True
    assert ssh_calls == [("203.0.113.30", "ubuntu", "/tmp/key.pem", ssh_calls[0][3])]
    assert "latest/api/token" in ssh_calls[0][3]
    assert "latest/meta-data/instance-id" in ssh_calls[0][3]


def test_reinstall_succeeds_when_identity_read_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A metadata-read hiccup must not abort a successful reinstall.

    The reinstall has finished by the time the node identity is queried, so a
    failed read reports an empty instance_id and lets StableIdentifierCheck flag
    it - it must not return non-zero, which would skip downstream checks and leak
    the detached old root volume.
    """
    module = _load_reinstall_script()
    ec2 = _FakeReinstallEc2()

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: ec2)
    monkeypatch.setattr(module, "wait_for_ssh", lambda host, user, key_file: True)
    monkeypatch.setattr(module, "ssh_run", lambda host, user, key_file, command: (1, "", "boom"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reinstall_instance.py",
            "--instance-id",
            "i-original",
            "--region",
            "us-west-2",
            "--key-file",
            "/tmp/key.pem",
        ],
    )

    exit_code = module.main()
    result: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["success"] is True
    assert result["instance_id"] == ""


def test_reinstall_uses_donor_volume_when_ami_snapshot_is_not_restorable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Public AMI launch can work even when its backing snapshot is not directly restorable."""
    module = _load_reinstall_script()
    ec2 = _FakeInaccessibleSnapshotEc2()

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: ec2)
    monkeypatch.setattr(module, "wait_for_ssh", lambda host, user, key_file: True)
    monkeypatch.setattr(module, "ssh_run", lambda host, user, key_file, command: (0, "i-original\n", ""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reinstall_instance.py",
            "--instance-id",
            "i-original",
            "--region",
            "us-west-2",
            "--key-file",
            "/tmp/key.pem",
        ],
    )

    exit_code = module.main()
    result: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["success"] is True
    assert result["new_volume_id"] == "vol-new"
    assert ec2.events[:4] == ["create_volume", "run_donor", "stop_donor", "detach_donor"]
    assert ec2.events.index("stop_target") > ec2.events.index("detach_donor")


def test_donor_volume_is_cleaned_up_when_detach_waiter_fails() -> None:
    """A donor root volume must not be orphaned when detach never completes."""
    module = _load_reinstall_script()
    ec2 = _FailingDonorVolumeWaiterEc2()

    with pytest.raises(WaiterError):
        module.create_root_volume_from_donor_instance(
            ec2,
            ami_id="ami-source",
            image_root_device="/dev/sda1",
            architecture="x86_64",
            volume_size=200,
            subnet_id="subnet-source",
            security_group_ids=["sg-source"],
            key_name="key-source",
            instance_id="i-original",
        )

    assert ec2.donor_delete_on_termination is True
    assert ec2.events == [
        "run_donor",
        "stop_donor",
        "detach_donor",
        "terminate_donor",
        "delete_donor_volume",
    ]


class _FakeFailingReinstallEc2(_FakeReinstallEc2):
    """Fake EC2 client that records deleted volumes for failure-cleanup tests."""

    def __init__(self) -> None:
        """Initialize deleted-volume tracking."""
        self.deleted_volumes: list[str] = []

    def delete_volume(self, VolumeId: str) -> None:
        """Record any volume deletion (replacement cleanup or old-volume cleanup)."""
        self.deleted_volumes.append(VolumeId)


def test_reinstall_deletes_replacement_volume_when_swap_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure after the replacement volume is created must not orphan it.

    The volume is created before the target is mutated, so a later failure
    (here SSH never comes up) must delete it - teardown only terminates the
    instance and never reclaims reinstall-* volumes.
    """
    module = _load_reinstall_script()
    ec2 = _FakeFailingReinstallEc2()

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: ec2)
    monkeypatch.setattr(module, "wait_for_ssh", lambda host, user, key_file: False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reinstall_instance.py",
            "--instance-id",
            "i-original",
            "--region",
            "us-west-2",
            "--key-file",
            "/tmp/key.pem",
        ],
    )

    exit_code = module.main()
    result: dict[str, Any] = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["success"] is False
    assert result["error"] == "SSH not ready after reinstall"
    # The newly created replacement volume is reclaimed; the old volume is not
    # deleted because post-success cleanup never runs.
    assert ec2.deleted_volumes == ["vol-new"]
