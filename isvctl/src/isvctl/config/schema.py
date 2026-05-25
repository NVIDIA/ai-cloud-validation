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

"""Pydantic schema models for isvctl configuration.

This module defines the unified schema for the merged configuration that
combines lab settings, lifecycle commands, context variables, and test config.

The key validation point is the command output schema - ISV stubs must return
JSON that matches these schemas, which then become the inventory for tests.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LabConfig(BaseModel):
    """Lab/environment metadata.

    Contains information about the ISV lab environment being tested.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(default=None, description="Lab identifier")
    name: str | None = Field(default=None, description="Human-readable lab name")
    bastion_host: str | None = Field(default=None, description="Bastion host IP or hostname")
    total_nodes: int | None = Field(default=None, description="Total number of nodes in the lab")


class CommandConfig(BaseModel):
    """Configuration for an ISV lifecycle command (stub).

    Each command defines how to invoke an ISV-provided script that performs
    cluster lifecycle operations (setup, teardown, etc.).
    """

    model_config = ConfigDict(extra="allow")

    command: str | None = Field(default=None, description="Command to execute")
    args: list[str] = Field(default_factory=list, description="Command arguments (supports Jinja2 templating)")
    timeout: int = Field(default=300, description="Timeout in seconds")
    skip: bool = Field(default=False, description="Skip this command (ISV doesn't support this operation)")
    working_dir: str | None = Field(default=None, description="Working directory for command execution")
    env: dict[str, str] = Field(default_factory=dict, description="Additional environment variables")


class StepConfig(BaseModel):
    """Configuration for a single step in a command sequence.

    Steps execute sequentially, with each step:
    - Producing JSON output that is validated against an explicit or auto-detected schema
    - Passing output to subsequent steps via Jinja2 templating

    Validations are defined separately in tests.validations, not inline with steps.
    Use the `phase` field in validation config to control when validations run.

    Output schema resolution order:
    1. Explicit `output_schema` field (if provided)
    2. Auto-detect from step name (e.g., 'launch_instance' -> 'instance')
    3. Fallback to 'generic' schema
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Unique identifier for this step")
    command: str = Field(description="Command to execute")
    args: list[str] = Field(
        default_factory=list,
        description="Command arguments (supports Jinja2 templating with {{ steps.prev_step.field }})",
    )
    timeout: int = Field(default=300, description="Timeout in seconds")
    env: dict[str, str] = Field(default_factory=dict, description="Additional environment variables")
    working_dir: str | None = Field(default=None, description="Working directory for command execution")
    skip: bool = Field(default=False, description="Skip this step")
    continue_on_failure: bool = Field(default=False, description="Continue to next step even if this step fails")
    phase: str = Field(
        default="setup",
        description="Phase this step belongs to: 'setup', 'test', or 'teardown'",
    )
    output_schema: str | None = Field(
        default=None,
        description="Schema name to validate output against. If not set, auto-detected from step name.",
    )
    sensitive_args: list[str] = Field(
        default_factory=list,
        description="Additional argument patterns to mask in logs (e.g., ['--my-secret'])",
    )


class PlatformCommands(BaseModel):
    """Lifecycle commands for a specific platform.

    Groups commands for a platform (kubernetes, slurm, bare_metal, network, vm, iam, image_registry, security).
    Supports skip at both platform level (skips all phases) and phase level.

    The `phases` field defines the execution order. Steps are grouped by their `phase`
    field and executed in the order defined by `phases`. Validations run after each phase.

    Example:
       ```yaml
       network:
         phases: ["setup", "test", "teardown"]  # Defines execution order
         steps:
           - name: create_vpc
             phase: setup
             command: "./create_vpc.py"
           - name: cleanup
             phase: teardown
             command: "./teardown.py"
       ```

    If a step's phase is not in the phases list, an error is raised.

    Supports skip at both platform level (skips all phases) and step level.
    """

    model_config = ConfigDict(extra="allow")

    skip: bool = Field(default=False, description="Skip all commands for this platform")
    phases: list[str] = Field(
        default_factory=lambda: ["setup", "teardown"],
        description="Ordered list of phases to execute. Steps are grouped by phase and run in this order.",
    )
    steps: list[StepConfig] = Field(
        default_factory=list,
        description="Sequential command steps grouped by phase",
    )


class KubernetesNodeOutput(BaseModel):
    """Schema for a single Kubernetes node in command output."""

    name: str = Field(description="Node name")
    ip: str | None = Field(default=None, description="Node IP address")
    gpus: int | None = Field(default=None, description="Number of GPUs on this node")


class KubernetesOutput(BaseModel):
    """Schema for Kubernetes cluster setup output.

    This is the JSON schema that ISV setup-kubernetes-cluster stubs must return.
    It maps directly to the inventory format used by isvtest.
    """

    model_config = ConfigDict(extra="allow")

    driver_version: str | None = Field(default=None, description="NVIDIA driver version")
    node_count: int | None = Field(default=None, description="Total number of nodes")
    nodes: list[str | KubernetesNodeOutput] = Field(
        default_factory=list, description="List of node names or node objects"
    )
    gpu_node_count: int | None = Field(default=None, description="Number of GPU nodes")
    gpu_per_node: int | None = Field(default=None, description="GPUs per node")
    total_gpus: int | None = Field(default=None, description="Total GPUs in cluster")
    control_plane_address: str | None = Field(default=None, description="Control plane IP/hostname")
    kubeconfig_path: str | None = Field(default=None, description="Path to kubeconfig file")
    gpu_operator_namespace: str = Field(default="nvidia-gpu-operator", description="GPU operator namespace")
    control_plane_namespace: str = Field(
        default="kube-system", description="Namespace where control-plane components run"
    )
    runtime_class: str = Field(default="nvidia", description="Kubernetes RuntimeClass for GPU pods")
    gpu_resource_name: str = Field(default="nvidia.com/gpu", description="GPU resource name")


class SlurmPartitionOutput(BaseModel):
    """Schema for a Slurm partition in command output."""

    nodes: list[str] = Field(default_factory=list, description="Node names in partition")
    node_count: int | None = Field(default=None, description="Number of nodes")


class SlurmOutput(BaseModel):
    """Schema for Slurm cluster setup output.

    This is the JSON schema that ISV setup-slurm-cluster stubs must return.
    """

    model_config = ConfigDict(extra="allow")

    partitions: dict[str, SlurmPartitionOutput] = Field(default_factory=dict, description="Partition configurations")
    cuda_arch: str | None = Field(default=None, description="CUDA compute capability")
    storage_path: str = Field(default="/tmp", description="Scratch storage path")
    default_partition: str | None = Field(default=None, description="Default partition name")


class BareMetalOutput(BaseModel):
    """Schema for bare metal server setup output.

    This is the JSON schema that ISV setup-bare-metal stubs must return.
    Used for standalone servers without orchestration (no K8s, no Slurm).
    """

    model_config = ConfigDict(extra="allow")

    hostname: str | None = Field(default=None, description="Server hostname")
    gpu_count: int | None = Field(default=None, description="Number of GPUs")
    driver_version: str | None = Field(default=None, description="NVIDIA driver version")
    cuda_version: str | None = Field(default=None, description="CUDA version")


class NetworkOutput(BaseModel):
    """Schema for network-only setup output.

    This is the JSON schema for network-only validation tests (VPC, subnets, etc.).
    These tests typically don't require pre-provisioned infrastructure.
    """

    model_config = ConfigDict(extra="allow")

    region: str | None = Field(default=None, description="AWS region")
    description: str | None = Field(default=None, description="Network test description")


class VmOutput(BaseModel):
    """Schema for VM setup output.

    This is the JSON schema for EC2/VM-based validation tests.
    Tests are self-contained and create their own infrastructure.
    """

    model_config = ConfigDict(extra="allow")

    region: str | None = Field(default=None, description="AWS region")
    account_id: str | None = Field(default=None, description="AWS account ID")
    description: str | None = Field(default=None, description="VM test description")


class IamRoleOutput(BaseModel):
    """Schema for a role in the IAM system."""

    name: str = Field(description="Role name")
    permissions: list[str] = Field(default_factory=list, description="List of permissions")


class IamOutput(BaseModel):
    """Schema for IAM (Identity and Access Management) system setup output.

    This is the JSON schema that ISV IAM stubs must return.
    Used for validating IAM implementations (user CRUD, authentication, roles).
    """

    model_config = ConfigDict(extra="allow")

    provider: str | None = Field(default=None, description="IAM provider (e.g., 'aws-iam', 'okta', 'custom')")
    api_endpoint: str | None = Field(default=None, description="IAM API endpoint URL")
    user_count: int | None = Field(default=None, description="Number of existing users")
    roles: list[str | IamRoleOutput] = Field(default_factory=list, description="Available roles")
    supports_mfa: bool = Field(default=False, description="Whether MFA is supported")
    supports_service_accounts: bool = Field(default=False, description="Whether service accounts are supported")
    auth_methods: list[str] = Field(
        default_factory=list, description="Supported auth methods (e.g., 'password', 'oauth', 'saml')"
    )


class IsoOutput(BaseModel):
    """Schema for ISO import setup output.

    This is the JSON schema for ISO import validation tests.
    Tests are self-contained and create their own S3 buckets, AMIs, etc.
    """

    model_config = ConfigDict(extra="allow")

    provider: str | None = Field(default=None, description="Import provider (e.g., 'aws_vm_import')")
    region: str | None = Field(default=None, description="AWS region")
    account_id: str | None = Field(default=None, description="AWS account ID")
    default_image_url: str | None = Field(default=None, description="Default image URL to download")
    supported_formats: list[str] = Field(
        default_factory=list, description="Supported image formats (vmdk, vhd, ova, raw)"
    )
    gpu_instance_types: list[str] = Field(default_factory=list, description="Supported GPU instance types")


class CommandOutput(BaseModel):
    """Schema for setup command JSON output.

    This is validated when an ISV's setup stub returns. The output becomes
    the inventory that is passed to test validations.
    """

    model_config = ConfigDict(extra="allow")

    platform: str = Field(
        description="Platform type: 'kubernetes', 'slurm', 'bare_metal', 'network', 'vm', 'iam', or 'iso'"
    )
    cluster_name: str = Field(description="Name of the cluster/server/service")
    kubernetes: KubernetesOutput | None = Field(default=None, description="Kubernetes-specific output")
    slurm: SlurmOutput | None = Field(default=None, description="Slurm-specific output")
    bare_metal: BareMetalOutput | None = Field(default=None, description="Bare metal-specific output")
    network: NetworkOutput | None = Field(default=None, description="Network-only output")
    vm: VmOutput | None = Field(default=None, description="VM output")
    iam: IamOutput | None = Field(default=None, description="IAM-specific output")
    iso: IsoOutput | None = Field(default=None, description="ISO import output")


class ValidationConfig(BaseModel):
    """Test configuration section.

    Validations are grouped by meaningful category names (e.g., 'network', 'ssh', 'gpu').
    Each validation can have a `phase` field to control execution timing:

    - No `phase`: Runs after setup steps complete (default)
    - `phase: teardown`: Runs after teardown steps complete
    - `phase: test`: Runs after test steps (if any exist)

    Three formats are supported:

    1. List format (each validation specifies its own step/phase):
        validations:
          network:
            - VpcCrudCheck:
                step: vpc_crud
            - NetworkProvisionedCheck:
                step: create_network

    2. Group defaults with list checks (step/phase apply to all checks):
        validations:
          credentials:
            step: test_credentials
            phase: test
            checks:
              - StepSuccessCheck: {}
              - FieldExistsCheck:
                  field: "account_id"

    3. Group defaults with dict checks (deep-merge friendly):
        validations:
          credentials:
            step: test_credentials
            checks:
              StepSuccessCheck: {}
              FieldExistsCheck:
                field: "account_id"
    """

    model_config = ConfigDict(extra="allow")

    cluster_name: str | None = Field(default=None, description="Cluster name for test run")
    description: str | None = Field(default=None, description="Test run description")
    platform: str | None = Field(
        default=None,
        description="Platform type: KUBERNETES, SLURM, BARE_METAL, CONTROL_PLANE, IAM, NETWORK, SECURITY, VM, IMAGE_REGISTRY",
    )
    settings: dict[str, Any] = Field(default_factory=dict, description="Test settings")
    validations: dict[str, list[dict[str, Any]] | dict[str, Any]] = Field(
        default_factory=dict,
        description="Validation checks by category. Supports list format or group defaults with 'checks' key.",
    )
    exclude: dict[str, Any] = Field(
        default_factory=dict, description="Exclusion rules. Prefer labels; markers is legacy."
    )


class RunConfig(BaseModel):
    """Unified configuration schema for a complete test run.

    This is the merged result of all YAML config files (from -f/--config). It contains:
    - lab: Lab/environment metadata
    - commands: ISV-provided lifecycle commands grouped by platform
    - context: Variables for Jinja2 templating
    - tests: Test configuration (validations to run)
    """

    model_config = ConfigDict(extra="allow")

    version: str = Field(default="1.0", description="Schema version")

    lab: LabConfig | None = Field(default=None, description="Lab configuration")
    commands: dict[str, PlatformCommands] = Field(
        default_factory=dict,
        description="Lifecycle commands by platform (kubernetes, slurm, bare_metal, network, vm, iam, image_registry, security)",
    )
    context: dict[str, Any] = Field(default_factory=dict, description="Context variables for templating")
    tests: ValidationConfig | None = Field(default=None, description="Test configuration")

    def get_steps(self, platform: str) -> list[StepConfig]:
        """Get the sequential steps for a given platform.

        Args:
            platform: 'kubernetes', 'slurm', 'bare_metal', 'network', 'vm', 'iam', 'security', etc.

        Returns:
            List of StepConfig if steps are defined, empty list otherwise

        Raises:
            KeyError: If platform is not configured
        """
        platform_cmds = self.commands.get(platform)
        if platform_cmds is None:
            raise KeyError(f"Platform '{platform}' not configured in commands")
        # Check platform-level skip first
        if platform_cmds.skip:
            return []
        return [step for step in platform_cmds.steps if not step.skip]

    def get_phases(self, platform: str) -> list[str]:
        """Get the ordered phases list for a given platform.

        Args:
            platform: 'kubernetes', 'slurm', 'bare_metal', 'network', 'vm', 'iam', 'security', etc.

        Returns:
            List of phase names in execution order

        Raises:
            KeyError: If platform is not configured
        """
        platform_cmds = self.commands.get(platform)
        if platform_cmds is None:
            raise KeyError(f"Platform '{platform}' not configured in commands")
        return platform_cmds.phases
