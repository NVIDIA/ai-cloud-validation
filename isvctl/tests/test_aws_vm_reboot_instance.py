# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for AWS VM reboot helper behavior."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import load_vm_script


def test_get_uptime_via_ssh_uses_shared_ssh_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uptime sampling should inherit the shared explicit-key SSH options."""
    module = load_vm_script("reboot_instance.py")
    calls: list[dict[str, Any]] = []

    def fake_ssh_run(
        host: str,
        user: str,
        key_file: str,
        command: str,
        *,
        timeout: int,
        connect_timeout: int,
    ) -> tuple[int, str, str]:
        """Capture the shared SSH helper invocation."""
        calls.append(
            {
                "host": host,
                "user": user,
                "key_file": key_file,
                "command": command,
                "timeout": timeout,
                "connect_timeout": connect_timeout,
            }
        )
        return 0, "12.34\n", ""

    monkeypatch.setattr(module, "ssh_run", fake_ssh_run, raising=False)
    if hasattr(module, "subprocess"):

        def fail_direct_subprocess_run(*_args: Any, **_kwargs: Any) -> None:
            """Fail if uptime sampling bypasses the shared SSH helper."""
            raise AssertionError("direct subprocess SSH should not be used")

        monkeypatch.setattr(module.subprocess, "run", fail_direct_subprocess_run)

    assert module.get_uptime_via_ssh("203.0.113.10", "ubuntu", "/tmp/key.pem") == 12.34
    assert calls == [
        {
            "host": "203.0.113.10",
            "user": "ubuntu",
            "key_file": "/tmp/key.pem",
            "command": "cat /proc/uptime | cut -d' ' -f1",
            "timeout": 30,
            "connect_timeout": 10,
        }
    ]
