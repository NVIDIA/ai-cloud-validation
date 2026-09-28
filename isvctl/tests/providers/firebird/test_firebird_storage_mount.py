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

"""Tests for the Firebird storage mount steps: the wekafs client, token, mount, I/O checks, and cleanup.

The Filesystem API is the stateful ``FakeStorage`` of the storage tests. SSH is
faked at ``common.wekafs.ssh_run`` by ``FakeBm``, which models the BM: whether
the filesystem client is installed, its mounts, and the root token file. It records
every command with its stdin, so the tests can prove the mount credential only
ever travels on stdin. The probe programs the steps send to the BM get canned
answers here; the portable ones also run for real, locally, at the end.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from isvtest.validations.home_directory import DirectoryFilesystemQuotaCheck, DirectoryUsageAccountingCheck
from isvtest.validations.hss import (
    HssChangelogAuditCheck,
    HssFlockMountCheck,
    HssLiveExpansionCheck,
    HssMultipathCheck,
    HssParallelFsProvisioningCheck,
    HssQosThroughputCheck,
    HssQuotaEnforcementCheck,
    HssRootSquashCheck,
)

from .harness import PROJECT, SCRIPTS, FakeApi, FakeClock, load, run, validate
from .test_firebird_storage import (
    ENDPOINT,
    RUN_ID,
    SECRET,
    TOKEN,
    FakeStorage,
    _assert_dev_untouched,
    _assert_secret_never_leaks,
)

BM = "bm.B"
BM_IP = "172.16.240.10"
BACKEND = "weka-backend.example"
MOUNT = f"/mnt/isv-{RUN_ID}"
TOKEN_PATH = f"/root/.weka/isv-{RUN_ID}.json"
VERSION = "5.1.32.15"
GIB = 1 << 30
BM_ARGS = ["--instance-id", BM, "--key-file", "/k/key"]
MOUNT_ARGS = [*BM_ARGS, "--mount-point", MOUNT]
SECRETS = (SECRET, TOKEN["access_token"], TOKEN["refresh_token"])

Handler = Callable[[list[str]], Any]


_WEKA_CMD = re.compile(r"^sudo (?:env WEKA_TOKEN=(?P<token>\S+) )?weka (?P<sub>.+)$")


class FakeBm:
    """A BM reached over SSH: the filesystem client, wekafs mounts, client tokens, and canned probes.

    ``programs`` maps a program the step sends on stdin (``sudo python3 -``) to
    a handler ``(argv) -> dict`` (printed as JSON) or ``(rc, stdout, stderr)``.
    ``weka`` maps a ``weka`` subcommand prefix to its JSON answer or an
    ``(rc, stdout, stderr)`` failure, whether the real command carried a
    ``WEKA_TOKEN`` env override or not. ``shell`` holds extra
    ``(substring, answer)`` pairs checked before everything else.

    ``tenant_token`` models the tenant's own ``/root/.weka/auth-token.json``:
    the suite never writes or reads it, only ever seeds and later checks it.
    ``tokens`` models every suite-owned ``/root/.weka/isv-*.json`` file, keyed
    by its path.
    """

    def __init__(self, *, installed: bool = False, weka_names: list[str] | None = None) -> None:
        """Start with no mounts and no token files."""
        self.installed = installed
        self.weka_names = weka_names
        self.mounts: dict[str, tuple[str, str]] = {}  # target -> (source, options)
        self.dirs: set[str] = set()
        self.tenant_token: dict[str, Any] | None = None
        self.tokens: dict[str, dict[str, Any]] = {}  # isv- token path -> decoded content
        self.calls: list[tuple[str, str | None]] = []
        self.events: list[str] | None = None  # shared with the API fake to check ordering
        self.programs: dict[str, Handler] = {}
        self.weka: dict[str, Any] = {}
        self.shell: list[tuple[str, Any]] = []
        self.install_result: tuple[int, str, str] = (0, "WekaIO CLI is now installed", "")
        self.install_results: dict[str, tuple[int, str, str]] = {}  # URL -> canned result, overrides install_result
        self.mount_result: tuple[int, str, str] | None = None
        self.workdir = "/root/isv-hss10-io.FAKE01"  # canned `sudo mktemp -d` result

    def seed_mount(self, target: str, source: str, options: str = "rw,relatime,writecache") -> None:
        """Add an existing wekafs mount."""
        self.mounts[target] = (source, options)
        self.dirs.add(target)

    def seed_tenant_token(self, content: dict[str, Any] | None = None) -> None:
        """Pre-seed the tenant's own token, to prove the suite never touches it."""
        self.tenant_token = content or {"access_token": "tenant-tok", "refresh_token": "tenant-r", "token_type": "x"}

    def _event(self, text: str) -> None:
        if self.events is not None:
            self.events.append(text)

    def ssh_run(self, host: str, user: str, key: str, command: str, **kwargs: Any) -> tuple[int, str, str]:
        """Stand in for ``ssh_run``."""
        assert (host, user, key) == (BM_IP, "ubuntu", "/k/key")
        stdin = kwargs.get("input_text")
        self.calls.append((command, stdin))
        for needle, answer in self.shell:
            if needle in command:
                return answer(command) if callable(answer) else answer
        if command.startswith("command -v mount.wekafs"):
            return (0, f"Weka CLI build {VERSION}\n", "") if self.installed else (1, "", "")
        if "/dist/v1/install" in command:
            url = next((w for w in shlex.split(command) if w.startswith(("http://", "https://"))), "")
            self._event("install")
            result = self.install_results.get(url, self.install_result)
            if result[0] == 0:
                self.installed = True
            return result
        if command.startswith("sudo python3 -c "):
            path = shlex.split(command)[-1]
            return self._write_token(path, stdin or "")
        if command.startswith("sudo python3 - "):
            argv = shlex.split(command)[3:]
            handler = self.programs.get(stdin or "")
            assert handler, f"unexpected program on stdin for {command}"
            answer = handler(argv)
            return answer if isinstance(answer, tuple) else (0, json.dumps(answer) + "\n", "")
        if command.startswith("sudo mktemp -d "):
            return 0, self.workdir + "\n", ""
        if command.startswith("sudo tee "):
            return 0, "", ""  # models `sudo tee <path> >/dev/null` staging IO_LOOP from stdin
        weka_match = _WEKA_CMD.match(command)
        if weka_match:
            sub = weka_match.group("sub")
            if sub.startswith("fs -H ") and "fs -H" not in self.weka:
                names = self.weka_names if self.weka_names is not None else []
                return 0, json.dumps([{"name": n} for n in names]), ""
            for prefix, answer in self.weka.items():
                if sub.startswith(prefix):
                    return answer if isinstance(answer, tuple) else (0, json.dumps(answer), "")
            raise AssertionError(f"unexpected weka command {command}")
        if command == "cat /proc/mounts":
            lines = [f"{src} {tgt} wekafs {opts} 0 0" for tgt, (src, opts) in self.mounts.items()]
            return 0, "\n".join(["/dev/sda1 / ext4 rw 0 0", *lines]) + "\n", ""
        if "mount -t wekafs" in command:
            return self._mount(command)
        if "umount " in command or "rmdir " in command:
            return self._unmount(command)
        if command.startswith("ls -d /mnt/isv-"):
            prefix = shlex.split(command)[2]
            return 0, "\n".join(sorted(d for d in self.dirs if d == prefix or d.startswith(prefix + "-"))), ""
        if command.startswith("ls -1 ") and "isv-*.json" in command:
            paths = sorted(self.tokens)
            return (0, "\n".join(paths) + "\n", "") if paths else (2, "", "No such file or directory")
        if command.startswith("if sudo test -e "):
            return self._remove_token(command)
        raise AssertionError(f"unexpected SSH command {command}")

    def _write_token(self, path: str, stdin: str) -> tuple[int, str, str]:
        """Model TOKEN_WRITER: check the decoded shape, store it at ``path``, print key names."""
        try:
            token = json.loads(base64.b64decode(stdin.strip(), validate=True))
        except Exception as e:
            return 1, "", f"the mount credential is not base64 JSON ({type(e).__name__})"
        if not isinstance(token, dict) or not {"access_token", "refresh_token", "token_type"} <= set(token):
            shape = sorted(token) if isinstance(token, dict) else type(token).__name__
            return 1, "", f"the mount credential is not a Weka auth token (keys: {shape})"
        self.tokens[path] = token
        return 0, json.dumps({"keys": sorted(token)}), ""

    def _remove_token(self, command: str) -> tuple[int, str, str]:
        """Model the ``if sudo test -e <path>; ...; sudo rm -f <path> <path>.tmp`` cleanup."""
        path = shlex.split(command.split(";", 1)[0])[-1]
        present = path in self.tokens
        self.tokens.pop(path, None)
        self._event(f"token removed:{path}")
        return 0, "present\n" if present else "", ""

    def _mount(self, command: str) -> tuple[int, str, str]:
        words = shlex.split(command.split("&&", 1)[1])
        target, source = words[-1], words[-2]
        options = words[words.index("-o") + 1] if "-o" in words else ""
        token_path = next((kv.split("=", 1)[1] for kv in options.split(",") if kv.startswith("auth_token_path=")), "")
        self.dirs.add(target)
        self._event(f"mount {target}")
        if self.mount_result:
            return self.mount_result
        assert token_path and token_path in self.tokens, f"mounted with no isv- token written first ({options!r})"
        self.mounts[target] = (source, f"rw,relatime,{options}")
        return 0, "Mount completed successfully", ""

    def _unmount(self, command: str) -> tuple[int, str, str]:
        words = shlex.split(command.replace("{", " ").replace("}", " ").replace(";", " "))
        for i, word in enumerate(words):
            if word == "umount":
                self.mounts.pop(words[i + 1], None)
                self._event(f"umount {words[i + 1]}")
            if word == "rmdir":
                self.dirs.discard(words[i + 1])
        return 0, "", ""

    def commands(self) -> list[str]:
        """Return every SSH command line sent, in order."""
        return [command for command, _ in self.calls]


def _bm_route(state: str = "RUNNING") -> dict[str, Any]:
    return {"bm": {"id": BM, "state": state, "ipAddress": BM_IP, "subnetId": "subnet.S"}}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: str,
    storage: FakeStorage,
    bm: FakeBm,
    argv: list[str],
    setup: Callable[[Any], None] | None = None,
) -> tuple[int, dict[str, Any], FakeApi]:
    """Run a storage script with the fake API and the fake BM; waits use a fake clock."""
    routes = {**storage.routes(), f"GET /projects/{PROJECT}/compute/bms/{BM}": _bm_route()}

    def prepare(module: Any) -> None:
        FakeClock().install(monkeypatch, module)
        wekafs = module._common["wekafs"]
        monkeypatch.setattr(wekafs, "ssh_run", bm.ssh_run)
        if setup:
            setup(module)

    return run(monkeypatch, capsys, f"storage/{script}", routes, argv, prepare=prepare)


def _assert_token_only_on_stdin(bm: FakeBm, out: dict[str, Any], api: FakeApi) -> None:
    """No credential value is in any SSH command line, the step's JSON, or its log."""
    for command, _ in bm.calls:
        for secret in SECRETS:
            assert secret not in command
    _assert_secret_never_leaks(out, api)


def _wekafs() -> Any:
    """Return a freshly loaded ``common.wekafs`` (its program texts key FakeBm.programs)."""
    return load("storage/setup_mount.py")._common["wekafs"]


def _rw_ok(bm: FakeBm) -> None:
    bm.programs[_wekafs().RW_PROBE] = lambda _argv: {"ok": True, "bytes": 1 << 20}


# ── setup_mount ───────────────────────────────────────────────────────


def test_setup_mount_installs_a_missing_client_delivers_the_token_on_stdin_and_mounts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No client: install from the endpoint, token via stdin to the root token file, mount, probe."""
    storage = FakeStorage()
    bm = FakeBm(installed=False, weka_names=["team-home", f"isv-fs-{RUN_ID}-mount"])
    _rw_ok(bm)

    code, out, api = _run(monkeypatch, capsys, "setup_mount.py", storage, bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 0 and out["success"], out
    assert out["mounted"] is True and out["mount_point"] == MOUNT
    assert out["client_version"] == VERSION and out["client_installed"] is True
    assert out["fs_name"] == f"isv-fs-{RUN_ID}-mount" and out["fs_type"] == "wekafs"
    installs = [c for c in bm.commands() if "/dist/v1/install" in c]
    assert len(installs) == 1  # https succeeds first try, no http fallback needed
    assert "https://weka-backend.example:14000/dist/v1/install" in installs[0] and "sudo timeout 900 sh" in installs[0]
    assert bm.tokens == {TOKEN_PATH: TOKEN}
    token_calls = [(c, i) for c, i in bm.calls if c.startswith("sudo python3 -c ")]
    assert len(token_calls) == 1 and token_calls[0][1] == SECRET + "\n" and token_calls[0][0].endswith(TOKEN_PATH)
    assert bm.mounts[MOUNT] == (f"{BACKEND}/isv-fs-{RUN_ID}-mount", f"rw,relatime,net=udp,auth_token_path={TOKEN_PATH}")
    assert f"sudo env WEKA_TOKEN={TOKEN_PATH} weka fs -H {BACKEND} -J" in bm.commands()
    assert storage.names() == sorted(["team-home", "shared-data", f"isv-fs-{RUN_ID}-mount"])
    _assert_token_only_on_stdin(bm, out, api)
    _assert_dev_untouched(storage, api)


def test_setup_mount_falls_back_to_http_when_https_install_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """https on the endpoint's host:port is tried first; only a failure falls back to the API's own URL."""
    bm = FakeBm(installed=False, weka_names=[f"isv-fs-{RUN_ID}-mount"])
    _rw_ok(bm)
    bm.install_results["https://weka-backend.example:14000/dist/v1/install"] = (
        7,
        "",
        "curl: (7) Failed to connect to weka-backend.example port 14000",
    )
    bm.install_result = (0, "WekaIO CLI is now installed", "")  # the http fallback succeeds

    code, out, _ = _run(monkeypatch, capsys, "setup_mount.py", FakeStorage(), bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 0 and out["success"], out
    installs = [c for c in bm.commands() if "/dist/v1/install" in c]
    assert len(installs) == 2
    assert "https://weka-backend.example:14000/dist/v1/install" in installs[0]
    assert f"{ENDPOINT}/dist/v1/install" in installs[1]


def test_setup_mount_install_url_env_override_is_the_only_url_tried(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ISV_WEKA_INSTALL_URL, when set, replaces both the https and API-returned URLs."""
    bm = FakeBm(installed=False, weka_names=[f"isv-fs-{RUN_ID}-mount"])
    _rw_ok(bm)
    override = "https://internal-mirror.example/dist/v1/install"
    bm.install_results[override] = (0, "installed", "")
    monkeypatch.setenv("ISV_WEKA_INSTALL_URL", override)

    code, out, _ = _run(monkeypatch, capsys, "setup_mount.py", FakeStorage(), bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 0 and out["success"], out
    installs = [c for c in bm.commands() if "/dist/v1/install" in c]
    assert len(installs) == 1 and override in installs[0]


def test_setup_mount_reuses_an_installed_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An installed client is not reinstalled."""
    bm = FakeBm(installed=True, weka_names=[f"isv-fs-{RUN_ID}-mount"])
    _rw_ok(bm)

    code, out, _ = _run(monkeypatch, capsys, "setup_mount.py", FakeStorage(), bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 0 and out["mounted"] and out["client_installed"] is False
    assert not [c for c in bm.commands() if "/dist/v1/install" in c]


def test_setup_mount_reports_a_failed_install_redacted_and_does_not_mount(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed install fails the step with the installer's error, secrets masked, and mounts nothing."""
    bm = FakeBm(installed=False)
    bm.install_result = (
        7,
        "",
        f'curl: (7) Failed to connect; {{"access_token": "{TOKEN["access_token"]}"}} Bearer abc',
    )

    code, out, api = _run(monkeypatch, capsys, "setup_mount.py", FakeStorage(), bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 1 and not out["success"] and out["mounted"] is False and out["mount_point"] == ""
    assert "Weka client install failed (exit 7)" in out["error"] and "Failed to connect" in out["error"]
    assert "Bearer <redacted>" in out["error"]
    assert len([c for c in bm.commands() if "/dist/v1/install" in c]) == 2  # https, then the http fallback
    assert not [c for c in bm.commands() if "mount -t wekafs" in c] and not bm.tokens
    _assert_token_only_on_stdin(bm, out, api)


def test_setup_mount_reports_a_failed_mount(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mount error fails the step with the mount helper's (redacted) output."""
    bm = FakeBm(installed=True, weka_names=[f"isv-fs-{RUN_ID}-mount"])
    bm.mount_result = (32, "", f"error: Failed joining the cluster; token {SECRET}")

    code, out, api = _run(monkeypatch, capsys, "setup_mount.py", FakeStorage(), bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 1 and out["mounted"] is False and out["mount_point"] == ""
    assert "failed (exit 32)" in out["error"] and "Failed joining the cluster" in out["error"]
    _assert_token_only_on_stdin(bm, out, api)


def test_setup_mount_redacts_a_credential_the_bm_echoes_back_on_token_write_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the BM's token writer ever echoed the credential in its stderr, it never reaches the report or logs."""
    bm = FakeBm(installed=True, weka_names=[f"isv-fs-{RUN_ID}-mount"])
    bm.shell = [("sudo python3 -c ", lambda _c: (1, "", f"unexpected crash, stdin was: {SECRET}"))]

    code, out, api = _run(monkeypatch, capsys, "setup_mount.py", FakeStorage(), bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 1 and not out["success"]
    assert "could not write the Weka token on the BM" in out["error"]
    assert SECRET not in out["error"] and SECRET not in api.stderr
    _assert_token_only_on_stdin(bm, out, api)


def test_setup_mount_refuses_a_credential_that_is_not_a_client_auth_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The BM checks the decoded shape before writing; a wrong shape names keys only and nothing mounts."""
    storage = FakeStorage()
    other = {"user": "u", "password": "PASSWORD-SECRET-1"}
    blob = base64.b64encode(json.dumps(other).encode()).decode()
    bm = FakeBm(installed=True, weka_names=[f"isv-fs-{RUN_ID}-mount"])

    def swap(module: Any) -> None:
        original = module._fb.FirebirdClient._send

        def send(self: Any, method: str, path: str, body: Any, *, token: str | None) -> dict[str, Any]:
            response = original(method, path, body, token=token)
            if "authCredentialsBase64" in response:
                response = {**response, "authCredentialsBase64": blob}
            return response

        monkeypatch.setattr(module._fb.FirebirdClient, "_send", send)

    code, out, api = _run(monkeypatch, capsys, "setup_mount.py", storage, bm, ["--run-id", RUN_ID, *BM_ARGS], swap)

    assert code == 1 and "not a Weka auth token (keys: ['password', 'user'])" in out["error"]
    assert "PASSWORD-SECRET-1" not in repr(out) and blob not in repr(out) and blob not in api.stderr
    assert not bm.tokens and not bm.mounts


def test_setup_mount_fails_when_the_token_cannot_see_the_filesystem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The API name must be a filesystem the client token lists; otherwise nothing is mounted."""
    bm = FakeBm(installed=True, weka_names=["team-home"])

    code, out, _ = _run(monkeypatch, capsys, "setup_mount.py", FakeStorage(), bm, ["--run-id", RUN_ID, *BM_ARGS])

    assert code == 1 and "is not visible to the Weka token" in out["error"] and not bm.mounts


# ── provision_parallel_fs (HSS07-01) ──────────────────────────────────


def _mounted_bm(storage: FakeStorage, **kwargs: Any) -> FakeBm:
    """Return a BM with the client installed and the run's mount filesystem mounted at MOUNT."""
    storage.add("filesystem.M", f"isv-fs-{RUN_ID}-mount", 1, "2026-09-28T10:00:00Z")
    kwargs.setdefault("weka_names", [f"isv-fs-{RUN_ID}-mount"])
    bm = FakeBm(installed=True, **kwargs)
    bm.seed_mount(MOUNT, f"{BACKEND}/isv-fs-{RUN_ID}-mount")
    bm.tokens[TOKEN_PATH] = dict(TOKEN)
    return bm


def test_parallel_fs_passes_on_the_mounted_filesystem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """API readback, READY, and a mount probe pass HSS07-01 with the mount fields."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    _rw_ok(bm)
    argv = [*MOUNT_ARGS, "--fs-id", "filesystem.M", "--client-version", VERSION]

    code, out, _ = _run(monkeypatch, capsys, "parallel_fs_test.py", storage, bm, argv)

    assert code == 0 and out["success"], out
    mounted = out["tests"]["mount_successful"]
    assert mounted["passed"] and mounted["mounted"] is True
    assert mounted["mount_point"] == MOUNT and mounted["client_version"] == VERSION
    assert out["tests"]["filesystem_provisioned"] == {"passed": True, "fs_type": "wekafs", "capacity_gib": 1}
    check = validate("storage", HssParallelFsProvisioningCheck, out)
    assert check._passed, check._error


def test_parallel_fs_fails_mount_successful_with_the_setup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When setup_mount failed, mount_successful carries its (redacted) error and HSS07-01 fails."""
    storage = FakeStorage()
    storage.add("filesystem.M", f"isv-fs-{RUN_ID}-mount", 1, "2026-09-28T10:00:00Z")
    argv = [*BM_ARGS, "--mount-point", "", "--mount-error", f"mount failed: {SECRET}", "--fs-id", "filesystem.M"]

    code, out, _ = _run(monkeypatch, capsys, "parallel_fs_test.py", storage, FakeBm(), argv)

    assert code == 0
    assert out["tests"]["api_available"]["passed"] and out["tests"]["filesystem_provisioned"]["passed"]
    assert not out["tests"]["mount_successful"]["passed"]
    assert "mount failed: <redacted>" in out["tests"]["mount_successful"]["error"]
    check = validate("storage", HssParallelFsProvisioningCheck, out)
    assert not check._passed and "mount_successful" in check._error


# ── The I/O checks: gating ────────────────────────────────────────────

IO_SCRIPTS = (
    "qos_throughput_test.py",
    "quota_enforcement_test.py",
    "root_squash_test.py",
    "flock_mount_test.py",
    "changelog_audit_test.py",
    "multipath_test.py",
)


@pytest.mark.parametrize("script", IO_SCRIPTS)
def test_io_checks_fail_with_the_setup_error_when_nothing_is_mounted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], script: str
) -> None:
    """A BM without the mount fails every subtest with setup_mount's error instead of skipping."""
    argv = [*BM_ARGS, "--mount-point", "", "--mount-error", "Weka client install failed (exit 7)"]

    code, out, _ = _run(monkeypatch, capsys, script, FakeStorage(), FakeBm(), argv)

    assert code == 1 and not out.get("skipped")
    assert out["tests"] and all(not t["passed"] for t in out["tests"].values())
    assert all("Weka client install failed" in t["error"] for t in out["tests"].values())


@pytest.mark.parametrize("script", IO_SCRIPTS)
def test_io_checks_refuse_a_mount_point_outside_the_run_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], script: str
) -> None:
    """A mount point that is not /mnt/isv-<run_id> is never probed."""
    bm = FakeBm(installed=True)
    bm.seed_mount("/mnt/data", f"{BACKEND}/team-home")

    code, out, _ = _run(monkeypatch, capsys, script, FakeStorage(), bm, [*BM_ARGS, "--mount-point", "/mnt/data"])

    assert code == 1 and "refusing to probe" in out["error"]
    assert bm.commands() == []


# ── qos_throughput (HSS02-01) ─────────────────────────────────────────


def _qos(bm: FakeBm, module_holder: dict[str, Any], bench: dict[str, float]) -> Callable[[Any], None]:
    def setup(module: Any) -> None:
        bm.programs[module.BENCH] = lambda argv: (module_holder.setdefault("argv", argv), bench)[1]
        bm.programs[module._common["wekafs"].RW_PROBE] = lambda _a: {"ok": True}

    return setup


def test_qos_passes_when_the_measured_rates_reach_the_minimums(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """measured_mbps is the lower of write and read; both floors met pass HSS02-01."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["fs -H"] = [{"name": f"isv-fs-{RUN_ID}-mount", "max_throughput": 0, "max_iops": 0}]
    seen: dict[str, Any] = {}
    bench = {"write_mbps": 1500.0, "read_mbps": 1200.0, "iops": 61000.4, "bytes": 512 * (1 << 20)}

    code, out, _ = _run(monkeypatch, capsys, "qos_throughput_test.py", storage, bm, MOUNT_ARGS, _qos(bm, seen, bench))

    assert code == 0 and out["success"], out
    bw, iops = out["tests"]["bandwidth_meets_min"], out["tests"]["iops_meets_min"]
    assert bw["passed"] and bw["measured_mbps"] == 1200.0 and bw["min_mbps"] == 1000
    assert iops["passed"] and iops["measured_iops"] == 61000 and iops["min_iops"] == 50000
    assert "max_throughput=0 max_iops=0" in bw["message"]
    assert seen["argv"] == [MOUNT, "4", "128", "16", "15"]
    assert validate("storage", HssQosThroughputCheck, out)._passed


def test_qos_fails_rates_below_the_minimums(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rates under the floor fail with the measured value in the error."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["fs -H"] = (1, "", "error: not allowed")
    bench = {"write_mbps": 26.7, "read_mbps": 14.3, "iops": 3000, "bytes": 1}

    code, out, _ = _run(monkeypatch, capsys, "qos_throughput_test.py", storage, bm, MOUNT_ARGS, _qos(bm, {}, bench))

    assert code == 0
    assert "measured 14.3 MB/s, below the 1000 MB/s minimum" in out["tests"]["bandwidth_meets_min"]["error"]
    assert "measured 3000 IOPS, below the 50000 IOPS minimum" in out["tests"]["iops_meets_min"]["error"]
    check = validate("storage", HssQosThroughputCheck, out)
    assert not check._passed and "bandwidth_meets_min" in check._error


def test_qos_passes_at_exactly_the_minimum(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A rate exactly at the floor passes (pins >= over >, both floors)."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["fs -H"] = [{"name": f"isv-fs-{RUN_ID}-mount", "max_throughput": 0, "max_iops": 0}]
    bench = {"write_mbps": 1000.0, "read_mbps": 1000.0, "iops": 50000, "bytes": 1}

    code, out, _ = _run(monkeypatch, capsys, "qos_throughput_test.py", storage, bm, MOUNT_ARGS, _qos(bm, {}, bench))

    assert code == 0 and out["success"], out
    assert (
        out["tests"]["bandwidth_meets_min"]["passed"] and out["tests"]["bandwidth_meets_min"]["measured_mbps"] == 1000
    )
    assert out["tests"]["iops_meets_min"]["passed"] and out["tests"]["iops_meets_min"]["measured_iops"] == 50000


# ── quota_enforcement (HSS12-01) ──────────────────────────────────────


def test_quota_fails_identity_quotas_and_reports_a_refused_directory_quota(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """uid/gid are unsupported; a role that cannot set quotas fails the quota subtests with the role error the mount reports."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.shell = [("sudo mkdir ", (0, "", "")), ("sudo rm -rf ", (0, "", ""))]
    bm.weka["fs quota set"] = (1, "", 'error: Current user role is "ReadOnlyUser" but "TenantAdminUser" is required')
    bm.weka["user whoami"] = [{"role": "ReadOnly"}]

    code, out, _ = _run(monkeypatch, capsys, "quota_enforcement_test.py", storage, bm, MOUNT_ARGS)

    assert code == 0 and out["success"], out
    tests = out["tests"]
    for key in ("uid_quota_enforced", "gid_quota_enforced"):
        assert tests[key]["supported"] is False and "no per-uid or per-gid quota" in tests[key]["error"]
    for key in ("project_quota_enforced", "soft_quota_grace", "hard_quota_blocks"):
        assert "ReadOnlyUser" in tests[key]["error"] and "(token role ReadOnly)" in tests[key]["error"]
    cleanup = bm.commands()[-1]
    assert cleanup.startswith(f"sudo rm -rf {MOUNT}/.isv-quota-") and "quota unset" not in cleanup
    assert not validate("storage", HssQuotaEnforcementCheck, out)._passed


def test_quota_passes_the_directory_subtests_when_the_quota_is_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Over soft succeeds, past hard is refused; the quota is unset and the directory removed."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    writes = iter([(0, "", ""), (1, "", "dd: error writing: Disk quota exceeded")])
    bm.shell = [
        ("sudo mkdir ", (0, "", "")),
        ("sudo dd ", lambda _c: next(writes)),
        ("quota unset", (0, "", "")),
    ]
    bm.weka["fs quota set"] = (0, "", "")

    code, out, _ = _run(monkeypatch, capsys, "quota_enforcement_test.py", storage, bm, MOUNT_ARGS)

    assert code == 0, out
    for key in ("project_quota_enforced", "soft_quota_grace", "hard_quota_blocks"):
        assert out["tests"][key]["passed"], key
    assert "Disk quota exceeded" in out["tests"]["hard_quota_blocks"]["message"]
    cleanup = bm.commands()[-1]
    assert f"env WEKA_TOKEN={TOKEN_PATH} weka fs quota unset" in cleanup and "sudo rm -rf" in cleanup
    dd = next(c for c in bm.commands() if "sudo dd " in c)
    assert dd.startswith(f"findmnt -n -t wekafs -M {MOUNT} >/dev/null && sudo dd ")


def test_quota_hard_write_failing_without_edquot_is_not_counted_as_blocked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A generic dd failure past the hard limit (not EDQUOT) must not pass hard_quota_blocks."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    writes = iter([(0, "", ""), (1, "", "dd: error writing: Input/output error")])
    bm.shell = [
        ("sudo mkdir ", (0, "", "")),
        ("sudo dd ", lambda _c: next(writes)),
        ("quota unset", (0, "", "")),
    ]
    bm.weka["fs quota set"] = (0, "", "")

    code, out, _ = _run(monkeypatch, capsys, "quota_enforcement_test.py", storage, bm, MOUNT_ARGS)

    assert code == 0, out
    tests = out["tests"]
    assert not tests["hard_quota_blocks"]["passed"]
    assert "not with a quota error (EDQUOT)" in tests["hard_quota_blocks"]["error"]
    assert "Input/output error" in tests["hard_quota_blocks"]["error"]
    assert not tests["project_quota_enforced"]["passed"]
    assert not validate("storage", HssQuotaEnforcementCheck, out)._passed


# ── root_squash (HSS13-01) ────────────────────────────────────────────


def test_root_squash_reports_the_toggles_unsupported_and_root_unsquashed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Root's file owned by uid 0: root_unsquashed passes, root_squashed fails, toggles unsupported."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["user whoami"] = [{"role": "ReadOnly"}]

    def setup(module: Any) -> None:
        bm.programs[module.OWNER] = lambda argv: {"uid": 0, "gid": 0}

    code, out, _ = _run(monkeypatch, capsys, "root_squash_test.py", storage, bm, MOUNT_ARGS, setup)

    tests = out["tests"]
    assert code == 0 and tests["root_unsquashed"] == {
        "passed": True,
        "owner_uid": 0,
        "message": "root keeps uid 0 on the mount (squash off, the default)",
    }
    assert not tests["root_squashed"]["passed"] and "owned by uid 0" in tests["root_squashed"]["error"]
    for key in ("enable_root_squash", "disable_root_squash"):
        assert tests[key]["supported"] is False and "Weka role is ReadOnly" in tests[key]["error"]
    assert not validate("storage", HssRootSquashCheck, out)._passed


def test_root_squash_reads_the_role_with_the_runs_own_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`weka user whoami` runs with the run's own suite-owned token, never a bare unauthenticated CLI."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["user whoami"] = [{"role": "TenantAdmin"}]

    def setup(module: Any) -> None:
        bm.programs[module.OWNER] = lambda argv: {"uid": 0, "gid": 0}

    code, out, _ = _run(monkeypatch, capsys, "root_squash_test.py", storage, bm, MOUNT_ARGS, setup)

    assert code == 0, out
    assert f"sudo env WEKA_TOKEN={TOKEN_PATH} weka user whoami -J" in bm.commands()


# ── flock_mount (HSS14-01) ────────────────────────────────────────────

FLOCK_OK = {
    "exclusive": True,
    "ex_blocks_ex": True,
    "ex_blocks_sh": True,
    "ex_after_release": True,
    "shared": True,
    "sh_blocks_ex": True,
}


@pytest.mark.parametrize(
    ("probe", "failing"),
    [
        (FLOCK_OK, set()),
        ({**FLOCK_OK, "ex_blocks_ex": False}, {"flock_contention"}),
        ({**FLOCK_OK, "shared": False}, {"flock_shared"}),
        (
            {"exclusive": False, "error": "LOCK_EX refused: [Errno 38]"},
            {"mounted_with_flock", "flock_exclusive", "flock_shared", "flock_contention"},
        ),
    ],
)
def test_flock_maps_the_probe_to_its_subtests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], probe: dict[str, Any], failing: set[str]
) -> None:
    """Each broken lock property fails exactly its subtest."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)

    def setup(module: Any) -> None:
        bm.programs[module.FLOCK] = lambda _argv: probe

    code, out, _ = _run(monkeypatch, capsys, "flock_mount_test.py", storage, bm, MOUNT_ARGS, setup)

    assert code == 0
    assert {key for key, test in out["tests"].items() if not test["passed"]} == failing
    assert validate("storage", HssFlockMountCheck, out)._passed == (not failing)


def test_flock_fails_a_mount_that_disables_flock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mount option that disables flock fails mounted_with_flock."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.seed_mount(MOUNT, f"{BACKEND}/isv-fs-{RUN_ID}-mount", "rw,noflock")

    def setup(module: Any) -> None:
        bm.programs[module.FLOCK] = lambda _argv: FLOCK_OK

    _, out, _ = _run(monkeypatch, capsys, "flock_mount_test.py", storage, bm, MOUNT_ARGS, setup)

    assert out["tests"]["mounted_with_flock"] == {
        "passed": False,
        "mount_options": "rw,noflock",
        "error": "mounted with noflock",
    }


# ── changelog_audit (HSS15-01) ────────────────────────────────────────


@pytest.mark.parametrize("audited", [False, True])
def test_changelog_reads_the_audit_state_of_the_run_filesystem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], audited: bool
) -> None:
    """changelog_enabled follows the filesystem's audit flag; records fail either way, with the reason."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["audit fs status"] = [
        {"name": "team-home", "audit": True},
        {"name": f"isv-fs-{RUN_ID}-mount", "audit": audited, "audit_operations_str": "All"},
    ]
    bm.weka["user whoami"] = [{"role": "ReadOnly"}]

    code, out, _ = _run(monkeypatch, capsys, "changelog_audit_test.py", storage, bm, MOUNT_ARGS)

    tests = out["tests"]
    assert code == 0 and tests["changelog_enabled"]["passed"] is audited
    for key in ("records_file_ops", "records_dir_ops", "tracks_uid_gid"):
        assert not tests[key]["passed"]
        assert ("not exposed through the tenant API" if audited else "audit is disabled on filesystem") in tests[key][
            "error"
        ]
    assert f"sudo env WEKA_TOKEN={TOKEN_PATH} weka audit fs status -H {BACKEND} -J" in bm.commands()
    assert not validate("storage", HssChangelogAuditCheck, out)._passed


def test_changelog_reads_the_role_with_the_runs_own_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When audit is off, the role lookup also runs with the run's own suite-owned token."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["audit fs status"] = [{"name": f"isv-fs-{RUN_ID}-mount", "audit": False}]
    bm.weka["user whoami"] = [{"role": "TenantAdmin"}]

    code, out, _ = _run(monkeypatch, capsys, "changelog_audit_test.py", storage, bm, MOUNT_ARGS)

    assert code == 0, out
    assert f"sudo env WEKA_TOKEN={TOKEN_PATH} weka user whoami -J" in bm.commands()


# ── multipath (HSS18-01) ──────────────────────────────────────────────


def _containers(client_ips: list[str], down: str = "") -> list[dict[str, Any]]:
    backends = [
        {
            "mode": "backend",
            "hostname": host,
            "ips": ips,
            "mgmt_port": 14000,
            "status": "DOWN" if host == down else "UP",
        }
        for host, ips in (("node-1", ["10.9.0.1", "10.8.0.1"]), ("node-2", ["10.9.0.2", "10.8.0.2"]))
        for _ in range(2)  # two containers per server (drives, compute)
    ]
    return [*backends, {"mode": "client", "hostname": "bm", "ips": client_ips, "status": "UP"}]


@pytest.mark.parametrize(
    ("client_ips", "down", "reachable", "expect"),
    [
        ([BM_IP], "", True, {"multiple_paths": False, "all_servers_reachable": True}),
        ([BM_IP, "172.16.241.10"], "", True, {"multiple_paths": True, "all_servers_reachable": True}),
        ([BM_IP], "node-2", True, {"multiple_paths": False, "all_servers_reachable": False}),
        ([BM_IP], "", False, {"multiple_paths": False, "all_servers_reachable": False}),
    ],
)
def test_multipath_counts_paths_and_probes_every_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    client_ips: list[str],
    down: str,
    reachable: bool,
    expect: dict[str, bool],
) -> None:
    """path_count is the client's addresses; every server must be UP and reachable; failover is untested."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["cluster container"] = _containers(client_ips, down)
    seen: dict[str, Any] = {}

    def setup(module: Any) -> None:
        def reach(argv: list[str]) -> dict[str, bool]:
            seen["targets"] = json.loads(argv[0])
            return {name: reachable or name == "node-1" for name in seen["targets"]}

        bm.programs[module.REACH] = reach

    code, out, _ = _run(monkeypatch, capsys, "multipath_test.py", storage, bm, MOUNT_ARGS, setup)

    tests = out["tests"]
    assert code == 0
    assert {key: tests[key]["passed"] for key in expect} == expect
    assert tests["multiple_paths"]["path_count"] == len(client_ips)
    assert tests["all_servers_reachable"]["server_count"] == 2
    assert seen["targets"]["node-1"] == [["10.9.0.1", 14000], ["10.8.0.1", 14000]]  # deduplicated
    assert tests["failover_works"]["supported"] is False
    assert not validate("storage", HssMultipathCheck, out)._passed


def test_multipath_reads_containers_with_the_runs_own_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`weka cluster container` runs with the run's own suite-owned token, never a bare unauthenticated CLI."""
    storage = FakeStorage()
    bm = _mounted_bm(storage)
    bm.weka["cluster container"] = _containers([BM_IP])

    def setup(module: Any) -> None:
        bm.programs[module.REACH] = lambda argv: dict.fromkeys(json.loads(argv[0]), True)

    code, out, _ = _run(monkeypatch, capsys, "multipath_test.py", storage, bm, MOUNT_ARGS, setup)

    assert code == 0, out
    assert f"sudo env WEKA_TOKEN={TOKEN_PATH} weka cluster container -J" in bm.commands()


# ── home_directory_storage (DIR01-01, DIR01-02) ───────────────────────

ACCOUNTED = {
    "expected": {"a": [20001, 30001, 8 << 20], "b": [20002, 30002, 4 << 20]},
    "by_uid": {"20001": 8 << 20, "20002": 4 << 20},
    "by_gid": {"30001": 8 << 20, "30002": 4 << 20},
}


def _home(bm: FakeBm, capacities: list[int], fill: tuple[int, str, str]) -> Callable[[Any], None]:
    sizes = iter(capacities)

    def setup(module: Any) -> None:
        wekafs = module._common["wekafs"]
        bm.programs[module.ACCOUNTING] = lambda argv: ACCOUNTED
        last = {"size": capacities[0]}

        def stat(argv: list[str]) -> dict[str, int]:
            last["size"] = next(sizes, last["size"])
            return {"total_bytes": last["size"], "free_bytes": last["size"], "files": 1000, "files_free": 900}

        bm.programs[wekafs.STATVFS] = stat
        bm.shell = [("sudo dd ", fill), ("sudo rm -f ", (0, "", ""))]

    return setup


def test_home_directory_configures_updates_and_enforces_its_quota_then_cleans_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 GiB reported, 2 GiB after the resize, ENOSPC past it; accounting exact; unmount and delete."""
    storage = FakeStorage()
    bm = _mounted_bm(storage, weka_names=[f"isv-fs-{RUN_ID}-mount", f"isv-fs-{RUN_ID}-dir"])
    fill = (1, f"{2 * GIB}\n", "dd: error writing '/mnt/x/fill': No space left on device")
    setup = _home(bm, [GIB, GIB, 2 * GIB], fill)

    code, out, api = _run(
        monkeypatch, capsys, "home_directory_storage_test.py", storage, bm, ["--run-id", RUN_ID, *MOUNT_ARGS], setup
    )

    assert code == 0 and out["success"], out
    tests = out["tests"]
    assert (
        tests["filesystem_quota_configured"]["passed"] and tests["filesystem_quota_configured"]["reported_bytes"] == GIB
    )
    assert (
        tests["filesystem_quota_updated"]["passed"] and tests["filesystem_quota_updated"]["reported_bytes"] == 2 * GIB
    )
    assert tests["filesystem_quota_enforced"] == {
        "passed": True,
        "written_bytes": 2 * GIB,
        "limit_bytes": 2 * GIB,
        "message": "dd: error writing '/mnt/x/fill': No space left on device",
    }
    dd = next(c for c in bm.commands() if "sudo dd " in c)
    assert f"of=/mnt/isv-{RUN_ID}-dir/fill" in dd and f"count={2048 + 64}" in dd
    assert dd.startswith(f"findmnt -n -t wekafs -M /mnt/isv-{RUN_ID}-dir >/dev/null && sudo dd ")
    assert validate("storage", DirectoryFilesystemQuotaCheck, out)._passed
    assert validate("storage", DirectoryUsageAccountingCheck, out)._passed
    assert f"/mnt/isv-{RUN_ID}-dir" not in bm.mounts and f"/mnt/isv-{RUN_ID}-dir" not in bm.dirs
    assert ("PUT", {"name": f"isv-fs-{RUN_ID}-dir", "capacity": {"size": 2, "sizeUnit": "GiB"}}) in storage.bodies
    assert f"isv-fs-{RUN_ID}-dir" not in storage.names()
    _assert_token_only_on_stdin(bm, out, api)
    _assert_dev_untouched(storage, api)


def test_home_directory_fails_an_update_the_mount_never_shows_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resize the client never sees fails filesystem_quota_updated; a write that fits fails enforcement."""
    storage = FakeStorage()
    bm = _mounted_bm(storage, weka_names=[f"isv-fs-{RUN_ID}-mount", f"isv-fs-{RUN_ID}-dir"])
    setup = _home(bm, [GIB], (0, f"{GIB + (64 << 20)}\n", ""))

    code, out, _ = _run(
        monkeypatch, capsys, "home_directory_storage_test.py", storage, bm, ["--run-id", RUN_ID, *MOUNT_ARGS], setup
    )

    tests = out["tests"]
    assert code == 0
    assert tests["filesystem_quota_configured"]["passed"]
    assert "still reports 1073741824 bytes" in tests["filesystem_quota_updated"]["error"]
    assert "was not refused with ENOSPC" in tests["filesystem_quota_enforced"]["error"]
    assert not validate("storage", DirectoryFilesystemQuotaCheck, out)._passed
    assert f"isv-fs-{RUN_ID}-dir" not in storage.names() and f"/mnt/isv-{RUN_ID}-dir" not in bm.mounts


def test_home_directory_enforcement_ignores_a_non_enospc_dd_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dd failure that is not ENOSPC (a transient I/O error) must not pass filesystem_quota_enforced."""
    storage = FakeStorage()
    bm = _mounted_bm(storage, weka_names=[f"isv-fs-{RUN_ID}-mount", f"isv-fs-{RUN_ID}-dir"])
    fill = (1, "0\n", "dd: error writing '/mnt/x/fill': Input/output error")
    setup = _home(bm, [GIB, GIB, 2 * GIB], fill)

    code, out, _ = _run(
        monkeypatch, capsys, "home_directory_storage_test.py", storage, bm, ["--run-id", RUN_ID, *MOUNT_ARGS], setup
    )

    tests = out["tests"]
    assert code == 0
    assert not tests["filesystem_quota_enforced"]["passed"]
    assert "was not refused with ENOSPC" in tests["filesystem_quota_enforced"]["error"]
    assert "Input/output error" in tests["filesystem_quota_enforced"]["error"]


def test_home_directory_fails_when_accounting_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per-owner totals that do not match what each owner wrote fail DIR01-02."""
    storage = FakeStorage()
    bm = _mounted_bm(storage, weka_names=[f"isv-fs-{RUN_ID}-mount", f"isv-fs-{RUN_ID}-dir"])
    setup = _home(bm, [GIB, 2 * GIB], (1, "0\n", "No space left on device"))
    wrong = {**ACCOUNTED, "by_uid": {"0": 12 << 20}}

    def with_wrong(module: Any) -> None:
        setup(module)
        bm.programs[module.ACCOUNTING] = lambda argv: wrong

    _, out, _ = _run(
        monkeypatch,
        capsys,
        "home_directory_storage_test.py",
        storage,
        bm,
        ["--run-id", RUN_ID, *MOUNT_ARGS],
        with_wrong,
    )

    assert not out["tests"]["uid_usage_accounted"]["passed"] and out["tests"]["gid_usage_accounted"]["passed"]
    assert not out["tests"]["identity_usage_isolated"]["passed"]
    assert not validate("storage", DirectoryUsageAccountingCheck, out)._passed


# ── live_expansion with a BM (HSS10-01) ───────────────────────────────


def _live(bm: FakeBm, io: dict[str, Any], files: list[int]) -> Callable[[Any], None]:
    counts = iter(files)

    def setup(module: Any) -> None:
        wekafs = module._common["wekafs"]
        bm.programs[wekafs.STATVFS] = lambda argv: {
            "total_bytes": GIB,
            "free_bytes": GIB,
            "files": next(counts),
            "files_free": 1,
        }
        staged: dict[str, str] = {}

        def stage(command: str) -> tuple[int, str, str]:
            staged["script"] = bm.calls[-1][1] or ""
            return 0, "", ""

        bm.shell = [
            ("sudo tee ", stage),
            ("setsid nohup python3", (0, "", "")),
            ("sudo touch ", (0, "", "")),
            (f"sudo cat {bm.workdir}/io.json", (0, json.dumps(io), "")),
            ("sleep 1; sudo rm -rf", (0, "", "")),
        ]
        bm.staged = staged  # type: ignore[attr-defined]

    return setup


def test_live_expansion_with_a_bm_watches_inodes_and_io_through_the_resize(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mounted: more inodes after the resize and I/O without error or stall pass all of HSS10-01."""
    storage = FakeStorage()
    bm = FakeBm(installed=True, weka_names=[f"isv-fs-{RUN_ID}-hss10"])
    events: list[str] = []
    bm.events = events
    setup = _live(bm, {"ops": 240, "errors": 0, "max_gap_s": 0.4, "first_error": ""}, [1000, 2000])

    code, out, api = _run(
        monkeypatch, capsys, "live_expansion_test.py", storage, bm, ["--run-id", RUN_ID, *BM_ARGS], setup
    )

    tests = out["tests"]
    assert code == 0 and out["success"], out
    assert tests["inodes_expanded"] == {"passed": True, "files_before": 1000, "files_after": 2000}
    assert tests["io_uninterrupted"] == {"passed": True, "ops": 240, "errors": 0, "max_gap_s": 0.4}
    check = validate("storage", HssLiveExpansionCheck, out)
    assert check._passed, check._error
    assert events == [f"mount /mnt/isv-{RUN_ID}-hss10", f"umount /mnt/isv-{RUN_ID}-hss10"]
    assert 'open(path, "wb")' in bm.staged["script"]  # type: ignore[attr-defined]
    assert any(c.startswith("sudo mktemp -d /root/isv-hss10-io.") for c in bm.commands())
    assert any(c.startswith(f"sudo tee {bm.workdir}/io.py") for c in bm.commands())
    assert any(c.startswith(f"sudo rm -f {bm.workdir}/io.json") for c in bm.commands())
    assert not any("/tmp/isv-" in c for c in bm.commands())
    assert storage.deleted == ["filesystem.1"]
    _assert_token_only_on_stdin(bm, out, api)


def test_live_expansion_with_a_bm_fails_io_errors_and_flat_inodes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An I/O error during the resize fails io_uninterrupted; an unchanged inode count fails inodes_expanded."""
    storage = FakeStorage()
    bm = FakeBm(installed=True, weka_names=[f"isv-fs-{RUN_ID}-hss10"])
    setup = _live(
        bm, {"ops": 10, "errors": 2, "max_gap_s": 45.0, "first_error": "OSError: [Errno 5] EIO"}, [1000, 1000]
    )

    code, out, _ = _run(
        monkeypatch, capsys, "live_expansion_test.py", storage, bm, ["--run-id", RUN_ID, *BM_ARGS], setup
    )

    tests = out["tests"]
    assert code == 0 and out["success"]  # the API subtests passed
    assert "1000 inodes after the resize, 1000 before" in tests["inodes_expanded"]["error"]
    assert "2 I/O error(s) during the resize, first: OSError: [Errno 5] EIO" in tests["io_uninterrupted"]["error"]
    assert not validate("storage", HssLiveExpansionCheck, out)._passed
    assert f"/mnt/isv-{RUN_ID}-hss10" not in bm.mounts and storage.deleted == ["filesystem.1"]


def test_live_expansion_reports_a_failed_mount_and_still_checks_the_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mount that fails leaves the API subtests intact and fails the mount ones with the error."""
    storage = FakeStorage()
    bm = FakeBm(installed=True, weka_names=[])

    code, out, _ = _run(monkeypatch, capsys, "live_expansion_test.py", storage, bm, ["--run-id", RUN_ID, *BM_ARGS])

    tests = out["tests"]
    assert code == 0 and tests["capacity_expanded"]["passed"], out
    for key in ("inodes_expanded", "io_uninterrupted"):
        assert "could not watch the resize from a mount" in tests[key]["error"]
    assert storage.deleted == ["filesystem.1"]


# ── teardown and sweep_leftovers on the BM ────────────────────────────


def test_teardown_unmounts_the_runs_mounts_removes_the_token_then_deletes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only /mnt/isv-<run_id>* is released, before any filesystem delete; the client stays installed."""
    storage = FakeStorage()
    storage.add("filesystem.M", f"isv-fs-{RUN_ID}-mount", 1, "2026-09-28T10:00:00Z")
    bm = FakeBm(installed=True)
    bm.seed_tenant_token()
    bm.tokens[TOKEN_PATH] = dict(TOKEN)
    bm.seed_mount(MOUNT, f"{BACKEND}/isv-fs-{RUN_ID}-mount")
    bm.seed_mount("/mnt/isv-ffffff", f"{BACKEND}/isv-fs-ffffff-mount")
    bm.seed_mount("/mnt/data", f"{BACKEND}/team-home")
    bm.dirs.add(f"/mnt/isv-{RUN_ID}-dir")  # an empty leftover mount point
    events: list[str] = []
    bm.events = events

    def track(module: Any) -> None:
        original = module._fb.FirebirdClient._send

        def send(self: Any, method: str, path: str, body: Any, *, token: str | None) -> dict[str, Any]:
            if method == "DELETE":
                events.append(f"DELETE {path.rsplit('/', 1)[-1]}")
            return original(method, path, body, token=token)

        monkeypatch.setattr(module._fb.FirebirdClient, "_send", send)

    code, out, api = _run(monkeypatch, capsys, "teardown.py", storage, bm, [f"--run-id={RUN_ID}", *BM_ARGS], track)

    assert code == 0 and out["success"], out
    assert events == [f"umount {MOUNT}", f"token removed:{TOKEN_PATH}", "DELETE filesystem.M"]
    assert set(bm.mounts) == {"/mnt/isv-ffffff", "/mnt/data"} and bm.installed
    assert TOKEN_PATH not in bm.tokens and bm.tenant_token is not None  # the tenant's own token is never touched
    assert f"/mnt/isv-{RUN_ID}-dir" not in bm.dirs
    assert out["resources_deleted"][:3] == [
        f"mount:{MOUNT}",
        f"mount-point:/mnt/isv-{RUN_ID}-dir",
        f"token:{TOKEN_PATH}",
    ]
    assert out["message"] == f"Deleted 1 filesystem(s) left by run {RUN_ID}"
    _assert_dev_untouched(storage, api)


def test_teardown_fails_when_an_unmount_fails_but_still_deletes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy mount is reported failed; the filesystems are still deleted."""
    storage = FakeStorage()
    storage.add("filesystem.M", f"isv-fs-{RUN_ID}-mount", 1, "2026-09-28T10:00:00Z")
    bm = FakeBm(installed=True)
    bm.seed_mount(MOUNT, f"{BACKEND}/isv-fs-{RUN_ID}-mount")
    bm.shell = [("umount ", (32, "", "umount: target is busy"))]

    code, out, _ = _run(monkeypatch, capsys, "teardown.py", storage, bm, [f"--run-id={RUN_ID}", *BM_ARGS])

    assert code == 1 and any("target is busy" in f for f in out["resources_failed"])
    assert "filesystem.M" in storage.deleted


def test_sweep_unmounts_only_stale_run_mounts_before_deleting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mounts of old or vanished isv-fs-* filesystems go; a young run's mount and token, and the tenant's, stay."""
    from .test_firebird_storage import _ts

    storage = FakeStorage()
    storage.add("filesystem.OLD", "isv-fs-0a0a0a-mount", 1, _ts(8))
    storage.add("filesystem.NEW", "isv-fs-0b0b0b-mount", 1, _ts(1))
    bm = FakeBm(installed=True)
    bm.seed_tenant_token()
    bm.tokens["/root/.weka/isv-0a0a0a.json"] = dict(TOKEN)
    bm.tokens["/root/.weka/isv-0b0b0b.json"] = dict(TOKEN)
    bm.seed_mount("/mnt/isv-0a0a0a", f"{BACKEND}/isv-fs-0a0a0a-mount")
    bm.seed_mount("/mnt/isv-0c0c0c", f"{BACKEND}/isv-fs-0c0c0c-mount")  # its filesystem is gone
    bm.seed_mount("/mnt/isv-0b0b0b", f"{BACKEND}/isv-fs-0b0b0b-mount")
    bm.seed_mount("/mnt/isv-0d0d0d", f"{BACKEND}/team-home")  # not a provider filesystem
    bm.seed_mount("/home/shared", f"{BACKEND}/isv-fs-0a0a0a-mount")  # not a provider mount point

    code, out, api = _run(monkeypatch, capsys, "sweep_leftovers.py", storage, bm, BM_ARGS)

    assert code == 0 and out["success"]
    assert set(bm.mounts) == {"/mnt/isv-0b0b0b", "/mnt/isv-0d0d0d", "/home/shared"}
    assert "/root/.weka/isv-0a0a0a.json" not in bm.tokens  # its run's mount was stale
    assert "/root/.weka/isv-0b0b0b.json" in bm.tokens  # still mounted (a younger run)
    assert bm.tenant_token is not None  # the tenant's own token is never touched
    assert out["resources_deleted"][:3] == [
        "mount:/mnt/isv-0a0a0a",
        "mount:/mnt/isv-0c0c0c",
        "token:/root/.weka/isv-0a0a0a.json",
    ]
    assert storage.deleted == ["filesystem.OLD"]
    _assert_dev_untouched(storage, api)


def test_sweep_removes_a_lone_leftover_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no wekafs mount left on the BM, a killed run's isv- token is removed; the tenant's own is untouched."""
    bm = FakeBm(installed=True)
    bm.seed_tenant_token()
    bm.tokens["/root/.weka/isv-0a0a0a.json"] = dict(TOKEN)

    code, out, _ = _run(monkeypatch, capsys, "sweep_leftovers.py", FakeStorage(), bm, BM_ARGS)

    assert code == 0
    assert "/root/.weka/isv-0a0a0a.json" not in bm.tokens
    assert bm.tenant_token is not None  # the suite never touches the tenant's own token
    assert out["resources_deleted"] == ["token:/root/.weka/isv-0a0a0a.json"]


def test_sweep_and_teardown_touch_no_bm_without_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without BM_INSTANCE_ID/BM_KEY_FILE neither sweep nor teardown opens SSH (the harness would fail it)."""
    code, out, _ = run(monkeypatch, capsys, "storage/teardown.py", FakeStorage().routes(), [f"--run-id={RUN_ID}"])
    assert code == 0 and out["success"]
    code, out, _ = run(monkeypatch, capsys, "storage/sweep_leftovers.py", FakeStorage().routes(), [])
    assert code == 0 and out["success"]


def _call_args(text: str, fn_name: str) -> list[str]:
    """Return the raw (unparsed) argument text of every top-level call to ``fn_name(...)`` in ``text``."""
    calls = []
    start = 0
    needle = fn_name + "("
    while (idx := text.find(needle, start)) != -1:
        open_paren = idx + len(fn_name)
        depth, i = 0, open_paren
        for i in range(open_paren, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
        calls.append(text[open_paren + 1 : i])
        start = i + 1
    return calls


def test_every_weka_json_and_token_role_call_in_storage_scripts_carries_a_token_path() -> None:
    """Regression guard (HSS13-01/HSS15-01/HSS18-01): every ``weka_json``/``token_role`` call under
    ``scripts/storage/`` must pass the run's own ``token_path`` - never fall back to a bare,
    unauthenticated ``sudo weka ...`` that could answer from the wrong (or no) backend identity."""
    offenders = []
    for path in sorted(SCRIPTS.glob("storage/*.py")):
        text = path.read_text()
        for fn_name in ("weka_json", "token_role"):
            for args in _call_args(text, fn_name):
                if "token_path" not in args:
                    offenders.append(f"{path.relative_to(SCRIPTS)}: {fn_name}({args.strip()})")
    assert not offenders, offenders


# ── wekafs helpers ────────────────────────────────────────────────────


def test_redact_masks_the_blob_token_fields_and_bearer_tokens() -> None:
    """Secrets given, JSON token fields, key=value passwords, and bearer tokens are masked."""
    wekafs = _wekafs()
    text = (
        f'blob {SECRET} {{"access_token": "{TOKEN["access_token"]}", "refresh_token": "r"}} '
        "password=hunter2 Authorization: Bearer eyJabc"
    )

    out = wekafs.redact(text, SECRET)

    for secret in (SECRET, TOKEN["access_token"], '"r"', "hunter2", "eyJabc"):
        assert secret not in out
    assert out.count("<redacted>") == 5
    assert wekafs.redact("error: role is ReadOnly\n\x00 ") == "error: role is ReadOnly"


@pytest.mark.parametrize(
    ("endpoint", "host", "url"),
    [
        ("http://10.0.0.5:14000", "10.0.0.5", "http://10.0.0.5:14000/dist/v1/install"),
        ("https://weka.example:14000/", "weka.example", "https://weka.example:14000/dist/v1/install"),
        ("weka.example:14000", "weka.example", "http://weka.example:14000/dist/v1/install"),
    ],
)
def test_endpoint_parsing(endpoint: str, host: str, url: str) -> None:
    """The backend host and installer URL come from the API's storageEndpoint."""
    wekafs = _wekafs()
    assert wekafs.backend_host(endpoint) == host
    assert wekafs.install_url(endpoint) == url


@pytest.mark.parametrize("endpoint", ["", "ftp://weka:21", "http://we ka:1", "http://$(reboot):1"])
def test_endpoint_parsing_refuses_unusable_values(endpoint: str) -> None:
    """An endpoint without a plain host (or not http[s]) is refused rather than put in a command."""
    wekafs = _wekafs()
    with pytest.raises(ValueError):
        wekafs.install_url(endpoint)


@pytest.mark.parametrize(
    ("path", "run_id", "owned"),
    [
        (f"/mnt/isv-{RUN_ID}", RUN_ID, True),
        (f"/mnt/isv-{RUN_ID}-dir", RUN_ID, True),
        (f"/mnt/isv-{RUN_ID}-dir", "", True),
        ("/mnt/isv-ffffff", RUN_ID, False),
        ("/mnt/data", "", False),
        (f"/mnt/isv-{RUN_ID}/../etc", "", False),
        (f"/mnt/isv-{RUN_ID}x", RUN_ID, False),
    ],
)
def test_only_run_mount_points_are_ever_unmounted(path: str, run_id: str, owned: bool) -> None:
    """The mount-point guard accepts /mnt/isv-<run_id>[-role] only."""
    assert _wekafs().is_run_mount_point(path, run_id) is owned


def test_unmount_refuses_a_foreign_path() -> None:
    """unmount raises before any SSH for a path that is not a run mount point."""
    wekafs = _wekafs()
    with pytest.raises(ValueError, match="refusing to unmount"):
        wekafs.unmount(object(), "/mnt/data")


def test_ssh_run_sends_input_on_stdin_not_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh_run passes input_text to the process's stdin; the command line never carries it."""
    ssh_utils = load("storage/setup_mount.py")._common["ssh_utils"]
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ssh_utils.subprocess, "run", fake_run)
    assert ssh_utils.ssh_run("h", "u", "/k", "sudo python3 -c prog", input_text=SECRET)[0] == 0
    assert seen["kwargs"]["input"] == SECRET and "stdin" in seen["kwargs"] and seen["kwargs"]["stdin"] is None
    assert SECRET not in " ".join(seen["argv"])
    ssh_utils.ssh_run("h", "u", "/k", "true")
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL and seen["kwargs"]["input"] is None


# ── The BM programs, run for real on this host ────────────────────────


def _python(program: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", program, *args], input=stdin, capture_output=True, text=True, timeout=60, check=False
    )


def test_token_writer_writes_a_0600_token_and_prints_key_names_only(tmp_path: Path) -> None:
    """TOKEN_WRITER stores the decoded token 0600 in a 0700 directory and echoes no value."""
    wekafs = _wekafs()
    target = tmp_path / ".weka" / "isv-a1b2c3.json"

    done = _python(wekafs.TOKEN_WRITER, str(target), stdin=SECRET + "\n")

    assert done.returncode == 0, done.stderr
    assert json.loads(target.read_text()) == TOKEN
    assert target.stat().st_mode & 0o777 == 0o600 and target.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(done.stdout) == {"keys": sorted(TOKEN)}
    for secret in SECRETS:
        assert secret not in done.stdout + done.stderr


@pytest.mark.parametrize(
    ("stdin", "message"),
    [
        ("not base64!", "not base64 JSON"),
        (base64.b64encode(b'{"password": "PASSWORD-SECRET-1"}').decode(), "keys: ['password']"),
        (base64.b64encode(b'["x"]').decode(), "keys: list"),
    ],
)
def test_token_writer_refuses_other_shapes_without_echoing_values(tmp_path: Path, stdin: str, message: str) -> None:
    """A credential that is not the auth-token JSON is refused, naming the shape only; nothing is written."""
    wekafs = _wekafs()
    target = tmp_path / "isv-a1b2c3.json"
    done = _python(wekafs.TOKEN_WRITER, str(target), stdin=stdin)

    assert done.returncode != 0 and message in done.stderr
    assert "PASSWORD-SECRET-1" not in done.stderr and not target.exists()


def test_probe_programs_run_on_a_local_directory(tmp_path: Path) -> None:
    """RW_PROBE, STATVFS, FLOCK, and OWNER work on a real filesystem and leave nothing behind."""
    wekafs = _wekafs()
    flock = load("storage/flock_mount_test.py")
    owner = load("storage/root_squash_test.py")

    assert json.loads(_python(wekafs.RW_PROBE, str(tmp_path)).stdout) == {"ok": True, "bytes": 1 << 20}
    assert json.loads(_python(wekafs.STATVFS, str(tmp_path)).stdout)["total_bytes"] > 0
    assert json.loads(_python(flock.FLOCK, str(tmp_path)).stdout) == FLOCK_OK
    assert json.loads(_python(owner.OWNER, str(tmp_path)).stdout)["uid"] == os.getuid()
    assert list(tmp_path.iterdir()) == []


def test_io_loop_runs_until_the_stop_file_and_reports(tmp_path: Path) -> None:
    """IO_LOOP completes operations without error and writes its summary atomically."""
    live = load("storage/live_expansion_test.py")
    out = tmp_path / "io.json"

    done = _python(live.IO_LOOP, str(tmp_path), str(out), "1")

    assert done.returncode == 0, done.stderr
    report = json.loads(out.read_text())
    assert report["ops"] > 0 and report["errors"] == 0 and report["first_error"] == ""


def test_reach_program_tells_an_open_port_from_a_closed_one() -> None:
    """REACH reports a server reachable when one of its addresses accepts a TCP connection."""
    multipath = load("storage/multipath_test.py")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    try:
        targets = {
            "up": [["127.0.0.1", closed_port], ["127.0.0.1", listener.getsockname()[1]]],
            "down": [["127.0.0.1", closed_port]],
        }
        done = _python(multipath.REACH, json.dumps(targets))
    finally:
        listener.close()

    assert json.loads(done.stdout) == {"up": True, "down": False}
