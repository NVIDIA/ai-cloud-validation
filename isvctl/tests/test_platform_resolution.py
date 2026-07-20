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


def test_plan_platform_run_orders_and_sets_column(tmp_path: Path) -> None:
    """A platform plan runs the platform first, then modules, all under the column."""
    _standard_provider(tmp_path)
    plan = plan_platform_run("acme", "vm", configs_root=tmp_path)
    runs = plan.runs

    assert plan.omitted == []
    assert runs[0].role == "platform"
    assert runs[0].platform == "vm"
    assert runs[0].column_platform == "vm"

    module_runs = runs[1:]
    assert [r.platform for r in module_runs] == ["iam", "network"]
    for module_run in module_runs:
        assert module_run.role == "module"
        # module runs execute under the column; checks declaring a platforms:
        # restriction that excludes it are skipped at resolution time
        assert module_run.column_platform == "vm"


def test_plan_platform_run_k8s_alias(tmp_path: Path) -> None:
    """`--platform k8s` resolves to the kubernetes platform config."""
    _standard_provider(tmp_path)
    write_axis_provider_config(tmp_path, "acme", "eks.yaml", "k8s.yaml")
    runs = plan_platform_run("acme", "k8s", configs_root=tmp_path).runs
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
    """`--module iam` resolves the one iam module config, no platform column."""
    _standard_provider(tmp_path)
    (run,) = resolve_module_configs("acme", ["iam"], configs_root=tmp_path)
    assert run.role == "module"
    assert run.platform == "iam"
    assert run.config_path.name == "iam.yaml"
    # standalone module runs have no platform column, hence no capability
    # and no platform filtering
    assert run.column_platform is None


def test_resolve_module_configs_missing_lists_available(tmp_path: Path) -> None:
    """An absent module lists the module platforms the provider exposes."""
    _standard_provider(tmp_path)
    with pytest.raises(PlatformResolutionError) as exc:
        resolve_module_configs("acme", ["storage"], configs_root=tmp_path)
    assert "no 'storage' module" in str(exc.value)
    assert "iam" in str(exc.value)
    assert "network" in str(exc.value)


def _foundational_provider(root: Path) -> None:
    """Standard provider plus a validation-less foundational suite; iam declares it."""
    _standard_provider(root)
    write_axis_suite(root, "foundational.yaml", "foundational", "platform", validations=False)
    write_axis_suite(root, "iam.yaml", "iam", "module", platforms=["foundational"])


def test_plan_foundational_column_is_modules_only(tmp_path: Path) -> None:
    """A validation-less platform suite plans no platform run and needs no provider config.

    Only modules positively declaring the column join it; the rest are omitted.
    """
    _foundational_provider(tmp_path)
    plan = plan_platform_run("acme", "foundational", configs_root=tmp_path)

    assert [(r.role, r.platform) for r in plan.runs] == [("module", "iam")]
    assert plan.runs[0].column_platform == "foundational"
    assert [(o.module, o.reason) for o in plan.omitted] == [
        ("network", "no checks compatible with column 'foundational'"),
    ]


def test_plan_platform_run_omits_column_incompatible_modules(tmp_path: Path) -> None:
    """A module whose checks all exclude the column is omitted from its plan."""
    _foundational_provider(tmp_path)
    plan = plan_platform_run("acme", "vm", configs_root=tmp_path)

    # iam's only check declares platforms: ["foundational"], so the vm column
    # would pay iam's setup/teardown to run zero checks - it is omitted.
    assert [(r.role, r.platform) for r in plan.runs] == [("platform", "vm"), ("module", "network")]
    assert [(o.module, o.reason) for o in plan.omitted] == [("iam", "no checks compatible with column 'vm'")]
    assert plan.omitted[0].config_path.name == "iam.yaml"


def test_plan_platform_run_still_errors_when_real_platform_config_missing(tmp_path: Path) -> None:
    """The no-platform-run rule applies only to validation-less suites."""
    _foundational_provider(tmp_path)
    # slurm's suite wires validations, so the missing provider config errors.
    with pytest.raises(PlatformResolutionError) as exc:
        plan_platform_run("acme", "slurm", configs_root=tmp_path)
    assert "no 'slurm' platform" in str(exc.value)


def test_resolve_module_configs_with_column_sets_normalized_column(tmp_path: Path) -> None:
    """The --platform X --module m intersect carries the aliased column on each run."""
    _standard_provider(tmp_path)
    (run,) = resolve_module_configs("acme", ["iam"], configs_root=tmp_path, column_platform="k8s")
    assert run.role == "module"
    assert run.platform == "iam"
    assert run.column_platform == "kubernetes"


def test_resolve_module_configs_rejects_unknown_column(tmp_path: Path) -> None:
    """An intersect column must be on the suite-derived platform axis."""
    _standard_provider(tmp_path)
    with pytest.raises(PlatformResolutionError) as exc:
        resolve_module_configs("acme", ["iam"], configs_root=tmp_path, column_platform="mainframe")
    assert "Unknown platform 'mainframe'" in str(exc.value)
    assert "vm" in str(exc.value)


REPO_CONFIGS_ROOT = Path(__file__).resolve().parents[1] / "configs"


@pytest.mark.parametrize("provider", ["aws", "my-isv"])
def test_repo_foundational_column_is_exactly_foundational_modules(provider: str) -> None:
    """The real foundational column contains every once-per-lab API module."""
    plan = plan_platform_run(provider, "foundational", configs_root=REPO_CONFIGS_ROOT)
    assert [(r.role, r.platform) for r in plan.runs] == [
        ("module", "control_plane"),
        ("module", "iam"),
        ("module", "image_registry"),
        ("module", "network"),
        ("module", "observability"),
        ("module", "security"),
    ]
    assert all(r.column_platform == "foundational" for r in plan.runs)


@pytest.mark.parametrize("column", ["vm", "kubernetes"])
def test_repo_runtime_columns_omit_foundational_modules(column: str) -> None:
    """Foundational-only modules are omitted from runtime columns."""
    plan = plan_platform_run("aws", column, configs_root=REPO_CONFIGS_ROOT)
    assert {run.platform for run in plan.runs} >= {column, "storage"}
    assert {(o.module, o.reason) for o in plan.omitted} == {
        ("control_plane", f"no checks compatible with column '{column}'"),
        ("iam", f"no checks compatible with column '{column}'"),
        ("image_registry", f"no checks compatible with column '{column}'"),
        ("network", f"no checks compatible with column '{column}'"),
        ("observability", f"no checks compatible with column '{column}'"),
        ("security", f"no checks compatible with column '{column}'"),
    }
