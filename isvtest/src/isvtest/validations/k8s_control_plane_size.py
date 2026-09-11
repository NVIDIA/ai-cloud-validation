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

from isvtest.core.k8s import (
    KubectlParseError,
    get_kubectl_base_shell,
    parse_kubectl_json,
    parse_kubectl_json_items,
)
from isvtest.core.runners import CommandResult
from isvtest.core.validation import BaseValidation

# The ``kubernetes`` Service in ``default`` is maintained by the API servers
# themselves - each instance that reaches the endpoint reconciler publishes one
# address. It is the only provider-neutral view of API server instance count a
# tenant gets on a managed control plane.
APISERVER_SERVICE = "kubernetes"
APISERVER_NAMESPACE = "default"

# EndpointSlices carry the name of the Service they back as a label.
SERVICE_NAME_LABEL = "kubernetes.io/service-name"

NO_ENDPOINTS_REASON = "registers no ready API server endpoints"


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
    endpoint per registered API server. That measurement is independent of the
    provider.

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

        corroboration = (
            "matching the pin"
            if registered == requested
            else f"which a load-balanced API address cannot distinguish from {requested}"
        )
        self.set_passed(
            f"Control plane is pinned at {requested} instance(s); "
            f"{registered} API server endpoint(s) are registered, {corroboration}"
        )

    def _registered_apiservers(self) -> int | None:
        """Return the API server endpoint count, or ``None`` after marking the check failed.

        The count is the independent half of the proof, so a probe that cannot
        be answered leaves only the provider's own report and fails rather than
        passing on it. EndpointSlice is asked first because the v1 Endpoints
        API is deprecated from Kubernetes 1.33; Endpoints remains the fallback
        for clusters, or RBAC grants, that only answer there.
        """
        kubectl_base = get_kubectl_base_shell()
        count, slice_error = self._count_via_endpoint_slices(kubectl_base)
        if count is not None:
            return count

        count, endpoints_error = self._count_via_endpoints(kubectl_base)
        if count is not None:
            return count

        self.set_failed(
            f"Could not count registered API servers for {APISERVER_NAMESPACE}/{APISERVER_SERVICE} - "
            f"EndpointSlice: {slice_error}; Endpoints: {endpoints_error}"
        )
        return None

    def _count_via_endpoint_slices(self, kubectl_base: str) -> tuple[int | None, str]:
        """Count ready API servers from the EndpointSlices backing the Service.

        Returns the count, or ``None`` alongside the reason it is unavailable.
        """
        result = self.run_command(
            f"{kubectl_base} get endpointslices -n {APISERVER_NAMESPACE} "
            f"-l {SERVICE_NAME_LABEL}={APISERVER_SERVICE} -o json"
        )
        if result.exit_code != 0:
            return None, _command_error(result)

        try:
            items = parse_kubectl_json_items(result, f"{APISERVER_NAMESPACE}/{APISERVER_SERVICE} endpointslices")
        except KubectlParseError as exc:
            return None, str(exc)

        count = _ready_endpoint_count(items)
        if count == 0:
            return None, NO_ENDPOINTS_REASON
        return count, ""

    def _count_via_endpoints(self, kubectl_base: str) -> tuple[int | None, str]:
        """Count ready API servers from the deprecated v1 Endpoints object.

        Returns the count, or ``None`` alongside the reason it is unavailable.
        """
        result = self.run_command(f"{kubectl_base} get endpoints {APISERVER_SERVICE} -n {APISERVER_NAMESPACE} -o json")
        if result.exit_code != 0:
            return None, _command_error(result)

        try:
            payload = parse_kubectl_json(result, f"{APISERVER_NAMESPACE}/{APISERVER_SERVICE} endpoints")
        except KubectlParseError as exc:
            return None, str(exc)

        addresses = _endpoint_addresses(payload)
        if not addresses:
            return None, NO_ENDPOINTS_REASON
        return len(addresses), ""


def _command_error(result: CommandResult) -> str:
    """Return the most informative line a failed kubectl invocation produced."""
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_code}"


def _ready_endpoint_count(items: list[dict[str, Any]]) -> int:
    """Return the number of distinct ready API server endpoints across EndpointSlices.

    One family's endpoints can be sharded over several slices, so addresses are
    pooled per family before being counted.
    """
    per_family: dict[str, set[str]] = {}
    for item in items:
        family = item.get("addressType")
        if not isinstance(family, str) or not family.strip():
            continue
        addresses = per_family.setdefault(family.strip(), set())
        for endpoint in item.get("endpoints") or []:
            if not isinstance(endpoint, dict):
                continue
            address = _ready_address(endpoint)
            if address:
                addresses.add(address)
    # A dual-stack Service is backed by one slice family per address type, each
    # enumerating every API server once, so the families are alternative views
    # of the same instances rather than additions to it.
    return max((len(addresses) for addresses in per_family.values()), default=0)


def _ready_address(endpoint: dict[str, Any]) -> str:
    """Return an EndpointSlice endpoint's identifying address, or ``""`` if it is not serving.

    ``conditions.ready`` only withholds an endpoint when explicitly ``false`` -
    the API defines an absent value as ready. Only the first address is
    meaningful to consumers, so it stands for the instance.
    """
    conditions = endpoint.get("conditions")
    if isinstance(conditions, dict) and conditions.get("ready") is False:
        return ""
    for address in endpoint.get("addresses") or []:
        if isinstance(address, str) and address.strip():
            return address.strip()
    return ""


def _endpoint_addresses(payload: dict[str, Any]) -> set[str]:
    """Return the distinct ready endpoint addresses across an Endpoints object's subsets."""
    addresses: set[str] = set()
    for subset in payload.get("subsets") or []:
        if not isinstance(subset, dict):
            continue
        for address in subset.get("addresses") or []:
            if not isinstance(address, dict):
                continue
            ip = address.get("ip")
            if isinstance(ip, str) and ip.strip():
                addresses.add(ip.strip())
    return addresses


def _instance_count(value: Any) -> int | None:
    """Return ``value`` as a non-negative int, rejecting bools and non-integers.

    The ``control_plane_size`` output schema already declares both counts as
    integers, so this is a second line of defence rather than the contract.
    Decimal strings are accepted so a provider script emitting ``"3"`` is not
    treated as a broken contract.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip())
    return None
