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

"""Control-plane size pinning checks (K8S27)."""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.k8s import KubectlParseError, get_kubectl_base_shell, parse_kubectl_json
from isvtest.core.validation import BaseValidation

# The ``kubernetes`` Service in ``default`` is maintained by the API servers
# themselves - each instance that reaches the endpoint reconciler publishes one
# address. It is the only provider-neutral view of API server instance count a
# tenant gets on a managed control plane.
APISERVER_SERVICE = "kubernetes"
APISERVER_NAMESPACE = "default"


class K8sControlPlaneSizePinnedCheck(BaseValidation):
    """Verify the control plane holds the instance count the tenant pinned.

    A managed Kubernetes control plane is normally sized by the provider, and
    the requirement is that a tenant can instead pin it to a chosen instance
    count so it is guaranteed to carry a known load limit. Pinning is an act,
    not an observation, so the bound provider step performs it and reports both
    the count the tenant requested and the count the provider ended up running.

    Taking that report at face value would let a provider certify by asserting
    its own compliance, so the delivered count is corroborated against the
    cluster itself: the ``kubernetes`` Service in ``default`` carries one
    endpoint address per registered API server. That measurement is
    independent of the provider, and the suite holds this check back to the
    test phase while the pin runs during setup, so it is also a later sample -
    a size that has drifted since the step reported it shows up here.

    The comparison is deliberately one-sided. A provider that fronts its API
    servers with a single load-balanced address publishes one endpoint however
    many instances are behind it, so fewer registered endpoints than the pin is
    expected and proves nothing either way. More registered endpoints than the
    pin cannot be explained that way: fronting and registration can hide
    instances, never invent them, so the count is not being held.

    Step output:
        requested_instance_count: Control-plane instance count the tenant
            pinned. Must be at least 1.
        instance_count: Control-plane instance count the provider runs after
            the pin.
    """

    description: ClassVar[str] = (
        "Verify the Kubernetes control plane is pinned to the instance count the tenant requested."
    )

    def run(self) -> None:
        """Compare the pin the step reported against the live API server registration."""
        step_output = self.config.get("step_output")
        if not isinstance(step_output, dict):
            self.set_failed("Missing step_output for control-plane size pinning validation")
            return

        if step_output.get("success") is False:
            self.set_failed(str(step_output.get("error") or step_output.get("message") or "Pin step reported failure"))
            return

        requested = _instance_count(step_output.get("requested_instance_count"))
        if requested is None or requested < 1:
            self.set_failed(
                "Step output must report requested_instance_count as an integer of at least 1, got "
                f"{step_output.get('requested_instance_count')!r}"
            )
            return

        delivered = _instance_count(step_output.get("instance_count"))
        if delivered is None:
            self.set_failed(
                f"Step output must report instance_count as an integer, got {step_output.get('instance_count')!r}"
            )
            return

        if delivered != requested:
            self.set_failed(
                f"Control plane was not pinned: the tenant requested {requested} instance(s) and the provider "
                f"reports {delivered}"
            )
            return

        registered = self._registered_apiservers()
        if registered is None:
            return

        if registered > requested:
            self.set_failed(
                f"Control plane is not held at {requested} instance(s): {APISERVER_NAMESPACE}/{APISERVER_SERVICE} "
                f"registers {registered} API server endpoint(s), more than the pin allows"
            )
            return

        if registered < requested:
            corroboration = (
                f"{registered} API server endpoint(s) are registered, which a load-balanced API address cannot "
                f"distinguish from {requested}"
            )
        else:
            corroboration = f"{registered} API server endpoint(s) are registered, matching the pin"
        self.set_passed(f"Control plane is pinned at {requested} instance(s); {corroboration}")

    def _registered_apiservers(self) -> int | None:
        """Return the API server endpoint count, or ``None`` after marking the check failed.

        The count is the independent half of the proof, so a probe that cannot
        be answered leaves only the provider's own report and fails rather than
        passing on it.
        """
        kubectl_base = get_kubectl_base_shell()
        result = self.run_command(f"{kubectl_base} get endpoints {APISERVER_SERVICE} -n {APISERVER_NAMESPACE} -o json")
        if result.exit_code != 0:
            self.set_failed(
                f"Could not count registered API servers from {APISERVER_NAMESPACE}/{APISERVER_SERVICE}: "
                f"{result.stderr.strip() or result.stdout.strip() or f'exit {result.exit_code}'}"
            )
            return None

        try:
            payload = parse_kubectl_json(result, f"{APISERVER_NAMESPACE}/{APISERVER_SERVICE} endpoints")
        except KubectlParseError as exc:
            self.set_failed(str(exc))
            return None

        addresses = _endpoint_addresses(payload)
        if not addresses:
            self.set_failed(
                f"{APISERVER_NAMESPACE}/{APISERVER_SERVICE} registers no API server endpoints, so the pinned "
                "control-plane size cannot be corroborated"
            )
            return None
        return len(addresses)


def _endpoint_addresses(payload: dict[str, Any]) -> set[str]:
    """Return the distinct ready endpoint addresses across an Endpoints object's subsets."""
    addresses: set[str] = set()
    subsets = payload.get("subsets")
    if not isinstance(subsets, list):
        return addresses
    for subset in subsets:
        if not isinstance(subset, dict):
            continue
        for address in subset.get("addresses") or []:
            if isinstance(address, dict) and isinstance(address.get("ip"), str) and address["ip"].strip():
                addresses.add(address["ip"].strip())
    return addresses


def _instance_count(value: Any) -> int | None:
    """Return ``value`` as a non-negative int, rejecting bools and non-integers.

    Decimal strings are accepted so a provider script emitting ``"3"`` is not
    treated as a broken contract.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip())
    return None
