# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the broadened ContainerRuntimeCheck.

Covers all five runtime detection levels:
  Level 1: docker  + GPU container runs
  Level 2: nerdctl + GPU container runs
  Level 3: containerd + nvidia-container-runtime installed (GPU operator proves capability)
  Level 4: runc    + nvidia-container-runtime installed
  Level 5: crun    + nvidia-container-runtime installed
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from isvtest.validations.host import ContainerRuntimeCheck


def _make_check(config: dict[str, Any] | None = None) -> ContainerRuntimeCheck:
    """Return a ContainerRuntimeCheck instance with an empty or provided config."""
    return ContainerRuntimeCheck(config=config or {})


def _patched_run(
    check: ContainerRuntimeCheck,
    command_map: dict[str, str],
    ngc_key: str = "",
) -> ContainerRuntimeCheck:
    """Run the check with command-aware mocked SSH responses.

    Args:
        check: The ContainerRuntimeCheck instance to run.
        command_map: Maps command substrings to their mocked stdout responses.
            The first key whose substring appears in the SSH command wins.
            Use ``"__default__"`` as a catch-all for unmatched commands.
        ngc_key: Optional NGC API key to inject into the check config.
    """
    cfg = dict(check.config)
    cfg["ngc_api_key"] = ngc_key
    check = ContainerRuntimeCheck(config=cfg)

    def _fake_ssh(ssh: object, cmd: str) -> tuple[int, str, str]:
        """Match the SSH command against command_map patterns; fall back to __default__."""
        default = command_map.get("__default__", "")
        for pattern, response in command_map.items():
            if pattern != "__default__" and pattern in cmd:
                return 0, response, ""
        return 0, default, ""

    with (
        patch(
            "isvtest.validations.host.get_ssh_config",
            return_value={
                "ssh_host": "10.0.0.1",
                "ssh_user": "ubuntu",
                "ssh_key_path": "/tmp/key.pem",
            },
        ),
        patch("isvtest.validations.host.get_ssh_client", return_value=MagicMock()),
        patch("isvtest.validations.host.run_ssh_command", side_effect=_fake_ssh),
    ):
        check.run()

    return check


# ---------------------------------------------------------------------------
# Level 1 — Docker
# ---------------------------------------------------------------------------


class TestDockerLevel:
    def test_passes_with_docker_and_gpu_container(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "Docker version 24.0.0",
                "docker run": "NVIDIA-SMI 595 ...",
            },
        )
        assert check.passed
        assert "docker" in check.message

    def test_docker_gpu_fails_falls_through_to_containerd(self) -> None:
        """Docker GPU fails but containerd + GPU operator present → PASS at level 3."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "Docker version 24.0.0",
                "docker run": "__gpu_run_failed__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "nvidia-container-runtime --version": "NVIDIA Container Runtime 1.19.0",
                "containerd config dump": "BinaryName = '/usr/bin/nvidia-container-runtime'",
                "__default__": "__not_found__",
            },
        )
        assert check.passed
        assert "containerd" in check.message

    def test_docker_ngc_login_passes(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "Docker version 24.0.0",
                "docker run": "NVIDIA-SMI 595 ...",
                "docker login": "Login Succeeded",
            },
            ngc_key="test-ngc-token",
        )
        assert check.passed

    def test_docker_ngc_login_failure_fails_check(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "Docker version 24.0.0",
                "docker run": "NVIDIA-SMI 595 ...",
                "docker login": "unauthorized: bad credentials",
            },
            ngc_key="test-bad-token",
        )
        assert not check.passed
        assert "NGC" in check.message

    def test_fails_when_no_runtime_found(self) -> None:
        check = _patched_run(_make_check(), {"__default__": "__not_found__"})
        assert not check.passed
        assert "No GPU-capable container runtime found" in check.message


# ---------------------------------------------------------------------------
# Level 2 — nerdctl
# ---------------------------------------------------------------------------


class TestNerdctlLevel:
    def test_passes_with_nerdctl_when_docker_absent(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "nerdctl version 2.2.2",
                "nerdctl run": "NVIDIA-SMI 595 ...",
            },
        )
        assert check.passed
        assert "nerdctl" in check.message

    def test_nerdctl_gpu_fails_falls_through_to_containerd(self) -> None:
        """nerdctl GPU fails (k8s namespace only) → falls through to containerd level."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "nerdctl version 2.2.2",
                "nerdctl run": "__gpu_run_failed__",
                "containerd --version": "containerd 1.7.0",
                "nvidia-container-runtime --version": "NVIDIA Container Runtime 1.19.0",
                "containerd config dump": "BinaryName = '/usr/bin/nvidia-container-runtime'",
                "__default__": "__not_found__",
            },
        )
        assert check.passed
        assert "containerd" in check.message

    def test_nerdctl_ngc_login_passes(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "nerdctl version 2.2.2",
                "nerdctl run": "NVIDIA-SMI 595 ...",
                "nerdctl login": "Login Succeeded",
            },
            ngc_key="test-ngc-token",
        )
        assert check.passed

    def test_nerdctl_ngc_login_failure_fails_check(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "nerdctl version 2.2.2",
                "nerdctl run": "NVIDIA-SMI 595 ...",
                "nerdctl login": "unauthorized: bad credentials",
            },
            ngc_key="test-bad-token",
        )
        assert not check.passed
        assert "NGC" in check.message


# ---------------------------------------------------------------------------
# Level 3 — containerd + nvidia-container-runtime
# ---------------------------------------------------------------------------


class TestContainerdLevel:
    """Docker absent, containerd + GPU operator present — standard k8s node scenario."""

    def test_passes_with_containerd_and_gpu_operator(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "nvidia-container-runtime --version": "NVIDIA Container Runtime 1.19.0",
                "containerd config dump": "BinaryName = '/usr/bin/nvidia-container-runtime'",
                "__default__": "__not_found__",
            },
        )
        assert check.passed
        assert "containerd" in check.message

    def test_fails_when_containerd_present_but_no_gpu_operator_binary(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "__default__": "__not_found__",
            },
        )
        assert not check.passed
        assert "nvidia-container-runtime not installed" in check.message

    def test_fails_when_binary_present_but_not_configured(self) -> None:
        """Binary exists but containerd config/plugin has no nvidia entry → fail."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "nvidia-container-runtime --version": "NVIDIA Container Runtime 1.19.0",
                "__default__": "",
            },
        )
        assert not check.passed

    def test_ngc_login_skipped_for_containerd_level(self) -> None:
        """NGC login is not applicable at the containerd level (no ctr login cmd)."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "nvidia-container-runtime --version": "NVIDIA Container Runtime 1.19.0",
                "containerd config dump": "BinaryName = '/usr/bin/nvidia-container-runtime'",
                "__default__": "__not_found__",
            },
            ngc_key="test-ngc-token",
        )
        assert check.passed


class TestContainerdConfigProbe:
    """Execute the real probe in sh against stub ``containerd`` and ``sudo`` binaries."""

    CONTAINERD2_GPU_OPERATOR = (
        "imports = ['/etc/containerd/conf.d/*.toml']\n"
        "[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]\n"
        "  BinaryName = '/usr/local/nvidia/toolkit/nvidia-container-runtime'\n"
        "[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]\n"
        "  BinaryName = '/usr/local/bin/runc'\n"
    )
    CONTAINERD1_NVIDIA_CTK = (
        '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]\n'
        '  BinaryName = "/usr/bin/nvidia-container-runtime"\n'
    )
    RUNC_ONLY = '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]\n  BinaryName = ""\n'
    CRI_DISABLED = 'disabled_plugins = ["cri"]\nimports = ["/etc/containerd/config.toml"]\n'

    @pytest.mark.parametrize(
        ("dump", "expected"),
        [
            (CONTAINERD2_GPU_OPERATOR, True),
            (CONTAINERD1_NVIDIA_CTK, True),
            (RUNC_ONLY, False),
            (CRI_DISABLED, False),
        ],
        ids=["containerd2-gpu-operator", "containerd1-nvidia-ctk", "runc-only", "cri-disabled"],
    )
    def test_detects_nvidia_runtime_handler(self, tmp_path: Path, dump: str, expected: bool) -> None:
        assert self._probe(tmp_path, dump, sudo_ok=True, readable_without_root=False) is expected

    @pytest.mark.parametrize(
        ("readable_without_root", "expected"), [(True, True), (False, False)], ids=["readable", "root-only"]
    )
    def test_without_sudo(self, tmp_path: Path, readable_without_root: bool, expected: bool) -> None:
        assert (
            self._probe(
                tmp_path, self.CONTAINERD1_NVIDIA_CTK, sudo_ok=False, readable_without_root=readable_without_root
            )
            is expected
        )

    @staticmethod
    def _probe(root: Path, dump: str, sudo_ok: bool, readable_without_root: bool) -> bool:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        dump_file = root / "dump.toml"
        dump_file.write_text(dump)
        stubs = {
            "sudo": '#!/bin/sh\n[ "$1" = -n ] && shift\nAS_ROOT=1 exec "$@"\n' if sudo_ok else "#!/bin/sh\nexit 1\n",
            "containerd": (
                f'#!/bin/sh\nif [ "$AS_ROOT" = 1 ] || [ {int(readable_without_root)} = 1 ]; then cat {dump_file}; '
                "else echo 'permission denied' >&2; exit 1; fi\n"
            ),
        }
        for name, script in stubs.items():
            (bin_dir / name).write_text(script)
            (bin_dir / name).chmod(0o755)

        def _run_locally(ssh: object, cmd: str) -> tuple[int, str, str]:
            if "nvidia-container-runtime --version" in cmd:
                return 0, "NVIDIA Container Runtime 1.19.0", ""
            proc = subprocess.run(
                ["sh", "-c", cmd],
                capture_output=True,
                text=True,
                env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr

        with patch("isvtest.validations.host.run_ssh_command", side_effect=_run_locally):
            return _make_check()._gpu_operator_installed(MagicMock())


# ---------------------------------------------------------------------------
# Level 4 — runc + nvidia-container-runtime
# ---------------------------------------------------------------------------


class TestRuncLevel:
    def test_passes_with_runc_and_gpu_operator(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "__not_found__",
                "runc --version": "runc version 1.1.0",
                "nvidia-container-runtime --version": "NVIDIA Container Runtime 1.19.0",
                "containerd config dump": "BinaryName = '/usr/bin/nvidia-container-runtime'",
                "__default__": "__not_found__",
            },
        )
        assert check.passed
        assert "runc" in check.message

    def test_fails_when_runc_present_but_no_gpu_operator(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "__not_found__",
                "runc --version": "runc version 1.1.0",
                "__default__": "__not_found__",
            },
        )
        assert not check.passed

    def test_runc_absent_falls_through_to_crun(self) -> None:
        """runc absent → tries crun → PASS at level 5."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "nerdctl --version": "__not_found__",
                "containerd --version": "__not_found__",
                "runc --version": "__not_found__",
                "crun --version": "crun version 1.0",
                "nvidia-container-runtime --version": "NVIDIA Container Runtime 1.19.0",
                "containerd config dump": "BinaryName = '/usr/bin/nvidia-container-runtime'",
                "__default__": "__not_found__",
            },
        )
        assert check.passed
        assert "crun" in check.message


# ---------------------------------------------------------------------------
# No runtime found
# ---------------------------------------------------------------------------


class TestNoRuntime:
    def test_fails_when_nothing_found(self) -> None:
        check = _patched_run(_make_check(), {"__default__": "__not_found__"})
        assert not check.passed
        assert "No GPU-capable container runtime found" in check.message

    def test_fails_when_host_config_missing(self) -> None:
        check = _make_check()
        with patch(
            "isvtest.validations.host.get_ssh_config",
            return_value={"ssh_host": "", "ssh_user": "ubuntu", "ssh_key_path": ""},
        ):
            check.run()
        assert not check.passed
        assert "Missing host" in check.message
