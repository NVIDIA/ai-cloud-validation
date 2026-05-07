# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Context management for isvctl orchestration.

The Context class manages variables used for Jinja2 templating throughout
the test lifecycle. It starts with user-provided context values and gets
enriched with inventory data from command outputs.
"""

import copy
import json
import os
from datetime import UTC, datetime
from typing import Any

from isvtest.config.loader import _ternary
from jinja2 import ChainableUndefined, Environment

from isvctl.config.schema import CommandOutput, RunConfig
from isvctl.redaction import filter_env


def _create_jinja_env() -> Environment:
    """Create Jinja2 environment with custom filters.

    Uses ChainableUndefined so that chained attribute access on missing
    step outputs (e.g., ``steps.setup.kubernetes.node_count | default(4)``)
    returns Undefined instead of raising UndefinedError.  This lets the
    ``| default()`` filter work even when a step has not yet run or failed.

    Returns:
        Configured Jinja2 Environment
    """
    env = Environment(undefined=ChainableUndefined)
    env.filters["tojson"] = lambda x: json.dumps(x)
    env.filters["ternary"] = _ternary
    return env


class Context:
    """Manages context variables for Jinja2 templating.

    The context provides variables for templating in:
    - Command arguments (e.g., --nodes {{context.node_count}})
    - Test configurations (e.g., count: {{inventory.kubernetes.node_count}})

    Context is built in layers:
    1. Base context from config (context: section in YAML)
    2. Lab metadata (lab: section)
    3. Built-in variables (timestamp, etc.)
    4. Inventory from create command output

    Attributes:
        data: The full context dictionary available for templating
    """

    def __init__(self, config: RunConfig) -> None:
        """Initialize context from configuration.

        Args:
            config: The merged RunConfig
        """
        self._config = config
        self.data: dict[str, Any] = {}

        # Layer 1: User-provided context
        if config.context:
            self.data["context"] = copy.deepcopy(config.context)

        # Layer 2: Lab metadata
        if config.lab:
            self.data["lab"] = config.lab.model_dump(exclude_none=True)

        # Layer 3: Built-in variables
        now = datetime.now(UTC)
        self.data["builtin"] = {
            "timestamp": now.strftime("%Y%m%d%H%M%S"),
            "date": now.strftime("%Y-%m-%d"),
        }

        # Layer 4: Inventory (populated after create command)
        self.data["inventory"] = {}

        # Layer 5: Step outputs (populated by StepExecutor)
        self.data["steps"] = {}

        # Step phases (for inferring validation phase from step)
        self._step_phases: dict[str, str] = {}

        # Layer 6: Environment variables (for {{env.VAR}} access)
        # Must be loaded before settings so settings can reference env vars.
        # Sensitive variables (API keys, secrets) are excluded to prevent
        # accidental exposure in logs, dumps, or error messages.
        self.data["env"] = filter_env(dict(os.environ))

        # Layer 7: Test settings (from tests.settings)
        # These are available as top-level variables for templating
        # Settings that contain templates are rendered after loading
        if config.tests and config.tests.settings:
            env = _create_jinja_env()
            for key, value in config.tests.settings.items():
                if isinstance(value, str) and "{{" in value and "}}" in value:
                    try:
                        template = env.from_string(value)
                        self.data[key] = template.render(**self.data)
                    except Exception:
                        # If rendering fails, store as-is (may be rendered later)
                        self.data[key] = value
                else:
                    self.data[key] = value

    def set_inventory(self, output: CommandOutput) -> None:
        """Set inventory from create command output.

        This is called after a successful create command to populate
        the inventory context used by test configurations.

        Args:
            output: Validated CommandOutput from create command
        """
        self.data["inventory"] = output.model_dump(exclude_none=True)

    def set_step_output(self, step_name: str, output: dict[str, Any]) -> None:
        """Store output from a step for use in subsequent steps.

        This is called by StepExecutor after each step completes to make
        the output available for Jinja2 templating in subsequent steps.

        Args:
            step_name: Unique identifier for the step
            output: JSON output from the step command

        Example:
            After setup step:
            >>> context.set_step_output("setup", {"cluster_name": "my-cluster"})

            Subsequent step can reference:
            >>> args: ["--cluster", "{{ steps.setup.cluster_name }}"]
        """
        self.data["steps"][step_name] = output

    def set_step_phase(self, step_name: str, phase: str) -> None:
        """Record the phase a step belongs to.

        This allows validations to infer phase from step.

        Args:
            step_name: Unique identifier for the step
            phase: The phase this step belongs to (setup, test, teardown)
        """
        self._step_phases[step_name] = phase

    def get_step_phase(self, step_name: str) -> str | None:
        """Get the phase a step belongs to.

        Args:
            step_name: Name of the step

        Returns:
            Phase name, or None if step not found
        """
        return self._step_phases.get(step_name)

    def get_all_step_phases(self) -> dict[str, str]:
        """Get all step phases for passing to validation runners.

        Returns:
            Dictionary mapping step names to their phases
        """
        return dict(self._step_phases)

    def get_step_output(self, step_name: str) -> dict[str, Any]:
        """Get output from a previous step.

        Args:
            step_name: Name of the step to retrieve output for

        Returns:
            Step output dictionary, or empty dict if not found
        """
        return self.data.get("steps", {}).get(step_name, {})

    def get_accumulated_context(self) -> dict[str, Any]:
        """Get all context including step outputs for Jinja2 templating.

        This is the full context available to step arguments:
        - context: User-provided context variables
        - lab: Lab metadata
        - builtin: Built-in variables (timestamp, date)
        - inventory: Inventory from setup command (legacy)
        - steps: Outputs from all completed steps

        Returns:
            Complete context dictionary for Jinja2 rendering
        """
        return self.data

    def render_string(self, template_str: str) -> str:
        """Render a Jinja2 template string with context.

        Args:
            template_str: String that may contain {{ }} templates

        Returns:
            Rendered string
        """
        if "{{" not in template_str or "}}" not in template_str:
            return template_str

        env = _create_jinja_env()
        template = env.from_string(template_str)
        return template.render(**self.data)

    def render_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Recursively render all string values in a dictionary.

        Args:
            data: Dictionary with potential template strings

        Returns:
            New dictionary with all templates rendered
        """
        result = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = self.render_string(value)
            elif isinstance(value, dict):
                result[key] = self.render_dict(value)
            elif isinstance(value, list):
                result[key] = self._render_list(value)
            else:
                result[key] = value
        return result

    def _render_list(self, items: list[Any]) -> list[Any]:
        """Recursively render all string values in a list.

        Args:
            items: List with potential template strings

        Returns:
            New list with all templates rendered
        """
        result = []
        for item in items:
            if isinstance(item, str):
                result.append(self.render_string(item))
            elif isinstance(item, dict):
                result.append(self.render_dict(item))
            elif isinstance(item, list):
                result.append(self._render_list(item))
            else:
                result.append(item)
        return result
