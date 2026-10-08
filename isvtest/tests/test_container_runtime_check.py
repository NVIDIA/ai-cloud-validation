# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ContainerRuntimeCheck.

Covers the custom ``commands.gpu_container`` override and the two auto-detected runtimes:
  Docker:     GPU container runs
  containerd: loaded config defines an nvidia-container-runtime handler
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


NERDCTL_CMD = "sudo nerdctl run --rm --gpus all nvcr.io/nvidia/cuda:13.0.3-base-ubuntu24.04 nvidia-smi"


# ---------------------------------------------------------------------------
# Custom runtime — commands.gpu_container
# ---------------------------------------------------------------------------


class TestCustomCommand:
    def test_passes_and_skips_detection(self) -> None:
        """A working custom command passes without probing Docker or containerd."""
        check = _patched_run(
            _make_check({"commands": {"gpu_container": NERDCTL_CMD}}),
            {"nerdctl run": "NVIDIA-SMI 595 ...", "docker run": "__gpu_run_failed__"},
        )
        assert check.passed
        assert "commands.gpu_container" in check.message

    def test_failure_does_not_fall_back(self) -> None:
        """A failing custom command fails even when Docker would have passed."""
        check = _patched_run(
            _make_check({"commands": {"gpu_container": NERDCTL_CMD}}),
            {
                "nerdctl run": "__gpu_run_failed__",
                "docker --version": "Docker version 24.0.0",
                "docker run": "NVIDIA-SMI 595 ...",
            },
        )
        assert not check.passed
        assert "commands.gpu_container" in check.message

    def test_ngc_login_not_applicable(self) -> None:
        """NGC login is skipped for a custom command; the check still passes."""
        check = _patched_run(
            _make_check({"commands": {"gpu_container": NERDCTL_CMD}}),
            {"nerdctl run": "NVIDIA-SMI 595 ..."},
            ngc_key="test-ngc-token",
        )
        assert check.passed


# ---------------------------------------------------------------------------
# Docker
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
        """Docker GPU fails but containerd + GPU operator present → PASS at containerd."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "Docker version 24.0.0",
                "docker run": "__gpu_run_failed__",
                "containerd --version": "containerd 1.7.0",
                "containerd config dump": "/usr/bin/nvidia-container-runtime",
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
# containerd + nvidia-container-runtime
# ---------------------------------------------------------------------------


class TestContainerdLevel:
    """Docker absent, containerd + GPU operator present — standard k8s node scenario."""

    def test_passes_with_containerd_and_gpu_operator(self) -> None:
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "containerd config dump": "/usr/bin/nvidia-container-runtime",
                "__default__": "__not_found__",
            },
        )
        assert check.passed
        assert "containerd" in check.message

    def test_fails_when_no_nvidia_handler(self) -> None:
        """containerd present but its loaded config has no usable NVIDIA handler → fail."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "__default__": "",
            },
        )
        assert not check.passed
        assert "no usable nvidia-container-runtime handler" in check.message
        assert "docker" not in check.message

    def test_fails_with_sudo_hint_when_config_unreadable(self) -> None:
        """containerd config can't be read (root-only, no passwordless sudo) → say so."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "containerd config dump": "__unreadable__",
                "__default__": "",
            },
        )
        assert not check.passed
        assert "needs passwordless sudo" in check.message

    def test_failure_mentions_failed_docker_run(self) -> None:
        """Docker present but its GPU run failed → the containerd failure says so."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "Docker version 24.0.0",
                "docker run": "__gpu_run_failed__",
                "containerd --version": "containerd 1.7.0",
                "__default__": "",
            },
        )
        assert not check.passed
        assert "docker GPU container failed" in check.message

    def test_ngc_login_skipped_for_containerd_level(self) -> None:
        """NGC login is not applicable at the containerd level (no ctr login cmd)."""
        check = _patched_run(
            _make_check(),
            {
                "docker --version": "__not_found__",
                "containerd --version": "containerd 1.7.0",
                "containerd config dump": "/usr/bin/nvidia-container-runtime",
                "__default__": "__not_found__",
            },
            ngc_key="test-ngc-token",
        )
        assert check.passed


class TestContainerdConfigProbe:
    """Execute the real probe in sh against stub ``containerd`` and ``sudo`` binaries.

    ``{root}`` in a dump is replaced with the test's temp dir, where the stub NVIDIA
    runtimes live, so results don't depend on what the test machine has installed.
    """

    CONTAINERD2_GPU_OPERATOR = (
        "imports = ['/etc/containerd/conf.d/*.toml']\n"
        "[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]\n"
        "  BinaryName = '{root}/usr/local/nvidia/toolkit/nvidia-container-runtime'\n"
        "[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]\n"
        "  BinaryName = '/usr/local/bin/runc'\n"
    )
    CONTAINERD1_NVIDIA_CTK = (
        '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]\n'
        '  BinaryName = "{root}/usr/bin/nvidia-container-runtime"\n'
    )
    BARE_NAME_ON_PATH = (
        '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]\n'
        '  BinaryName = "nvidia-container-runtime"\n'
    )
    BINARY_MISSING = (
        "[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]\n"
        "  BinaryName = '{root}/opt/missing/nvidia-container-runtime'\n"
    )
    RUNC_ONLY = '[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]\n  BinaryName = ""\n'
    CRI_DISABLED = 'disabled_plugins = ["cri"]\nimports = ["/etc/containerd/config.toml"]\n'

    @pytest.mark.parametrize(
        ("dump", "expected"),
        [
            (CONTAINERD2_GPU_OPERATOR, True),
            (CONTAINERD1_NVIDIA_CTK, True),
            (BARE_NAME_ON_PATH, True),
            (BINARY_MISSING, False),
            (RUNC_ONLY, False),
            (CRI_DISABLED, False),
        ],
        ids=[
            "containerd2-gpu-operator",
            "containerd1-nvidia-ctk",
            "bare-name-on-path",
            "binary-missing",
            "runc-only",
            "cri-disabled",
        ],
    )
    def test_detects_nvidia_runtime_handler(self, tmp_path: Path, dump: str, expected: bool) -> None:
        """Only a loaded runtime handler backed by nvidia-container-runtime counts."""
        assert self._probe(tmp_path, dump, sudo_ok=True, readable_without_root=False)[0] is expected

    @pytest.mark.parametrize(
        ("readable_without_root", "expected"), [(True, True), (False, False)], ids=["readable", "root-only"]
    )
    def test_without_sudo(self, tmp_path: Path, readable_without_root: bool, expected: bool) -> None:
        """Without passwordless sudo, the probe falls back to an unprivileged dump."""
        ok, detail = self._probe(
            tmp_path, self.CONTAINERD1_NVIDIA_CTK, sudo_ok=False, readable_without_root=readable_without_root
        )
        assert ok is expected
        if not ok:
            assert "needs passwordless sudo" in detail

    @staticmethod
    def _probe(root: Path, dump: str, sudo_ok: bool, readable_without_root: bool) -> tuple[bool, str]:
        """Run ``_containerd_nvidia_runtime`` with stubs that print ``dump`` when permitted."""
        bin_dir = root / "bin"
        dump_file = root / "dump.toml"
        dump_file.write_text(dump.replace("{root}", str(root)))
        stubs = {
            bin_dir / "sudo": (
                '#!/bin/sh\n[ "$1" = -n ] && shift\nAS_ROOT=1 exec "$@"\n' if sudo_ok else "#!/bin/sh\nexit 1\n"
            ),
            bin_dir / "containerd": (
                f'#!/bin/sh\nif [ "$AS_ROOT" = 1 ] || [ {int(readable_without_root)} = 1 ]; then cat {dump_file}; '
                "else echo 'permission denied' >&2; exit 1; fi\n"
            ),
            bin_dir / "nvidia-container-runtime": "#!/bin/sh\n",
            root / "usr/bin/nvidia-container-runtime": "#!/bin/sh\n",
            root / "usr/local/nvidia/toolkit/nvidia-container-runtime": "#!/bin/sh\n",
        }
        for path, script in stubs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(script)
            path.chmod(0o755)

        def _run_locally(ssh: object, cmd: str) -> tuple[int, str, str]:
            proc = subprocess.run(
                ["sh", "-c", cmd],
                capture_output=True,
                text=True,
                env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr

        with patch("isvtest.validations.host.run_ssh_command", side_effect=_run_locally):
            return _make_check()._containerd_nvidia_runtime(MagicMock())


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
