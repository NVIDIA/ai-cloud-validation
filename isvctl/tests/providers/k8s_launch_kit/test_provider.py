# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract and framework tests for the generic Kubernetes Launch Kit provider."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from isvtest.core.resolution import State

from isvctl.config.merger import merge_yaml_files
from isvctl.config.output_schemas import validate_output
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.loop import Orchestrator, Phase

_ISVCTL_ROOT = Path(__file__).resolve().parents[3]
_PROVIDERS = _ISVCTL_ROOT / "configs" / "providers"
_LAUNCH_KIT_PROVIDER = _PROVIDERS / "k8s-launch-kit"
_PROVIDER = _LAUNCH_KIT_PROVIDER / "scripts" / "adapter.py"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_MOCK_L8K = _FIXTURES / "mock_l8k.py"
_MOCK_KUBECTL = _FIXTURES / "mock_kubectl.py"
_GENERIC_CONFIG = _LAUNCH_KIT_PROVIDER / "config" / "provider.yaml"
_NETWORK_OPERATOR_CONFIG = _LAUNCH_KIT_PROVIDER / "config" / "network-operator.yaml"


def _load_provider_module() -> ModuleType:
    """Load the provider script for isolated installer tests."""
    spec = importlib.util.spec_from_file_location("k8s_launch_kit_provider", _PROVIDER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {_PROVIDER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_provider(
    *arguments: str,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run one provider operation from the same directory used by isvctl."""
    completed = subprocess.run(
        [sys.executable, str(_PROVIDER), *arguments],
        cwd=_PROVIDERS,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"provider emitted non-JSON stdout (exit {completed.returncode}): "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        ) from error
    assert isinstance(output, dict)
    return completed, output


def _run_workflow(
    command: str,
    arguments: list[str],
    *,
    working_dir: Path,
    artifact_dir: Path,
    user_config: Path | None = None,
    deployment_files: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run a mocked l8k workflow command through the generic transport."""
    provider_arguments = [
        "run",
        "--executable",
        str(_MOCK_L8K),
        "--command",
        command,
        "--arguments-json",
        json.dumps(arguments),
        "--environment-json",
        "{}",
        "--working-dir",
        str(working_dir),
        "--artifact-dir",
        str(artifact_dir),
    ]
    if user_config is not None:
        provider_arguments.extend(["--user-config", str(user_config)])
    if deployment_files is not None:
        provider_arguments.extend(["--deployment-files", str(deployment_files)])
    return _run_provider(*provider_arguments, env=env)


def _mocked_network_operator_config(tmp_path: Path) -> RunConfig:
    """Load production wiring, then inject test-owned executables and paths."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    context = merged["context"]["k8s_launch_kit"]
    user_config = tmp_path / "cluster-config.yaml"
    user_config.write_text(
        """networkOperator:
  selectedRelease: "26.4"
profile:
  fabric: ethernet
  deployment: sriov
clusterConfig: []
""",
        encoding="utf-8",
    )
    deployment_files = tmp_path / "deployment"
    deployment_files.mkdir()
    context["executable"] = str(_MOCK_L8K)
    context["user_config"] = str(user_config)
    context["deployment_files"] = str(deployment_files)
    context["working_dir"] = str(tmp_path / "work")
    context["artifact_dir"] = str(tmp_path / "evidence")
    return RunConfig.model_validate(merged)


def test_generic_provider_has_no_launch_kit_domain_defaults() -> None:
    """AI Cloud Validation exposes raw argv while Launch Kit owns domain defaults."""
    merged = merge_yaml_files([_GENERIC_CONFIG])
    config = RunConfig.model_validate(merged)
    context = merged["context"]["k8s_launch_kit"]

    assert set(context) == {
        "executable",
        "installation",
        "user_config",
        "kubectl_command",
        "working_dir",
        "artifact_dir",
        "environment",
        "discover",
        "generate",
        "deploy",
        "validate",
        "clean",
    }
    assert context["user_config"] == ""
    assert context["installation"] == {
        "mode": "verify",
        "version": "",
        "installer_ref": "",
        "installer_sha256": "",
        "prefix": "",
    }
    assert all(
        context[command]["arguments"] == [] for command in ("discover", "generate", "deploy", "validate", "clean")
    )
    assert [step.name for step in config.commands["network_operator"].steps] == [
        "launch_kit_prepare",
        "launch_kit_verify",
        "launch_kit_kubernetes_preflight",
        "launch_kit_discover",
        "launch_kit_generate",
        "launch_kit_deploy",
        "launch_kit_validate",
        "launch_kit_clean",
    ]
    assert config.commands["network_operator"].phases == ["setup", "test", "teardown"]
    discover_step = next(
        step for step in config.commands["network_operator"].steps if step.name == "launch_kit_discover"
    )
    prepare_step = next(step for step in config.commands["network_operator"].steps if step.name == "launch_kit_prepare")
    assert "--installer-ref={{ context.k8s_launch_kit.installation.installer_ref }}" in prepare_step.args
    assert "--installer-sha256={{ context.k8s_launch_kit.installation.installer_sha256 }}" in prepare_step.args
    assert "--user-config={{ context.k8s_launch_kit.user_config }}" in discover_step.args
    assert config.commands["network_operator"].steps[-1].phase == "teardown"
    assert config.commands["network_operator"].steps[-1].finalizer_for == "launch_kit_deploy"
    forbidden = {
        "namespace",
        "node_selector",
        "expected_network_operator_version",
        "driver_mode",
        "rail_names",
        "sriov_resource_names",
        "ip_pool_names",
        "gpu_count",
        "validation_mode",
        "validation_checks",
        "rdma_rping_iterations",
        "rdma_ib_write_size",
        "rdma_min_bandwidth_gbps",
        "timeout_seconds",
    }
    assert forbidden.isdisjoint(context)


def test_network_operator_provider_defaults_to_real_cli_tools() -> None:
    """The shipped provider contains one real, validation-only workflow."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    config = RunConfig.model_validate(merged)
    context = merged["context"]["k8s_launch_kit"]

    assert context == {
        "executable": "l8k",
        "user_config": "",
        "deployment_files": "",
        "working_dir": "../../../../../_output/k8s-launch-kit/network-operator/work",
        "artifact_dir": "../../../../../_output/k8s-launch-kit/network-operator/evidence",
        "environment": {},
    }
    assert "mock" not in json.dumps(merged).lower()
    assert "poc" not in json.dumps(merged).lower()
    command = config.commands["network_operator"]
    assert command.phases == ["test"]
    assert [step.name for step in command.steps] == ["launch_kit_validate"]
    step = command.steps[0]
    assert step.timeout is None
    assert "--user-config={{ context.k8s_launch_kit.user_config }}" in step.args
    assert "--deployment-files={{ context.k8s_launch_kit.deployment_files }}" in step.args
    assert step.requires_selected_validations == ["LaunchKitConnectivityCheck"]


def test_network_operator_suite_has_one_catalog_check() -> None:
    """Fabric and deployment choices are prerequisites, not test entries."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    checks = merged["tests"]["validations"]["network_operator"]["checks"]

    assert list(checks) == ["LaunchKitConnectivityCheck"]
    assert checks["LaunchKitConnectivityCheck"]["test_id"] == "K8S42-01"


def test_kubectl_defaults_to_the_real_binary() -> None:
    """An empty provider override resolves to kubectl from PATH."""
    module = _load_provider_module()

    assert module._kubectl_prefix("[]", {}) == ["kubectl"]


def test_launch_kit_executable_resolves_from_path(tmp_path: Path, monkeypatch: Any) -> None:
    """The production `l8k` setting is resolved as a normal executable."""
    module = _load_provider_module()
    executable = tmp_path / "l8k"
    executable.write_text("test executable", encoding="utf-8")
    monkeypatch.setattr(module.shutil, "which", lambda value: str(executable) if value == "l8k" else None)

    assert module._resolve_executable("l8k") == executable.resolve()


def test_prepare_verifies_version_and_schema(tmp_path: Path) -> None:
    """Verify mode proves the executable and captures Launch Kit's schema."""
    completed, output = _run_provider(
        "prepare",
        "--mode",
        "verify",
        "--executable",
        str(_MOCK_L8K),
        "--artifact-dir",
        str(tmp_path),
    )

    assert completed.returncode == 0
    assert output["success"] is True
    assert output["operation"] == "prepare"
    assert output["installed"] is False
    assert set(output["checks"]) == {"version", "schema"}
    assert all(check["passed"] is True for check in output["checks"].values())
    assert validate_output(output, "k8s_launch_kit") == (True, [])


def test_verification_rejects_an_unexpected_launch_kit_version(tmp_path: Path) -> None:
    """A pinned installation cannot silently verify a different binary on PATH."""
    module = _load_provider_module()

    verification, success, error = module._verify_executable(
        _MOCK_L8K,
        tmp_path,
        "v9.9.9",
    )

    assert success is False
    assert verification["checks"]["version"]["passed"] is False
    assert error == "l8k version mismatch: expected 'v9.9.9', got 'v0.1.0-mock'"


def test_verification_requires_the_launch_kit_clean_command(tmp_path: Path) -> None:
    """A pre-clean Launch Kit binary is rejected before deployment begins."""
    module = _load_provider_module()
    executable = tmp_path / "l8k"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = version ]; then\n'
        '  echo \'{"version": "v0.1.0"}\'\n'
        "else\n"
        '  echo \'{"commands": {"discover": {}, "generate": {}, "deploy": {}, "validate": {}}}\'\n'
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    verification, success, error = module._verify_executable(executable, tmp_path)

    assert success is False
    assert verification["checks"]["schema"]["passed"] is False
    assert error == "l8k schema does not advertise required command(s): clean"


def test_installed_executable_is_resolved_from_the_installer_prefix(tmp_path: Path) -> None:
    """Install mode verifies the binary written by the installer, not a stale PATH entry."""
    module = _load_provider_module()
    executable = tmp_path / "bin" / "l8k"
    executable.parent.mkdir(parents=True)
    executable.write_text("mock", encoding="utf-8")

    assert module._installed_executable(str(tmp_path)) == executable.resolve()


def test_installer_download_verifies_expected_digest(tmp_path: Path, monkeypatch: Any) -> None:
    """Install mode verifies an immutable official installer before writing it."""
    module = _load_provider_module()
    content = b"#!/bin/sh\nset -eu\n"
    installer_ref = "a" * 40
    expected_sha256 = hashlib.sha256(content).hexdigest()

    class Response:
        """Minimal context-managed urllib response."""

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return content

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    installer, url = module._download_installer(installer_ref, expected_sha256, tmp_path)
    metadata = json.loads((tmp_path / "installer-download.json").read_text(encoding="utf-8"))

    assert installer.read_bytes() == content
    assert url.endswith(f"/{installer_ref}/scripts/install.sh")
    assert metadata == {
        "url": url,
        "ref": installer_ref,
        "expected_sha256": expected_sha256,
        "sha256": expected_sha256,
        "verified": True,
    }


def test_installer_download_rejects_a_digest_mismatch(tmp_path: Path, monkeypatch: Any) -> None:
    """A downloaded installer is never persisted when its trusted digest differs."""
    module = _load_provider_module()
    content = b"#!/bin/sh\nexit 0\n"

    class Response:
        """Minimal context-managed urllib response."""

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return content

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(ValueError, match="installer SHA-256 mismatch"):
        module._download_installer("b" * 40, "0" * 64, tmp_path)

    metadata = json.loads((tmp_path / "installer-download.json").read_text(encoding="utf-8"))
    assert metadata["verified"] is False
    assert metadata["sha256"] == hashlib.sha256(content).hexdigest()
    assert not (tmp_path / "installer.sh").exists()


@pytest.mark.parametrize("installer_ref", ["", "main", "v0.1.0", "a" * 39])
def test_installer_download_requires_an_immutable_commit_ref(tmp_path: Path, installer_ref: str) -> None:
    """Install mode rejects mutable or abbreviated installer references before download."""
    module = _load_provider_module()

    with pytest.raises(ValueError, match="full 40-character Git commit SHA"):
        module._download_installer(installer_ref, "0" * 64, tmp_path)


def test_install_mode_delegates_to_the_upstream_installer(tmp_path: Path, monkeypatch: Any) -> None:
    """The provider does not reimplement Launch Kit archive or checksum logic."""
    module = _load_provider_module()
    installer = tmp_path / "installer.sh"
    installer.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setattr(
        module,
        "_download_installer",
        lambda _ref, _sha256, _artifact_dir: (installer, "https://example.invalid/installer.sh"),
    )
    monkeypatch.setattr(module, "_installed_executable", lambda _prefix: tmp_path / "bin" / "l8k")
    monkeypatch.setattr(
        module,
        "_verify_executable",
        lambda _executable, _artifact_dir, _expected_version, _environment: (
            {"checks": {}, "artifacts": {}},
            True,
            None,
        ),
    )

    def fake_run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
        del cwd
        calls.append((argv, env))
        return {"exit_code": 0, "stdout": "", "stderr": "", "duration_seconds": 0.1}

    monkeypatch.setattr(module, "_run_process", fake_run)
    monkeypatch.setattr(module, "_record_process", lambda *_args, **_kwargs: {})
    args = argparse.Namespace(
        mode="install",
        executable="l8k",
        version="v0.1.0",
        installer_ref="a" * 40,
        installer_sha256="0" * 64,
        prefix=str(tmp_path),
        environment_json=json.dumps({"HTTPS_PROXY": "http://proxy.example.test"}),
        artifact_dir=str(tmp_path / "evidence"),
    )

    output, exit_code = module._prepare(args)

    assert exit_code == 0
    assert output["installed"] is True
    assert calls[0][0] == ["/bin/sh", str(installer), "-d", str(tmp_path)]
    assert calls[0][1]["L8K_VERSION"] == "v0.1.0"
    assert calls[0][1]["HTTPS_PROXY"] == "http://proxy.example.test"


def test_provider_runs_the_real_launch_kit_workflow_shape(tmp_path: Path) -> None:
    """The transport runs the full Launch Kit lifecycle with raw argv."""
    working_dir = tmp_path / "work"
    artifact_dir = tmp_path / "evidence"
    kubeconfig = "kubeconfig"
    discover_args = [
        "--kubeconfig",
        kubeconfig,
        "--fabric",
        "ethernet",
        "--deployment-type",
        "sriov",
    ]
    commands = [
        ("discover", discover_args),
        ("generate", []),
        ("deploy", ["--kubeconfig", kubeconfig]),
        ("validate", ["--kubeconfig", kubeconfig]),
        ("clean", ["--kubeconfig", kubeconfig]),
    ]
    outputs: dict[str, dict[str, Any]] = {}

    for command, arguments in commands:
        completed, output = _run_workflow(
            command,
            arguments,
            working_dir=working_dir,
            artifact_dir=artifact_dir,
        )
        assert completed.returncode == 0
        assert output["success"] is True
        assert output["operation"] == command
        assert output["working_directory"] == str(working_dir.resolve())
        command_index = output["argv"].index(command)
        assert output["argv"][command_index + 1 :] == [*arguments, "--output", "json"]
        assert validate_output(output, "k8s_launch_kit") == (True, [])
        assert all(Path(path).is_file() for path in output["artifacts"].values())
        outputs[command] = output

    assert len(outputs["discover"]["documents"]) == 1
    assert len(outputs["generate"]["documents"]) == 1
    generated_files = [Path(path) for path in outputs["generate"]["documents"][0]["generatedFiles"]]
    daemonset_path = next(path for path in generated_files if "example-daemonset" in path.name)
    daemonset = yaml.safe_load(daemonset_path.read_text(encoding="utf-8"))
    assert [container["name"] for container in daemonset["spec"]["template"]["spec"]["containers"]] == [
        "test-container",
        "netshoot",
    ]
    test_container = daemonset["spec"]["template"]["spec"]["containers"][0]
    assert test_container["resources"]["requests"]["nvidia.com/gpu"] == "2"
    assert test_container["resources"]["limits"]["nvidia.com/gpu"] == "2"
    assert outputs["deploy"]["documents"] == []
    assert len(outputs["validate"]["documents"]) == 3
    families = {row["Family"] for row in outputs["validate"]["documents"][1]["connectivity"]["PingResults"]}
    assert families == {"icmp", "rping", "ib_write_bw", "gpudirect_dmabuf"}
    assert outputs["clean"]["documents"][0]["cleanup"] == {
        "namespace": "nvidia-network-operator",
        "customResourcesDeleted": 12,
        "helmReleaseRemoved": True,
        "keepHelmChart": False,
    }
    assert (working_dir / "cluster-config.yaml").is_file()
    assert (working_dir / "deployment" / "k8s-launch-kit-validation-report.html").is_file()


def test_discover_stages_user_config_transiently_without_retaining_secrets(tmp_path: Path) -> None:
    """Discovery uses a private staged config but retains only safe input provenance."""
    source = tmp_path / "customer-cluster-config.yaml"
    secret_values = ("customer-api-token", "registry-password", "embedded-kubeconfig")
    source_contents = """networkOperator:
  selectedRelease: "26.4"
profile:
  fabric: ethernet
  deployment: sriov
clusterConfig: []
credentials:
  token: customer-api-token
  registryPassword: registry-password
  kubeconfig: embedded-kubeconfig
"""
    source.write_text(source_contents, encoding="utf-8")
    working_dir = tmp_path / "work"
    artifact_dir = tmp_path / "evidence"

    completed, output = _run_workflow(
        "discover",
        ["--fabric", "ethernet", "--deployment-type", "sriov"],
        working_dir=working_dir,
        artifact_dir=artifact_dir,
        user_config=source,
    )

    staged = working_dir / "user-config.yaml"
    discovered = working_dir / "cluster-config.yaml"
    assert completed.returncode == 0
    assert output["success"] is True
    assert source.read_text(encoding="utf-8") == source_contents
    assert not staged.exists()
    assert discovered.is_file()
    metadata_path = artifact_dir / "inputs" / "user-config.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata == {
        "source_path": str(source.resolve()),
        "staged_path": str(staged.resolve()),
        "sha256": hashlib.sha256(source_contents.encode()).hexdigest(),
        "size_bytes": len(source_contents.encode()),
        "retained": False,
    }
    assert output["artifacts"]["user_config"] == str(metadata_path.resolve())
    assert output["argv"][-6:] == [
        "--user-config",
        str(staged.resolve()),
        "--save-cluster-config",
        str(discovered.resolve()),
        "--output",
        "json",
    ]
    retained_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for root in (working_dir, artifact_dir)
        for path in root.rglob("*")
        if path.is_file()
    )
    assert all(secret not in retained_text for secret in secret_values)


def test_user_config_must_not_be_inside_the_retained_working_directory(tmp_path: Path) -> None:
    """A source inside the retained output tree is rejected before it can leak as evidence."""
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    source = working_dir / "customer-cluster-config.yaml"
    source.write_text("profile: {}\ncredentials: customer-api-token\n", encoding="utf-8")

    completed, output = _run_workflow(
        "discover",
        [],
        working_dir=working_dir,
        artifact_dir=tmp_path / "evidence",
        user_config=source,
    )

    assert completed.returncode == 1
    assert output["success"] is False
    assert "must be outside the retained provider working directory" in output["error"]
    assert not (working_dir / "user-config.yaml").exists()


def test_staged_user_config_is_removed_when_discovery_fails(tmp_path: Path) -> None:
    """A failed l8k discovery cannot leave the sensitive staged input behind."""
    source = tmp_path / "customer-cluster-config.yaml"
    source_contents = "profile: {fabric: ethernet, deployment: sriov}\ncredentials: customer-api-token\n"
    source.write_text(source_contents, encoding="utf-8")
    working_dir = tmp_path / "work"

    completed, output = _run_workflow(
        "discover",
        [],
        working_dir=working_dir,
        artifact_dir=tmp_path / "evidence",
        user_config=source,
        env={**os.environ, "L8K_MOCK_FAIL": "discover"},
    )

    assert completed.returncode != 0
    assert output["success"] is False
    assert source.read_text(encoding="utf-8") == source_contents
    assert not (working_dir / "user-config.yaml").exists()


def test_staged_user_config_is_created_with_restricted_permissions(tmp_path: Path) -> None:
    """Sensitive input is private from the instant its staged file is created."""
    module = _load_provider_module()
    source = tmp_path / "customer-cluster-config.yaml"
    source.write_text("credentials: customer-api-token\n", encoding="utf-8")
    working_dir = tmp_path / "work"
    working_dir.mkdir()

    _, staged, _ = module._stage_user_config(str(source), working_dir, [])

    assert staged is not None
    assert staged.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("flag", ["--user-config", "--save-cluster-config"])
def test_staged_user_config_rejects_conflicting_raw_discovery_paths(tmp_path: Path, flag: str) -> None:
    """The first-class input owns both discovery config paths."""
    source = tmp_path / "customer-cluster-config.yaml"
    source.write_text("profile: {}\n", encoding="utf-8")

    completed, output = _run_workflow(
        "discover",
        [flag, str(tmp_path / "raw.yaml")],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=source,
    )

    assert completed.returncode == 1
    assert output["success"] is False
    assert f"cannot be combined with raw discovery flag(s): {flag}" in output["error"]


def test_staged_user_config_must_exist(tmp_path: Path) -> None:
    """A missing first-class user config fails before l8k starts."""
    working_dir = tmp_path / "work"

    completed, output = _run_workflow(
        "discover",
        [],
        working_dir=working_dir,
        artifact_dir=tmp_path / "evidence",
        user_config=tmp_path / "missing.yaml",
    )

    assert completed.returncode == 1
    assert output["success"] is False
    assert "Launch Kit user config not found" in output["error"]
    assert not (working_dir / "user-config.yaml").exists()


def test_clean_forwards_launch_kit_boolean_flags_unchanged(tmp_path: Path) -> None:
    """The transport accepts Launch Kit's native bare boolean flag syntax."""
    working_dir = tmp_path / "work"
    artifact_dir = tmp_path / "evidence"
    completed, _ = _run_workflow(
        "discover",
        ["--fabric", "ethernet", "--deployment-type", "sriov"],
        working_dir=working_dir,
        artifact_dir=artifact_dir,
    )
    assert completed.returncode == 0

    completed, output = _run_workflow(
        "clean",
        ["--keep-helm-chart"],
        working_dir=working_dir,
        artifact_dir=artifact_dir,
    )

    assert completed.returncode == 0
    assert output["argv"][-3:] == ["--keep-helm-chart", "--output", "json"]
    assert output["documents"][0]["cleanup"] == {
        "namespace": "nvidia-network-operator",
        "customResourcesDeleted": 12,
        "helmReleaseRemoved": False,
        "keepHelmChart": True,
    }


@pytest.mark.parametrize(
    ("fabric", "deployment", "network_kind"),
    [
        ("ethernet", "sriov", "SriovNetwork"),
        ("infiniband", "sriov", "SriovIBNetwork"),
        ("ethernet", "rdma_shared", "MacvlanNetwork"),
        ("infiniband", "rdma_shared", "IPoIBNetwork"),
        ("ethernet", "host_device", "HostDeviceNetwork"),
        ("infiniband", "host_device", "HostDeviceNetwork"),
    ],
)
def test_mock_supports_each_launch_kit_profile(
    tmp_path: Path,
    fabric: str,
    deployment: str,
    network_kind: str,
) -> None:
    """Every pinned profile can traverse the same real command sequence."""
    working_dir = tmp_path / f"{fabric}-{deployment}"
    artifact_dir = working_dir / "evidence"
    commands = [
        (
            "discover",
            [
                "--fabric",
                fabric,
                "--deployment-type",
                deployment,
            ],
        ),
        ("generate", []),
        ("deploy", []),
        ("validate", []),
        ("clean", []),
    ]
    outputs: dict[str, dict[str, Any]] = {}

    for command, arguments in commands:
        completed, output = _run_workflow(
            command,
            arguments,
            working_dir=working_dir,
            artifact_dir=artifact_dir,
        )
        assert completed.returncode == 0
        outputs[command] = output

    manifest_kinds = {row["Kind"] for row in outputs["validate"]["documents"][0]["manifests"]}
    assert network_kind in manifest_kinds
    assert outputs["clean"]["documents"][0]["phase"] == "clean"


def test_preflight_uses_the_workflow_kubeconfig(tmp_path: Path) -> None:
    """kubectl probes target the same explicit kubeconfig supplied to l8k."""
    workflow = {
        command: ["--kubeconfig", "partner.kubeconfig"] if command != "generate" else []
        for command in ("discover", "generate", "deploy", "validate", "clean")
    }
    completed, output = _run_provider(
        "preflight",
        "--kubectl-command-json",
        json.dumps([sys.executable, str(_MOCK_KUBECTL)]),
        "--workflow-arguments-json",
        json.dumps(workflow),
        "--working-dir",
        str(tmp_path / "work"),
        "--artifact-dir",
        str(tmp_path / "evidence"),
    )

    assert completed.returncode == 0
    assert output["success"] is True
    assert output["kubeconfig_source"] == "workflow arguments"
    assert output["node_count"] == 2
    assert output["ready_node_count"] == 2
    command_file = Path(output["artifacts"]["api_version"]["command"])
    argv = json.loads(command_file.read_text(encoding="utf-8"))["argv"]
    kubeconfig_index = argv.index("--kubeconfig")
    assert argv[kubeconfig_index : kubeconfig_index + 2] == ["--kubeconfig", "partner.kubeconfig"]


def test_preflight_forwards_the_launch_kit_environment(tmp_path: Path) -> None:
    """The safety probes use the same environment that the provider gives l8k."""
    workflow = {command: [] for command in ("discover", "generate", "deploy", "validate", "clean")}
    completed, output = _run_provider(
        "preflight",
        "--kubectl-command-json",
        json.dumps([sys.executable, str(_MOCK_KUBECTL)]),
        "--workflow-arguments-json",
        json.dumps(workflow),
        "--environment-json",
        json.dumps(
            {
                "KUBECONFIG": "environment.kubeconfig",
                "L8K_MOCK_EXPECT_KUBECONFIG": "environment.kubeconfig",
            }
        ),
        "--working-dir",
        str(tmp_path / "work"),
        "--artifact-dir",
        str(tmp_path / "evidence"),
    )

    assert completed.returncode == 0
    assert output["success"] is True


def test_preflight_accepts_a_validation_only_workflow(tmp_path: Path) -> None:
    """The prerequisite gate follows the caller's actual Launch Kit command subset."""
    workflow = {command: [] for command in ("discover", "generate", "validate")}
    completed, output = _run_provider(
        "preflight",
        "--kubectl-command-json",
        json.dumps([sys.executable, str(_MOCK_KUBECTL)]),
        "--workflow-arguments-json",
        json.dumps(workflow),
        "--working-dir",
        str(tmp_path / "work"),
        "--artifact-dir",
        str(tmp_path / "evidence"),
    )

    assert completed.returncode == 0
    assert output["success"] is True


def test_preflight_rejects_conflicting_workflow_kubeconfigs(tmp_path: Path) -> None:
    """The safety gate fails closed when l8k commands would target different clusters."""
    workflow = {
        "discover": ["--kubeconfig", "cluster-a"],
        "generate": [],
        "deploy": ["--kubeconfig=cluster-b"],
        "validate": [],
        "clean": [],
    }
    completed, output = _run_provider(
        "preflight",
        "--kubectl-command-json",
        "[]",
        "--workflow-arguments-json",
        json.dumps(workflow),
        "--working-dir",
        str(tmp_path / "work"),
        "--artifact-dir",
        str(tmp_path / "evidence"),
    )

    assert completed.returncode == 1
    assert output["success"] is False
    assert "different kubeconfigs" in output["error"]


def test_network_operator_provider_runs_only_validate(tmp_path: Path) -> None:
    """The production configuration invokes one validation over prerequisite inputs."""
    config = _mocked_network_operator_config(tmp_path)

    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
    )

    assert result.success is True
    assert list(result.inventory) == ["launch_kit_validate"]
    assert [phase.name for phase in result.phases] == ["test"]
    assert len(result.validations) == 1
    validation = result.validations[0]
    assert validation.entry.name == "LaunchKitConnectivityCheck"
    assert validation.state is State.PASSED
    assert validation.subtest_summary.passed == 32
    assert validation.subtest_summary.failed == 0
    assert validation.subtest_summary.skipped == 0

    argv = result.inventory["launch_kit_validate"]["argv"]
    assert argv[1] == "validate"
    assert argv[argv.index("--user-config") + 1] == str((tmp_path / "cluster-config.yaml").resolve())
    assert argv[argv.index("--deployment-files") + 1] == str((tmp_path / "deployment").resolve())
    assert argv[-2:] == ["--output", "json"]


def test_network_operator_provider_expands_input_paths(tmp_path: Path, monkeypatch: Any) -> None:
    """Tilde inputs are resolved before they are supplied to Launch Kit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    user_config = tmp_path / "l8k" / "cluster-config.yaml"
    user_config.parent.mkdir()
    user_config.write_text(
        "profile:\n  fabric: ethernet\n  deployment: sriov\n",
        encoding="utf-8",
    )
    deployment_files = tmp_path / "l8k" / "deployment"
    deployment_files.mkdir()

    completed, output = _run_workflow(
        "validate",
        [],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=Path("~/l8k/cluster-config.yaml"),
        deployment_files=Path("~/l8k/deployment"),
    )

    assert completed.returncode == 0
    assert output["argv"][output["argv"].index("--user-config") + 1] == str(user_config)
    assert output["argv"][output["argv"].index("--deployment-files") + 1] == str(deployment_files)


@pytest.mark.parametrize(
    ("user_config", "deployment_files", "expected"),
    [
        (None, "deployment", "user_config is required"),
        ("cluster-config.yaml", None, "--user-config requires --deployment-files"),
    ],
)
def test_validate_requires_both_prerequisite_inputs(
    tmp_path: Path,
    user_config: str | None,
    deployment_files: str | None,
    expected: str,
) -> None:
    """Partial prerequisite input fails before Launch Kit execution."""
    config_path = tmp_path / "cluster-config.yaml"
    config_path.write_text("profile: {}\n", encoding="utf-8")
    deployment_path = tmp_path / "deployment"
    deployment_path.mkdir()

    completed, output = _run_workflow(
        "validate",
        [],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=config_path if user_config is not None else None,
        deployment_files=deployment_path if deployment_files is not None else None,
    )

    assert completed.returncode == 1
    assert output["success"] is False
    assert expected in output["error"]


def test_validate_rejects_duplicate_path_flags(tmp_path: Path) -> None:
    """Dedicated inputs cannot silently conflict with raw Launch Kit arguments."""
    user_config = tmp_path / "cluster-config.yaml"
    user_config.write_text("profile: {}\n", encoding="utf-8")
    deployment_files = tmp_path / "deployment"
    deployment_files.mkdir()

    completed, output = _run_workflow(
        "validate",
        ["--user-config", "other.yaml"],
        working_dir=tmp_path / "work",
        artifact_dir=tmp_path / "evidence",
        user_config=user_config,
        deployment_files=deployment_files,
    )

    assert completed.returncode == 1
    assert "cannot be combined with raw flag(s): --user-config" in output["error"]


def test_failed_connectivity_is_a_junit_failure(tmp_path: Path, monkeypatch: Any) -> None:
    """A failed Launch Kit matrix row is retained as a test failure in JUnit."""
    monkeypatch.setenv("L8K_MOCK_FAIL", "validate:ib_write_bw")
    config = _mocked_network_operator_config(tmp_path)
    junit_path = tmp_path / "junit.xml"

    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
        junitxml=str(junit_path),
    )

    assert result.success is False
    assert list(result.inventory) == ["launch_kit_validate"]
    assert result.validations[0].state is State.FAILED
    assert result.validations[0].subtest_summary.failed == 1
    case = next(
        case
        for case in ET.parse(junit_path).getroot().iter("testcase")
        if case.get("name") == "LaunchKitConnectivityCheck"
    )
    assert case.find("failure") is not None
    assert case.find("error") is None
    assert case.find("skipped") is None


def test_missing_prerequisites_are_a_step_error(tmp_path: Path) -> None:
    """An unset prerequisite produces an actionable validation error."""
    merged = merge_yaml_files([_NETWORK_OPERATOR_CONFIG])
    merged["context"]["k8s_launch_kit"]["executable"] = str(_MOCK_L8K)
    merged["context"]["k8s_launch_kit"]["working_dir"] = str(tmp_path / "work")
    merged["context"]["k8s_launch_kit"]["artifact_dir"] = str(tmp_path / "evidence")
    config = RunConfig.model_validate(merged)

    result = Orchestrator(config, working_dir=_NETWORK_OPERATOR_CONFIG.parent).run(
        phases=[Phase.TEST],
        capability="kubernetes",
    )

    assert result.success is False
    assert result.validations[0].state is State.FAILED
    assert "user_config is required" in result.validations[0].message


def test_failed_validate_preserves_documents_and_process_error(tmp_path: Path) -> None:
    """A non-zero l8k result retains every JSON document and a clear exit diagnostic."""
    working_dir = tmp_path / "work"
    artifact_dir = tmp_path / "evidence"
    discover, _ = _run_workflow(
        "discover",
        [
            "--fabric",
            "ethernet",
            "--deployment-type",
            "sriov",
        ],
        working_dir=working_dir,
        artifact_dir=artifact_dir,
    )
    assert discover.returncode == 0
    generate, _ = _run_workflow(
        "generate",
        [],
        working_dir=working_dir,
        artifact_dir=artifact_dir,
    )
    assert generate.returncode == 0
    env = os.environ.copy()
    env["L8K_MOCK_FAIL"] = "validate:ib_write_bw"

    completed, output = _run_workflow(
        "validate",
        [],
        working_dir=working_dir,
        artifact_dir=artifact_dir,
        env=env,
    )

    assert completed.returncode == 4
    assert output["success"] is False
    assert len(output["documents"]) == 3
    assert "l8k validate exited with code 4" in output["error"]
    assert Path(output["artifacts"]["stdout"]).read_text(encoding="utf-8")
