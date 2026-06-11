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

"""Output schema registry for step-based command execution.

This module provides:
1. Step name -> Schema mapping for auto-detection
2. Built-in JSON schemas for validating command output
3. Registration API for custom schemas and mappings

Schemas are generic and provider-agnostic - the same schemas work for
AWS, Azure, GCP, or any other ISV provider.

Example usage:
    from isvctl.config.output_schemas import get_schema_for_step, validate_output

    # Auto-detect schema from step name
    schema_name = get_schema_for_step("provision_cluster")  # Returns "cluster"

    # Validate output against schema
    is_valid, errors = validate_output(output_json, schema_name)
"""

from typing import Any

import jsonschema

# Step name -> Schema mapping for auto-detection
# The step name determines which schema is used for validation
STEP_SCHEMA_MAPPING: dict[str, str | None] = {
    # Cluster operations -> "cluster" schema
    "provision_cluster": "cluster",
    "create_cluster": "cluster",
    "setup_cluster": "cluster",
    # Network operations -> "network" schema
    "create_network": "network",
    "provision_network": "network",
    "create_vpc": "network",
    "setup_network": "network",
    # Instance/VM operations -> "instance" schema
    "launch_instance": "instance",
    "create_instance": "instance",
    "provision_vm": "instance",
    "create_vm": "instance",
    # Instance list operations -> "instance_list" schema
    "list_instances": "instance_list",
    # GPU setup -> "gpu_setup" schema
    "install_gpu_operator": "gpu_setup",
    "setup_gpu": "gpu_setup",
    "install_drivers": "gpu_setup",
    # Workload execution -> "workload_result" schema
    "run_workload": "workload_result",
    "run_test": "workload_result",
    "run_benchmark": "workload_result",
    "execute_workload": "workload_result",
    # NIM deployment -> "nim_deploy" schema
    "deploy_nim": "nim_deploy",
    # NIM teardown -> "teardown" schema
    "teardown_nim": "teardown",
    # Teardown operations -> "teardown" schema
    "teardown": "teardown",
    "cleanup": "teardown",
    "destroy": "teardown",
    # Control plane - API health
    "check_api": "api_health",
    "test_api": "api_health",
    "verify_api": "api_health",
    # Control plane - Access key operations
    "create_access_key": "access_key",
    "test_access_key": "auth_result",
    "disable_access_key": "access_key_status",
    "verify_key_rejected": "auth_rejection",
    "delete_access_key": "teardown",
    # Control plane - Tenant/Resource group operations
    "create_tenant": "tenant",
    "list_tenants": "tenant_list",
    "get_tenant": "tenant",
    "delete_tenant": "teardown",
    # IAM operations
    "create_user": "iam_user",
    "delete_user": "teardown",
    # Network test operations
    "vpc_crud": "vpc_crud",
    "vpc_crud_test": "vpc_crud",
    "subnet_test": "subnet_config",
    "subnet_config": "subnet_config",
    "isolation_test": "vpc_isolation",
    "vpc_isolation": "vpc_isolation",
    "security_test": "security_blocking",
    "security_blocking": "security_blocking",
    "traffic_test": "traffic_flow",
    "traffic_validation": "traffic_flow",
    "test_connectivity": "connectivity_result",
    "connectivity_test": "connectivity_result",
    "launch_instances": "instance_launch",
    "launch_test_instances": "instance_launch",
    # DDI (DNS/DHCP/IP Management) test operations
    "vpc_ip_config": "vpc_ip_config",
    "vpc_ip_config_test": "vpc_ip_config",
    "dhcp_ip_test": "dhcp_ip",
    "dhcp_ip": "dhcp_ip",
    # ISO/Image import operations (provider-agnostic)
    "upload_image": "iso_import",
    "import_image": "iso_import",
    "upload_iso": "iso_import",
    "import_iso": "iso_import",
    "create_image": "iso_import",
    # SDN Controller test operations
    "byoip_test": "byoip",
    "byoip_validation": "byoip",
    "stable_ip_test": "stable_ip",
    "stable_ip_validation": "stable_ip",
    "stable_egress_ip_test": "stable_egress_ip",
    "stable_egress_ip_validation": "stable_egress_ip",
    "floating_ip_test": "floating_ip",
    "floating_ip_validation": "floating_ip",
    "dns_test": "localized_dns",
    "dns_validation": "localized_dns",
    "peering_test": "vpc_peering",
    "peering_validation": "vpc_peering",
    "backend_switch_fabric": "backend_switch_fabric",
    "backend_switch_fabric_test": "backend_switch_fabric",
    "nvlink_domain": "nvlink_domain",
    "nvlink_domain_test": "nvlink_domain",
    "sg_crud_test": "sg_crud",
    "sg_crud": "sg_crud",
    # Node pool operations
    "create_test_node_pool": "node_pool",
    "create_node_pool": "node_pool",
    "update_test_node_pool": "node_pool",
    "update_node_pool": "node_pool",
    "destroy_test_node_pool": "teardown",
    "destroy_node_pool": "teardown",
    # Multi-cluster operations
    "create_test_shared_vpc_cluster": "multi_cluster",
    "destroy_test_shared_vpc_cluster": "teardown",
}

# Common fields present in all outputs
COMMON_PROPERTIES = {
    "success": {"type": "boolean", "description": "Whether the operation succeeded"},
    "platform": {"type": "string", "description": "Platform type (e.g., kubernetes, vm, iam, security)"},
}

# Built-in schemas (generic, provider-agnostic)
OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "cluster": {
        "type": "object",
        "required": ["success", "platform", "cluster_name", "node_count"],
        "properties": {
            **COMMON_PROPERTIES,
            "cluster_name": {"type": "string", "description": "Name of the cluster"},
            "endpoint": {"type": "string", "description": "Cluster API endpoint"},
            "node_count": {"type": "integer", "minimum": 0, "description": "Total number of nodes"},
            "nodes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of node names",
            },
            "gpu_count": {"type": "integer", "minimum": 0, "description": "Total GPUs in cluster"},
            "gpu_per_node": {"type": "integer", "minimum": 0, "description": "GPUs per node"},
            "driver_version": {"type": "string", "description": "NVIDIA driver version"},
            "kubeconfig_path": {"type": "string", "description": "Path to kubeconfig file"},
        },
        "additionalProperties": True,
    },
    "network": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "network_id": {"type": "string", "description": "Network/VPC identifier"},
            "cidr": {"type": "string", "description": "CIDR block for the network"},
            "subnets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subnet_id": {"type": "string"},
                        "cidr": {"type": "string"},
                        "availability_zone": {"type": "string"},
                        "az": {"type": "string"},
                    },
                },
                "description": "List of subnets",
            },
            "region": {"type": "string", "description": "Cloud region"},
        },
        "additionalProperties": True,
    },
    "instance": {
        "type": "object",
        "required": ["success", "platform", "instance_id"],
        "properties": {
            **COMMON_PROPERTIES,
            "instance_id": {"type": "string", "description": "Instance identifier"},
            "state": {
                "type": "string",
                "enum": ["pending", "running", "stopped", "terminated"],
                "description": "Instance state",
            },
            "public_ip": {"type": "string", "description": "Public IP address"},
            "private_ip": {"type": "string", "description": "Private IP address"},
            "instance_type": {"type": "string", "description": "Instance type/size"},
            "ssh_user": {"type": "string", "description": "SSH username"},
            "ssh_key_path": {"type": "string", "description": "Path to SSH private key"},
        },
        "additionalProperties": True,
    },
    "instance_list": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "instances": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "instance_id": {"type": "string"},
                        "instance_type": {"type": "string"},
                        "state": {"type": "string"},
                        "public_ip": {"type": ["string", "null"]},
                        "private_ip": {"type": ["string", "null"]},
                        "vpc_id": {"type": "string"},
                    },
                },
                "description": "List of instances in the VPC",
            },
            "count": {"type": "integer", "description": "Number of instances"},
            "found_target": {"type": "boolean", "description": "Target instance was found"},
            "target_instance": {"type": "string", "description": "Target instance ID searched for"},
        },
        "additionalProperties": True,
    },
    "nim_deploy": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "container_id": {"type": ["string", "null"], "description": "Docker container ID"},
            "container_name": {"type": "string", "description": "Docker container name"},
            "model": {"type": "string", "description": "NIM model name"},
            "image": {"type": "string", "description": "Container image used"},
            "endpoint": {"type": "string", "description": "NIM endpoint URL"},
            "port": {"type": "integer", "description": "Host port NIM is exposed on"},
            "health_ready": {"type": "boolean", "description": "Whether health check passed"},
            "host": {"type": "string", "description": "Remote host IP"},
            "key_file": {"type": "string", "description": "SSH key file path"},
            "ssh_user": {"type": "string", "description": "SSH username"},
        },
        "additionalProperties": True,
    },
    "workload_result": {
        "type": "object",
        "required": ["success", "platform", "status"],
        "properties": {
            **COMMON_PROPERTIES,
            "status": {
                "type": "string",
                "enum": ["passed", "failed", "skipped"],
                "description": "Workload execution status",
            },
            "duration_seconds": {"type": "number", "minimum": 0, "description": "Execution duration"},
            "metrics": {
                "type": "object",
                "additionalProperties": True,
                "description": "Workload metrics (e.g., bandwidth, latency)",
            },
            "logs": {"type": "string", "description": "Workload output logs"},
        },
        "additionalProperties": True,
    },
    "gpu_setup": {
        "type": "object",
        "required": ["success", "platform", "installed"],
        "properties": {
            **COMMON_PROPERTIES,
            "installed": {"type": "boolean", "description": "Whether GPU setup completed"},
            "driver_version": {"type": "string", "description": "NVIDIA driver version"},
            "cuda_version": {"type": "string", "description": "CUDA version"},
            "gpu_count": {"type": "integer", "minimum": 0, "description": "Number of GPUs"},
            "gpu_model": {"type": "string", "description": "GPU model name"},
        },
        "additionalProperties": True,
    },
    "teardown": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "resources_deleted": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of resources that were deleted",
            },
            "resources_failed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of resources that failed to delete",
            },
            "message": {"type": "string", "description": "Teardown status message"},
            "duration_seconds": {"type": "number", "minimum": 0, "description": "Teardown duration"},
        },
        "additionalProperties": True,
    },
    # =========================================================================
    # ISO/Image Import Schemas (provider-agnostic)
    # =========================================================================
    "iso_import": {
        "type": "object",
        "required": ["success", "platform", "image_id"],
        "properties": {
            **COMMON_PROPERTIES,
            "image_id": {"type": "string", "description": "Imported image identifier (AMI, Azure Image, GCP Image)"},
            "image_name": {"type": "string", "description": "Human-readable image name"},
            "storage_bucket": {"type": "string", "description": "Storage bucket/container used for import"},
            "storage_path": {"type": "string", "description": "Object key/path in storage"},
            "disk_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Disk/snapshot IDs created during import",
            },
            "image_format": {
                "type": "string",
                "enum": ["vmdk", "vhd", "vhdx", "ova", "raw", "qcow2"],
                "description": "Source image format",
            },
            "region": {"type": "string", "description": "Cloud region"},
            "image_state": {"type": "string", "description": "Image state (available, pending, etc.)"},
        },
        "additionalProperties": True,
    },
    # =========================================================================
    # Control Plane Schemas (provider-agnostic)
    # =========================================================================
    "api_health": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "account_id": {"type": "string", "description": "Cloud account/project identifier"},
            "region": {"type": "string", "description": "Cloud region"},
            "tests": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "passed": {"type": "boolean"},
                        "message": {"type": "string"},
                        "latency_ms": {"type": "number"},
                    },
                },
                "description": "Individual API health test results",
            },
        },
        "additionalProperties": True,
    },
    "access_key": {
        "type": "object",
        "required": ["success", "platform", "access_key_id"],
        "properties": {
            **COMMON_PROPERTIES,
            "access_key_id": {"type": "string", "description": "Access key identifier"},
            "secret_access_key": {"type": "string", "description": "Secret key (only on create)"},
            "username": {"type": "string", "description": "User the key belongs to"},
            "user_id": {"type": "string", "description": "User unique identifier"},
        },
        "additionalProperties": True,
    },
    "auth_result": {
        "type": "object",
        "required": ["success", "platform", "authenticated"],
        "properties": {
            **COMMON_PROPERTIES,
            "authenticated": {"type": "boolean", "description": "Authentication succeeded"},
            "identity_id": {"type": "string", "description": "Authenticated identity identifier"},
            "account_id": {"type": "string", "description": "Account identifier"},
            "error": {"type": "string", "description": "Error message if failed"},
        },
        "additionalProperties": True,
    },
    "access_key_status": {
        "type": "object",
        "required": ["success", "platform", "status"],
        "properties": {
            **COMMON_PROPERTIES,
            "access_key_id": {"type": "string", "description": "Access key identifier"},
            "status": {
                "type": "string",
                "enum": ["Active", "Inactive"],
                "description": "Key status",
            },
        },
        "additionalProperties": True,
    },
    "auth_rejection": {
        "type": "object",
        "required": ["success", "platform", "rejected"],
        "properties": {
            **COMMON_PROPERTIES,
            "rejected": {"type": "boolean", "description": "Auth was correctly rejected"},
            "error_code": {"type": "string", "description": "Error code from rejection"},
        },
        "additionalProperties": True,
    },
    "tenant": {
        "type": "object",
        "required": ["success", "platform", "tenant_name"],
        "properties": {
            **COMMON_PROPERTIES,
            "tenant_name": {"type": "string", "description": "Tenant name"},
            "tenant_id": {"type": "string", "description": "Tenant unique identifier"},
            "description": {"type": "string", "description": "Tenant description"},
            "tags": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Tenant tags/labels",
            },
        },
        "additionalProperties": True,
    },
    "tenant_list": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tenants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tenant_name": {"type": "string"},
                        "tenant_id": {"type": "string"},
                    },
                },
                "description": "List of tenants",
            },
            "count": {"type": "integer", "description": "Number of tenants"},
            "found_target": {"type": "boolean", "description": "Target tenant was found"},
            "target_tenant": {"type": "string", "description": "Target tenant name searched for"},
        },
        "additionalProperties": True,
    },
    "iam_user": {
        "type": "object",
        "required": ["success", "platform", "username"],
        "properties": {
            **COMMON_PROPERTIES,
            "username": {"type": "string", "description": "User name"},
            "user_id": {"type": "string", "description": "User unique identifier"},
        },
        "additionalProperties": True,
    },
    # =========================================================================
    # Network Test Schemas
    # =========================================================================
    "vpc_crud": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc": {"type": "object"},
                    "read_vpc": {"type": "object"},
                    "update_tags": {"type": "object"},
                    "update_dns": {"type": "object"},
                    "delete_vpc": {"type": "object"},
                },
                "description": "Individual CRUD test results",
            },
            "network_id": {"type": "string", "description": "VPC ID (if created)"},
            "vpc_name": {"type": "string", "description": "VPC name"},
        },
        "additionalProperties": True,
    },
    "subnet_config": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc": {"type": "object"},
                    "create_subnets": {"type": "object"},
                    "az_distribution": {"type": "object"},
                    "subnets_available": {"type": "object"},
                    "route_table_exists": {"type": "object"},
                },
                "description": "Subnet configuration test results",
            },
            "subnets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subnet_id": {"type": "string"},
                        "cidr": {"type": "string"},
                        "az": {"type": "string"},
                    },
                },
                "description": "Created subnets",
            },
            "network_id": {"type": "string", "description": "VPC ID"},
        },
        "additionalProperties": True,
    },
    "vpc_isolation": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc_a": {"type": "object"},
                    "create_vpc_b": {"type": "object"},
                    "no_peering": {"type": "object"},
                    "no_cross_routes_a": {"type": "object"},
                    "no_cross_routes_b": {"type": "object"},
                    "sg_isolation_a": {"type": "object"},
                    "sg_isolation_b": {"type": "object"},
                },
                "description": "VPC isolation test results",
            },
            "vpc_a": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "cidr": {"type": "string"}},
            },
            "vpc_b": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "cidr": {"type": "string"}},
            },
        },
        "additionalProperties": True,
    },
    "security_blocking": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc": {"type": "object"},
                    "sg_default_deny_inbound": {"type": "object"},
                    "sg_allows_specific_ssh": {"type": "object"},
                    "sg_denies_vpc_icmp": {"type": "object"},
                    "nacl_explicit_deny": {"type": "object"},
                    "default_nacl_allows_inbound": {"type": "object"},
                    "sg_restricted_egress": {"type": "object"},
                },
                "description": "Security blocking test results",
            },
            "network_id": {"type": "string", "description": "VPC ID"},
        },
        "additionalProperties": True,
    },
    "traffic_flow": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc": {"type": "object"},
                    "create_igw": {"type": "object"},
                    "network_setup": {"type": "object"},
                    "create_iam": {"type": "object"},
                    "create_security_groups": {"type": "object"},
                    "launch_instances": {"type": "object"},
                    "instances_running": {"type": "object"},
                    "ssm_ready": {"type": "object"},
                    "traffic_allowed": {"type": "object"},
                    "traffic_blocked": {"type": "object"},
                    "internet_icmp": {"type": "object"},
                    "internet_http": {"type": "object"},
                },
                "description": "Traffic flow test results",
            },
            "network_id": {"type": "string", "description": "VPC ID"},
        },
        "additionalProperties": True,
    },
    "connectivity_result": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "additionalProperties": True,
                "description": "Connectivity test results",
            },
            "instances": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Instance information",
            },
            "network_id": {"type": "string", "description": "VPC/Network ID"},
        },
        "additionalProperties": True,
    },
    "instance_launch": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "instances": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "instance_id": {"type": "string"},
                        "private_ip": {"type": "string"},
                        "public_ip": {"type": "string"},
                        "state": {"type": "string"},
                    },
                },
                "description": "Launched instances",
            },
        },
        "additionalProperties": True,
    },
    # =========================================================================
    # DDI (DNS/DHCP/IP Management) and SDN controller schemas
    # =========================================================================
    "vpc_ip_config": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "network_id": {"type": "string", "description": "VPC/Network identifier"},
            "cidr": {"type": "string", "description": "VPC CIDR block"},
            "subnets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subnet_id": {"type": "string"},
                        "cidr": {"type": "string"},
                        "az": {"type": "string"},
                        "auto_assign_public_ip": {"type": "boolean"},
                        "available_ips": {"type": "integer"},
                    },
                },
                "description": "Subnet configurations with IP details",
            },
            "dhcp_options": {
                "type": "object",
                "properties": {
                    "dhcp_options_id": {"type": "string"},
                    "domain_name": {"type": "string"},
                    "domain_name_servers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "ntp_servers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "description": "DHCP options configuration",
            },
        },
        "additionalProperties": True,
    },
    "dhcp_ip": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "test_name": {"type": "string", "description": "Always 'dhcp_ip'"},
            "public_ip": {"type": ["string", "null"], "description": "SSH target address"},
            "private_ip": {"type": ["string", "null"], "description": "Expected private IP"},
            "key_file": {"type": ["string", "null"], "description": "SSH private key path"},
            "key_name": {"type": ["string", "null"], "description": "Key pair name"},
            "ssh_user": {"type": "string", "description": "SSH username"},
            "instance_id": {"type": ["string", "null"], "description": "Instance identifier"},
        },
        "additionalProperties": True,
    },
    "byoip": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "custom_cidr_create": {"type": "object"},
                    "custom_cidr_verify": {"type": "object"},
                    "standard_cidr_create": {"type": "object"},
                    "no_conflict": {"type": "object"},
                    "custom_cidr_subnet": {"type": "object"},
                },
                "description": "BYOIP test results",
            },
        },
        "additionalProperties": True,
    },
    "stable_ip": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_instance": {"type": "object"},
                    "record_ip": {"type": "object"},
                    "stop_instance": {"type": "object"},
                    "start_instance": {"type": "object"},
                    "ip_unchanged": {"type": "object"},
                },
                "description": "Stable private IP test results",
            },
        },
        "additionalProperties": True,
    },
    "stable_egress_ip": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_instance": {"type": "object"},
                    "probe_egress_ip": {"type": "object"},
                    "egress_ip_stable": {"type": "object"},
                },
                "description": "Stable egress IP test results",
            },
        },
        "additionalProperties": True,
    },
    "floating_ip": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "allocate_eip": {"type": "object"},
                    "associate_to_a": {"type": "object"},
                    "verify_on_a": {"type": "object"},
                    "reassociate_to_b": {"type": "object"},
                    "verify_on_b": {"type": "object"},
                    "verify_not_on_a": {"type": "object"},
                },
                "description": "Floating IP test results",
            },
        },
        "additionalProperties": True,
    },
    "localized_dns": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc_with_dns": {"type": "object"},
                    "create_hosted_zone": {"type": "object"},
                    "create_dns_record": {"type": "object"},
                    "verify_dns_settings": {"type": "object"},
                    "resolve_record": {"type": "object"},
                },
                "description": "Localized DNS test results",
            },
        },
        "additionalProperties": True,
    },
    "vpc_peering": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc_a": {"type": "object"},
                    "create_vpc_b": {"type": "object"},
                    "create_peering": {"type": "object"},
                    "accept_peering": {"type": "object"},
                    "add_routes": {"type": "object"},
                    "peering_active": {"type": "object"},
                },
                "description": "VPC peering test results",
            },
            "vpc_a": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "cidr": {"type": "string"}},
            },
            "vpc_b": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "cidr": {"type": "string"}},
            },
        },
        "additionalProperties": True,
    },
    "backend_switch_fabric": {
        "type": "object",
        "required": ["success", "platform", "node_id", "fabric", "tests"],
        "properties": {
            **COMMON_PROPERTIES,
            "test_name": {"type": "string", "description": "Always 'backend_switch_fabric'"},
            "node_id": {"type": "string", "minLength": 1, "description": "Compute node identifier"},
            "fabric": {
                "type": "object",
                "required": ["leaf_switch_ids", "spine_switch_ids", "core_switch_ids"],
                "properties": {
                    "leaf_switch_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "spine_switch_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "core_switch_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "description": "Backend switch identifiers by fabric layer",
            },
            "tests": {
                "type": "object",
                "required": [
                    "node_resolved",
                    "leaf_switch_ids_present",
                    "spine_switch_ids_present",
                    "core_switch_ids_present",
                ],
                "properties": {
                    "node_resolved": {"type": "object"},
                    "leaf_switch_ids_present": {"type": "object"},
                    "spine_switch_ids_present": {"type": "object"},
                    "core_switch_ids_present": {"type": "object"},
                },
                "description": "Backend switch fabric metadata checks",
            },
        },
        "additionalProperties": True,
    },
    "nvlink_domain": {
        "type": "object",
        "required": ["success", "platform", "node_id", "nvlink_supported", "tests"],
        "properties": {
            **COMMON_PROPERTIES,
            "test_name": {"type": "string", "description": "Always 'nvlink_domain'"},
            "node_id": {"type": "string", "minLength": 1, "description": "Compute node identifier"},
            "nvlink_supported": {"type": "boolean", "description": "Whether the node supports NVLink"},
            "nvlink_domain_id": {"type": "string", "minLength": 1, "description": "NVLink domain identifier"},
            "tests": {
                "type": "object",
                "required": [
                    "node_resolved",
                    "nvlink_support_detected",
                    "nvlink_domain_id_present",
                ],
                "properties": {
                    "node_resolved": {"type": "object"},
                    "nvlink_support_detected": {"type": "object"},
                    "nvlink_domain_id_present": {"type": "object"},
                },
                "description": "NVLink domain metadata checks",
            },
        },
        "if": {"properties": {"nvlink_supported": {"const": True}}},
        "then": {"required": ["nvlink_domain_id"]},
        "additionalProperties": True,
    },
    "sg_crud": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": {
            **COMMON_PROPERTIES,
            "tests": {
                "type": "object",
                "properties": {
                    "create_vpc": {"type": "object"},
                    "create_sg": {"type": "object"},
                    "read_sg": {"type": "object"},
                    "update_sg_add_rule": {"type": "object"},
                    "update_sg_modify_rule": {"type": "object"},
                    "update_sg_remove_rule": {"type": "object"},
                    "delete_sg": {"type": "object"},
                    "verify_deleted": {"type": "object"},
                },
                "description": "Security group CRUD test results",
            },
            "network_id": {"type": "string", "description": "VPC ID"},
        },
        "additionalProperties": True,
    },
    "generic": {
        "type": "object",
        "required": ["success", "platform"],
        "properties": COMMON_PROPERTIES,
        "additionalProperties": True,
        "description": "Generic schema for unrecognized step names",
    },
    # =========================================================================
    # Multi-cluster schemas
    # =========================================================================
    "multi_cluster": {
        "type": "object",
        "required": [
            "success",
            "platform",
            "test_id",
            "tenancy_id",
            "network_id",
            "clusters",
        ],
        "properties": {
            **COMMON_PROPERTIES,
            "test_id": {"type": "string", "description": "Validation test identifier"},
            "tenancy_id": {"type": "string", "minLength": 1, "description": "Cloud account or tenancy identifier"},
            "network_id": {"type": "string", "minLength": 1, "description": "Shared network/VPC identifier"},
            "clusters": {
                "type": "array",
                "minItems": 2,
                "items": {
                    "type": "object",
                    "required": ["name", "tenancy_id", "network_id", "status"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "description": "Cluster name"},
                        "role": {
                            "type": "string",
                            "description": "Optional provider role label for this cluster",
                        },
                        "tenancy_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Cloud account or tenancy identifier for this cluster",
                        },
                        "network_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Network/VPC identifier reported by this cluster",
                        },
                        "status": {"type": "string", "minLength": 1, "description": "Cluster lifecycle status"},
                        "ready_node_count": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Ready node count for this cluster when available",
                        },
                    },
                    "additionalProperties": True,
                },
                "description": "Clusters participating in the shared-network proof",
            },
        },
        "additionalProperties": True,
    },
    # =========================================================================
    # Node pool schemas (Terraform or Cluster API provisioned)
    # =========================================================================
    # Labels / taints / instance_types are carried as JSON-encoded strings so
    # the downstream Jinja ``render_string`` can substitute them into check
    # config without lossy str() conversion of a dict/list.
    "node_pool": {
        "type": "object",
        "required": [
            "success",
            "platform",
            "node_pool_name",
            "label_selector",
            "expected_replicas",
        ],
        "properties": {
            **COMMON_PROPERTIES,
            "node_pool_name": {"type": "string", "description": "Name of the created node pool"},
            "label_selector": {
                "type": "string",
                "description": (
                    "kubectl label selector that identifies the new nodes (e.g. eks.amazonaws.com/nodegroup=<name>)"
                ),
            },
            "expected_replicas": {
                "type": "integer",
                "minimum": 0,
                "description": "Node count that must reach Ready",
            },
            "expected_labels_json": {
                "type": "string",
                "description": "JSON-encoded mapping of labels each node must carry",
            },
            "expected_taints_json": {
                "type": "string",
                "description": "JSON-encoded list of {key,value,effect} each node must carry",
            },
            "expected_instance_types_json": {
                "type": "string",
                "description": "JSON-encoded list of allowed instance-type strings",
            },
            "node_type": {
                "type": "string",
                "enum": ["cpu", "gpu"],
                "description": "Informational node pool flavor",
            },
        },
        "additionalProperties": True,
    },
}


def get_schema_for_step(step_name: str) -> str | None:
    """Auto-detect schema name from step name.

    Uses a three-tier matching strategy:
    1. Exact match: step_name == key
    2. Partial match: key in step_name
    3. Fallback: returns "generic"

    Args:
        step_name: The name of the step to look up

    Returns:
        Schema name for validation

    Examples:
        >>> get_schema_for_step("provision_cluster")
        "cluster"
        >>> get_schema_for_step("my_custom_cluster_setup")
        "cluster"  # Partial match - contains "cluster"
        >>> get_schema_for_step("teardown")
        "teardown"  # Validates teardown success
        >>> get_schema_for_step("unknown_step")
        "generic"  # Fallback
    """
    # Exact match first
    if step_name in STEP_SCHEMA_MAPPING:
        return STEP_SCHEMA_MAPPING[step_name]

    # Partial match - check if any key is contained in step_name
    for key, schema in STEP_SCHEMA_MAPPING.items():
        if key in step_name:
            return schema

    # Fallback to generic schema
    return "generic"


def get_schema(schema_name: str) -> dict[str, Any] | None:
    """Get a schema by name.

    Args:
        schema_name: Name of the schema to retrieve

    Returns:
        Schema dictionary if found, None otherwise
    """
    return OUTPUT_SCHEMAS.get(schema_name)


def validate_output(output: dict[str, Any], schema_name: str) -> tuple[bool, list[str]]:
    """Validate command output against a schema.

    Args:
        output: The JSON output to validate
        schema_name: Name of the schema to validate against

    Returns:
        Tuple of (is_valid, error_messages)

    Raises:
        ValueError: If schema_name is not registered
    """
    schema = OUTPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ValueError(f"Unknown schema: {schema_name}")

    errors: list[str] = []
    try:
        jsonschema.validate(instance=output, schema=schema)
        return True, []
    except jsonschema.ValidationError as e:
        errors.append(f"Validation error at {e.json_path}: {e.message}")
        return False, errors
    except jsonschema.SchemaError as e:
        errors.append(f"Schema error: {e.message}")
        return False, errors


def register_step_mapping(step_name: str, schema_name: str | None) -> None:
    """Register a custom step name -> schema mapping.

    ISVs can use this to map their custom step names to schemas.

    Args:
        step_name: The step name to register
        schema_name: The schema name to map to, or None for no validation

    Example:
        >>> register_step_mapping("my_custom_provision", "cluster")
        >>> register_step_mapping("my_cleanup", None)  # No validation
    """
    STEP_SCHEMA_MAPPING[step_name] = schema_name


def register_schema(name: str, schema: dict[str, Any]) -> None:
    """Register a custom output schema.

    ISVs can use this to add schemas for their custom output formats.

    Args:
        name: Schema name
        schema: JSON Schema dictionary

    Example:
        >>> register_schema("custom_output", {
        ...     "type": "object",
        ...     "required": ["my_field"],
        ...     "properties": {"my_field": {"type": "string"}}
        ... })
    """
    OUTPUT_SCHEMAS[name] = schema


def list_schemas() -> list[str]:
    """List all registered schema names.

    Returns:
        List of schema names
    """
    return list(OUTPUT_SCHEMAS.keys())


def list_step_mappings() -> dict[str, str | None]:
    """List all step name -> schema mappings.

    Returns:
        Dictionary of step_name -> schema_name mappings
    """
    return dict(STEP_SCHEMA_MAPPING)
