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

"""Unit tests for ``isvtest.validations.k8s_filesystem``."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from isvtest.core.k8s import render_k8s_manifest
from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_filesystem import (
    _PJDFSTEST_POD_MANIFEST,
    K8sCrossNodeWriteVisibilityCheck,
    K8sFileLockingCheck,
    K8sLargeDirListingFilesCheck,
    K8sPosixComplianceCheck,
    _set_fs_pod_fields,
    append_payload_cmd,
    count_entries_cmd,
    create_dirs_cmd,
    create_files_cmd,
    flock_hold_command,
    flock_nonblock_cmd,
    list_dir_quiet_cmd,
    parse_pjdfstest_output,
    read_file_cmd,
    stat_size_mtime_cmd,
    write_payload_cmd,
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


def _fake_kubectl_get_nodes_result(nodes: dict[str, str]) -> Any:
    """Build a fake ``run_kubectl`` result for ``kubectl get nodes -o json``.

    ``nodes`` maps node-name -> "Ready"|"NotReady"; the returned object has
    the ``returncode``, ``stdout``, ``stderr`` attributes ``run_kubectl``
    callers consume.
    """
    items = [
        {
            "metadata": {"name": name},
            "status": {"conditions": [{"type": "Ready", "status": "True" if status == "Ready" else "False"}]},
        }
        for name, status in nodes.items()
    ]
    return _FakeProc(returncode=0, stdout=json.dumps({"items": items}), stderr="")


_BOUND_PVC_JSON = '{"status":{"phase":"Bound"}}'


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def time(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += float(seconds)


@contextmanager
def _patched_clock() -> Any:
    """Patch ``k8s_filesystem`` time + the ``kubectl apply`` subprocess in one place."""
    clock = _FakeClock()
    with (
        patch("isvtest.validations.k8s_filesystem.time.sleep", side_effect=clock.sleep),
        patch("isvtest.validations.k8s_filesystem.time.time", side_effect=clock.time),
        patch("isvtest.validations.k8s_storage.subprocess.run", return_value=_FakeProc(0)),
    ):
        yield clock


def _clear_sc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _SC_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# Snippet helpers.
# --------------------------------------------------------------------------


class TestSnippets:
    def test_write_and_append_payload(self) -> None:
        assert write_payload_cmd("/data/f", "abc") == "printf %s abc > /data/f"
        assert append_payload_cmd("/data/f", "abc") == "printf %s abc >> /data/f"

    def test_write_payload_quotes_special_chars(self) -> None:
        # A payload with a space must be shell-quoted so it is one argument.
        assert write_payload_cmd("/data/f", "a b") == "printf %s 'a b' > /data/f"

    def test_read_and_stat(self) -> None:
        assert read_file_cmd("/data/f") == "cat /data/f"
        assert stat_size_mtime_cmd("/data/f") == "stat -c '%s %Y' /data/f"

    def test_flock_helpers(self) -> None:
        assert flock_nonblock_cmd("/data/lock") == "flock -xn /data/lock true"
        assert flock_hold_command("/data/lock") == [
            "flock",
            "-x",
            "/data/lock",
            "sh",
            "-c",
            "while true; do sleep 3600; done",
        ]

    def test_create_files_cmd(self) -> None:
        cmd = create_files_cmd("/data/big", 1000, prefix="f")
        assert cmd.startswith("mkdir -p /data/big &&")
        assert "seq 1 1000" in cmd
        assert "xargs touch" in cmd

    def test_create_dirs_cmd(self) -> None:
        cmd = create_dirs_cmd("/data/big", 500, prefix="d")
        assert "seq 1 500" in cmd
        assert "xargs mkdir" in cmd

    def test_list_and_count(self) -> None:
        assert list_dir_quiet_cmd("/data/big") == "ls -1A /data/big >/dev/null"
        assert count_entries_cmd("/data/big") == "find /data/big -mindepth 1 -maxdepth 1 | wc -l"


# --------------------------------------------------------------------------
# Manifest mutator.
# --------------------------------------------------------------------------


class TestSetFsPodFields:
    def _base_doc(self) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "placeholder", "namespace": "placeholder"},
            "spec": {
                "containers": [{"name": "probe", "image": "placeholder", "command": ["sh", "-c", "sleep 3600"]}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "placeholder"}}],
            },
        }

    def test_binds_pvc_and_sets_metadata(self) -> None:
        out = _set_fs_pod_fields(self._base_doc(), namespace="ns1", name="pod1", pvc_name="pvc1", image="busybox:1.36")
        assert out["metadata"]["name"] == "pod1"
        assert out["metadata"]["namespace"] == "ns1"
        assert out["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "pvc1"
        assert out["spec"]["containers"][0]["image"] == "busybox:1.36"
        assert "nodeName" not in out["spec"]

    def test_pins_node_and_overrides_command_and_image(self) -> None:
        out = _set_fs_pod_fields(
            self._base_doc(),
            namespace="ns1",
            name="pod1",
            pvc_name="pvc1",
            image="mirror.local/busybox:1.36",
            node_name="node-a",
            command=["sh", "-c", "flock -x /data/lock -c 'sleep 10'"],
        )
        assert out["spec"]["nodeName"] == "node-a"
        assert out["spec"]["containers"][0]["image"] == "mirror.local/busybox:1.36"
        assert out["spec"]["containers"][0]["command"] == ["sh", "-c", "flock -x /data/lock -c 'sleep 10'"]

    def test_sets_node_selector(self) -> None:
        out = _set_fs_pod_fields(
            self._base_doc(),
            namespace="ns1",
            name="pod1",
            pvc_name="pvc1",
            image="busybox:1.36",
            node_selector={"scd.vastdata.com/node": "true", "kubernetes.io/os": "linux"},
        )
        assert out["spec"]["nodeSelector"] == {
            "scd.vastdata.com/node": "true",
            "kubernetes.io/os": "linux",
        }
        assert "nodeName" not in out["spec"]

    def test_no_node_selector_omitted(self) -> None:
        out = _set_fs_pod_fields(self._base_doc(), namespace="ns1", name="pod1", pvc_name="pvc1", image="busybox:1.36")
        assert "nodeSelector" not in out["spec"]


# --------------------------------------------------------------------------
# Skip behaviour.
# --------------------------------------------------------------------------


class TestSkipBehaviour:
    def test_no_storage_class_skips_without_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sFileLockingCheck(config={})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed
        assert "Skipped" in check._output

    def test_cross_node_skips_when_fewer_than_two_ready_nodes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sCrossNodeWriteVisibilityCheck(config={"shared_fs_storage_class": "sc-rwx"})
        with (
            patch(
                "isvtest.validations.k8s_filesystem.run_kubectl",
                return_value=_fake_kubectl_get_nodes_result({"node-a": "Ready", "node-b": "NotReady"}),
            ),
            patch.object(check, "run_command") as mock_run,
        ):
            check.run()
        # Skipped before any namespace/pod work.
        mock_run.assert_not_called()
        assert check.passed
        assert "2 Ready nodes" in check._output

    def test_ready_nodes_filters_and_sorts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sCrossNodeWriteVisibilityCheck(config={})
        with patch(
            "isvtest.validations.k8s_filesystem.run_kubectl",
            return_value=_fake_kubectl_get_nodes_result({"b": "Ready", "a": "Ready", "c": "NotReady"}),
        ):
            assert check._ready_nodes() == ["a", "b"]


class TestNodeSelector:
    """Unit tests for _node_selector() and its effect on _ready_nodes()."""

    def test_absent_returns_empty_dict(self) -> None:
        check = K8sFileLockingCheck(config={})
        assert check._node_selector() == {}

    def test_empty_dict_returns_empty_dict(self) -> None:
        check = K8sFileLockingCheck(config={"node_selector": {}})
        assert check._node_selector() == {}

    def test_non_dict_returns_empty_dict(self) -> None:
        check = K8sFileLockingCheck(config={"node_selector": "bad-value"})
        assert check._node_selector() == {}

    def test_selector_returned_as_str_dict(self) -> None:
        check = K8sFileLockingCheck(
            config={"node_selector": {"scd.vastdata.com/node": "true", "kubernetes.io/os": "linux"}}
        )
        assert check._node_selector() == {"scd.vastdata.com/node": "true", "kubernetes.io/os": "linux"}

    def test_json_string_selector_parsed(self) -> None:
        """Manifest-driven node_selector arrives as a JSON string via the setup step."""
        check = K8sFileLockingCheck(config={"node_selector": '{"csi.acme.com/node": "true"}'})
        assert check._node_selector() == {"csi.acme.com/node": "true"}

    def test_empty_json_string_returns_empty_dict(self) -> None:
        check = K8sFileLockingCheck(config={"node_selector": ""})
        assert check._node_selector() == {}

    def _node_item(self, name: str, ready: bool) -> dict[str, Any]:
        status = "True" if ready else "False"
        return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": status}]}}

    def test_ready_nodes_uses_run_kubectl_with_label_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When node_selector is set, _ready_nodes() filters via kubectl -l and the items' Ready condition."""
        check = K8sCrossNodeWriteVisibilityCheck(
            config={"shared_fs_storage_class": "sc-rwx", "node_selector": {"foo": "bar"}}
        )
        fake_nodes_json = json.dumps(
            {
                "items": [
                    self._node_item("node-y", ready=True),
                    self._node_item("node-x", ready=True),
                    self._node_item("node-z", ready=False),  # filtered out: NotReady
                ]
            }
        )

        class _FakeResult:
            returncode = 0
            stdout = fake_nodes_json
            stderr = ""

        calls: list[list[str]] = []

        def _fake_run_kubectl(args: list[str]) -> _FakeResult:
            calls.append(args)
            return _FakeResult()

        monkeypatch.setattr("isvtest.validations.k8s_filesystem.run_kubectl", _fake_run_kubectl)

        result = check._ready_nodes()

        assert len(calls) == 1
        assert "-l" in calls[0]
        assert "foo=bar" in calls[0]
        # Only Ready nodes, sorted; node-z dropped.
        assert result == ["node-x", "node-y"]

    def test_ready_nodes_returns_empty_when_kubectl_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        check = K8sCrossNodeWriteVisibilityCheck(
            config={"shared_fs_storage_class": "sc-rwx", "node_selector": {"foo": "bar"}}
        )

        class _FailResult:
            returncode = 1
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr("isvtest.validations.k8s_filesystem.run_kubectl", lambda args: _FailResult())
        assert check._ready_nodes() == []


# --------------------------------------------------------------------------
# Happy-path flows (mocked kubectl).
# --------------------------------------------------------------------------


class TestFileLockingFlow:
    def _router(self) -> Any:
        """Answer the kubectl commands K8sFileLockingCheck issues.

        First flock exec (contention while pod A holds) fails; the second
        (after pod A is deleted) succeeds.
        """
        state = {"flock_calls": 0}

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "create namespace" in cmd or "delete namespace" in cmd or "delete pod" in cmd:
                return _ok()
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd:
                return _ok(stdout=_BOUND_PVC_JSON)
            if "flock -xn" in cmd:
                state["flock_calls"] += 1
                return _fail() if state["flock_calls"] == 1 else _ok()
            return _ok()

        return _side_effect

    def test_locking_enforced_then_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sFileLockingCheck(
            config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5, "release_timeout_s": 10}
        )
        with (
            _patched_clock(),
            patch.object(check, "_ready_nodes", return_value=["node-a", "node-b"]),
            patch.object(check, "run_command", side_effect=self._router()),
        ):
            check.run()
        assert check.passed, check._error
        names = {s["name"]: s for s in check._subtest_results}
        assert names["lock-contention"]["passed"]
        assert names["lock-release"]["passed"]

    def test_locking_fails_when_contention_not_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sFileLockingCheck(config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5})

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "get pvc" in cmd:
                return _ok(stdout=_BOUND_PVC_JSON)
            if "flock -xn" in cmd:
                return _ok()  # pod B always acquires - locking NOT enforced
            return _ok()

        with (
            _patched_clock(),
            patch.object(check, "_ready_nodes", return_value=["node-a", "node-b"]),
            patch.object(check, "run_command", side_effect=_side_effect),
        ):
            check.run()
        assert not check.passed
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["lock-contention"]["passed"]

    def test_contention_flock_error_does_not_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flock failure that is not EAGAIN (exit != 0/1) must not count as 'denied'."""
        _clear_sc_env(monkeypatch)
        check = K8sFileLockingCheck(config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5})

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "get pvc" in cmd:
                return _ok(stdout=_BOUND_PVC_JSON)
            if "flock -xn" in cmd:
                return _fail(stderr="flock: applet not found", exit_code=127)
            return _ok()

        with (
            _patched_clock(),
            patch.object(check, "_ready_nodes", return_value=["node-a", "node-b"]),
            patch.object(check, "run_command", side_effect=_side_effect),
        ):
            check.run()
        assert not check.passed
        names = {s["name"]: s for s in check._subtest_results}
        assert not names["lock-contention"]["passed"]
        assert "errored" in names["lock-contention"]["message"]


class TestCrossNodeVisibilityFlow:
    def _router(self) -> Any:
        """Echo back whatever payload pod A wrote when pod B reads visfile."""
        state = {"payload": ""}

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "create namespace" in cmd or "delete namespace" in cmd:
                return _ok()
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd:
                return _ok(stdout=_BOUND_PVC_JSON)
            if "printf %s" in cmd and "visfile" in cmd:
                match = re.search(r"printf %s (\S+) >", cmd)
                if match:
                    state["payload"] = match.group(1)
                return _ok()
            if "cat /data/visfile" in cmd:
                return _ok(stdout=state["payload"] + "\n")
            return _ok()

        return _side_effect

    def test_visible_within_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sCrossNodeWriteVisibilityCheck(
            config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5, "visibility_window_s": 5.0}
        )
        with (
            _patched_clock(),
            patch.object(check, "_ready_nodes", return_value=["node-a", "node-b"]),
            patch.object(check, "run_command", side_effect=self._router()),
        ):
            check.run()
        assert check.passed, check._error


class TestLargeDirListingFlow:
    def test_lists_all_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sLargeDirListingFilesCheck(
            config={"shared_fs_storage_class": "sc-rwx", "files_count": 1000, "bind_timeout_s": 5}
        )

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "create namespace" in cmd or "delete namespace" in cmd:
                return _ok()
            if "wait --for=condition=Ready" in cmd:
                return _ok()
            if "get pvc" in cmd:
                return _ok(stdout=_BOUND_PVC_JSON)
            if "xargs touch" in cmd:
                return _ok()
            if "ls -1A" in cmd:
                return _ok()
            if "wc -l" in cmd:
                return _ok(stdout="1000\n")
            return _ok()

        with _patched_clock(), patch.object(check, "run_command", side_effect=_side_effect):
            check.run()
        assert check.passed, check._error

    def test_truncated_listing_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sLargeDirListingFilesCheck(
            config={"shared_fs_storage_class": "sc-rwx", "files_count": 1000, "bind_timeout_s": 5}
        )

        def _side_effect(cmd: str, *args: Any, **kwargs: Any) -> CommandResult:
            if "get pvc" in cmd:
                return _ok(stdout=_BOUND_PVC_JSON)
            if "wc -l" in cmd:
                return _ok(stdout="999\n")  # one short - truncation
            return _ok()

        with _patched_clock(), patch.object(check, "run_command", side_effect=_side_effect):
            check.run()
        assert not check.passed
        assert "truncation" in check._error


# --------------------------------------------------------------------------
# pjdfstest (prove/TAP) parsing.
# --------------------------------------------------------------------------


_PROVE_PASS = (
    "tests/chmod/00.t .. ok\n"
    "tests/open/00.t .. ok\n"
    "All tests successful.\n"
    "Files=238, Tests=8000, 42 wallclock secs ( 1.20 usr  0.30 sys + ... )\n"
    "Result: PASS\n"
)

_PROVE_FAIL = (
    "tests/open/00.t .. ok\n"
    "tests/chmod/12.t .. 1/203 Failed 1/203 subtests\n"
    "tests/open/05.t .. Failed 2/30 subtests\n"
    "\n"
    "Test Summary Report\n"
    "-------------------\n"
    "tests/chmod/12.t (Wstat: 0 Tests: 203 Failed: 1)\n"
    "  Failed tests:  57\n"
    "tests/open/05.t (Wstat: 0 Tests: 30 Failed: 2)\n"
    "  Failed tests:  3, 7\n"
    "Files=238, Tests=8000, 50 wallclock secs ( ... )\n"
    "Result: FAIL\n"
)

# A file whose subtests all pass but that exits non-zero: prove flags it
# "Dubious" and reports a non-zero Wstat with Failed: 0. It must still be
# treated as a failed file.
_PROVE_FAIL_WSTAT = (
    "tests/open/00.t .. ok\n"
    "tests/chmod/01.t .. Dubious, test returned 1 (wstat 256, 0x100)\n"
    "All 5 subtests passed\n"
    "\n"
    "Test Summary Report\n"
    "-------------------\n"
    "tests/chmod/01.t (Wstat: 256 Tests: 5 Failed: 0)\n"
    "  Non-zero exit status: 1\n"
    "Files=238, Tests=8000, 50 wallclock secs ( ... )\n"
    "Result: FAIL\n"
)


class TestParsePjdfstest:
    def test_pass_output(self) -> None:
        r = parse_pjdfstest_output(_PROVE_PASS)
        assert r.success
        assert r.result == "PASS"
        assert r.files_total == 238
        assert r.tests_total == 8000
        assert r.failed_files == []
        assert r.all_files == ["tests/chmod/00.t", "tests/open/00.t"]

    def test_all_ok_without_result_line(self) -> None:
        r = parse_pjdfstest_output("tests/x.t .. ok\nAll tests successful.\nFiles=1, Tests=5, 1 wallclock secs\n")
        assert r.success
        assert r.result == "PASS"

    def test_fail_output_lists_failed_files(self) -> None:
        r = parse_pjdfstest_output(_PROVE_FAIL)
        assert not r.success
        assert r.result == "FAIL"
        assert r.failed_files == [("tests/chmod/12.t", 1), ("tests/open/05.t", 2)]
        # Every file is enumerated once (progress + summary lines deduped),
        # preserving execution order, including the passing file.
        assert r.all_files == ["tests/open/00.t", "tests/chmod/12.t", "tests/open/05.t"]

    def test_nonzero_wstat_with_zero_failed_is_a_failure(self) -> None:
        # A file that prove marks "Dubious" (non-zero Wstat, Failed: 0) must be
        # recorded as a failed file (with a 0 subtest count) so its per-file
        # subtest is reported failing rather than passing.
        r = parse_pjdfstest_output(_PROVE_FAIL_WSTAT)
        assert not r.success
        assert r.result == "FAIL"
        assert r.failed_files == [("tests/chmod/01.t", 0)]
        assert r.all_files == ["tests/open/00.t", "tests/chmod/01.t"]

    def test_unparseable_output_is_error(self) -> None:
        r = parse_pjdfstest_output("bash: prove: command not found\n")
        assert not r.success
        assert r.error
        assert r.result == ""


# --------------------------------------------------------------------------
# POSIX-compliance manifest + flow.
# --------------------------------------------------------------------------


class TestPosixManifest:
    def test_rendered_pod_is_root_and_privileged(self) -> None:
        rendered = render_k8s_manifest(
            _PJDFSTEST_POD_MANIFEST,
            lambda d: _set_fs_pod_fields(
                d, namespace="ns1", name="posix-pod", pvc_name="pvc1", image="gcc:12", command=None
            ),
        )
        doc = yaml.safe_load(rendered)
        container = doc["spec"]["containers"][0]
        assert doc["spec"]["securityContext"]["runAsUser"] == 0
        assert container["securityContext"]["privileged"] is True
        assert container["image"] == "gcc:12"
        # The manifest's sleep-infinity keepalive is preserved when command=None.
        assert container["command"] == ["sh", "-c", "sleep infinity"]
        assert doc["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "pvc1"


class TestPosixSkipBehaviour:
    def test_no_storage_class_skips_without_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sPosixComplianceCheck(config={})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed
        assert "Skipped" in check._output

    def test_podsecurity_denial_skips(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        monkeypatch.setattr("isvtest.validations.k8s_filesystem._PJDFSTEST_SRC_DIR", tmp_path)
        check = K8sPosixComplianceCheck(config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5})
        denial = 'pods "x" is forbidden: violates PodSecurity "restricted:latest": privileged (container ...)'
        with (
            patch.object(check, "run_command", return_value=_ok()),
            patch.object(check, "_apply_pvc", return_value=(0, "")),
            patch.object(check, "_wait_pvc_bound", return_value=True),
            patch.object(check, "_apply_posix_pod", return_value=(1, denial)),
        ):
            check.run()
        assert check.passed
        assert "Skipped" in check._output

    def test_is_podsecurity_denial_detection(self) -> None:
        assert K8sPosixComplianceCheck._is_podsecurity_denial("violates PodSecurity ...")
        assert K8sPosixComplianceCheck._is_podsecurity_denial("privileged is forbidden")
        assert not K8sPosixComplianceCheck._is_podsecurity_denial("ImagePullBackOff")
        assert not K8sPosixComplianceCheck._is_podsecurity_denial("")


class TestPosixComplianceFlow:
    @pytest.fixture(autouse=True)
    def _stub_vendored_src(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("isvtest.validations.k8s_filesystem._PJDFSTEST_SRC_DIR", tmp_path)

    def test_passes_when_prove_reports_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sPosixComplianceCheck(config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5})

        def _route(cmd: str, *a: Any, **k: Any) -> CommandResult:
            if "prove -r" in cmd:
                return _ok(stdout=_PROVE_PASS)
            return _ok()

        with (
            patch.object(check, "_apply_pvc", return_value=(0, "")),
            patch.object(check, "_wait_pvc_bound", return_value=True),
            patch.object(check, "_apply_posix_pod", return_value=(0, "")),
            patch.object(check, "_wait_ready", return_value=(True, "")),
            patch.object(check, "run_command", side_effect=_route),
        ):
            check.run()
        assert check.passed, check._error
        assert "0 failures" in check._output
        # Passing files are still reported as subtests for a complete export.
        passed_subtests = {s["name"] for s in check._subtest_results if s["passed"]}
        assert {"tests/chmod/00.t", "tests/open/00.t"} <= passed_subtests

    def test_fails_and_reports_subtests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sPosixComplianceCheck(config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5})

        def _route(cmd: str, *a: Any, **k: Any) -> CommandResult:
            if "prove -r" in cmd:
                return _fail(stdout=_PROVE_FAIL)
            return _ok()

        with (
            patch.object(check, "_apply_pvc", return_value=(0, "")),
            patch.object(check, "_wait_pvc_bound", return_value=True),
            patch.object(check, "_apply_posix_pod", return_value=(0, "")),
            patch.object(check, "_wait_ready", return_value=(True, "")),
            patch.object(check, "run_command", side_effect=_route),
        ):
            check.run()
        assert not check.passed
        names = {s["name"]: s for s in check._subtest_results}
        assert "tests/chmod/12.t" in names
        assert "tests/open/05.t" in names
        assert not names["tests/chmod/12.t"]["passed"]
        # Passing files in a failing run are still reported (complete export).
        assert names["tests/open/00.t"]["passed"]

    def test_build_failure_fails_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_sc_env(monkeypatch)
        check = K8sPosixComplianceCheck(config={"shared_fs_storage_class": "sc-rwx", "bind_timeout_s": 5})

        def _route(cmd: str, *a: Any, **k: Any) -> CommandResult:
            if "autoreconf" in cmd:
                return _fail(stderr="autoreconf: command not found")
            if "prove -r" in cmd:
                raise AssertionError("prove must not run when the build fails")
            return _ok()

        with (
            patch.object(check, "_apply_pvc", return_value=(0, "")),
            patch.object(check, "_wait_pvc_bound", return_value=True),
            patch.object(check, "_apply_posix_pod", return_value=(0, "")),
            patch.object(check, "_wait_ready", return_value=(True, "")),
            patch.object(check, "run_command", side_effect=_route),
        ):
            check.run()
        assert not check.passed
        assert "Building pjdfstest" in check._error
