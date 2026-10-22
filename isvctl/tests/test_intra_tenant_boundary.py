# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""SEC30-02 must consume its own probes, independently of SEC30-01."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from isvtest.core.resolution import parse_validations, resolve_entries
from isvtest.validations.network import SegmentBoundaryDefaultDenyCheck

from isvctl.config.merger import merge_yaml_files
from isvctl.config.output_schemas import validate_output

CONFIGS = Path(__file__).resolve().parents[1] / "configs"


@pytest.mark.parametrize("provider", ["aws", "my-isv"])
@pytest.mark.parametrize(
    "control,prohibited,passed",
    [
        ("connected", "timeout", True),
        ("timeout", "timeout", False),
        ("connected", "connected", False),
        ("connected", "refused", False),
        ("connected", "error", False),
    ],
)
def test_intra_tenant_result_is_independent(
    provider: str,
    control: str,
    prohibited: str,
    passed: bool,
) -> None:
    """A passing provider boundary cannot hide failed intra-tenant probes."""
    config = merge_yaml_files([CONFIGS / f"providers/{provider}/config/network.yaml"])
    entries = [
        e
        for e in parse_validations(config["tests"]["validations"])
        if e.name.startswith("SegmentBoundaryDefaultDenyCheck")
    ]
    assert len(entries) == 2
    assert len({entry.name for entry in entries}) == 2
    steps = {s["name"]: s for s in config["commands"]["network"]["steps"]}
    intra = steps["intra_tenant_boundary"]
    assert intra["args"][-2:] == ["--boundary", "intra-tenant"]
    assert intra["output_schema"] == "segment_boundary"
    output = {
        "success": True,
        "platform": "network",
        "positive_control": {"protocol": "tcp", "port": 22, "result": "connected"},
        "prohibited_flows": [{"protocol": "tcp", "port": 443, "result": "timeout"}],
    }
    intra_output = {
        **output,
        "positive_control": {"protocol": "tcp", "port": 22, "result": control},
        "prohibited_flows": [{"protocol": "tcp", "port": 443, "result": prohibited}],
    }
    resolved = resolve_entries(
        entries,
        step_outputs={"segment_boundary": output, "intra_tenant_boundary": intra_output},
        step_phases={name: step["phase"] for name, step in steps.items()},
        requested_phases={"test"},
        include_labels=set(),
        exclude_labels=set(),
        exclude_tests=set(),
        render_context={},
        capability="vm",
    )
    results = {}
    for item in resolved:
        assert item.is_ready
        results[item.rendered_params["test_id"]] = SegmentBoundaryDefaultDenyCheck(
            config=item.rendered_params
        ).execute()["passed"]
    assert results == {"SEC30-01": True, "SEC30-02": passed}


@pytest.mark.parametrize("demo", ["0", "1"])
def test_intra_tenant_template_requires_implementation(demo: str) -> None:
    """The new mode must not emit synthetic success during a real run."""
    script = CONFIGS / "providers/my-isv/scripts/network/segment_boundary_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "test", "--cidr", "10.87.0.0/16", "--boundary", "intra-tenant"],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ | {"ISVCTL_DEMO_MODE": demo},
    )
    output = json.loads(completed.stdout)
    assert validate_output(output, "segment_boundary")[0]
    assert completed.returncode == (0 if demo == "1" else 1)
    assert SegmentBoundaryDefaultDenyCheck(config={"step_output": output}).execute()["passed"] == (demo == "1")
