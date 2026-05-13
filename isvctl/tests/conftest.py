# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Shared test helpers for the isvctl test suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_VM_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "vm"

_LOADED_MODULES: dict[str, ModuleType] = {}


def load_vm_script(script_name: str) -> ModuleType:
    """Load an AWS VM script as a module for direct helper testing.

    Cached per script name so tests don't re-import boto3 on every call.
    """
    if script_name in _LOADED_MODULES:
        return _LOADED_MODULES[script_name]
    script_path = AWS_VM_SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(f"test_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _LOADED_MODULES[script_name] = module
    return module
