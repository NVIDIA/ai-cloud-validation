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

"""Unit tests for provider scaffold CLI commands."""

import shlex
import stat
from pathlib import Path

import pytest
import yaml
from click.utils import strip_ansi
from typer.testing import CliRunner

from isvctl.cli import provider as provider_cli
from isvctl.cli import test as test_cli
from isvctl.cli.provider import RELATIVE_PATH_RE, _rewrite_text_files
from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.main import app as main_app

runner = CliRunner()


def test_provider_help_renders() -> None:
    """Test provider command help."""
    result = runner.invoke(main_app, ["provider", "--help"])

    assert result.exit_code == 0
    assert "Manage provider scaffolds" in result.output
    assert "scaffold" in result.output


def test_provider_scaffold_help_renders() -> None:
    """Test provider scaffold command help."""
    result = runner.invoke(main_app, ["provider", "scaffold", "--help"])
    output = strip_ansi(result.output)

    assert result.exit_code == 0
    assert "Create a ready-to-edit provider scaffold" in output
    assert "--output-dir" in output
    assert "--dry-run" in output
    assert "--overwrite" in output


def test_successful_scaffold_into_output_dir(tmp_path: Path) -> None:
    """Test creating a provider scaffold in a custom output directory."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 0
    assert target.is_dir()
    assert (target / "README.md").is_file()
    assert (target / "config" / "vm.yaml").is_file()
    assert (target / "scripts" / "vm" / "launch_instance.py").is_file()
    assert "Created provider scaffold:" in result.output
    assert "ISVCTL_DEMO_MODE=1 uv run isvctl test run -f" in result.output
    assert "scripts/vm/launch_instance.py" in result.output


def test_scaffold_next_steps_quote_paths_with_spaces(tmp_path: Path) -> None:
    """Test generated shell snippets quote paths that contain spaces."""
    target = tmp_path / "provider dir" / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target), "--dry-run"])

    assert result.exit_code == 0
    assert f"uv run isvctl test run -f {shlex.quote(f'{target}/config/vm.yaml')}" in result.output
    assert f"  {shlex.quote(f'{target}/scripts/vm/launch_instance.py')}" in result.output


def test_scaffold_rewrites_my_isv_text(tmp_path: Path) -> None:
    """Test generated text files are rewritten to the requested provider name."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 0
    readme = (target / "scripts" / "README.md").read_text(encoding="utf-8")
    config = (target / "config" / "vm.yaml").read_text(encoding="utf-8")
    assert "acme scaffold" in readme
    assert "providers/acme" in readme
    assert "my-isv" not in readme
    assert "acme VM Validation Configuration" in config
    assert "my-isv" not in config


def test_rewrite_skips_embedded_template_token(tmp_path: Path) -> None:
    """Test the my-isv rewrite leaves embedded occurrences alone."""
    target = tmp_path / "scaffold"
    target.mkdir()
    sample = target / "notes.txt"
    sample.write_text(
        "my-isv scaffold\nproviders/my-isv/config\nmy-isv-vm-validation\nmy-isv.gpu.1x\nmy-isvfoo bar\nxxxmy-isv yyy\n",
        encoding="utf-8",
    )

    _rewrite_text_files(target, "acme")

    rewritten = sample.read_text(encoding="utf-8")
    assert "acme scaffold" in rewritten
    assert "providers/acme/config" in rewritten
    assert "acme-vm-validation" in rewritten
    assert "acme.gpu.1x" in rewritten
    assert "my-isvfoo bar" in rewritten
    assert "xxxmy-isv yyy" in rewritten


def test_invalid_provider_names_are_rejected(tmp_path: Path) -> None:
    """Test path-like provider names are rejected."""
    target = tmp_path / "bad"

    result = runner.invoke(main_app, ["provider", "scaffold", "../bad", "--output-dir", str(target)])

    assert result.exit_code != 0
    assert "Provider name must contain only lowercase letters" in result.output
    assert not target.exists()


def test_scaffold_rejects_output_dir_nested_under_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test scaffold refuses targets inside the source template tree."""
    template_dir = tmp_path / "providers" / "my-isv"
    template_dir.mkdir(parents=True)
    (template_dir / "README.md").write_text("my-isv scaffold\n", encoding="utf-8")
    monkeypatch.setattr(provider_cli, "_find_template_dir", lambda: template_dir.resolve())

    target = template_dir / "generated" / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 1
    assert "Target path points inside the provider template" in result.output
    assert not target.exists()


def test_existing_target_rejected_without_overwrite(tmp_path: Path) -> None:
    """Test existing targets are rejected unless overwrite is requested."""
    target = tmp_path / "acme"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 1
    assert "Target already exists" in result.output
    assert marker.read_text(encoding="utf-8") == "keep"


def test_overwrite_replaces_existing_scaffold(tmp_path: Path) -> None:
    """Test overwrite replaces an existing scaffold target directory."""
    target = tmp_path / "acme"

    first = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])
    assert first.exit_code == 0

    stale_file = target / "scripts" / "stale.txt"
    stale_file.write_text("old", encoding="utf-8")

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target), "--overwrite"])

    assert result.exit_code == 0
    assert not stale_file.exists()
    assert (target / "README.md").is_file()
    assert (target / "scripts" / "vm" / "launch_instance.py").is_file()


def test_overwrite_refuses_non_scaffold_directory(tmp_path: Path) -> None:
    """Test overwrite refuses directories that don't look like a scaffold."""
    target = tmp_path / "unrelated"
    target.mkdir()
    keep = target / "important.txt"
    keep.write_text("do not delete", encoding="utf-8")

    result = runner.invoke(
        main_app,
        ["provider", "scaffold", "acme", "--output-dir", str(target), "--overwrite"],
    )

    assert result.exit_code == 1
    assert "Refusing to overwrite" in result.output
    assert "missing scaffold marker" in result.output
    assert keep.read_text(encoding="utf-8") == "do not delete"


def test_overwrite_refuses_when_marker_dirs_alone_present(tmp_path: Path) -> None:
    """Test overwrite refuses a directory that mimics the layout but lacks the meta sentinel."""
    target = tmp_path / "look-alike"
    (target / "config").mkdir(parents=True)
    (target / "scripts").mkdir()
    keep = target / "config" / "important.yaml"
    keep.write_text("preserve: true\n", encoding="utf-8")

    result = runner.invoke(
        main_app,
        ["provider", "scaffold", "acme", "--output-dir", str(target), "--overwrite"],
    )

    assert result.exit_code == 1
    assert "missing scaffold marker" in result.output
    assert keep.read_text(encoding="utf-8") == "preserve: true\n"


def test_overwrite_refuses_when_output_dir_is_a_file(tmp_path: Path) -> None:
    """Test overwrite refuses a file path masquerading as a target directory."""
    target = tmp_path / "not-a-dir"
    target.write_text("just a file", encoding="utf-8")

    result = runner.invoke(
        main_app,
        ["provider", "scaffold", "acme", "--output-dir", str(target), "--overwrite"],
    )

    assert result.exit_code == 1
    assert "Refusing to overwrite" in result.output
    assert "not a directory" in result.output
    assert target.read_text(encoding="utf-8") == "just a file"


def test_overwrite_refuses_template_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test overwrite refuses when the target contains the provider template."""
    template_dir = tmp_path / "providers" / "my-isv"
    template_dir.mkdir(parents=True)
    (template_dir / "README.md").write_text("my-isv scaffold\n", encoding="utf-8")
    monkeypatch.setattr(provider_cli, "_find_template_dir", lambda: template_dir.resolve())
    providers_dir = template_dir.parent

    result = runner.invoke(
        main_app,
        ["provider", "scaffold", "acme", "--output-dir", str(providers_dir), "--overwrite"],
    )

    assert result.exit_code == 1
    assert "Refusing to overwrite" in result.output
    assert "would delete the provider template" in result.output
    assert template_dir.is_dir()


def test_dry_run_warns_when_target_exists_without_overwrite(tmp_path: Path) -> None:
    """Test dry-run flags an existing target instead of erroring."""
    target = tmp_path / "acme"
    target.mkdir()

    result = runner.invoke(
        main_app,
        ["provider", "scaffold", "acme", "--output-dir", str(target), "--dry-run"],
    )

    assert result.exit_code == 0
    assert "--overwrite would be required" in result.output
    assert "Would create provider scaffold:" in result.output


def test_executable_bits_are_preserved(tmp_path: Path) -> None:
    """Test executable shell scripts stay executable in the scaffold."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 0
    mode = (target / "scripts" / "k8s" / "setup.sh").stat().st_mode
    assert mode & stat.S_IXUSR


def test_generated_yaml_files_parse(tmp_path: Path) -> None:
    """Test generated provider YAML files are valid YAML."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 0
    for yaml_path in sorted((target / "config").glob("*.yaml")):
        parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), yaml_path


def test_output_dir_scaffold_keeps_config_references_resolvable(tmp_path: Path) -> None:
    """Test generated provider configs still load from an out-of-tree directory."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 0
    config_path = target / "config" / "vm.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    merged = merge_yaml_files([config_path])
    deploy_nim = next(step for step in merged["commands"]["vm"]["steps"] if step["name"] == "deploy_nim")
    shared_script = deploy_nim["command"].removeprefix("python ")
    repo_root = Path(__file__).resolve().parents[2]

    assert merged["tests"]["cluster_name"] == "acme-vm-validation"
    assert not Path(shared_script).is_absolute()
    assert not shared_script.startswith("../")
    assert (repo_root / shared_script).resolve().is_file()
    assert "isvctl/configs/suites/vm.yaml" in config_text
    assert "isvctl/configs/providers/shared/deploy_nim.py" in config_text
    assert "/Users/" not in config_text
    assert "../../.." not in config_text


def test_output_dir_scaffold_imports_load_for_every_config(tmp_path: Path) -> None:
    """Test every out-of-tree scaffold config can import its validation suite."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 0
    config_files = sorted((target / "config").glob("*.yaml"))
    assert config_files, "no generated config files"
    for config_path in config_files:
        config_text = config_path.read_text(encoding="utf-8")
        merged = merge_yaml_files([config_path])

        RunConfig.model_validate(merged)
        assert "commands" in merged, config_path
        assert "tests" in merged, config_path
        assert "/Users/" not in config_text
        assert "../../.." not in config_text


def test_output_dir_scaffold_runs_external_provider_script_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test an out-of-tree scaffold can run provider-local imports from the checkout root."""
    target = tmp_path / "external-provider" / "acme"
    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])
    assert result.exit_code == 0

    common_dir = target / "scripts" / "common"
    common_dir.mkdir()
    (common_dir / "__init__.py").write_text("", encoding="utf-8")
    (common_dir / "provider_ids.py").write_text(
        "def instance_id() -> str:\n    return 'acme-external-vm-0001'\n",
        encoding="utf-8",
    )

    marker_path = tmp_path / "provider-import-marker.txt"
    (target / "scripts" / "vm" / "launch_instance.py").write_text(
        """#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.provider_ids import instance_id

parser = argparse.ArgumentParser()
parser.add_argument("--name", required=True)
parser.add_argument("--instance-type", required=True)
parser.add_argument("--region", required=True)
args = parser.parse_args()

marker = os.environ.get("ACME_IMPORT_MARKER")
if marker:
    Path(marker).write_text(instance_id(), encoding="utf-8")

print(json.dumps({
    "success": True,
    "platform": "vm",
    "instance_id": instance_id(),
    "public_ip": "203.0.113.10",
    "private_ip": "10.0.0.10",
    "key_file": "/tmp/dummy-key.pem",
    "vpc_id": "dummy-vpc-0001",
    "state": "running",
    "security_group_id": "dummy-sg-0001",
    "requested_key_name": args.name,
    "key_name": args.name,
    "tests": {
        "specified_key": {
            "passed": True,
            "message": "dummy provider preserved requested key name",
            "probes": ["provider-local-common-import"],
        },
    },
}))
""",
        encoding="utf-8",
    )

    def _test_output_dir(root: Path | None = None) -> Path:
        output_dir = tmp_path / "run-output"
        output_dir.mkdir(exist_ok=True)
        return output_dir

    monkeypatch.setattr(test_cli, "get_output_dir", _test_output_dir)
    monkeypatch.setenv("ACME_IMPORT_MARKER", str(marker_path))
    monkeypatch.setenv("ISVTEST_INCLUDE_UNRELEASED", "1")

    run = runner.invoke(
        main_app,
        [
            "test",
            "run",
            "-f",
            str(target / "config" / "vm.yaml"),
            "--phase",
            "setup",
            "--no-upload",
            "--junitxml",
            str(tmp_path / "junit.xml"),
            "--",
            "-k",
            "InstanceStateCheck or InstanceCreatedCheck or InstanceSpecifiedKeyCheck",
        ],
    )

    assert run.exit_code == 0, run.output
    assert marker_path.read_text(encoding="utf-8") == "acme-external-vm-0001"


def test_every_generated_config_relative_path_resolves(tmp_path: Path) -> None:
    """Test every ../-rooted reference in generated configs points at a real file.

    Catches silent breakage if a future template introduces a path pattern the
    rewriter doesn't know about.
    """
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])
    assert result.exit_code == 0

    config_files = sorted((target / "config").glob("*.yaml"))
    assert config_files, "no generated config files"

    total_refs = 0
    for yaml_path in config_files:
        content = yaml_path.read_text(encoding="utf-8")
        refs = RELATIVE_PATH_RE.findall(content)
        assert refs, f"{yaml_path} has no relative references; template change?"
        for ref in refs:
            resolved = (yaml_path.parent / ref).resolve()
            assert resolved.is_file(), f"{yaml_path} references missing file: {ref} -> {resolved}"
            total_refs += 1

    assert total_refs >= len(config_files), "expected at least one reference per config file"


def test_meta_file_is_written_and_records_provider_name(tmp_path: Path) -> None:
    """Test the scaffold sentinel file is created with the provider name."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target)])

    assert result.exit_code == 0
    meta = target / ".scaffold-meta"
    assert meta.is_file()
    assert "provider_name=acme" in meta.read_text(encoding="utf-8")


def test_dry_run_does_not_create_target(tmp_path: Path) -> None:
    """Test dry-run prints next steps without creating files."""
    target = tmp_path / "acme"

    result = runner.invoke(main_app, ["provider", "scaffold", "acme", "--output-dir", str(target), "--dry-run"])

    assert result.exit_code == 0
    assert "Would create provider scaffold:" in result.output
    assert "Preview without cloud:" in result.output
    assert not target.exists()
