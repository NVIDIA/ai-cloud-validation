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

"""Tests for Pydantic schema models and output schema registry."""

import copy
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from isvctl.config.output_schemas import (
    OUTPUT_SCHEMAS,
    STEP_SCHEMA_MAPPING,
    get_schema_for_step,
    validate_output,
)
from isvctl.config.schema import CommandConfig, CommandOutput, PlatformCommands, RunConfig, StepConfig


class TestCommandConfig:
    """Tests for CommandConfig model."""

    def test_minimal_config(self) -> None:
        """Test creating a minimal command config."""
        config = CommandConfig()
        assert config.command is None
        assert config.args == []
        assert config.timeout == 300
        assert config.skip is False

    def test_full_config(self) -> None:
        """Test creating a fully specified command config."""
        config = CommandConfig(
            command="./stubs/k8s-create.sh",
            args=["--nodes", "4"],
            timeout=600,
            skip=False,
            working_dir="/tmp",
            env={"FOO": "bar"},
        )
        assert config.command == "./stubs/k8s-create.sh"
        assert config.args == ["--nodes", "4"]
        assert config.timeout == 600
        assert config.env == {"FOO": "bar"}

    def test_skip_flag(self) -> None:
        """Test skip flag for unsupported commands."""
        config = CommandConfig(skip=True)
        assert config.skip is True
        assert config.command is None


class TestStepConfig:
    """Tests for StepConfig model."""

    def test_minimal_step(self) -> None:
        """Test creating a minimal step config."""
        step = StepConfig(name="test_step", command="echo")
        assert step.name == "test_step"
        assert step.command == "echo"
        assert step.args == []
        assert step.timeout == 300
        assert step.phase == "setup"
        assert step.skip is False
        assert step.requires_available_validations == []

    def test_full_step(self) -> None:
        """Test creating a fully specified step config."""
        step = StepConfig(
            name="create_vpc",
            command="./scripts/create_vpc.py",
            args=["--name", "test-vpc"],
            timeout=600,
            env={"AWS_REGION": "us-west-2"},
            working_dir="/tmp",
            phase="setup",
            skip=False,
            requires_available_validations=["NewCheck"],
            continue_on_failure=True,
            output_schema="vpc",
        )
        assert step.name == "create_vpc"
        assert step.command == "./scripts/create_vpc.py"
        assert step.args == ["--name", "test-vpc"]
        assert step.timeout == 600
        assert step.env == {"AWS_REGION": "us-west-2"}
        assert step.phase == "setup"
        assert step.requires_available_validations == ["NewCheck"]
        assert step.continue_on_failure is True
        assert step.output_schema == "vpc"


class TestCommandOutput:
    """Tests for CommandOutput model (setup command JSON output)."""

    def test_kubernetes_output(self) -> None:
        """Test parsing Kubernetes setup output."""
        output = CommandOutput(
            platform="kubernetes",
            cluster_name="test-cluster",
            kubernetes={
                "node_count": 4,
                "nodes": ["node1", "node2", "node3", "node4"],
                "total_gpus": 16,
                "driver_version": "580.95.05",
            },
        )
        assert output.platform == "kubernetes"
        assert output.cluster_name == "test-cluster"
        assert output.kubernetes is not None
        assert output.kubernetes.node_count == 4
        assert output.kubernetes.total_gpus == 16

    def test_slurm_output(self) -> None:
        """Test parsing Slurm setup output."""
        output = CommandOutput(
            platform="slurm",
            cluster_name="slurm-cluster",
            slurm={
                "partitions": {
                    "gpu": {"nodes": ["gpu1", "gpu2"], "node_count": 2},
                    "cpu": {"nodes": ["cpu1"], "node_count": 1},
                },
                "cuda_arch": "90",
            },
        )
        assert output.platform == "slurm"
        assert output.slurm is not None
        assert "gpu" in output.slurm.partitions
        assert output.slurm.cuda_arch == "90"

    def test_missing_required_fields(self) -> None:
        """Test that missing required fields raise ValidationError."""
        with pytest.raises(ValidationError):
            CommandOutput()  # Missing platform and cluster_name


class TestRunConfigModel:
    """Tests for RunConfig model."""

    def test_empty_config(self) -> None:
        """Test creating an empty config."""
        config = RunConfig()
        assert config.version == "1.0"
        assert config.commands == {}
        assert config.context == {}

    def test_get_steps(self) -> None:
        """Test getting steps for a platform."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command="./k8s-setup.sh", phase="setup"),
                        StepConfig(name="teardown_cluster", command="./k8s-teardown.sh", phase="teardown"),
                    ]
                ),
                "slurm": PlatformCommands(
                    steps=[
                        StepConfig(name="skipped_step", command="./slurm-setup.sh", skip=True),
                    ]
                ),
            }
        )

        k8s_steps = config.get_steps("kubernetes")
        assert len(k8s_steps) == 2
        assert k8s_steps[0].command == "./k8s-setup.sh"

        # Skipped steps are returned so the executor can record skipped placeholders.
        slurm_steps = config.get_steps("slurm")
        assert len(slurm_steps) == 1
        assert slurm_steps[0].name == "skipped_step"
        assert slurm_steps[0].skip is True

    def test_platform_level_skip_preserves_configured_steps(self) -> None:
        """Test platform-level skip does not hide configured steps."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command="./k8s-setup.sh", phase="setup"),
                        StepConfig(name="teardown_cluster", command="./k8s-teardown.sh", phase="teardown"),
                    ]
                ),
                # Platform-level skip - simpler than skipping each step
                "slurm": PlatformCommands(
                    skip=True,
                    steps=[
                        StepConfig(name="setup_slurm", command="./slurm-setup.sh", phase="setup"),
                    ],
                ),
            }
        )

        # Kubernetes should have steps
        k8s_steps = config.get_steps("kubernetes")
        assert len(k8s_steps) == 2

        # Platform-level skip is handled by the orchestrator, not by dropping steps here.
        slurm_steps = config.get_steps("slurm")
        assert len(slurm_steps) == 1
        assert slurm_steps[0].name == "setup_slurm"

    def test_get_phases(self) -> None:
        """Test getting phases for a platform."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    phases=["setup", "test", "teardown"],
                    steps=[
                        StepConfig(name="setup_cluster", command="./k8s-setup.sh", phase="setup"),
                    ],
                ),
            }
        )

        phases = config.get_phases("kubernetes")
        assert phases == ["setup", "test", "teardown"]

    def test_full_config(self) -> None:
        """Test parsing a full configuration with steps."""
        config = RunConfig.model_validate(
            {
                "version": "1.0",
                "lab": {"id": "lab-001", "name": "Test Lab"},
                "commands": {
                    "kubernetes": {
                        "phases": ["setup", "teardown"],
                        "steps": [
                            {
                                "name": "setup_cluster",
                                "command": "./k8s-setup.sh",
                                "args": ["--nodes", "4"],
                                "timeout": 600,
                                "phase": "setup",
                            },
                            {
                                "name": "teardown_cluster",
                                "command": "./k8s-teardown.sh",
                                "phase": "teardown",
                            },
                        ],
                    }
                },
                "context": {"node_count": 4},
                "tests": {
                    "platform": "kubernetes",
                    "validations": {"kubernetes": [{"K8sNodeCountCheck": {"count": 4}}]},
                },
            }
        )
        assert config.lab is not None
        assert config.lab.id == "lab-001"
        assert config.tests is not None
        assert config.tests.platform == "kubernetes"
        # Verify step-based command structure
        steps = config.get_steps("kubernetes")
        assert len(steps) == 2
        assert steps[0].name == "setup_cluster"
        assert steps[0].command == "./k8s-setup.sh"
        assert steps[1].name == "teardown_cluster"
        assert steps[1].command == "./k8s-teardown.sh"


class TestOutputSchemaMapping:
    """Tests for step name -> schema auto-detection.

    Prevents regressions where overly-broad mapping keys (like "setup")
    silently break platforms that share generic step names.
    """

    def test_setup_does_not_map_to_cluster(self) -> None:
        """'setup' must NOT auto-map to the cluster schema.

        The name 'setup' is used by slurm, k8s, bare_metal, etc. - each with
        different output shapes. Mapping it to 'cluster' broke every non-EKS
        platform (#191). Configs that need cluster validation should set
        output_schema: cluster explicitly.
        """
        schema = get_schema_for_step("setup")
        assert schema != "cluster", (
            "'setup' must not map to 'cluster' - it is used across platforms "
            "with different output shapes. Use output_schema in step config instead."
        )

    def test_all_mapped_schemas_exist(self) -> None:
        """Every schema referenced in STEP_SCHEMA_MAPPING must be registered."""
        for step_name, schema_name in STEP_SCHEMA_MAPPING.items():
            if schema_name is not None:
                assert schema_name in OUTPUT_SCHEMAS, (
                    f"Step '{step_name}' maps to schema '{schema_name}' which is not registered in OUTPUT_SCHEMAS"
                )

    def test_explicit_output_schema_overrides_autodetect(self) -> None:
        """StepConfig.output_schema should take priority over name-based detection."""
        step = StepConfig(name="setup", command="./setup.sh", output_schema="cluster")
        assert step.output_schema == "cluster"
        assert get_schema_for_step(step.name) != "cluster"

    @pytest.mark.parametrize(
        ("step_name", "expected_schema"),
        [
            ("provision_cluster", "cluster"),
            ("create_cluster", "cluster"),
            ("create_network", "network"),
            ("create_test_shared_vpc_cluster", "multi_cluster"),
            ("launch_instance", "instance"),
            ("teardown", "teardown"),
            ("create_access_key", "access_key"),
        ],
    )
    def test_specific_step_names_resolve_correctly(self, step_name: str, expected_schema: str) -> None:
        """Verify well-known step names map to the correct schemas."""
        assert get_schema_for_step(step_name) == expected_schema


class TestOutputSchemaValidation:
    """Tests that representative stub outputs conform to their resolved schemas.

    Each sample mirrors the actual JSON structure produced by the corresponding
    setup stub (e.g., slurm/setup.sh, k8s/_common.sh, aws/eks/setup.sh).
    If a stub's output shape changes, these tests must be updated to match.
    """

    SLURM_SETUP_OUTPUT: ClassVar[dict] = {
        "success": True,
        "platform": "slurm",
        "cluster_name": "demo-slurm",
        "slurm": {
            "partitions": {"gpu": {"nodes": ["n1", "n2"]}, "all": {"nodes": ["n1", "n2"]}},
            "cuda_arch": "90",
            "storage_path": "/home",
            "default_partition": "gpu",
            "driver_version": "560.35.03",
            "gpu_per_node": 4,
            "total_gpus": 8,
        },
    }

    K8S_SETUP_OUTPUT: ClassVar[dict] = {
        "success": True,
        "platform": "kubernetes",
        "cluster_name": "test-cluster",
        "kubernetes": {
            "driver_version": "560.35.03",
            "node_count": 3,
            "nodes": ["node1", "node2", "node3"],
            "gpu_node_count": 2,
            "gpu_per_node": 4,
            "total_gpus": 8,
            "gpu_operator_namespace": "nvidia-gpu-operator",
            "runtime_class": "nvidia",
            "gpu_resource_name": "nvidia.com/gpu",
        },
    }

    EKS_SETUP_OUTPUT: ClassVar[dict] = {
        "success": True,
        "platform": "kubernetes",
        "cluster_name": "eks-cluster",
        "node_count": 3,
        "endpoint": "https://eks.amazonaws.com",
        "gpu_count": 8,
        "gpu_per_node": 4,
        "driver_version": "560.35.03",
        "kubeconfig_path": "/tmp/kubeconfig",
        "kubernetes": {
            "driver_version": "560.35.03",
            "node_count": 3,
            "nodes": ["node1", "node2", "node3"],
            "gpu_node_count": 2,
            "gpu_per_node": 4,
            "total_gpus": 8,
            "gpu_operator_namespace": "nvidia-gpu-operator",
            "cluster_autoscaler_namespace": "kube-system",
            "cluster_autoscaler_deployment": "cluster-autoscaler",
            "runtime_class": "nvidia",
            "gpu_resource_name": "nvidia.com/gpu",
        },
    }

    MULTI_CLUSTER_OUTPUT: ClassVar[dict[str, Any]] = {
        "success": True,
        "platform": "kubernetes",
        "test_id": "K8S26-01",
        "tenancy_id": "123456789012",
        "network_id": "vpc-123",
        "clusters": [
            {
                "name": "isvtest-eks-dev",
                "role": "primary",
                "tenancy_id": "123456789012",
                "network_id": "vpc-123",
                "status": "ACTIVE",
            },
            {
                "name": "isvtest-eks-dev-shared-vpc",
                "role": "secondary",
                "tenancy_id": "123456789012",
                "network_id": "vpc-123",
                "status": "ACTIVE",
                "ready_node_count": 1,
            },
        ],
    }

    @staticmethod
    def _backend_switch_fabric_output() -> dict[str, Any]:
        """Return a backend switch fabric schema payload.

        Parameters: none. Returns: dict[str, Any]. Provides a valid baseline
        output for tests that mutate switch fabric fields.
        """
        return {
            "success": True,
            "platform": "network",
            "node_id": "compute-node-1",
            "fabric": {
                "leaf_switch_ids": ["leaf-1"],
                "spine_switch_ids": ["spine-1"],
                "core_switch_ids": ["core-1"],
            },
            "tests": {
                "node_resolved": {"passed": True},
                "leaf_switch_ids_present": {"passed": True},
                "spine_switch_ids_present": {"passed": True},
                "core_switch_ids_present": {"passed": True},
            },
        }

    @staticmethod
    def _nvlink_domain_output() -> dict[str, Any]:
        """Return an NVLink domain schema payload.

        Parameters: none. Returns: dict[str, Any]. Provides a valid baseline
        output for tests that mutate NVLink domain fields.
        """
        return {
            "success": True,
            "platform": "network",
            "node_id": "compute-node-1",
            "nvlink_supported": True,
            "nvlink_domain_id": "domain-1",
            "tests": {
                "node_resolved": {"passed": True},
                "nvlink_support_detected": {"passed": True},
                "nvlink_domain_id_present": {"passed": True},
            },
        }

    def test_slurm_output_passes_autodetected_schema(self) -> None:
        """Slurm setup.sh output must pass its auto-detected schema (generic)."""
        schema = get_schema_for_step("setup")
        is_valid, errors = validate_output(self.SLURM_SETUP_OUTPUT, schema)
        assert is_valid, f"Slurm output failed '{schema}' schema: {errors}"

    def test_k8s_output_passes_autodetected_schema(self) -> None:
        """k8s _common.sh output must pass its auto-detected schema (generic).

        Note: _common.sh does NOT put node_count at the top level, so it
        cannot pass the 'cluster' schema without modification.
        """
        schema = get_schema_for_step("setup")
        is_valid, errors = validate_output(self.K8S_SETUP_OUTPUT, schema)
        assert is_valid, f"K8s output failed '{schema}' schema: {errors}"

    def test_k8s_output_fails_cluster_schema(self) -> None:
        """k8s _common.sh output lacks top-level node_count, so cluster schema must reject it."""
        is_valid, _errors = validate_output(self.K8S_SETUP_OUTPUT, "cluster")
        assert not is_valid, "K8s _common.sh output should NOT pass cluster schema (no top-level node_count)"

    def test_eks_output_passes_cluster_schema(self) -> None:
        """EKS setup.sh output has top-level node_count and must pass cluster schema."""
        is_valid, errors = validate_output(self.EKS_SETUP_OUTPUT, "cluster")
        assert is_valid, f"EKS output failed 'cluster' schema: {errors}"

    def test_multi_cluster_output_passes_schema(self) -> None:
        """K8S26-01 shared-VPC cluster output must pass the multi_cluster schema."""
        is_valid, errors = validate_output(self.MULTI_CLUSTER_OUTPUT, "multi_cluster")
        assert is_valid, f"Multi-cluster output failed 'multi_cluster' schema: {errors}"

    def test_multi_cluster_output_passes_without_cluster_roles(self) -> None:
        """K8S26-01 proves cluster coexistence without requiring role labels."""
        output = copy.deepcopy(self.MULTI_CLUSTER_OUTPUT)
        for cluster in output["clusters"]:
            cluster.pop("role")

        is_valid, errors = validate_output(output, "multi_cluster")

        assert is_valid, f"Multi-cluster output without roles failed 'multi_cluster' schema: {errors}"

    @pytest.mark.parametrize(
        "field_name",
        ["leaf_switch_ids", "spine_switch_ids", "core_switch_ids"],
    )
    def test_backend_switch_fabric_schema_rejects_empty_switch_id_collections(self, field_name: str) -> None:
        """Backend switch fabric schema must reject empty fabric switch ID arrays."""
        output = self._backend_switch_fabric_output()
        output["fabric"][field_name] = []

        is_valid, errors = validate_output(output, "backend_switch_fabric")

        assert not is_valid
        assert field_name in errors[0]

    @pytest.mark.parametrize(
        "test_name",
        ["node_resolved", "leaf_switch_ids_present", "spine_switch_ids_present", "core_switch_ids_present"],
    )
    def test_backend_switch_fabric_schema_requires_test_keys(self, test_name: str) -> None:
        """Backend switch fabric schema must require all contract test result keys."""
        output = self._backend_switch_fabric_output()
        output["tests"].pop(test_name)

        is_valid, errors = validate_output(output, "backend_switch_fabric")

        assert not is_valid
        assert test_name in errors[0]

    @pytest.mark.parametrize(
        "test_name",
        ["node_resolved", "nvlink_support_detected", "nvlink_domain_id_present"],
    )
    def test_nvlink_domain_schema_requires_test_keys(self, test_name: str) -> None:
        """NVLink domain schema must require all contract test result keys."""
        output = self._nvlink_domain_output()
        output["tests"].pop(test_name)

        is_valid, errors = validate_output(output, "nvlink_domain")

        assert not is_valid
        assert test_name in errors[0]

    def test_nvlink_domain_schema_requires_domain_id_when_supported(self) -> None:
        """NVLink domain schema must require nvlink_domain_id only for NVLink-supported nodes."""
        output = self._nvlink_domain_output()
        output.pop("nvlink_domain_id")

        is_valid, errors = validate_output(output, "nvlink_domain")

        assert not is_valid
        assert "nvlink_domain_id" in errors[0]

    def test_nvlink_domain_schema_allows_missing_domain_id_when_unsupported(self) -> None:
        """Non-NVLink nodes may omit nvlink_domain_id."""
        output = self._nvlink_domain_output()
        output["nvlink_supported"] = False
        output.pop("nvlink_domain_id")
        output["tests"]["nvlink_domain_id_present"] = {"passed": False}

        is_valid, errors = validate_output(output, "nvlink_domain")

        assert is_valid, f"Unsupported NVLink output failed schema validation: {errors}"
