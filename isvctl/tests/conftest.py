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

"""Shared test helpers for the isvctl test suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_VM_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "vm"

_LOADED_MODULES: dict[str, ModuleType] = {}


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point isvctl user config at an empty temp dir for every test.

    Several CLI commands (`configure`, `doctor`, `test`) now load
    config.yml / secrets.yml. Without this, tests would read the developer's
    real ~/.config/isvctl and become environment-dependent. Tests that need a
    populated config override XDG_CONFIG_HOME themselves.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))
    monkeypatch.delenv("ISVCTL_CONFIG", raising=False)
    monkeypatch.delenv("ISVCTL_SECRETS", raising=False)


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
