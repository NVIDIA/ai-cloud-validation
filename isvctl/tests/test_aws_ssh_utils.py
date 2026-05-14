# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for AWS provider SSH helpers."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_COMMON_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "common"


def _load_ssh_utils() -> ModuleType:
    """Load the AWS SSH utilities module for direct helper testing."""
    script_path = AWS_COMMON_SCRIPTS / "ssh_utils.py"
    spec = importlib.util.spec_from_file_location("test_aws_ssh_utils_module", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ssh_run_uses_only_the_explicit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh_run should not offer local SSH agent identities before the requested key."""
    module = _load_ssh_utils()
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Capture the SSH command."""
        captured_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    exit_code, stdout, stderr = module.ssh_run("host", "user", "key.pem", "true")

    assert exit_code == 0
    assert stdout == "ok"
    assert stderr == ""
    assert len(captured_cmds) == 1
    assert "-i" in captured_cmds[0]
    assert captured_cmds[0][captured_cmds[0].index("-i") + 1] == "key.pem"
    assert "IdentitiesOnly=yes" in captured_cmds[0]
    assert "IdentityAgent=none" in captured_cmds[0]


def test_wait_for_ssh_uses_only_the_explicit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """wait_for_ssh should also avoid unrelated SSH agent identities."""
    module = _load_ssh_utils()
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Capture the SSH readiness command."""
        captured_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.wait_for_ssh("host", "user", "key.pem", max_attempts=1, interval=0)
    assert len(captured_cmds) == 1
    assert "-i" in captured_cmds[0]
    assert captured_cmds[0][captured_cmds[0].index("-i") + 1] == "key.pem"
    assert "IdentitiesOnly=yes" in captured_cmds[0]
    assert "IdentityAgent=none" in captured_cmds[0]


def test_ssh_run_returns_tuple_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeouts return the ssh_run tuple contract instead of raising."""
    module = _load_ssh_utils()

    def fake_run(*_args: Any, **_kwargs: Any) -> None:
        """Raise a subprocess timeout."""
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    exit_code, stdout, stderr = module.ssh_run("host", "user", "key.pem", "true")

    assert exit_code == 124
    assert stdout == ""
    assert "TimeoutExpired:" in stderr


def test_ssh_run_returns_tuple_on_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn failures return the ssh_run tuple contract instead of raising."""
    module = _load_ssh_utils()

    def fake_run(*_args: Any, **_kwargs: Any) -> None:
        """Raise an operating system error."""
        raise OSError("ssh missing")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    exit_code, stdout, stderr = module.ssh_run("host", "user", "key.pem", "true")

    assert exit_code == 255
    assert stdout == ""
    assert "OSError: ssh missing" in stderr
