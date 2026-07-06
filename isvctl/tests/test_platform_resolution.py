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
# See the License for the License governing permissions and
# limitations under the License.

"""Tests for platform/module resolution (--platform / --module)."""

from __future__ import annotations

from pathlib import Path

import pytest

from isvctl.config.platform_resolution import (
    PlatformResolutionError,
    classify_provider_configs,
    plan_platform_run,
    resolve_module_configs,
)

from .conftest import write_axis_provider_config, write_axis_suite


def _standard_provider(root: Path) -> None:
    """Build a provider with vm/bare_metal platforms and iam/network modules."""
    write_axis_suite(root, "vm.yaml", "vm", "platform")
    write_axis_suite(root, "bare_metal.yaml", "bare_metal", "platform")
    write_axis_suite(root, "k8s.yaml", "kubernetes", "platform")
    write_axis_suite(root, "slurm.yaml", "slurm", "platform")
    write_axis_suite(root, "iam.yaml", "iam", "module")
    write_axis_suite(root, "network.yaml", "network", "module")
    write_axis_provider_config(root, "acme", "vm.yaml", "vm.yaml")
    write_axis_provider_config(root, "acme", "bare_metal.yaml", "bare_metal.yaml")
    write_axis_provider_config(root, "acme", "iam.yaml", "iam.yaml")
    write_axis_provider_config(root, "acme", "network.yaml", "network.yaml")


def test_classify_reads_kind_via_imports(tmp_path: Path) -> None:
    """Configs classify by the effective kind/platform inherited from the suite."""
    _standard_provider(tmp_path)
    classified = classify_provider_configs("acme", configs_root=tmp_path)
    by_platform = {c.platform: c.kind for c in classified}
    assert by_platform == {
        "vm": "platform",
        "bare_metal": "platform",
        "iam": "module",
        "network": "module",
    }


def test_classify_errors_on_missing_kind(tmp_path: Path) -> None:
    """A config that inherits no kind raises a clear error naming the file."""
    _standard_provider(tmp_path)
    stray = tmp_path / "providers" / "acme" / "config" / "stray.yaml"
    stray.write_text("tests:\n  cluster_name: mystery\n", encoding="utf-8")
    with pytest.raises(PlatformResolutionError) as exc:
        classify_provider_configs("acme", configs_root=tmp_path)
    assert "stray.yaml" in str(exc.value)
    assert "tests.platform" in str(exc.value)
    assert "tests.module" in str(exc.value)


def test_plan_platform_run_orders_and_excludes(tmp_path: Path) -> None:
    """A platform plan runs the platform first, then modules with other-platform excludes."""
    _standard_provider(tmp_path)
    runs = plan_platform_run("acme", "vm", configs_root=tmp_path)

    assert runs[0].role == "platform"
    assert runs[0].platform == "vm"
    assert runs[0].exclude_labels == ()

    module_runs = runs[1:]
    assert [r.platform for r in module_runs] == ["iam", "network"]
    for module_run in module_runs:
        assert module_run.role == "module"
        # every platform label except the selected one
        assert module_run.exclude_labels == ("bare_metal", "kubernetes", "slurm")


def test_plan_platform_run_k8s_alias(tmp_path: Path) -> None:
    """`--platform k8s` resolves to the kubernetes platform config."""
    _standard_provider(tmp_path)
    write_axis_provider_config(tmp_path, "acme", "eks.yaml", "k8s.yaml")
    runs = plan_platform_run("acme", "k8s", configs_root=tmp_path)
    assert runs[0].role == "platform"
    assert runs[0].platform == "kubernetes"


def test_plan_platform_run_missing_lists_available(tmp_path: Path) -> None:
    """Selecting an absent platform lists the ones the provider does expose."""
    _standard_provider(tmp_path)
    (tmp_path / "providers" / "acme" / "config" / "k8s.yaml").unlink(missing_ok=True)
    # acme has vm + bare_metal platforms; slurm is absent
    with pytest.raises(PlatformResolutionError) as exc:
        plan_platform_run("acme", "slurm", configs_root=tmp_path)
    assert "no 'slurm' platform" in str(exc.value)
    assert "bare_metal" in str(exc.value)
    assert "vm" in str(exc.value)


def test_plan_platform_run_duplicate_platform_errors(tmp_path: Path) -> None:
    """Two configs for the same platform tell the user to disambiguate with -f."""
    _standard_provider(tmp_path)
    write_axis_provider_config(tmp_path, "acme", "vm2.yaml", "vm.yaml")
    with pytest.raises(PlatformResolutionError) as exc:
        plan_platform_run("acme", "vm", configs_root=tmp_path)
    assert "multiple" in str(exc.value)
    assert "--config/-f" in str(exc.value)


def test_resolve_module_configs_returns_single(tmp_path: Path) -> None:
    """`--module iam` resolves the one iam module config, no platform excludes."""
    _standard_provider(tmp_path)
    (run,) = resolve_module_configs("acme", ["iam"], configs_root=tmp_path)
    assert run.role == "module"
    assert run.platform == "iam"
    assert run.exclude_labels == ()
    assert run.config_path.name == "iam.yaml"


def test_resolve_module_configs_missing_lists_available(tmp_path: Path) -> None:
    """An absent module lists the module platforms the provider exposes."""
    _standard_provider(tmp_path)
    with pytest.raises(PlatformResolutionError) as exc:
        resolve_module_configs("acme", ["storage"], configs_root=tmp_path)
    assert "no 'storage' module" in str(exc.value)
    assert "iam" in str(exc.value)
    assert "network" in str(exc.value)
