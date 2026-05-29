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

"""Config and provider-asset sanity checks."""

from pathlib import Path

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.doctor.result import CategoryReport, CheckResult, Status


def _find_repo_root() -> Path | None:
    """Walk up from this file until we find an ``isvctl/configs`` directory.

    Returns the workspace root (parent of ``isvctl/``), or None if we can't
    locate it (e.g., the package was installed outside its source tree).
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "isvctl" / "configs").is_dir():
            return ancestor
    return None


def _check_repo_layout(root: Path) -> list[CheckResult]:
    """Verify the shipped suites directory is intact.

    Provider directories are not enumerated here — `doctor` is ISV-agnostic by
    default; the shipped `aws` and `my-isv` trees are reference/scaffold and
    should not be flagged as something the user is expected to have. Per-provider
    script checks happen only when the user opts in via ``--provider <name>``.
    """
    results: list[CheckResult] = []

    suites_dir = root / "isvctl" / "configs" / "suites"
    suite_yamls = sorted(suites_dir.glob("*.yaml")) if suites_dir.is_dir() else []
    if suite_yamls:
        results.append(
            CheckResult(
                name="configs/suites",
                status=Status.OK,
                message=f"{len(suite_yamls)} suite(s) present",
                detail="\n".join(str(p.relative_to(root)) for p in suite_yamls),
            )
        )
    else:
        results.append(
            CheckResult(
                name="configs/suites",
                status=Status.FAIL,
                message="no suite YAMLs found",
                remediation=f"re-clone the repository; expected files under {suites_dir}",
            )
        )

    return results


def _check_provider_arg(root: Path, providers: list[str]) -> list[CheckResult]:
    """Validate every --provider value against the on-disk provider directories."""
    results: list[CheckResult] = []
    base = root / "isvctl" / "configs" / "providers"
    for prov in providers:
        prov_dir = base / prov
        scripts_dir = prov_dir / "scripts"
        if scripts_dir.is_dir():
            results.append(
                CheckResult(
                    name=f"--provider {prov}",
                    status=Status.OK,
                    message="scripts directory present",
                    detail=f"path: {scripts_dir}",
                )
            )
        else:
            results.append(
                CheckResult(
                    name=f"--provider {prov}",
                    status=Status.FAIL,
                    message="provider scripts directory not found",
                    remediation=f"expected path: {scripts_dir}",
                )
            )
    return results


def _validate_config_file(paths: list[Path]) -> CheckResult:
    """Run the merge + RunConfig pipeline against a list of -f paths."""
    name = " + ".join(p.name for p in paths)
    try:
        merged = merge_yaml_files([str(p) for p in paths], [])
    except Exception as exc:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            message=f"merge failed: {exc.__class__.__name__}",
            detail=str(exc),
            remediation="check YAML syntax and any `import:` paths",
        )

    try:
        RunConfig.model_validate(merged)
    except Exception as exc:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            message=f"schema validation failed: {exc.__class__.__name__}",
            detail=str(exc),
            remediation="see `isvctl test validate -f ...` for full Pydantic errors",
        )

    return CheckResult(
        name=name,
        status=Status.OK,
        message="merged and validated",
    )


def check_configs(
    config_files: list[Path] | None = None,
    providers: list[str] | None = None,
) -> CategoryReport:
    """Run the config category.

    Args:
        config_files: When provided, merge + validate these -f files together
            (same semantics as ``isvctl test run -f ...``).
        providers: --provider values to verify against on-disk provider dirs.

    Returns:
        CategoryReport with at least a repo-layout result, plus per-config
        validation results when ``config_files`` is supplied.
    """
    results: list[CheckResult] = []
    root = _find_repo_root()

    if root is None:
        results.append(
            CheckResult(
                name="repo layout",
                status=Status.WARN,
                message="could not locate workspace root from package location",
                remediation="run `doctor` from inside a source checkout",
            )
        )
    else:
        results.extend(_check_repo_layout(root))
        if providers:
            results.extend(_check_provider_arg(root, providers))

    if config_files:
        results.append(_validate_config_file(config_files))

    return CategoryReport(name="config", results=results)
