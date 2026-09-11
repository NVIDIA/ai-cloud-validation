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

SLICES_COMMAND = "kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o json"
ENDPOINTS_COMMAND = "kubectl get endpoints kubernetes -n default -o json"


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failed ``CommandResult``."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _slice(*ips: str, family: str = "IPv4", ready: bool | None = True) -> dict[str, Any]:
    """Return one EndpointSlice publishing an endpoint per ``ips`` entry."""
    endpoint: list[dict[str, Any]] = []
    for ip in ips:
        record: dict[str, Any] = {"addresses": [ip]}
        if ready is not None:
            record["conditions"] = {"ready": ready}
        endpoint.append(record)
    return {"addressType": family, "endpoints": endpoint}


def _slices(*slices: dict[str, Any]) -> str:
    """Return a kubectl EndpointSlice list payload wrapping ``slices``."""
    return json.dumps({"items": list(slices)})


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


def _run(
    step_output: Any,
    slices: CommandResult | None = None,
    endpoints: CommandResult | None = None,
) -> tuple[K8sControlPlaneSizePinnedCheck, list[str]]:
    """Run the check, answering the EndpointSlice probe and its Endpoints fallback.

    Returns the check alongside the kubectl commands it issued, so a test can
    assert which probes ran. The fallback defaults to a refusal so a test
    exercising the EndpointSlice path cannot pass by silently falling through
    to Endpoints.
    """
    check = K8sControlPlaneSizePinnedCheck(config={"step_output": step_output})
    responses = {
        SLICES_COMMAND: slices if slices is not None else _ok(_slices(_slice("10.0.0.1", "10.0.0.2", "10.0.0.3"))),
        ENDPOINTS_COMMAND: endpoints if endpoints is not None else _fail(stderr="Endpoints was not expected"),
    }
    with (
        patch("isvtest.validations.k8s_control_plane_size.get_kubectl_base_shell", return_value="kubectl"),
        patch.object(check, "run_command", side_effect=lambda command, **_: responses[command]) as mock_run,
    ):
        check.run()
    return check, [call[0][0] for call in mock_run.call_args_list]


def test_passes_when_registered_apiservers_match_the_pin() -> None:
    """A pin the provider delivered and the cluster corroborates exactly passes."""
    check, commands = _run(_step_output())

    assert check.passed, check.message
    assert "pinned at 3 instance(s)" in check.message
    assert "matching the pin" in check.message
    assert commands == [SLICES_COMMAND]


def test_passes_when_a_load_balanced_api_address_hides_instances() -> None:
    """One endpoint behind a fronted API address cannot contradict a larger pin."""
    check, _ = _run(_step_output(), slices=_ok(_slices(_slice("10.0.0.1"))))

    assert check.passed, check.message
    assert "load-balanced" in check.message


def test_fails_when_more_apiservers_are_registered_than_pinned() -> None:
    """Extra registered API servers cannot be explained by fronting, so the pin is not held."""
    check, _ = _run(
        _step_output(),
        slices=_ok(_slices(_slice("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"))),
    )

    assert not check.passed
    assert "not held at 3 instance(s)" in check.message
    assert "registers 4" in check.message


def test_fails_when_the_provider_delivered_a_different_count() -> None:
    """A delivered count that differs from the request is not a pin, and needs no probe."""
    check, commands = _run(_step_output(instance_count=2))

    assert not check.passed
    assert "requested 3 instance(s) and the provider reports 2" in check.message
    assert commands == []


def test_counts_each_dual_stack_address_family_once() -> None:
    """A dual-stack Service lists every API server per family, so families are not additive."""
    check, _ = _run(
        _step_output(),
        slices=_ok(
            _slices(
                _slice("10.0.0.1", "10.0.0.2", "10.0.0.3"),
                _slice("fd00::1", "fd00::2", "fd00::3", family="IPv6"),
            )
        ),
    )

    assert check.passed, check.message
    assert "3 API server endpoint(s)" in check.message


def test_pools_endpoints_sharded_across_slices_of_one_family() -> None:
    """One family's endpoints may span several slices, which together describe the instances."""
    check, _ = _run(
        _step_output(),
        slices=_ok(_slices(_slice("10.0.0.1", "10.0.0.2"), _slice("10.0.0.3"))),
    )

    assert check.passed, check.message
    assert "3 API server endpoint(s)" in check.message


def test_ignores_endpoints_that_are_not_ready() -> None:
    """An API server withdrawn from service is not registered capacity."""
    check, _ = _run(
        _step_output(requested_instance_count=2, instance_count=2),
        slices=_ok(_slices(_slice("10.0.0.1", "10.0.0.2"), _slice("10.0.0.3", ready=False))),
    )

    assert check.passed, check.message
    assert "2 API server endpoint(s)" in check.message


def test_counts_an_endpoint_that_states_no_ready_condition() -> None:
    """The EndpointSlice API defines an absent ready condition as ready."""
    check, _ = _run(_step_output(), slices=_ok(_slices(_slice("10.0.0.1", "10.0.0.2", "10.0.0.3", ready=None))))

    assert check.passed, check.message
    assert "matching the pin" in check.message


def test_falls_back_to_endpoints_when_endpointslices_are_refused() -> None:
    """A cluster or RBAC grant that only answers on Endpoints still yields the measurement."""
    check, commands = _run(
        _step_output(),
        slices=_fail(stderr="Error from server (Forbidden): endpointslices is forbidden"),
        endpoints=_ok(_endpoints("10.0.0.1", "10.0.0.2", "10.0.0.3")),
    )

    assert check.passed, check.message
    assert "matching the pin" in check.message
    assert commands == [SLICES_COMMAND, ENDPOINTS_COMMAND]


def test_falls_back_to_endpoints_when_no_slices_back_the_service() -> None:
    """An empty slice list is inconclusive rather than proof of an empty control plane."""
    check, commands = _run(
        _step_output(),
        slices=_ok(json.dumps({"items": []})),
        endpoints=_ok(_endpoints("10.0.0.1", "10.0.0.2", "10.0.0.3")),
    )

    assert check.passed, check.message
    assert commands == [SLICES_COMMAND, ENDPOINTS_COMMAND]


def test_counts_distinct_addresses_across_endpoints_subsets() -> None:
    """On the fallback path, addresses repeated across subsets describe one API server each."""
    payload = json.dumps(
        {
            "subsets": [
                {"addresses": [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]},
                {"addresses": [{"ip": "10.0.0.2"}, {"ip": "10.0.0.3"}]},
            ]
        }
    )

    check, _ = _run(_step_output(), slices=_fail(stderr="no EndpointSlice support"), endpoints=_ok(payload))

    assert check.passed, check.message
    assert "3 API server endpoint(s)" in check.message


def test_accepts_counts_emitted_as_decimal_strings() -> None:
    """A provider script emitting counts as strings still satisfies the contract."""
    check, _ = _run(_step_output(requested_instance_count="3", instance_count="3"))

    assert check.passed, check.message


def test_fails_when_the_pin_step_reported_failure() -> None:
    """A pin that did not happen is reported with the step's own error."""
    check, commands = _run(_step_output(success=False, error="control-plane resize quota exceeded"))

    assert not check.passed
    assert "control-plane resize quota exceeded" in check.message
    assert commands == []


def test_fails_without_step_output() -> None:
    """An unbound check has no pin to verify."""
    check, _ = _run(None)

    assert not check.passed
    assert "Missing step_output" in check.message


@pytest.mark.parametrize("requested", [0, -1, True, "three", None, 2.5])
def test_fails_on_an_unusable_requested_count(requested: Any) -> None:
    """The requested count has to be a real instance count for the pin to mean anything."""
    check, _ = _run(_step_output(requested_instance_count=requested))

    assert not check.passed
    assert "requested_instance_count" in check.message


def test_fails_when_the_delivered_count_is_missing() -> None:
    """Without a delivered count there is nothing to compare the request against."""
    output = _step_output()
    output.pop("instance_count")

    check, _ = _run(output)

    assert not check.passed
    assert "instance_count" in check.message


def test_fails_when_both_endpoint_probes_are_refused() -> None:
    """Losing the independent measurement leaves only the provider's own report."""
    check, _ = _run(
        _step_output(),
        slices=_fail(stderr="Error from server (Forbidden): endpointslices is forbidden"),
        endpoints=_fail(stderr='Error from server (Forbidden): endpoints "kubernetes" is forbidden'),
    )

    assert not check.passed
    assert "Could not count registered API servers" in check.message
    assert "endpointslices is forbidden" in check.message
    assert 'endpoints "kubernetes" is forbidden' in check.message


def test_fails_when_both_endpoint_payloads_are_malformed() -> None:
    """Unparseable probe output is inconclusive, not a pass."""
    check, _ = _run(_step_output(), slices=_ok("not json"), endpoints=_ok("not json"))

    assert not check.passed
    assert "Failed to parse" in check.message


def test_fails_when_no_apiserver_endpoints_are_registered_anywhere() -> None:
    """A Service backing no ready endpoints corroborates nothing."""
    check, _ = _run(
        _step_output(),
        slices=_ok(_slices(_slice())),
        endpoints=_ok(json.dumps({"subsets": []})),
    )

    assert not check.passed
    assert "registers no ready API server endpoints" in check.message
