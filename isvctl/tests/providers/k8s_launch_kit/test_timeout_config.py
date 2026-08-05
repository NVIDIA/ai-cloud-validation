# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Launch Kit timeout ownership."""

from pathlib import Path
from typing import Any

import yaml

PROVIDER_CONFIG_DIR = Path(__file__).parents[3] / "configs" / "providers" / "k8s-launch-kit" / "config"


def _steps(config_name: str) -> list[dict[str, Any]]:
    """Return Network Operator command steps from a provider configuration."""
    config = yaml.safe_load((PROVIDER_CONFIG_DIR / config_name).read_text())
    return config["commands"]["network_operator"]["steps"]


def test_generic_validate_delegates_timeout_to_launch_kit() -> None:
    """The generic validate workflow must not preempt l8k's matrix budget."""
    validate_steps = [step for step in _steps("provider.yaml") if step["name"] == "launch_kit_validate"]

    assert len(validate_steps) == 1
    assert validate_steps[0]["timeout"] is None


def test_network_operator_validates_delegate_timeout_to_launch_kit() -> None:
    """Every grouped use case must leave its validation deadline to l8k."""
    validate_steps = [step for step in _steps("network-operator.yaml") if step["name"].endswith("_validate")]

    assert len(validate_steps) == 6
    assert all(step["timeout"] is None for step in validate_steps)
