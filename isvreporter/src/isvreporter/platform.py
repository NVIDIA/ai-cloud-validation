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

"""Platform constants and utilities for isvreporter.

Mirrors isvctl.config.platform for standalone isvreporter installations.
"""

from pathlib import Path

import yaml

# Canonical platform types (uppercase, matching backend enums)
KUBERNETES = "KUBERNETES"
SLURM = "SLURM"
BARE_METAL = "BARE_METAL"
CONTROL_PLANE = "CONTROL_PLANE"
IAM = "IAM"
NETWORK = "NETWORK"
SECURITY = "SECURITY"
VM = "VM"
IMAGE_REGISTRY = "IMAGE_REGISTRY"
OBSERVABILITY = "OBSERVABILITY"
STORAGE = "STORAGE"

ALL_PLATFORMS = {
    KUBERNETES,
    SLURM,
    BARE_METAL,
    CONTROL_PLANE,
    IAM,
    NETWORK,
    SECURITY,
    VM,
    IMAGE_REGISTRY,
    OBSERVABILITY,
    STORAGE,
}

# Platform aliases (normalized to canonical uppercase names)
PLATFORM_ALIASES: dict[str, str] = {
    "k8s": KUBERNETES,
    "kubernetes": KUBERNETES,
    "slurm": SLURM,
    "bare_metal": BARE_METAL,
    "bm": BARE_METAL,
    "control_plane": CONTROL_PLANE,
    "iam": IAM,
    "network": NETWORK,
    "security": SECURITY,
    "vm": VM,
    "image_registry": IMAGE_REGISTRY,
    "observability": OBSERVABILITY,
    "storage": STORAGE,
}

DEFAULT_PLATFORM = KUBERNETES


def normalize_platform(platform: str | None) -> str:
    """Normalize a platform string to a canonical uppercase name.

    Args:
        platform: Platform string (e.g., 'k8s', 'kubernetes', 'KUBERNETES',
            'iam', 'control_plane')

    Returns:
        Canonical uppercase platform string (e.g., 'KUBERNETES', 'IAM')
    """
    if not platform:
        return DEFAULT_PLATFORM

    cleaned = platform.strip()

    if cleaned.upper() in ALL_PLATFORMS:
        return cleaned.upper()

    normalized = cleaned.lower().replace("-", "_")
    return PLATFORM_ALIASES.get(normalized, DEFAULT_PLATFORM)


def get_platform_from_config(config_path: Path | str) -> str:
    """Extract and normalize platform from a config file.

    Reads the file raw (imports are not resolved), so it prefers an explicit
    ``tests.platform`` (platform suites) and falls back to ``tests.module``
    (module suites, whose module value is the platform). Configs that inherit
    their axis key only via ``import:`` still need an explicit value here.

    Args:
        config_path: Path to the YAML config file

    Returns:
        Canonical uppercase platform string
    """
    capability, module = get_axes_from_config(config_path)
    return capability or module or DEFAULT_PLATFORM


def _normalize_axis_value(value: str) -> str:
    """Uppercase an axis value, resolving known aliases (k8s -> KUBERNETES).

    Unknown values pass through uppercased (hyphens as underscores) so new
    catalog-driven axis values (e.g. a future platform) are not coerced to a
    legacy name.
    """
    normalized = value.strip().lower().replace("-", "_")
    return PLATFORM_ALIASES.get(normalized, normalized.upper())


def get_axes_from_config(config_path: Path | str) -> tuple[str | None, str | None]:
    """Return the ``(capability, module)`` pair a config's axis keys declare.

    A config declaring ``tests.module`` is a module suite: it exercises a
    module and targets no capability of its own -> ``(None, MODULE)``. One
    declaring ``tests.platform`` is a platform suite -> ``(CAPABILITY, None)``.
    The suite schema makes the keys mutually exclusive, but a config carrying
    both (legacy) reports both. Reads the file raw (imports are not resolved);
    ``(None, None)`` when the file is unreadable or declares neither key.
    """
    try:
        with open(config_path) as f:
            config_data = yaml.safe_load(f)
        tests = (config_data or {}).get("tests", {}) or {}
    except Exception:
        return None, None

    def _axis(key: str) -> str | None:
        value = tests.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_axis_value(value)
        return None

    return _axis("platform"), _axis("module")


def is_valid_platform(platform: str | None) -> bool:
    """Check if a platform string is valid (after normalization).

    Args:
        platform: Platform string to check

    Returns:
        True if the platform is valid, False otherwise
    """
    if not platform:
        return False
    cleaned = platform.strip()
    if cleaned.upper() in ALL_PLATFORMS:
        return True
    normalized = cleaned.lower().replace("-", "_")
    return normalized in PLATFORM_ALIASES
