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

"""Tests for capability/module resolution (--capability / --module)."""

from __future__ import annotations

from pathlib import Path

import pytest

from isvctl.config.capability_resolution import (
    CapabilityResolutionError,
    classify_provider_configs,
    plan_capability_run,
    resolve_module_config,
)


def _write_suite(root: Path, name: str, platform: str, kind: str) -> None:
    """Write a provider-neutral suite declaring its capability/module axis key."""
    axis_key = "capability" if kind == "capability" else "module"
    suite_path = root / "suites" / name
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(
        f"""\
tests:
  {axis_key}: {platform}
  validations: {{}}
""",
        encoding="utf-8",
    )


def _write_provider_config(root: Path, provider: str, name: str, suite: str) -> Path:
    """Write a provider config importing one suite (inheriting its kind/platform)."""
    config_path = root / "providers" / provider / "config" / name
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""\
import:
  - ../../../suites/{suite}
version: "1.0"
""",
        encoding="utf-8",
    )
    return config_path


def _standard_provider(root: Path) -> None:
    """Build a provider with vm/bare_metal capabilities and iam/network modules."""
    _write_suite(root, "vm.yaml", "vm", "capability")
    _write_suite(root, "bare_metal.yaml", "bare_metal", "capability")
    _write_suite(root, "k8s.yaml", "kubernetes", "capability")
    _write_suite(root, "slurm.yaml", "slurm", "capability")
    _write_suite(root, "iam.yaml", "iam", "module")
    _write_suite(root, "network.yaml", "network", "module")
    _write_provider_config(root, "acme", "vm.yaml", "vm.yaml")
    _write_provider_config(root, "acme", "bare_metal.yaml", "bare_metal.yaml")
    _write_provider_config(root, "acme", "iam.yaml", "iam.yaml")
    _write_provider_config(root, "acme", "network.yaml", "network.yaml")


def test_classify_reads_kind_via_imports(tmp_path: Path) -> None:
    """Configs classify by the effective kind/platform inherited from the suite."""
    _standard_provider(tmp_path)
    classified = classify_provider_configs("acme", configs_root=tmp_path)
    by_platform = {c.platform: c.kind for c in classified}
    assert by_platform == {
        "vm": "capability",
        "bare_metal": "capability",
        "iam": "module",
        "network": "module",
    }


def test_classify_errors_on_missing_kind(tmp_path: Path) -> None:
    """A config that inherits no kind raises a clear error naming the file."""
    _standard_provider(tmp_path)
    stray = tmp_path / "providers" / "acme" / "config" / "stray.yaml"
    stray.write_text("tests:\n  platform: mystery\n", encoding="utf-8")
    with pytest.raises(CapabilityResolutionError) as exc:
        classify_provider_configs("acme", configs_root=tmp_path)
    assert "stray.yaml" in str(exc.value)
    assert "tests.capability" in str(exc.value)
    assert "tests.module" in str(exc.value)


def test_plan_capability_run_orders_and_excludes(tmp_path: Path) -> None:
    """A capability plan runs the capability first, then modules with other-capability excludes."""
    _standard_provider(tmp_path)
    runs = plan_capability_run("acme", "vm", configs_root=tmp_path)

    assert runs[0].role == "capability"
    assert runs[0].platform == "vm"
    assert runs[0].exclude_labels == ()

    module_runs = runs[1:]
    assert [r.platform for r in module_runs] == ["iam", "network"]
    for module_run in module_runs:
        assert module_run.role == "module"
        # every capability label except the selected one
        assert module_run.exclude_labels == ("bare_metal", "kubernetes", "slurm")


def test_plan_capability_run_k8s_alias(tmp_path: Path) -> None:
    """`--capability k8s` resolves to the kubernetes capability config."""
    _standard_provider(tmp_path)
    _write_provider_config(tmp_path, "acme", "eks.yaml", "k8s.yaml")
    runs = plan_capability_run("acme", "k8s", configs_root=tmp_path)
    assert runs[0].role == "capability"
    assert runs[0].platform == "kubernetes"


def test_plan_capability_run_missing_lists_available(tmp_path: Path) -> None:
    """Selecting an absent capability lists the ones the provider does expose."""
    _standard_provider(tmp_path)
    (tmp_path / "providers" / "acme" / "config" / "k8s.yaml").unlink(missing_ok=True)
    # acme has vm + bare_metal capabilities; slurm is absent
    with pytest.raises(CapabilityResolutionError) as exc:
        plan_capability_run("acme", "slurm", configs_root=tmp_path)
    assert "no 'slurm' capability" in str(exc.value)
    assert "bare_metal" in str(exc.value)
    assert "vm" in str(exc.value)


def test_plan_capability_run_duplicate_platform_errors(tmp_path: Path) -> None:
    """Two configs for the same capability tell the user to disambiguate with -f."""
    _standard_provider(tmp_path)
    _write_provider_config(tmp_path, "acme", "vm2.yaml", "vm.yaml")
    with pytest.raises(CapabilityResolutionError) as exc:
        plan_capability_run("acme", "vm", configs_root=tmp_path)
    assert "multiple" in str(exc.value)
    assert "--config/-f" in str(exc.value)


def test_resolve_module_config_returns_single(tmp_path: Path) -> None:
    """`--module iam` resolves the one iam module config, no capability excludes."""
    _standard_provider(tmp_path)
    run = resolve_module_config("acme", "iam", configs_root=tmp_path)
    assert run.role == "module"
    assert run.platform == "iam"
    assert run.exclude_labels == ()
    assert run.config_path.name == "iam.yaml"


def test_resolve_module_config_missing_lists_available(tmp_path: Path) -> None:
    """An absent module lists the module platforms the provider exposes."""
    _standard_provider(tmp_path)
    with pytest.raises(CapabilityResolutionError) as exc:
        resolve_module_config("acme", "storage", configs_root=tmp_path)
    assert "no 'storage' module" in str(exc.value)
    assert "iam" in str(exc.value)
    assert "network" in str(exc.value)
