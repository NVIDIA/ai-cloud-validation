# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify upgrade script, schema and canonical suite integration without a cluster."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from isvctl.config.merger import merge_yaml_files
from isvctl.config.output_schemas import get_schema_for_step, validate_output
from isvctl.config.schema import RunConfig
from isvtest.validations.k8s_upgrades import (
    K8sControlPlaneUpgradeCheck,
    K8sManagedUpgradePolicyCheck,
    K8sNodePoolUpgradeCheck,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "isvctl/configs/providers/shared/k8s/upgrades.yaml"
SCRIPT = ROOT / "isvctl/configs/providers/my-isv/scripts/k8s/upgrade_test.py"
CHECKS = {
    "managed_upgrade_policy": K8sManagedUpgradePolicyCheck,
    "control_plane_upgrade": K8sControlPlaneUpgradeCheck,
    "node_pool_upgrade": K8sNodePoolUpgradeCheck,
}


@pytest.mark.parametrize("name", CHECKS)
@pytest.mark.parametrize("demo", [True, False])
def test_adapter_contract(name: str, demo: bool) -> None:
    """Demo output passes, but real mode never fabricates an observed upgrade."""
    env = {**os.environ, "ISVCTL_DEMO_MODE": "1" if demo else "0"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", name], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == (0 if demo else 1), result.stderr
    output = json.loads(result.stdout)
    valid, errors = validate_output(output, "k8s_upgrade")
    assert valid, errors
    check = CHECKS[name](config={"step_output": output})
    check.run()
    assert check.passed is demo, check.message
    if not demo:
        assert "Not implemented" in output["error"]
        assert "operation" not in output


def test_suite_wiring_and_opt_in() -> None:
    """Every contract is discovered and bound only when upgrade steps are opted in."""
    merged = merge_yaml_files([str(CONFIG)])
    RunConfig.model_validate(merged)
    entries = merged["tests"]["validations"]["k8s_upgrades"]
    checks = [next(iter(entry.values())) for entry in entries]
    steps = merged["commands"]["kubernetes"]["steps"]
    assert {next(iter(entry)) for entry in entries} == {c.__name__ for c in CHECKS.values()}
    assert {c["test_id"] for c in checks} == {"K8S08-01", "K8S09-01", "K8S10-01"}
    assert {c["step"] for c in checks} == {s["name"] for s in steps}
    assert all(get_schema_for_step(s["name"]) == "k8s_upgrade" for s in steps)
    assert all(s["phase"] == "test" for s in steps)
    default = yaml.safe_load((ROOT / "isvctl/configs/suites/k8s.yaml").read_text())
    assert not {s["name"] for s in steps} & {s["name"] for s in default["commands"]["kubernetes"]["steps"]}
