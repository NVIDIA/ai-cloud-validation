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

"""Tests for the AWS FSx for OpenZFS home-directory probe."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any

import pytest

from .conftest import load_aws_script


def _load_script() -> ModuleType:
    """Load the provider script for direct unit testing."""
    return load_aws_script("storage", "home_directory_storage_test.py")


def _volume(quota_gib: int) -> dict[str, Any]:
    """Build an AVAILABLE OpenZFS volume with the expected identity quotas."""
    return {
        "Lifecycle": "AVAILABLE",
        "OpenZFSConfiguration": {
            "VolumePath": "/fsx/isvdir",
            "StorageCapacityQuotaGiB": quota_gib,
            "UserAndGroupQuotas": [
                {"Type": "USER", "Id": 20001, "StorageCapacityQuotaGiB": 1},
                {"Type": "GROUP", "Id": 30001, "StorageCapacityQuotaGiB": 1},
            ],
        },
    }


def _passed(names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Return passing subtest objects for the requested names."""
    return {name: {"passed": True} for name in names}


def _patch_provisioning(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> list[tuple[str | None, str | None]]:
    """Install the fakes shared by the main() tests; return the cleanup recorder."""
    cleanup_calls: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        module,
        "instance_network",
        lambda *args: {
            "public_ip": "203.0.113.10",
            "private_ip": "10.0.0.10",
            "subnet_id": "subnet-1",
            "vpc_id": "vpc-1",
            "security_group_id": "sg-client",
        },
    )
    monkeypatch.setattr(module, "wait_for_ssh", lambda *args: True)
    monkeypatch.setattr(
        module,
        "create_nfs_security_group",
        lambda ec2, vpc_id, client_sg_id, suffix, created: created.update(sg_id="sg-nfs") or "sg-nfs",
    )
    monkeypatch.setattr(module, "_run_remote", lambda *args, **kwargs: (0, "", ""))
    monkeypatch.setattr(
        module,
        "cleanup_resources",
        lambda ec2, fsx, fs_id, sg_id: cleanup_calls.append((fs_id, sg_id)) or [],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["home_directory_storage_test.py", "--instance-id", "i-1", "--key-file", "/tmp/key.pem"],
    )
    return cleanup_calls


def _patch_successful_run(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> list[tuple[str | None, str | None]]:
    """Install a complete successful-run fixture and return cleanup calls."""
    cleanup_calls = _patch_provisioning(monkeypatch, module)
    quota = {"value": 2}
    monkeypatch.setattr(module, "create_filesystem", lambda *args: "fs-1")
    monkeypatch.setattr(
        module,
        "wait_filesystem_available",
        lambda *args: {"DNSName": "fs.example", "OpenZFSConfiguration": {"RootVolumeId": "vol-root"}},
    )
    monkeypatch.setattr(module, "create_test_volume", lambda *args: "vol-child")
    monkeypatch.setattr(module, "wait_volume", lambda *args, **kwargs: _volume(quota["value"]))
    monkeypatch.setattr(module, "set_volume_quota", lambda *args: quota.update(value=args[2]))
    monkeypatch.setattr(module, "_mount_volume", lambda *args: None)
    monkeypatch.setattr(
        module,
        "_probe_nfs",
        lambda *args: _passed(("nfsv4_mounted", "nfs_read_write", "nfs_shared_visibility")),
    )
    monkeypatch.setattr(
        module,
        "_probe_accounting",
        lambda *args: _passed(("uid_usage_accounted", "gid_usage_accounted", "identity_usage_isolated")),
    )
    monkeypatch.setattr(module, "_probe_quota_enforcement", lambda *args: {"passed": True})
    return cleanup_calls


def test_quota_config_requires_volume_and_identity_quotas() -> None:
    """Quota configuration matching checks both volume and UID/GID limits."""
    module = _load_script()
    assert module._quota_config_matches(_volume(2), 2) is True
    assert module._quota_config_matches(_volume(2), 1) is False
    missing_group = _volume(2)
    missing_group["OpenZFSConfiguration"]["UserAndGroupQuotas"].pop()
    assert module._quota_config_matches(missing_group, 2) is False


def test_accounting_probe_parses_identity_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    """The accounting probe passes only for exact, identity-scoped byte totals."""
    module = _load_script()
    monkeypatch.setattr(
        module,
        "_run_remote",
        lambda *args, **kwargs: (0, f"{8 * 1024 * 1024} {4 * 1024 * 1024} {8 * 1024 * 1024} {4 * 1024 * 1024}", ""),
    )
    result = module._probe_accounting("host", "key")
    assert all(test["passed"] for test in result.values())


def test_accounting_probe_rejects_unparseable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed remote accounting output fails all dependent probes."""
    module = _load_script()
    monkeypatch.setattr(module, "_run_remote", lambda *args, **kwargs: (1, "bad", "find failed"))
    result = module._probe_accounting("host", "key")
    assert not any(test["passed"] for test in result.values())
    assert all("find failed" in test["error"] for test in result.values())


def test_wait_volume_ignores_stale_available_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """An AVAILABLE response is not enough until it contains the requested quota."""
    _load_script()
    helper = sys.modules["common.openzfs"]
    responses = iter((_volume(2), _volume(1)))
    monkeypatch.setattr(helper, "describe_volume", lambda *args: next(responses))
    monkeypatch.setattr(helper.time, "sleep", lambda delay: None)

    result = helper.wait_volume(object(), "vol-child", expected_quota_gib=1, timeout=5, delay=0)

    assert result["OpenZFSConfiguration"]["StorageCapacityQuotaGiB"] == 1


def test_main_happy_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """One shared filesystem run can satisfy all three DIR validations."""
    module = _load_script()
    cleanup_calls = _patch_successful_run(monkeypatch, module)

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert all(test["passed"] for test in payload["tests"].values())
    assert cleanup_calls == [("fs-1", "sg-nfs")]


def test_main_combines_unmount_and_aws_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Teardown errors fail the run without preventing AWS cleanup."""
    module = _load_script()
    cleanup_calls = _patch_successful_run(monkeypatch, module)
    monkeypatch.setattr(module, "_run_remote", lambda *args, **kwargs: (1, "", "device busy"))
    monkeypatch.setattr(
        module,
        "cleanup_resources",
        lambda ec2, fsx, fs_id, sg_id: cleanup_calls.append((fs_id, sg_id)) or ["filesystem cleanup failed"],
    )

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["cleanup"] is False
    assert payload["cleanup_errors"] == [
        "unmount cleanup failed: device busy",
        "filesystem cleanup failed",
    ]
    assert cleanup_calls[-1] == ("fs-1", "sg-nfs")


def test_main_cleans_partial_resources_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A provisioning failure is emitted as JSON and still triggers cleanup."""
    module = _load_script()
    cleanup_calls = _patch_provisioning(monkeypatch, module)
    monkeypatch.setattr(module, "create_filesystem", lambda *args: (_ for _ in ()).throw(RuntimeError("create failed")))

    assert module.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert "create failed" in payload["error"]
    assert cleanup_calls == [(None, "sg-nfs")]
