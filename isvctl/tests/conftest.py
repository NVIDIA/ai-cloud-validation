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
AWS_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts"

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


def write_axis_suite(
    root: Path,
    name: str,
    value: str,
    axis_key: str,
    *,
    platforms: list[str] | None = None,
    validations: bool = True,
) -> None:
    """Write a provider-neutral suite declaring one platform/module axis key.

    Wires one check so the suite counts as a "real" platform/module to the
    planner (a validation-less platform suite defines a modules-only column).
    ``platforms`` adds a positive column declaration to the check;
    ``validations=False`` writes an axis-only suite with no checks.
    """
    suite_path = root / "suites" / name
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    if not validations:
        body = f"tests:\n  {axis_key}: {value}\n  validations: {{}}\n"
    else:
        platforms_yaml = ""
        if platforms is not None:
            platforms_yaml = "\n          platforms: [" + ", ".join(f'"{p}"' for p in platforms) + "]"
        body = f"""\
tests:
  {axis_key}: {value}
  validations:
    sample:
      checks:
        FieldExistsCheck-{value}_axis:
          test_id: "N/A"
          labels: ["{value}"]{platforms_yaml}
          fields: ["success"]
"""
    suite_path.write_text(body, encoding="utf-8")


def write_axis_provider_config(
    root: Path,
    provider: str,
    name: str,
    suite: str,
    *,
    run_platform: str | None = None,
    session_keys: list[str] | None = None,
) -> Path:
    """Write a provider config importing one suite (inheriting its kind/platform).

    With ``run_platform``, the config also carries a runnable echo step in that
    platform's commands group so CLI tests can execute it end-to-end.
    ``session_keys`` adds a ``commands[<key>]`` session lifecycle per key
    (``<module>@<column>``) so the module joins that column's session plan.
    """
    config_path = root / "providers" / provider / "config" / name
    config_path.parent.mkdir(parents=True, exist_ok=True)
    body = f"""\
import:
  - ../../../suites/{suite}
version: "1.0"
"""
    command_groups = []
    if run_platform:
        command_groups.append(run_platform)
    command_groups.extend(session_keys or [])
    if command_groups:
        body += "commands:\n"
        for group in command_groups:
            body += f"""\
  {group}:
    phases: [test]
    steps:
      - name: test_step
        command: echo
        args: ['{{"success": true}}']
        phase: test
"""
    config_path.write_text(body, encoding="utf-8")
    return config_path


def load_aws_script(domain: str, script_name: str) -> ModuleType:
    """Load an AWS provider script as a module for direct helper testing.

    Cached per script so tests don't re-import boto3 on every call.
    """
    key = f"{domain}/{script_name}"
    if key in _LOADED_MODULES:
        return _LOADED_MODULES[key]
    script_path = AWS_SCRIPTS / domain / script_name
    spec = importlib.util.spec_from_file_location(f"test_{domain}_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _LOADED_MODULES[key] = module
    return module


def load_vm_script(script_name: str) -> ModuleType:
    """Load an AWS VM script as a module for direct helper testing."""
    return load_aws_script("vm", script_name)
