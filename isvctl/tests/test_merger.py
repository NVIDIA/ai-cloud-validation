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

"""Tests for YAML merging functionality."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from isvctl.config.merger import (
    apply_set_value,
    deep_merge,
    merge_yaml_files,
    parse_set_value,
)
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.context import Context


class TestDeepMerge:
    """Tests for deep_merge function."""

    def test_simple_merge(self) -> None:
        """Test merging flat dictionaries."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        """Test merging nested dictionaries."""
        base = {"outer": {"a": 1, "b": 2}}
        override = {"outer": {"b": 3, "c": 4}}
        result = deep_merge(base, override)
        assert result == {"outer": {"a": 1, "b": 3, "c": 4}}

    def test_list_replacement(self) -> None:
        """Test that lists are replaced, not concatenated."""
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        result = deep_merge(base, override)
        assert result == {"items": [4, 5]}

    def test_original_not_modified(self) -> None:
        """Test that original dicts are not modified."""
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        result = deep_merge(base, override)
        assert base == {"a": {"b": 1}}
        assert override == {"a": {"c": 2}}
        assert result == {"a": {"b": 1, "c": 2}}


class TestParseSetValue:
    """Tests for parse_set_value function."""

    def test_simple_key_value(self) -> None:
        """Test parsing simple key=value."""
        path, value = parse_set_value("key=value")
        assert path == ["key"]
        assert value == "value"

    def test_dotted_path(self) -> None:
        """Test parsing dotted key path."""
        path, value = parse_set_value("context.node_count=8")
        assert path == ["context", "node_count"]
        assert value == 8

    def test_boolean_value(self) -> None:
        """Test parsing boolean values."""
        path, value = parse_set_value("enabled=true")
        assert path == ["enabled"]
        assert value is True

    def test_list_value(self) -> None:
        """Test parsing list values."""
        path, value = parse_set_value("items=[1, 2, 3]")
        assert path == ["items"]
        assert value == [1, 2, 3]

    def test_invalid_format(self) -> None:
        """Test that invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid --set format"):
            parse_set_value("no_equals_sign")

    def test_empty_key_raises(self) -> None:
        """Test that empty key raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            parse_set_value("=value")

    def test_yaml_error_fallback(self) -> None:
        """Test that invalid YAML falls back to string."""
        path, value = parse_set_value("key={invalid yaml")
        assert path == ["key"]
        assert value == "{invalid yaml"  # Falls back to string


class TestApplySetValue:
    """Tests for apply_set_value function."""

    def test_simple_set(self) -> None:
        """Test setting a simple value."""
        config: dict[str, Any] = {}
        apply_set_value(config, ["key"], "value")
        assert config == {"key": "value"}

    def test_nested_set(self) -> None:
        """Test setting a nested value."""
        config: dict[str, Any] = {}
        apply_set_value(config, ["context", "node_count"], 8)
        assert config == {"context": {"node_count": 8}}

    def test_override_existing(self) -> None:
        """Test overriding an existing value."""
        config = {"context": {"node_count": 4, "other": "keep"}}
        apply_set_value(config, ["context", "node_count"], 8)
        assert config == {"context": {"node_count": 8, "other": "keep"}}

    def test_overwrite_non_dict_with_dict(self) -> None:
        """Test overwriting a non-dict value when creating nested path."""
        config: dict[str, Any] = {"context": "string"}  # Not a dict
        apply_set_value(config, ["context", "node_count"], 8)
        assert config == {"context": {"node_count": 8}}  # Overwrites string with dict


class TestMergeYamlFiles:
    """Tests for merge_yaml_files function."""

    def test_merge_single_file(self, tmp_path: Path) -> None:
        """Test merging a single YAML file."""
        file1 = tmp_path / "config.yaml"
        file1.write_text("a: 1\nb: 2")

        result = merge_yaml_files([str(file1)])
        assert result == {"a": 1, "b": 2}

    def test_merge_multiple_files(self, tmp_path: Path) -> None:
        """Test merging multiple YAML files."""
        file1 = tmp_path / "base.yaml"
        file1.write_text("a: 1\nb: 2")
        file2 = tmp_path / "override.yaml"
        file2.write_text("b: 3\nc: 4")

        result = merge_yaml_files([str(file1), str(file2)])
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_merge_with_set_values(self, tmp_path: Path) -> None:
        """Test --set overrides."""
        file1 = tmp_path / "config.yaml"
        file1.write_text("node_count: 4\nother: keep")

        result = merge_yaml_files([str(file1)], set_values=["node_count=8"])
        assert result == {"node_count": 8, "other": "keep"}

    def test_merge_with_nested_set_values(self, tmp_path: Path) -> None:
        """Test --set with nested paths."""
        file1 = tmp_path / "config.yaml"
        file1.write_text("context:\n  node_count: 4")

        result = merge_yaml_files([str(file1)], set_values=["context.node_count=8"])
        assert result == {"context": {"node_count": 8}}

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        """Test that missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="not found"):
            merge_yaml_files([str(tmp_path / "nonexistent.yaml")])

    def test_non_dict_yaml_raises(self, tmp_path: Path) -> None:
        """Test that non-dict YAML raises ValueError."""
        file1 = tmp_path / "invalid.yaml"
        file1.write_text("- item1\n- item2")  # List, not dict

        with pytest.raises(ValueError, match="must contain a YAML mapping"):
            merge_yaml_files([str(file1)])

    def test_empty_file_ignored(self, tmp_path: Path) -> None:
        """Test that empty YAML files are ignored."""
        file1 = tmp_path / "empty.yaml"
        file1.write_text("")
        file2 = tmp_path / "valid.yaml"
        file2.write_text("a: 1")

        result = merge_yaml_files([str(file1), str(file2)])
        assert result == {"a": 1}


class TestImportDirective:
    """Tests for the ``import:`` directive in YAML configs."""

    def test_simple_import(self, tmp_path: Path) -> None:
        """Imported file is used as the base."""
        base = tmp_path / "base.yaml"
        base.write_text("a: 1\nb: 2")
        child = tmp_path / "child.yaml"
        child.write_text("import:\n  - base.yaml\nb: 99\nc: 3")

        result = merge_yaml_files([str(child)])
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_import_stripped_from_result(self, tmp_path: Path) -> None:
        """The import key must not leak into the merged output."""
        base = tmp_path / "base.yaml"
        base.write_text("x: 1")
        child = tmp_path / "child.yaml"
        child.write_text("import:\n  - base.yaml\ny: 2")

        result = merge_yaml_files([str(child)])
        assert "import" not in result
        assert result == {"x": 1, "y": 2}

    def test_relative_path_resolution(self, tmp_path: Path) -> None:
        """Import paths are resolved relative to the importing file."""
        sub = tmp_path / "sub"
        sub.mkdir()
        base = tmp_path / "templates" / "t.yaml"
        base.parent.mkdir()
        base.write_text("val: from_template")
        child = sub / "provider.yaml"
        child.write_text("import:\n  - ../templates/t.yaml\nval: overridden")

        result = merge_yaml_files([str(child)])
        assert result == {"val": "overridden"}

    def test_import_falls_back_to_current_working_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Checkout-root-relative imports work for out-of-tree provider configs."""
        repo_root = tmp_path / "repo"
        template = repo_root / "isvctl" / "configs" / "suites" / "vm.yaml"
        template.parent.mkdir(parents=True)
        template.write_text(
            "tests:\n  cluster_name: template\n  suite_only: inherited\n",
            encoding="utf-8",
        )

        provider_dir = tmp_path / "provider" / "config"
        provider_dir.mkdir(parents=True)
        provider = provider_dir / "vm.yaml"
        provider.write_text(
            "import:\n  - isvctl/configs/suites/vm.yaml\ntests:\n  cluster_name: provider\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(repo_root / "isvctl")

        result = merge_yaml_files([str(provider)])
        assert result == {"tests": {"cluster_name": "provider", "suite_only": "inherited"}}

    def test_multiple_imports(self, tmp_path: Path) -> None:
        """Multiple imports are merged in order, child wins."""
        (tmp_path / "a.yaml").write_text("x: 1\ny: from_a")
        (tmp_path / "b.yaml").write_text("y: from_b\nz: 3")
        child = tmp_path / "child.yaml"
        child.write_text("import:\n  - a.yaml\n  - b.yaml\nz: 99")

        result = merge_yaml_files([str(child)])
        assert result == {"x": 1, "y": "from_b", "z": 99}

    def test_nested_imports(self, tmp_path: Path) -> None:
        """Imports can themselves import other files."""
        (tmp_path / "grandparent.yaml").write_text("a: 1")
        (tmp_path / "parent.yaml").write_text("import:\n  - grandparent.yaml\nb: 2")
        child = tmp_path / "child.yaml"
        child.write_text("import:\n  - parent.yaml\nc: 3")

        result = merge_yaml_files([str(child)])
        assert result == {"a": 1, "b": 2, "c": 3}

    def test_diamond_dependency(self, tmp_path: Path) -> None:
        """Two siblings importing the same file (diamond) must not raise."""
        (tmp_path / "common.yaml").write_text("shared: 1")
        (tmp_path / "a.yaml").write_text("import:\n  - common.yaml\na: 2")
        (tmp_path / "b.yaml").write_text("import:\n  - common.yaml\nb: 3")
        child = tmp_path / "child.yaml"
        child.write_text("import:\n  - a.yaml\n  - b.yaml\nc: 4")

        result = merge_yaml_files([str(child)])
        assert result == {"shared": 1, "a": 2, "b": 3, "c": 4}

    def test_circular_import_raises(self, tmp_path: Path) -> None:
        """Circular imports must raise ValueError."""
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        a.write_text("import:\n  - b.yaml\nx: 1")
        b.write_text("import:\n  - a.yaml\ny: 2")

        with pytest.raises(ValueError, match="Circular import"):
            merge_yaml_files([str(a)])

    def test_self_import_raises(self, tmp_path: Path) -> None:
        """A file importing itself must raise ValueError."""
        f = tmp_path / "self.yaml"
        f.write_text("import:\n  - self.yaml\nx: 1")

        with pytest.raises(ValueError, match="Circular import"):
            merge_yaml_files([str(f)])

    def test_import_missing_file_raises(self, tmp_path: Path) -> None:
        """Importing a nonexistent file must raise FileNotFoundError."""
        child = tmp_path / "child.yaml"
        child.write_text("import:\n  - missing.yaml\nx: 1")

        with pytest.raises(FileNotFoundError):
            merge_yaml_files([str(child)])

    def test_import_entry_must_be_string_or_path(self, tmp_path: Path) -> None:
        """Invalid import entry types raise a config-specific ValueError."""
        child = tmp_path / "child.yaml"
        child.write_text("import:\n  - 123\nx: 1")

        with pytest.raises(ValueError, match=r"Import entries must be strings or paths.*child\.yaml"):
            merge_yaml_files([str(child)])

    def test_import_with_f_flag_merge(self, tmp_path: Path) -> None:
        """Import + additional -f file are merged correctly."""
        (tmp_path / "template.yaml").write_text("a: 1\nb: 2")
        provider = tmp_path / "provider.yaml"
        provider.write_text("import:\n  - template.yaml\nb: 99")
        extra = tmp_path / "extra.yaml"
        extra.write_text("c: 3")

        result = merge_yaml_files([str(provider), str(extra)])
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_no_import_key_unchanged(self, tmp_path: Path) -> None:
        """Files without import: work exactly as before."""
        f = tmp_path / "plain.yaml"
        f.write_text("a: 1\nb: 2")

        result = merge_yaml_files([str(f)])
        assert result == {"a": 1, "b": 2}

    def test_import_single_string(self, tmp_path: Path) -> None:
        """import: can be a single string instead of a list."""
        (tmp_path / "base.yaml").write_text("x: 1")
        child = tmp_path / "child.yaml"
        child.write_text("import: base.yaml\ny: 2")

        result = merge_yaml_files([str(child)])
        assert result == {"x": 1, "y": 2}


class TestDictChecksDeepMerge:
    """Tests for dict-based checks merging via deep_merge."""

    def test_override_single_check_param(self) -> None:
        """Provider can override one check's param without affecting others."""
        template = {
            "tests": {
                "validations": {
                    "ssh": {
                        "step": "describe_instance",
                        "checks": {
                            "ConnectivityCheck": {},
                            "OsCheck": {"expected_os": "ubuntu"},
                        },
                    }
                }
            }
        }
        provider = {
            "tests": {
                "validations": {
                    "ssh": {
                        "checks": {
                            "OsCheck": {"expected_os": "rhel"},
                        }
                    }
                }
            }
        }
        result = deep_merge(template, provider)
        checks = result["tests"]["validations"]["ssh"]["checks"]
        assert checks["ConnectivityCheck"] == {}
        assert checks["OsCheck"] == {"expected_os": "rhel"}
        assert result["tests"]["validations"]["ssh"]["step"] == "describe_instance"

    def test_add_new_check(self) -> None:
        """Provider can add a new check to an existing group."""
        template = {"tests": {"validations": {"gpu": {"checks": {"GpuCheck": {"expected_gpus": 8}}}}}}
        provider = {"tests": {"validations": {"gpu": {"checks": {"GpuStressCheck": {"runtime": 30}}}}}}
        result = deep_merge(template, provider)
        checks = result["tests"]["validations"]["gpu"]["checks"]
        assert "GpuCheck" in checks
        assert "GpuStressCheck" in checks

    def test_add_new_validation_group(self) -> None:
        """Provider can add an entirely new validation group."""
        template = {"tests": {"validations": {"ssh": {"checks": {"ConnectivityCheck": {}}}}}}
        provider = {
            "tests": {"validations": {"image_installed": {"step": "verify_image", "checks": {"StepSuccessCheck": {}}}}}
        }
        result = deep_merge(template, provider)
        assert "ssh" in result["tests"]["validations"]
        assert "image_installed" in result["tests"]["validations"]

    def test_template_untouched(self) -> None:
        """deep_merge must not mutate the template."""
        template = {"tests": {"validations": {"ssh": {"checks": {"OsCheck": {"expected_os": "ubuntu"}}}}}}
        import copy

        original = copy.deepcopy(template)
        provider = {"tests": {"validations": {"ssh": {"checks": {"OsCheck": {"expected_os": "rhel"}}}}}}
        deep_merge(template, provider)
        assert template == original


class TestImportEndToEnd:
    """Integration test using real config files to validate the import approach."""

    CONFIGS_DIR = Path(__file__).parent.parent / "configs"

    def test_aws_iam_inherits_test_validations(self) -> None:
        """providers/aws/config/iam.yaml imports suites/iam.yaml and gets its validations."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "aws" / "config" / "iam.yaml"])

        assert "commands" in result, "AWS provider must supply commands"
        assert "tests" in result, "Merged config must have tests"
        validations = result["tests"]["validations"]
        assert "setup_checks" in validations
        assert "credentials" in validations
        assert "teardown_checks" in validations
        assert result["tests"]["cluster_name"] == "aws-iam-validation"
        assert result["tests"]["platform"] == "iam"

    def test_my_isv_observability_declares_raw_platform_for_report_upload(self) -> None:
        """Raw observability config exposes platform for upload paths that skip imports."""
        config_path = self.CONFIGS_DIR / "providers" / "my-isv" / "config" / "observability.yaml"

        raw_config = yaml.safe_load(config_path.read_text()) or {}
        assert raw_config.get("tests", {}).get("platform") == "observability"

        result = merge_yaml_files([config_path])
        assert result["tests"]["platform"] == "observability"

    def test_aws_observability_inherits_supported_validations(self) -> None:
        """AWS observability imports the canonical suite and wires supported steps."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "aws" / "config" / "observability.yaml"])

        assert result["tests"]["platform"] == "observability"
        assert result["tests"]["cluster_name"] == "aws-observability-validation"

        steps = result["commands"]["observability"]["steps"]
        step_names = {step["name"] for step in steps}
        assert {
            "create_network",
            "enable_vpc_flow_logs",
            "launch_host",
            "vpc_flow_logs",
            "host_syslogs",
            "bmc_sel_logs",
            "bmc_gpu_telemetry",
        } <= step_names

        steps_by_name = {step["name"]: step for step in steps}
        assert steps_by_name["host_syslogs"]["timeout"] >= 600
        assert "{{steps.enable_vpc_flow_logs.flow_log_id}}" in steps_by_name["vpc_flow_logs"]["args"]
        launch_host_args = steps_by_name["launch_host"]["args"]
        key_name_arg = launch_host_args[launch_host_args.index("--key-name") + 1]
        assert key_name_arg == "isv-observability-host-key-{{steps.create_network.network_id}}"

        validations = result["tests"]["validations"]
        assert validations["network_logs"]["checks"]["VpcFlowLogsCheck"]["step"] == "vpc_flow_logs"
        assert validations["host_logs"]["checks"]["HostSyslogCheck"]["step"] == "host_syslogs"
        assert validations["bmc_logs"]["checks"]["BmcSelLogsCheck"]["step"] == "bmc_sel_logs"
        assert validations["bmc_telemetry"]["checks"]["BmcGpuTelemetryCheck"]["step"] == "bmc_gpu_telemetry"

        excluded = set(result["tests"].get("exclude", {}).get("tests", []))
        assert "BmcSelLogsCheck" not in excluded
        assert "BmcGpuTelemetryCheck" not in excluded

    def test_aws_iam_commands_override_test_stubs(self) -> None:
        """AWS commands replace the test definition's placeholder stubs."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "aws" / "config" / "iam.yaml"])
        steps = result["commands"]["iam"]["steps"]
        assert any("scripts/iam" in s["command"] for s in steps)

    def test_aws_eks_inherits_k8s_validations(self) -> None:
        """providers/aws/config/eks.yaml imports suites/k8s.yaml and gets K8s checks."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "aws" / "config" / "eks.yaml"])

        assert "commands" in result
        assert "tests" in result
        validations = result["tests"]["validations"]
        assert "kubernetes" in validations
        assert "k8s_workloads" in validations
        node_count_check = validations["kubernetes"]["checks"]["K8sNodeCountCheck"]
        gpu_pod_access_check = validations["kubernetes"]["checks"]["K8sGpuPodAccessCheck"]
        gpu_capacity_check = validations["kubernetes"]["checks"]["K8sGpuCapacityCheck"]
        node_count = node_count_check["count"]
        exclude_selector = node_count_check["exclude_label_selector"]
        total_gpu_count = gpu_pod_access_check["total_gpu_count"]
        expected_total = gpu_capacity_check["expected_total"]
        # Separate CPU and GPU test pools both carry the stable pool marker, so
        # the baseline node count excludes them with one static selector.
        assert exclude_selector == "isv.ncp.validation/pool=test"

        # Separate CPU and GPU node pools are created with independent state
        # files and instance types (K8S06), each validated by its own check.
        steps = result["commands"]["kubernetes"]["steps"]
        steps_by_name = {s["name"]: s for s in steps}
        step_names = [step["name"] for step in steps]
        assert "create_test_shared_vpc_cluster" in steps_by_name
        assert "destroy_test_shared_vpc_cluster" in steps_by_name
        assert (
            step_names.index("setup")
            < step_names.index("create_test_shared_vpc_cluster")
            < step_names.index("create_test_node_pool")
        )
        assert (
            step_names.index("destroy_test_gpu_node_pool")
            < step_names.index("destroy_test_shared_vpc_cluster")
            < step_names.index("teardown")
        )
        assert steps_by_name["create_test_shared_vpc_cluster"]["output_schema"] == "multi_cluster"
        assert steps_by_name["create_test_shared_vpc_cluster"]["requires_available_validations"] == [
            "K8sMultiClusterSameVpcCheck"
        ]
        cpu_create = steps_by_name["create_test_node_pool"]["env"]
        gpu_create = steps_by_name["create_test_gpu_node_pool"]["env"]
        assert cpu_create["NODE_POOL_STATE_FILE"] != gpu_create["NODE_POOL_STATE_FILE"]
        assert cpu_create["TF_VAR_test_pool_node_type"] == "cpu"
        assert gpu_create["TF_VAR_test_pool_node_type"] == "gpu"
        # The GPU destroy step must target the GPU pool's state file.
        assert (
            steps_by_name["destroy_test_gpu_node_pool"]["env"]["NODE_POOL_STATE_FILE"]
            == gpu_create["NODE_POOL_STATE_FILE"]
        )
        node_pool_checks = validations["k8s_node_pools"]
        checked_steps = {next(iter(entry.values()))["step"] for entry in node_pool_checks}
        assert {"create_test_node_pool", "create_test_gpu_node_pool", "update_test_node_pool"} <= checked_steps
        multi_cluster_check = validations["k8s_multi_cluster"]["checks"]["K8sMultiClusterSameVpcCheck"]
        assert multi_cluster_check["step"] == "create_test_shared_vpc_cluster"

        config = RunConfig.model_validate(result)
        context = Context(config)
        for step in config.get_steps("kubernetes"):
            context.set_step_phase(step.name, step.phase or "setup")
        context.set_step_output(
            "setup",
            {"kubernetes": {"node_count": 3, "gpu_node_count": 1, "gpu_per_node": 1, "total_gpus": 1}},
        )
        context.set_step_output("create_test_gpu_node_pool", {"expected_replicas": 1})
        assert context.render_string(node_count) == "3"
        assert context.render_string(exclude_selector) == "isv.ncp.validation/pool=test"
        assert context.render_string(total_gpu_count) == "2"
        assert context.render_string(expected_total) == "2"
        assert result["tests"]["platform"] == "kubernetes"

    def test_aws_eks_does_not_hardcode_world_open_endpoint_allowlist(self) -> None:
        """EKS setup must not create clusters that make the security suite fail."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "aws" / "config" / "eks.yaml"])
        setup_step = next(step for step in result["commands"]["kubernetes"]["steps"] if step["name"] == "setup")
        setup_env = setup_step.get("env", {})

        assert "TF_VAR_cluster_endpoint_public_access_cidrs" not in setup_env
        assert "0.0.0.0/0" not in str(setup_step)

    def test_aws_bare_metal_overrides_serial_console_retention_check(self) -> None:
        """AWS BM must not inherit the retention check until archive evidence exists."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "aws" / "config" / "bare_metal.yaml"])

        checks = result["tests"]["validations"]["serial_console"]["checks"]
        assert checks == [{"SerialConsoleCheck": {}}]

    def test_microk8s_inherits_k8s_validations(self) -> None:
        """providers/microk8s.yaml imports suites/k8s.yaml and adds overrides."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "microk8s.yaml"])

        assert "tests" in result
        validations = result["tests"]["validations"]
        assert "kubernetes" in validations
        assert "bare_metal" in validations  # microk8s adds host checks
        assert "reframe" in validations  # microk8s adds reframe checks

    def test_minikube_inherits_k8s_validations(self) -> None:
        """providers/minikube.yaml imports suites/k8s.yaml and adds overrides."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "minikube.yaml"])

        assert "tests" in result
        validations = result["tests"]["validations"]
        assert "kubernetes" in validations
        assert "bare_metal" in validations  # minikube adds host checks
        assert "reframe" in validations  # minikube adds reframe checks

    def test_k3s_inherits_k8s_validations(self) -> None:
        """providers/k3s.yaml imports suites/k8s.yaml and adds overrides."""
        result = merge_yaml_files([self.CONFIGS_DIR / "providers" / "k3s.yaml"])

        assert "tests" in result
        validations = result["tests"]["validations"]
        assert "kubernetes" in validations
        assert "bare_metal" in validations  # k3s adds host checks
        assert "reframe" in validations  # k3s adds reframe checks
