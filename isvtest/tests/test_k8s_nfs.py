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

"""Unit tests for the NFS-focused Kubernetes storage validations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_nfs import (
    K8sNfsMountOptionsCheck,
    K8sNodeKernelModulesCheck,
    _parse_nfs_mount_options,
)

_SC_ENV_VARS = ("K8S_CSI_SHARED_FS_SC", "K8S_CSI_NFS_SC")


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


class _FakeProc:
    """Stand-in for ``subprocess.CompletedProcess`` returned by ``kubectl apply``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@contextmanager
def _patched_clock() -> Any:
    """Patch the ``kubectl apply`` subprocess used by the shared harness."""
    with patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(0)):
        yield


def _clear_sc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _SC_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# _parse_nfs_mount_options
# --------------------------------------------------------------------------

_NFS4_LINE = "36 1 0:35 / /data rw,relatime - nfs4 server:/export rw,vers=4.1,nconnect=4,proto=rdma,rsize=1048576"
_NFS3_LINE = "100 1 8:16 / /data rw,relatime - nfs 10.0.0.1:/vol1 rw,vers=3,hard,proto=tcp,addr=10.0.0.1"


class TestParseNfsMountOptions:
    def test_parses_nfs4_superblock_options(self) -> None:
        fstype, opts = _parse_nfs_mount_options(_NFS4_LINE, "/data")
        assert fstype == "nfs4"
        assert opts["vers"] == "4.1"
        assert opts["nconnect"] == "4"
        assert opts["proto"] == "rdma"

    def test_attaches_major_minor(self) -> None:
        _, opts = _parse_nfs_mount_options(_NFS4_LINE, "/data")
        assert opts["__major_minor__"] == "0:35"

    def test_bare_flags_stored_with_empty_value(self) -> None:
        _, opts = _parse_nfs_mount_options(_NFS3_LINE, "/data")
        assert opts["hard"] == ""
        assert opts["vers"] == "3"

    def test_returns_none_when_path_not_found(self) -> None:
        assert _parse_nfs_mount_options(_NFS4_LINE, "/other") is None

    def test_returns_none_for_empty_mountinfo(self) -> None:
        assert _parse_nfs_mount_options("", "/data") is None

    def test_multiple_lines_matches_correct_path(self) -> None:
        mountinfo = "\n".join(
            [
                "10 1 0:10 / /proc rw - proc proc rw",
                _NFS3_LINE,
                "20 1 0:20 / /sys ro - sysfs sysfs ro",
            ]
        )
        fstype, opts = _parse_nfs_mount_options(mountinfo, "/data")
        assert fstype == "nfs"
        assert opts["vers"] == "3"


# --------------------------------------------------------------------------
# K8sNfsMountOptionsCheck
# --------------------------------------------------------------------------

_MOUNTINFO_NFS3 = "5632 5624 0:176 / /data rw,relatime - nfs 10.57.0.130:/vol1 rw,vers=3,proto=tcp,addr=10.57.0.130"
_MOUNTINFO_NOT_NFS = "5632 5624 8:1 / /data rw,relatime - ext4 /dev/sda1 rw"
_BOUND_PVC_JSON = '{"status": {"phase": "Bound"}}'


def _nfs_router(mountinfo: str = _MOUNTINFO_NFS3, readahead: str = "128") -> Any:
    """Route kubectl calls for K8sNfsMountOptionsCheck."""

    def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
        if "create namespace" in cmd or "delete namespace" in cmd:
            return _ok()
        if "get pvc" in cmd:
            return _ok(stdout=_BOUND_PVC_JSON)
        if "wait --for=condition=Ready" in cmd:
            return _ok()
        if "mountinfo" in cmd:
            return _ok(stdout=mountinfo)
        if "read_ahead_kb" in cmd:
            return _ok(stdout=readahead + "\n")
        return _ok()

    return _side_effect


class TestK8sNfsMountOptionsCheckSkip:
    def test_skips_when_no_sc_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sNfsMountOptionsCheck(config={})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed
        assert "Skipped" in check._output

    def test_non_nfs_fstype_skips_all_subtests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sNfsMountOptionsCheck(
            config={
                "nfs_storage_class": "sc-rwx",
                "expected_version": "4.1",
                "bind_timeout_s": 5,
            }
        )
        with _patched_clock(), patch.object(check, "run_command", side_effect=_nfs_router(_MOUNTINFO_NOT_NFS)):
            check.run()
        assert check.passed
        names = {s["name"]: s for s in check._subtest_results}
        assert names["nfs-version"]["skipped"]
        assert names["nfs-nconnect"]["skipped"]
        assert names["nfs-proto"]["skipped"]


class TestK8sNfsMountOptionsSubtests:
    def _run(
        self, config: dict[str, Any], mountinfo: str = _MOUNTINFO_NFS3, readahead: str = "128"
    ) -> K8sNfsMountOptionsCheck:
        check = K8sNfsMountOptionsCheck(config={"nfs_storage_class": "sc-rwx", "bind_timeout_s": 5, **config})
        with _patched_clock(), patch.object(check, "run_command", side_effect=_nfs_router(mountinfo, readahead)):
            check.run()
        return check

    def test_version_match_passes(self) -> None:
        check = self._run({"expected_version": "3"})
        names = {s["name"]: s for s in check._subtest_results}
        assert names["nfs-version"]["passed"]
        assert check.passed

    def test_version_mismatch_fails(self) -> None:
        check = self._run({"expected_version": "4.1"})
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["nfs-version"]["passed"]
        assert not check.passed
        assert "4.1" in names["nfs-version"]["message"]

    def test_version_empty_skipped(self) -> None:
        check = self._run({"expected_version": ""})
        names = {s["name"]: s for s in check._subtest_results}
        assert names["nfs-version"]["skipped"]

    def test_nconnect_absent_fails_when_expected(self) -> None:
        # _NFS3_LINE has no nconnect in options
        check = self._run({"expected_nconnect": 4})
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["nfs-nconnect"]["passed"]
        assert "absent" in names["nfs-nconnect"]["message"]

    def test_nconnect_empty_skipped(self) -> None:
        check = self._run({"expected_nconnect": ""})
        names = {s["name"]: s for s in check._subtest_results}
        assert names["nfs-nconnect"]["skipped"]

    def test_proto_match_passes(self) -> None:
        check = self._run({"expected_proto": "tcp"})
        names = {s["name"]: s for s in check._subtest_results}
        assert names["nfs-proto"]["passed"]

    def test_proto_mismatch_fails(self) -> None:
        check = self._run({"expected_proto": "rdma"})
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["nfs-proto"]["passed"]

    def test_readahead_match_passes(self) -> None:
        check = self._run({"expected_read_ahead_kb": 128}, readahead="128")
        names = {s["name"]: s for s in check._subtest_results}
        assert names["read-ahead-kb"]["passed"]
        assert check.passed

    def test_readahead_mismatch_fails(self) -> None:
        check = self._run({"expected_read_ahead_kb": 256}, readahead="128")
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["read-ahead-kb"]["passed"]
        assert not check.passed

    def test_readahead_empty_skipped(self) -> None:
        check = self._run({"expected_read_ahead_kb": ""})
        names = {s["name"]: s for s in check._subtest_results}
        assert names["read-ahead-kb"]["skipped"]


# --------------------------------------------------------------------------
# K8sNodeKernelModulesCheck
# --------------------------------------------------------------------------


class TestK8sNodeKernelModulesCheckSkip:
    def test_skips_when_no_modules_configured(self) -> None:
        check = K8sNodeKernelModulesCheck(config={})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed
        assert "Skipped" in check._output

    def test_skips_when_no_ready_nodes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = K8sNodeKernelModulesCheck(config={"kernel_modules": ["lustre"]})
        with (
            patch.object(check, "_ready_nodes", return_value=[]),
            patch.object(check, "run_command") as mock_run,
        ):
            check.run()
        mock_run.assert_not_called()
        assert not check.passed
        assert "No Ready nodes" in check._error


class TestK8sNodeKernelModulesCheckFlow:
    def _run(
        self,
        modules: list[str],
        nodes: list[str],
        fail_modules: set[str] | None = None,
    ) -> K8sNodeKernelModulesCheck:
        """
        Run K8sNodeKernelModulesCheck with mocked kubectl.

        ``fail_modules``: module names whose grep always returns non-zero (absent on every node).
        """
        fail_modules = fail_modules or set()

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "create namespace" in cmd or "delete namespace" in cmd:
                return _ok()
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "delete pod" in cmd:
                return _ok()
            for module in modules:
                if f"^{module}" in cmd:
                    return _fail() if module in fail_modules else _ok()
            return _ok()

        check = K8sNodeKernelModulesCheck(config={"kernel_modules": modules, "bind_timeout_s": 5})
        with (
            _patched_clock(),
            patch.object(check, "_ready_nodes", return_value=nodes),
            patch.object(check, "run_command", side_effect=_side_effect),
        ):
            check.run()
        return check

    def test_all_modules_on_all_nodes_passes(self) -> None:
        check = self._run(["lustre", "lnet"], ["node-a", "node-b"])
        assert check.passed
        names = {s["name"]: s for s in check._subtest_results}
        assert names["lustre"]["passed"]
        assert names["lnet"]["passed"]

    def test_module_absent_on_all_nodes_fails(self) -> None:
        check = self._run(["lustre", "lnet"], ["node-a", "node-b"], fail_modules={"lustre"})
        assert not check.passed
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["lustre"]["passed"]
        assert "2/2" in names["lustre"]["message"]
        assert names["lnet"]["passed"]

    def test_module_absent_on_one_node_reports_count(self) -> None:
        """Fail grep on the first call (first node), pass on the second."""
        call_counts: dict[str, int] = {}

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "create namespace" in cmd or "delete namespace" in cmd or "delete pod" in cmd:
                return _ok()
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "^lustre" in cmd:
                call_counts["lustre"] = call_counts.get("lustre", 0) + 1
                return _fail() if call_counts["lustre"] == 1 else _ok()
            return _ok()

        check = K8sNodeKernelModulesCheck(config={"kernel_modules": ["lustre"], "bind_timeout_s": 5})
        with (
            _patched_clock(),
            patch.object(check, "_ready_nodes", return_value=["node-a", "node-b"]),
            patch.object(check, "run_command", side_effect=_side_effect),
        ):
            check.run()

        assert not check.passed
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["lustre"]["passed"]
        assert "1/2" in names["lustre"]["message"]

    def test_single_node_pass(self) -> None:
        check = self._run(["nfsv3"], ["node-a"])
        assert check.passed
