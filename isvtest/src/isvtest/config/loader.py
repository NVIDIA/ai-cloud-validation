# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Configuration loader for test configs.

This module provides utilities to load and manage test configurations
(what validations to run) and cluster inventory files (cluster-specific values).

Supports Jinja2 templating in YAML configs. Example:
    - K8sNodeCountCheck:
        count: {{ inventory.kubernetes.node_count }}
    - K8sDriverVersionCheck:
        driver_version: "{{ inventory.kubernetes.driver_version }}"
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from jinja2 import BaseLoader, ChainableUndefined, Environment

from isvtest.config.constants import RESOLVED_ENTRIES_FLAG
from isvtest.config.inventory import ClusterInventory, inventory_to_dict, parse_inventory


def _ternary(value: Any, true_val: Any, false_val: Any = "") -> Any:
    """Jinja2 ternary filter (Ansible-style).

    Usage: {{ condition | ternary('yes', 'no') }}

    Args:
        value: Condition to evaluate (truthy/falsy)
        true_val: Value to return if condition is truthy
        false_val: Value to return if condition is falsy (default: empty string)

    Returns:
        true_val if value is truthy, else false_val
    """
    return true_val if value else false_val


def _create_jinja_env() -> Environment:
    """Create Jinja2 environment with custom filters.

    Returns:
        Configured Jinja2 Environment with ChainableUndefined
    """
    env = Environment(loader=BaseLoader(), undefined=ChainableUndefined)
    env.filters["tojson"] = lambda x: json.dumps(x)
    env.filters["ternary"] = _ternary
    return env


logger = logging.getLogger(__name__)


class ConfigLoader:
    """Load and manage test configurations."""

    def __init__(self) -> None:
        """Initialize config loader."""
        pass

    def load_cluster_config(
        self,
        config_file: str | None = None,
        inventory_path: str | None = None,
    ) -> dict[str, Any]:
        """Load cluster configuration, optionally with inventory templating.

        Supports Jinja2 templating in YAML configs. When an inventory is provided,
        template variables like {{ inventory.kubernetes.node_count }} are replaced
        with inventory values.

        Args:
            config_file: Path to config file (required)
            inventory_path: Path to inventory file (JSON or YAML) for templating.
                           If not provided, checks ISV_INVENTORY_PATH environment variable.

        Returns:
            Cluster configuration dictionary, with inventory values templated in

        Raises:
            FileNotFoundError: If config file not found
            ValueError: If config_file not provided
        """
        if not config_file:
            raise ValueError("config_file must be provided")

        config_path = Path(config_file)

        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        # Read config as string for Jinja2 templating
        with open(config_path) as f:
            config_content = f.read()

        pre_rendered_config = self._load_pre_rendered_config(config_content)
        if pre_rendered_config is not None:
            return pre_rendered_config

        # Check for inventory path from argument or environment variable
        effective_inventory_path = inventory_path or os.environ.get("ISV_INVENTORY_PATH")
        inventory_dict: dict[str, Any] = {}

        if effective_inventory_path:
            inventory = self.load_inventory(effective_inventory_path)
            inventory_dict = inventory_to_dict(inventory)

        # Always render Jinja2 templates (uses |default values when no inventory)
        config_content = self._render_template(config_content, inventory_dict)

        # Parse YAML after templating
        config = yaml.safe_load(config_content)

        # Validate basic structure
        if not isinstance(config, dict):
            raise ValueError(f"Invalid config format in {config_path}")

        # Store inventory in config for access by validations
        if inventory_dict:
            config["inventory"] = inventory_dict
            # Override cluster_name if provided in inventory
            inventory_cluster_name = inventory_dict.get("cluster_name")
            if inventory_cluster_name:
                yaml_cluster_name = config.get("cluster_name")
                if yaml_cluster_name and yaml_cluster_name != inventory_cluster_name:
                    logger.debug(
                        f"Inventory cluster_name '{inventory_cluster_name}' "
                        f"overrides YAML cluster_name '{yaml_cluster_name}'"
                    )
                config["cluster_name"] = inventory_cluster_name

        return config

    def _load_pre_rendered_config(self, content: str) -> dict[str, Any] | None:
        """Return an already-resolved JSON config without applying Jinja rendering."""
        if not content.lstrip().startswith("{") or RESOLVED_ENTRIES_FLAG not in content:
            return None
        try:
            config = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(config, dict) or config.get(RESOLVED_ENTRIES_FLAG) is not True:
            return None
        return config

    def _render_template(self, content: str, context: dict[str, Any]) -> str:
        """Render Jinja2 template with the given context.

        Uses ChainableUndefined so that nested undefined variables (e.g., {{ a.b.c }})
        can be chained with the |default() filter. Note that undefined variables without
        a |default() filter will render as empty strings, not as the original {{ ... }}
        placeholders.

        Args:
            content: Template string (YAML content)
            context: Dictionary of values for template substitution

        Returns:
            Rendered string
        """
        # ChainableUndefined allows {{ a.b.c | default(x) }} to work when a.b is missing
        env = _create_jinja_env()
        template = env.from_string(content)
        return template.render(**context)

    def load_inventory(self, inventory_path: str) -> ClusterInventory:
        """Load cluster inventory from a JSON or YAML file.

        Args:
            inventory_path: Path to the inventory file

        Returns:
            Parsed ClusterInventory object

        Raises:
            FileNotFoundError: If inventory file not found
            ValueError: If inventory format is invalid
        """
        path = Path(inventory_path)

        if not path.exists():
            raise FileNotFoundError(f"Inventory file not found: {inventory_path}")

        with open(path) as f:
            content = f.read()

        # Determine format based on extension
        if path.suffix.lower() == ".json":
            data = json.loads(content)
        else:
            # Default to YAML (covers .yaml, .yml, and extensionless)
            data = yaml.safe_load(content)

        if not isinstance(data, dict):
            raise ValueError(f"Invalid inventory format in {path}: expected a dictionary")

        return parse_inventory(data)

    def get_validations_for_category(self, config: dict[str, Any], category: str) -> dict[str, dict[str, Any]]:
        """Extract validations for a specific category from config.

        Args:
            config: Cluster configuration
            category: Validation category name (e.g., 'kubernetes', 'slurm', 'bare_metal')

        Returns:
            Dictionary of validation names to their configurations
        """
        validations_config = config.get("validations", {}) or {}
        if category not in validations_config:
            return {}

        result = {}
        category_items = validations_config[category]

        # Handle both list and dict formats
        if isinstance(category_items, list):
            # Format: list of {validation_name: {config}}
            for item in category_items:
                if isinstance(item, dict):
                    for validation_name, validation_config in item.items():
                        result[validation_name] = validation_config or {}
        elif isinstance(category_items, dict):
            # Format: {validation_name: {config}}
            result = category_items

        return result

    def get_all_validations(
        self, config: dict[str, Any], categories: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Get all validations for specified categories or all categories.

        Args:
            config: Cluster configuration
            categories: List of validation categories to include. If None, includes all categories.

        Returns:
            Dictionary of all validation names to their configurations
        """
        all_validations = {}
        config_categories = config.get("validations", {}) or {}

        if categories is None:
            categories = list(config_categories.keys())

        for category in categories:
            category_validations = self.get_validations_for_category(config, category)
            # Merge validations, later categories override earlier ones
            all_validations.update(category_validations)

        return all_validations

    def detect_platform(self) -> str:
        """Detect the current platform (kubernetes, slurm, bare_metal).

        Returns:
            Platform identifier
        """
        # Check for Kubernetes
        if os.path.exists("/var/run/secrets/kubernetes.io") or os.environ.get("KUBERNETES_SERVICE_HOST"):
            return "kubernetes"

        # Check for Slurm
        if os.path.exists("/etc/slurm") or os.environ.get("SLURM_CONF"):
            return "slurm"

        # VMs are treated as bare_metal for validation purposes
        return "bare_metal"

    def _is_virtual_machine(self) -> bool:
        """Detect if running in a virtual machine.

        Returns:
            True if in a VM, False otherwise
        """
        vm_indicators = [
            "/sys/class/dmi/id/product_name",
            "/sys/class/dmi/id/sys_vendor",
        ]

        vm_strings = ["vmware", "virtualbox", "kvm", "qemu", "xen", "hyperv", "parallels"]

        for indicator_file in vm_indicators:
            if os.path.exists(indicator_file):
                try:
                    with open(indicator_file) as f:
                        content = f.read().lower()
                        if any(vm_str in content for vm_str in vm_strings):
                            return True
                except (OSError, PermissionError):
                    pass

        return False


def load_config(
    config_file: str,
    inventory_path: str | None = None,
) -> dict[str, Any]:
    """Convenience function to load cluster config.

    Args:
        config_file: Path to config file (required)
        inventory_path: Path to inventory file (JSON or YAML) for templating.
                        If not provided, checks ISV_INVENTORY_PATH environment variable.

    Returns:
        Cluster configuration
    """
    loader = ConfigLoader()
    return loader.load_cluster_config(
        config_file=config_file,
        inventory_path=inventory_path,
    )
