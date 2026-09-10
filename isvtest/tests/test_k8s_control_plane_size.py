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

"""Tests for the control-plane size pinning validation (K8S27-01)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_control_plane_size import K8sControlPlaneSizePinnedCheck

ENDPOINTS_COMMAND = "kubectl get endpoints kubernetes -n default -o json"


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failed ``CommandResult``."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _endpoints(*ips: str) -> str:
    """Return an Endpoints payload publishing one address per ``ips`` entry."""
    return json.dumps({"subsets": [{"addresses": [{"ip": ip} for ip in ips]}]})


def _step_output(**overrides: Any) -> dict[str, Any]:
    """Return a control-plane pin step output honouring a 3-instance pin."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "kubernetes",
        "requested_instance_count": 3,
        "instance_count": 3,
    }
    output.update(overrides)
    return output


def _run(step_output: Any, endpoints: CommandResult | None = None) -> K8sControlPlaneSizePinnedCheck:
    """Run the check against ``step_output``, answering the endpoints probe with ``endpoints``."""
    check = K8sControlPlaneSizePinnedCheck(config={"step_output": step_output})
    response = endpoints if endpoints is not None else _ok(_endpoints("10.0.0.1", "10.0.0.2", "10.0.0.3"))
    with (
        patch("isvtest.validations.k8s_control_plane_size.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", return_value=response) as mock_run,
    ):
        check.run()
    check.commands_run = [call[0][0] for call in mock_run.call_args_list]  # type: ignore[attr-defined]
    return check


def test_passes_when_registered_apiservers_match_the_pin() -> None:
    """A pin the provider delivered and the cluster corroborates exactly passes."""
    check = _run(_step_output())

    assert check.passed, check.message
    assert "pinned at 3 instance(s)" in check.message
    assert "matching the pin" in check.message
    assert check.commands_run == [ENDPOINTS_COMMAND]


def test_passes_when_a_load_balanced_api_address_hides_instances() -> None:
    """One endpoint behind a fronted API address cannot contradict a larger pin."""
    check = _run(_step_output(), endpoints=_ok(_endpoints("10.0.0.1")))

    assert check.passed, check.message
    assert "load-balanced" in check.message


def test_fails_when_more_apiservers_are_registered_than_pinned() -> None:
    """Extra registered API servers cannot be explained by fronting, so the pin is not held."""
    check = _run(_step_output(), endpoints=_ok(_endpoints("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4")))

    assert not check.passed
    assert "not held at 3 instance(s)" in check.message
    assert "registers 4" in check.message


def test_fails_when_the_provider_delivered_a_different_count() -> None:
    """A delivered count that differs from the request is not a pin, and needs no probe."""
    check = _run(_step_output(instance_count=2))

    assert not check.passed
    assert "requested 3 instance(s) and the provider reports 2" in check.message
    assert check.commands_run == []


def test_counts_distinct_addresses_across_subsets() -> None:
    """Addresses repeated across subsets describe one API server each, not several."""
    payload = json.dumps(
        {
            "subsets": [
                {"addresses": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]},
                {"addresses": [{"ip": "10.0.0.2"}, {"ip": "10.0.0.3"}]},
            ]
        }
    )

    check = _run(_step_output(), endpoints=_ok(payload))

    assert check.passed, check.message
    assert "3 API server endpoint(s)" in check.message


def test_accepts_counts_emitted_as_decimal_strings() -> None:
    """A provider script emitting counts as strings still satisfies the contract."""
    check = _run(_step_output(requested_instance_count="3", instance_count="3"))

    assert check.passed, check.message


def test_fails_when_the_pin_step_reported_failure() -> None:
    """A pin that did not happen is reported with the step's own error."""
    check = _run(_step_output(success=False, error="control-plane resize quota exceeded"))

    assert not check.passed
    assert "control-plane resize quota exceeded" in check.message
    assert check.commands_run == []


def test_fails_without_step_output() -> None:
    """An unbound check has no pin to verify."""
    check = _run(None)

    assert not check.passed
    assert "Missing step_output" in check.message


@pytest.mark.parametrize("requested", [0, -1, True, "three", None, 2.5])
def test_fails_on_an_unusable_requested_count(requested: Any) -> None:
    """The requested count has to be a real instance count for the pin to mean anything."""
    check = _run(_step_output(requested_instance_count=requested))

    assert not check.passed
    assert "requested_instance_count" in check.message


def test_fails_when_the_delivered_count_is_missing() -> None:
    """Without a delivered count there is nothing to compare the request against."""
    output = _step_output()
    output.pop("instance_count")

    check = _run(output)

    assert not check.passed
    assert "instance_count" in check.message


def test_fails_when_the_endpoints_probe_is_refused() -> None:
    """Losing the independent measurement leaves only the provider's own report."""
    check = _run(
        _step_output(),
        endpoints=_fail(stderr='Error from server (Forbidden): endpoints "kubernetes" is forbidden'),
    )

    assert not check.passed
    assert "Could not count registered API servers" in check.message
    assert "Forbidden" in check.message


def test_fails_when_the_endpoints_payload_is_malformed() -> None:
    """Unparseable probe output is inconclusive, not a pass."""
    check = _run(_step_output(), endpoints=_ok("not json"))

    assert not check.passed
    assert "Failed to parse" in check.message


def test_fails_when_no_apiserver_endpoints_are_registered() -> None:
    """An Endpoints object with no addresses corroborates nothing."""
    check = _run(_step_output(), endpoints=_ok(json.dumps({"subsets": []})))

    assert not check.passed
    assert "registers no API server endpoints" in check.message
