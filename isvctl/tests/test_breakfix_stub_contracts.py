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

"""Contract test that a break-fix scaffold satisfies the check wired to it.

``test_stub_contracts.py`` guards the *CLI* edge -- the ``--flags`` a YAML step
passes must exist in the script's argparse. This guards the other edge: the JSON
a scaffold prints must satisfy the validation the suite binds to that step.

Nothing else covers it. ``make demo-test`` exercises the pairing only as a side
effect of running whole suites, and it does not run the ``k8s`` config at all --
that suite's setup requires a live cluster -- so a break-fix contract change
could sail through green while ``reset_gpus`` silently stopped matching
``GpuResetCheck``. Running the scaffold directly needs no cluster and no cloud.

Scope is the my-isv break-fix scaffolds: they exist to be copied by an ISV, so
their output is the worked example of each contract. Shared and provider scripts
are excluded because they do real work rather than emitting a fixed payload.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from isvtest.core.discovery import discover_all_tests

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO_ROOT / "isvctl" / "configs"
SCAFFOLD_DIR = CONFIGS_DIR / "providers" / "my-isv" / "scripts" / "breakfix"

# Scaffolds print a fixed payload and exit, so anything approaching this is a
# hang rather than slow work.
SCRIPT_TIMEOUT_SECONDS = 30

# Wiring keys that configure coverage reporting rather than the check itself.
NON_CHECK_PARAMS = frozenset({"test_id", "labels"})

JINJA_RE = re.compile(r"\{\{.*?\}\}")


@dataclass(frozen=True)
class ScaffoldCheck:
    """One scaffold-to-validation pairing to exercise."""

    config_name: str
    step_name: str
    script: Path
    args: tuple[str, ...]
    check_name: str
    params: tuple[tuple[str, Any], ...]

    def id(self) -> str:
        return f"{self.step_name}->{self.check_name}"


def _suite_wiring() -> dict[str, dict[str, dict[str, Any]]]:
    """Return ``step name -> {check class: wiring params}`` across every suite."""
    wiring: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(CONFIGS_DIR.glob("suites/*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        validations = (doc.get("tests") or {}).get("validations") or {}
        for entry in validations.values():
            if isinstance(entry, dict) and entry.get("step"):
                wiring.setdefault(entry["step"], {}).update(entry.get("checks") or {})
    return wiring


def _collect() -> list[ScaffoldCheck]:
    """Pair every my-isv break-fix scaffold with the checks its step is wired to."""
    wiring = _suite_wiring()
    found: list[ScaffoldCheck] = []
    for path in sorted(CONFIGS_DIR.glob("providers/my-isv/config/*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for entry in (doc.get("commands") or {}).values():
            for step in (entry.get("steps") or []) if isinstance(entry, dict) else []:
                # Opt-in scaffolds still need contract coverage before users enable them.
                if not isinstance(step, dict):
                    continue
                command = step.get("command") or ""
                name = step.get("name")
                script_token = next((t for t in command.split() if t.endswith(".py")), None)
                if script_token is None or name not in wiring:
                    continue
                script = (path.parent / script_token).resolve()
                if script.parent != SCAFFOLD_DIR:
                    continue
                for check_name, params in wiring[name].items():
                    found.append(
                        ScaffoldCheck(
                            config_name=path.name,
                            step_name=name,
                            script=script,
                            args=tuple(step.get("args") or []),
                            check_name=check_name,
                            params=tuple((params or {}).items()),
                        )
                    )
    return found


def _run_scaffold(case: ScaffoldCheck) -> dict[str, Any]:
    """Run a scaffold in demo mode and return the JSON it printed."""
    # Templates resolve from live run context that does not exist here. The value
    # never matters: a scaffold echoes it back at most, and the contract under
    # test is the payload's shape.
    argv = [sys.executable, str(case.script), *(JINJA_RE.sub("demo", a) for a in case.args)]
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=case.script.parent,
        env={**os.environ, "ISVCTL_DEMO_MODE": "1"},
        stdin=subprocess.DEVNULL,
        timeout=SCRIPT_TIMEOUT_SECONDS,
        check=False,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"{case.id()}: {case.script.relative_to(REPO_ROOT)} printed no JSON "
            f"(exit {result.returncode})\n  stdout: {result.stdout[:300]!r}\n  stderr: {result.stderr[:300]!r}"
        )


@pytest.fixture(scope="module")
def validation_classes() -> dict[str, type]:
    """Return every discoverable validation class by name (discovery is slow)."""
    return {cls.__name__: cls for cls in discover_all_tests()}


@pytest.mark.parametrize("case", _collect(), ids=lambda c: c.id() if isinstance(c, ScaffoldCheck) else "")
def test_scaffold_output_satisfies_its_check(case: ScaffoldCheck, validation_classes: dict[str, type]) -> None:
    """A scaffold's demo payload must pass the validation its step is wired to.

    Demo mode exists to make the wiring pass end to end, so a skip here is a
    failure too: it means the payload no longer carries what the check reads.
    """
    check_class = validation_classes.get(case.check_name)
    assert check_class is not None, f"{case.id()}: no validation class named {case.check_name!r}"

    step_output = _run_scaffold(case)
    params = {k: v for k, v in case.params if k not in NON_CHECK_PARAMS}
    check = check_class(config={"step_output": step_output, **params})

    try:
        check.run()
    except pytest.skip.Exception as exc:
        pytest.fail(
            f"{case.id()}: scaffold payload made the check skip rather than pass ({exc}).\n"
            f"  scaffold: {case.script.relative_to(REPO_ROOT)}\n  payload: {json.dumps(step_output)[:300]}"
        )

    assert check.passed, (
        f"{case.id()}: scaffold payload does not satisfy the check.\n"
        f"  scaffold: {case.script.relative_to(REPO_ROOT)}\n"
        f"  message:  {check.message}\n  payload:  {json.dumps(step_output)[:300]}"
    )


def test_every_breakfix_scaffold_is_covered() -> None:
    """Every scaffold in the break-fix directory must be reachable by this test.

    A scaffold nobody wires is dead weight, and one wired under a name this test
    does not resolve is worse: it looks covered and is not.
    """
    exercised = {c.script.name for c in _collect()}
    on_disk = {p.name for p in SCAFFOLD_DIR.glob("*.py")}
    assert on_disk - exercised == set(), (
        f"break-fix scaffolds not reached by any wired step: {sorted(on_disk - exercised)}"
    )
