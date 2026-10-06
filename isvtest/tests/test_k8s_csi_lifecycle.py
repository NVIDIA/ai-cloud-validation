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

"""Regression coverage for actual CSI lifecycle evidence, rather than API-only success."""

from __future__ import annotations

import json
import shlex
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from isvtest.core.runners import CommandResult
from isvtest.validations import k8s_csi_lifecycle as lifecycle
from isvtest.validations import k8s_storage as storage


def result(stdout: str = "", code: int = 0) -> CommandResult:
    """Build a command result for an API or consumer probe."""
    return CommandResult(exit_code=code, stdout=stdout, stderr="probe failed" if code else "", duration=0)


@pytest.mark.parametrize("check_class", [lifecycle.K8sCsiHelmInstallCheck, lifecycle.K8sCsiKustomizeInstallCheck])
@pytest.mark.parametrize(
    "evidence", [{}, {"success": True}, {"success": True, "method": "helm", "version": "3.20"}, "bad json"]
)
def test_install_requires_operations(check_class: type, evidence: Any) -> None:
    """A CLI version or missing installation must never satisfy K8S23."""
    check = check_class(config={"installation": evidence})
    check.run()
    assert not check.passed


@pytest.mark.parametrize("method", ["helm", "kustomize"])
def test_install_alternative_requires_complete_success(method: str) -> None:
    """One successful supported method passes; only the verified alternative skips."""
    evidence = {
        "success": True,
        "method": method,
        "tests": {key: {"passed": True} for key in ("installed", "drivers_registered", "workloads_ready")},
    }
    matching = lifecycle.K8sCsiHelmInstallCheck if method == "helm" else lifecycle.K8sCsiKustomizeInstallCheck
    other = lifecycle.K8sCsiKustomizeInstallCheck if method == "helm" else lifecycle.K8sCsiHelmInstallCheck
    check = matching(config={"installation": json.dumps(evidence)})
    check.run()
    assert check.passed
    with pytest.raises(pytest.skip.Exception):
        other(config={"installation": evidence}).run()
    evidence["tests"]["workloads_ready"]["passed"] = False
    failed = other(config={"installation": evidence})
    failed.run()
    assert not failed.passed


@pytest.mark.parametrize(
    "config",
    [
        {"required": True},
        {"required": "true", "storage_class": "sc"},
        {"storage_class": "sc", "initial_size": "2Gi", "expanded_size": "1Gi"},
        {"storage_class": "sc", "initial_size": "1Gi", "expanded_size": "1Gi"},
        {"storage_class": "sc", "initial_size": "bad", "expanded_size": "2Gi"},
    ],
)
def test_resize_rejects_incomplete_or_invalid_configuration(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required capabilities and a real increase must be configured before provisioning."""
    monkeypatch.delenv("K8S_CSI_BLOCK_SC", raising=False)
    check = storage.K8sCsiPvcExpandCheck(config=config)
    with patch.object(check, "run_command") as command:
        check.run()
    assert not check.passed
    command.assert_not_called()


@pytest.mark.parametrize("required", [True, False])
def test_resize_nonexpandable_class(required: bool) -> None:
    """Required expansion fails while optional standalone checks retain explicit skips."""
    check = storage.K8sCsiPvcExpandCheck(config={"storage_class": "sc", "required": required})
    with patch.object(check, "run_command", return_value=result('{"allowVolumeExpansion": false}')):
        if required:
            check.run()
            assert not check.passed
        else:
            with pytest.raises(pytest.skip.Exception):
                check.run()


@pytest.mark.parametrize(
    "before_kb,after_kb,expected",
    [
        (100000000, 100000000, False),  # Reference hostpath driver only changes metadata.
        (1950000, 1950000, False),  # Already >=90% of target, but did not grow.
        (1000000, 1000000, False),
        (1000000, 1950000, True),
    ],
)
def test_resize_requires_filesystem_growth(before_kb: int, after_kb: int, expected: bool) -> None:
    """The final absolute capacity alone cannot demonstrate filesystem expansion."""
    check = storage.K8sCsiPvcExpandCheck()
    check._kubectl_base, check._namespace = "kubectl", "probe"
    df = f"Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/test {after_kb} 0 0 0% /data\n"
    with (
        patch.object(check, "run_command", return_value=result(df)),
        patch.object(storage.time, "sleep"),
        patch.object(storage.time, "time", side_effect=[0, 0, 0, 10]),
    ):
        assert check._poll_df_size("pod", "2Gi", 5, before_kb * 1024) is expected


@pytest.mark.parametrize("fault", ["", "oversized", "data-lost", "patch", "cleanup"])
def test_resize_workflow_preserves_data_and_cleans_up(fault: str) -> None:
    """Exercise the full probe and verify failed evidence never leaves the parent passed."""
    check = storage.K8sCsiPvcExpandCheck(config={"storage_class": "sc", "required": True})
    commands: list[str] = []
    canary = ""

    def run(cmd: str, **kwargs: Any) -> CommandResult:
        """Emulate the API and mounted filesystem across the resize."""
        nonlocal canary
        commands.append(cmd)
        if "get storageclass" in cmd:
            return result('{"allowVolumeExpansion": true}')
        if "df -k" in cmd:
            size = 100000000 if fault == "oversized" else 1000000
            return result(f"Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/test {size} 0 0 0% /data")
        if "sh -c" in cmd:
            canary = shlex.split(cmd)[-1].split()[1]
        if "cat /data/resize-canary" in cmd:
            return result("lost" if fault == "data-lost" else canary)
        if (fault == "patch" and "patch pvc" in cmd) or (fault == "cleanup" and "delete namespace" in cmd):
            return result(code=1)
        return result()

    with (
        patch.object(check, "run_command", side_effect=run),
        patch.object(storage, "_apply_pvc_manifest", return_value=(0, "")),
        patch.object(storage, "_apply_mount_pod_manifest", return_value=(0, "")),
        patch.object(storage, "_wait_pod_ready", return_value=(True, "")),
        patch.object(storage, "_poll_pvc_bound", return_value=True),
        patch.object(check, "_poll_pvc_capacity_updated", return_value="pv"),
        patch.object(check, "_check_pv_capacity", return_value=True),
        patch.object(check, "_poll_df_size", return_value=True),
    ):
        check.run()
    assert check.passed is (not fault)
    assert any("delete namespace" in command for command in commands)
    if fault == "oversized":
        assert not any("patch pvc" in command for command in commands)


@pytest.mark.parametrize(
    "fault", ["", "not-ready", "wrong-driver", "same-volume", "data-lost", "source-reverted", "cleanup", "apply"]
)
def test_snapshot_restores_point_in_time_data(fault: str) -> None:
    """API readiness, actual restore data, independent source and cleanup are all required."""
    check = lifecycle.K8sCsiSnapshotRestoreCheck(config={"storage_class": "sc", "snapshot_class": "snap-sc"})
    canary = ""
    commands: list[str] = []
    manifests: list[dict[str, Any]] = []

    def apply(parts: list[str], manifest: str, timeout: int) -> tuple[int, str]:
        """Capture the restored PVC's data source and simulate provisioning failure."""
        manifests.append(yaml.safe_load(manifest))
        return (1, "apply failed") if fault == "apply" else (0, "")

    def run(cmd: str, **kwargs: Any) -> CommandResult:
        """Respond to commands using independent snapshot and source data."""
        nonlocal canary
        commands.append(cmd)
        args = shlex.split(cmd)
        if "get" in args:
            resource = args[args.index("get") + 1]
            if resource == "storageclass":
                payload = {"provisioner": "test.csi", "reclaimPolicy": "Delete"}
            elif resource == "volumesnapshotclass":
                payload = {"driver": "other" if fault == "wrong-driver" else "test.csi", "deletionPolicy": "Delete"}
            elif resource == "pvc":
                payload = {"spec": {"volumeName": args[3]}, "status": {"phase": "Bound"}}
            elif resource == "pv":
                payload = {
                    "spec": {
                        "csi": {"driver": "test.csi", "volumeHandle": "same" if fault == "same-volume" else args[3]}
                    }
                }
            elif resource == "volumesnapshot":
                payload = {
                    "metadata": {"uid": "snap-uid"},
                    "status": {"readyToUse": fault != "not-ready", "boundVolumeSnapshotContentName": "content"},
                }
            elif resource == "volumesnapshotcontent":
                payload = {
                    "spec": {"driver": "test.csi", "volumeSnapshotRef": {"uid": "snap-uid"}},
                    "status": {"readyToUse": True, "snapshotHandle": "snap-handle"},
                }
            else:
                raise AssertionError(cmd)
            return result(json.dumps(payload))
        if "sh" in args and args[-1].startswith("echo ") and "changed-after" not in args[-1]:
            canary = args[-1].split()[1]
        if "cat" in args:
            if "restored" in args:
                return result("wrong" if fault == "data-lost" else canary)
            return result(canary if fault == "source-reverted" else "changed-after-snapshot")
        if fault == "cleanup" and "delete" in args:
            return result(code=1)
        return result()

    with (
        patch.object(check, "run_command", side_effect=run),
        patch.object(lifecycle, "_apply_manifest", side_effect=apply),
        patch.object(lifecycle, "_apply_mount_pod_manifest", return_value=(0, "")),
    ):
        check.run()
    assert check.passed is (not fault)
    if fault != "wrong-driver":
        assert any("delete namespace" in command for command in commands)
    if not fault:
        # A pending restore claim holds snapshot protection until its consumer is removed.
        pvc_delete = next(i for i, cmd in enumerate(commands) if "delete pvc restored" in cmd)
        snapshot_delete = next(i for i, cmd in enumerate(commands) if "delete volumesnapshot snapshot" in cmd)
        assert pvc_delete < snapshot_delete
        restore = next(doc for doc in manifests if doc["metadata"]["name"] == "restored")
        assert restore["spec"]["dataSource"] == {
            "apiGroup": "snapshot.storage.k8s.io",
            "kind": "VolumeSnapshot",
            "name": "snapshot",
        }


@pytest.mark.parametrize("config", [{}, {"storage_class": "sc"}, {"snapshot_class": "snap-sc"}])
def test_snapshot_missing_configuration(config: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing class configuration skips before the snapshot probe can start."""
    monkeypatch.delenv("K8S_CSI_BLOCK_SC", raising=False)
    monkeypatch.delenv("K8S_CSI_SNAPSHOT_CLASS", raising=False)
    check = lifecycle.K8sCsiSnapshotRestoreCheck(config=config)
    with patch.object(check, "run_command") as command:
        with pytest.raises(pytest.skip.Exception, match="requires storage_class and snapshot_class"):
            check.run()
    command.assert_not_called()
