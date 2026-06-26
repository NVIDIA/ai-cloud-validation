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

"""Shared Compute Engine helpers for GCP network stubs.

This module is the canonical home for the network-domain divergences from
the AWS provider. It complements ``common.compute`` (which owns VM-domain
helpers: project/zone resolution, op waiters, SSH key pairs, canonical
state) and reuses those primitives rather than re-implementing them.

Compute Engine network facts that shape these helpers:

  * Networks have NO CIDR. CIDR ranges live on subnetworks only. We create
    custom-mode networks (``auto_create_subnetworks=false``); the contract's
    ``cidr`` field echoes the create-time aggregate.
  * Subnetworks are REGIONAL (one subnetwork covers every zone in its
    region). There is no zone/AZ on a subnetwork; the suite's ``az`` field
    is populated from real zones in the configured region.
  * There is NO DHCP-options resource. Linux guests get internal DNS from
    the metadata server at ``169.254.169.254``.
  * Firewall rules are project-scoped, network-bound, unidirectional
    (INGRESS / EGRESS), and applied by ``source_ranges`` + ``target_tags``
    / ``target_service_accounts`` — never attached to a NIC. An empty
    ``allowed[]`` is rejected (HTTP 400): every allow rule sets at least
    one ``Allowed`` with ``I_p_protocol``.
  * ``Network`` / ``Subnetwork`` / ``Firewall`` protos have NO ``labels``
    field (only ``Address`` does). Provenance for these types uses the
    immutable ``description`` marker only.
  * VPC peering is bilateral + symmetric: BOTH sides call ``add_peering``
    (no accept handshake). ``list_peering_routes`` REQUIRES ``region`` AND
    ``direction`` keywords or it raises ``InvalidArgument`` 400.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from google.api_core import exceptions as gax
from google.cloud import compute_v1

from common.compute import short_name, zone_to_region
from common.errors import delete_with_retry

# --------------------------------------------------------------------- #
# Provenance markers                                                    #
# --------------------------------------------------------------------- #

# Compute Engine Network / Subnetwork / Firewall protos have NO labels
# field, so the AWS provider's CreatedBy=isvtest tag has no direct analog.
# The closest portable marker is the (immutable) ``description`` field;
# every resource this domain creates stamps it so an operator sweep can
# attribute orphans and verified-reuse can refuse to adopt resources it
# did not create.
ISV_OWNERSHIP_MARKER = "createdby=isvtest"
ISV_RESOURCE_DESCRIPTION = f"ISV network validation resource ({ISV_OWNERSHIP_MARKER})"

# Network tag applied to probe instances so tag-scoped firewall rules can
# target them. Mirrors common.compute.ISV_NETWORK_TAG semantics for the
# network domain's tag-scoping tests.
ISV_NETWORK_TAG = "isv-net-probe"

# Public GCP image used for ephemeral probe VMs. Ubuntu LTS is the
# canonical SSH-able guest (matches the suite's ssh_user="ubuntu"); it
# carries no GPU/driver baggage the network probes do not need.
DEFAULT_PROBE_IMAGE = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
DEFAULT_PROBE_MACHINE_TYPE = "e2-small"
DEFAULT_SSH_USER = "ubuntu"

# Metadata-server resolver — the only internal-DNS source Compute Engine
# exposes to Linux guests. There is no per-VPC DHCP-options API.
METADATA_DNS_SERVER = "169.254.169.254"


# --------------------------------------------------------------------- #
# Operator firewall-ingress policy (SSH / RDP trusted source)           #
# --------------------------------------------------------------------- #

# The ONLY trusted source of SSH (tcp/22) / RDP (tcp/3389) ingress ranges is
# this operator environment variable. Generated firewalls MUST NOT open those
# admin ports from 0.0.0.0/0: SSH/RDP ingress must come from the operator-
# trusted source range, never the whole internet (see docs/references/gcp.md).
# Live mode is additionally rejected when this var is unset, so a live run
# always supplies it; the runtime resolver below is the stub-side fail-closed
# defense.
FIREWALL_TRUST_IP_ENV = "NETWORK_FIREWALL_TRUST_IP"
_FORBIDDEN_FIREWALL_RANGES = {"0.0.0.0/0"}


class OperatorConfigError(RuntimeError):
    """A required operator env var is unset or invalid — fail closed.

    Raised by :func:`resolve_trusted_firewall_sources`. Callers let it
    propagate so the step records the operator error, sets ``success=false``,
    and exits non-zero. There is no fallback source range for SSH/RDP ingress.
    """


def resolve_trusted_firewall_sources(env_var: str = FIREWALL_TRUST_IP_ENV) -> list[str]:
    """Return the operator-trusted SSH/RDP ingress CIDRs from the environment.

    Reads ``NETWORK_FIREWALL_TRUST_IP`` and normalizes it to the list assigned
    to a firewall's ``source_ranges``. Bare IPv4 addresses normalize to ``/32``;
    comma-separated IPv4 CIDRs are allowed. There is NO fallback: when the var
    is unset, empty, non-IPv4, or normalizes to the all-internet range
    ``0.0.0.0/0``, raise :class:`OperatorConfigError` so the stub fails closed
    rather than opening tcp/22 or tcp/3389 to the whole internet.

    Mirrors the live-mode firewall ingress gate so the stub and that gate agree
    on what is acceptable.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        raise OperatorConfigError(
            f"operator error: {env_var} is required to open SSH/RDP firewall "
            f"ingress. Set it to a trusted IPv4 address or CIDR (e.g. "
            f"203.0.113.10 or 203.0.113.0/24); 0.0.0.0/0 is forbidden."
        )
    sources: list[str] = []
    for token in raw.split(","):
        value = token.strip()
        if not value:
            raise OperatorConfigError(f"operator error: {env_var} contains an empty CIDR entry.")
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise OperatorConfigError(f"operator error: invalid {env_var} entry {value!r}: {exc}") from None
        if network.version != 4:
            raise OperatorConfigError(
                f"operator error: {env_var} entry {value!r} must be IPv4 (SSH/RDP trust ranges are IPv4)."
            )
        rendered = str(network)
        if rendered in _FORBIDDEN_FIREWALL_RANGES:
            raise OperatorConfigError(
                f"operator error: {env_var} value {rendered} is forbidden; it defeats the SSH/RDP ingress guardrail."
            )
        sources.append(rendered)
    return sources


# --------------------------------------------------------------------- #
# Resource URL builders                                                 #
# --------------------------------------------------------------------- #


def network_url(project: str, name: str) -> str:
    """Return the project-relative URL of a global network resource."""
    return f"projects/{project}/global/networks/{name}"


def subnetwork_url(project: str, region: str, name: str) -> str:
    """Return the project-relative URL of a regional subnetwork resource."""
    return f"projects/{project}/regions/{region}/subnetworks/{name}"


def _op_name(op: Any) -> str:
    """Extract the operation name from an insert/delete/patch return."""
    return getattr(op, "name", None) or getattr(op, "operation", "") or ""


class PartialCreateError(RuntimeError):
    """Typed, caller-visible partial-create handoff for a leaked async resource.

    Raised by ``_wait_or_rollback`` when an async create+wait failed AND the
    retry-aware rollback could not confirm deletion. The resource was accepted
    by the API (``created=True``) but may still exist cloud-side. Callers that
    gate cleanup/teardown on a local ``*_created`` tracker can catch this to
    record the resource for cleanup instead of silently skipping it; a stderr
    warning is not a teardown handoff. ``resource_desc`` carries the resource
    identity, and the resource always retains the ``ISV_OWNERSHIP_MARKER`` so an
    operator ownership sweep can reclaim it regardless.
    """

    def __init__(self, resource_desc: str) -> None:
        super().__init__(
            f"partial create of {resource_desc}: async create+wait failed and "
            f"rollback could not confirm deletion; the resource may be leaked but "
            f"carries the {ISV_OWNERSHIP_MARKER!r} provenance marker for cleanup"
        )
        self.resource_desc = resource_desc
        self.created = True


def _wait_or_rollback(
    wait_fn: Callable[[], Any],
    rollback_fn: Callable[[], bool],
    *,
    resource_desc: str = "resource",
) -> None:
    """Run ``wait_fn``; on failure roll back the just-created resource, then re-raise.

    Compute Engine ``insert`` returns a synchronous ack while the resource
    is created asynchronously. If the op-wait raises (timeout, or DONE with
    errors), the resource may already exist cloud-side even though the
    caller has NOT yet stamped its ``*_created`` tracker (that stamp happens
    only after the insert helper returns). The caller's cleanup-on-failure
    path and the teardown step are both gated on that unstamped flag, so a
    half-created resource would leak with no automatic cleanup hook.

    To guarantee cleanup provenance survives an async wait failure, the
    insert helpers own their own partial-cleanup and that cleanup is made
    *reliable and truthfully reported*: ``rollback_fn`` MUST be a
    ``delete_with_retry`` partial (retry-aware over transient + dependency-
    in-use errors, never raising, ``NotFound`` counted as success). It
    returns True iff the resource reached the deleted state. When it does,
    the resource is genuinely gone, so the caller's tracker correctly
    staying False is truthful (nothing leaked) and the original wait error is
    re-raised unchanged so the step fails loudly.

    If the retry-aware rollback still cannot confirm deletion (terminal
    failure), the just-created resource may be leaked. We do NOT swallow that
    silently and a warning alone is not a teardown handoff: we raise a typed
    ``PartialCreateError`` (chained from the original wait error) carrying the
    resource identity and ``created=True``, so a caller can record the resource
    for cleanup rather than skip it on an unstamped tracker. The resource also
    retains the ``ISV_OWNERSHIP_MARKER`` for an operator ownership sweep.
    """
    try:
        wait_fn()
    except Exception as wait_exc:
        try:
            rolled_back = rollback_fn()
        except Exception as exc:  # defensive: delete_with_retry must not raise
            print(f"  warn: rollback of {resource_desc} raised: {exc}", file=sys.stderr)
            rolled_back = False
        if rolled_back:
            # Rollback confirmed deletion: nothing leaked, the caller's
            # unstamped tracker is truthful. Re-raise the original error.
            raise
        # Terminal rollback failure: the accepted resource may still exist.
        # Hand a typed partial-create signal up so cleanup/teardown is not
        # silently skipped on an unstamped tracker.
        print(
            f"  warn: rollback of {resource_desc} did not confirm deletion after an "
            f"async create+wait failure; raising PartialCreateError so the caller can "
            f"record it for cleanup (resource carries the {ISV_OWNERSHIP_MARKER!r} marker).",
            file=sys.stderr,
        )
        raise PartialCreateError(resource_desc) from wait_exc


# --------------------------------------------------------------------- #
# Operation waiters (regional ops; zonal/global live in common.compute) #
# --------------------------------------------------------------------- #


def wait_for_regional_op(
    project: str,
    region: str,
    operation_name: str,
    *,
    timeout: int = 300,
) -> compute_v1.Operation:
    """Block until a regional Compute Operation reaches DONE.

    Subnetwork inserts/deletes return regional operations. Raises
    ``RuntimeError`` if the op's error list is non-empty (the joined
    message carries ``code:message`` so callers can classify). This is
    the regional sibling of ``common.compute.wait_for_zonal_op`` /
    ``wait_for_global_op``.
    """
    client = compute_v1.RegionOperationsClient()
    deadline = time.monotonic() + timeout
    while True:
        op = client.get(project=project, region=region, operation=operation_name)
        if op.status == compute_v1.Operation.Status.DONE:
            if op.error and op.error.errors:
                msg = "; ".join(f"{getattr(e, 'code', '')}:{getattr(e, 'message', str(e))}" for e in op.error.errors)
                raise RuntimeError(f"Regional op {operation_name} failed: {msg}")
            return op
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Regional operation {operation_name} did not complete in {timeout}s")
        time.sleep(3)


def wait_for_global_op(project: str, operation_name: str, *, timeout: int = 300) -> compute_v1.Operation:
    """Re-export ``common.compute.wait_for_global_op`` shape for network callers."""
    from common.compute import wait_for_global_op as _w

    return _w(project, operation_name, timeout=timeout)


def wait_for_zonal_op(project: str, zone: str, operation_name: str, *, timeout: int = 300) -> compute_v1.Operation:
    """Re-export ``common.compute.wait_for_zonal_op`` shape for network callers."""
    from common.compute import wait_for_zonal_op as _w

    return _w(project, zone, operation_name, timeout=timeout)


# --------------------------------------------------------------------- #
# CIDR helpers                                                          #
# --------------------------------------------------------------------- #


def carve_subnet_cidrs(aggregate_cidr: str, count: int, *, new_prefix: int = 24) -> list[str]:
    """Carve ``count`` non-overlapping subnet CIDRs from an aggregate range.

    Compute Engine networks own no CIDR; the suite's ``--cidr`` arg is an
    aggregate the test carves subnet ranges from. Returns the first
    ``count`` ``/new_prefix`` subranges. Raises ``ValueError`` if the
    aggregate cannot supply that many subranges (surface the
    misconfiguration rather than silently truncating).
    """
    network = ipaddress.ip_network(aggregate_cidr, strict=False)
    if new_prefix < network.prefixlen:
        raise ValueError(f"new_prefix /{new_prefix} is wider than aggregate {aggregate_cidr}")
    subnets = list(network.subnets(new_prefix=new_prefix))
    if len(subnets) < count:
        raise ValueError(f"aggregate {aggregate_cidr} yields {len(subnets)} /{new_prefix} subnets, need {count}")
    return [str(s) for s in subnets[:count]]


def usable_ip_count(cidr: str) -> int:
    """Return the count of usable host IPs in a subnet primary range.

    Compute Engine reserves four addresses per subnet primary range
    (network, default gateway, second-to-last, and broadcast). Mirrors
    the AWS provider's ``AvailableIpAddressCount`` intent — the suite's
    ``VpcIpConfigCheck`` requires ``available_ips >= 16``.
    """
    network = ipaddress.ip_network(cidr, strict=False)
    return max(network.num_addresses - 4, 0)


# --------------------------------------------------------------------- #
# Zone enumeration                                                      #
# --------------------------------------------------------------------- #


def region_zones(project: str, region: str) -> list[str]:
    """Return the live zone short-names the GCP API reports for ``region``.

    Subnetworks are regional, but the suite contract's ``az`` field expects
    a zone-shaped value. We populate it from REAL zones in the configured
    region (never fabricated names) so ``SubnetConfigCheck``'s multi-AZ
    requirement is satisfied honestly. Raises ``RuntimeError`` on a bad /
    unauthorized region rather than silently returning an empty list.
    """
    try:
        region_obj = compute_v1.RegionsClient().get(project=project, region=region)
    except (gax.NotFound, gax.PermissionDenied, gax.InvalidArgument, gax.Unauthenticated) as e:
        raise RuntimeError(f"region {region!r} is invalid or unauthorized: {e}") from e
    return [url.rsplit("/", 1)[-1] for url in region_obj.zones or ()]


def cycle_zones(zones: list[str], count: int) -> list[str]:
    """Return ``count`` zone names cycling through ``zones`` (multi-AZ spread).

    A region with two live zones cycled over four subnets yields
    ``[z0, z1, z0, z1]`` — two distinct values, satisfying
    ``require_multi_az`` without inventing zone names.
    """
    if not zones:
        return []
    return [zones[i % len(zones)] for i in range(count)]


# --------------------------------------------------------------------- #
# Network (VPC) lifecycle                                               #
# --------------------------------------------------------------------- #


def insert_network(
    project: str,
    name: str,
    *,
    routing_mode: str = "REGIONAL",
    description: str = ISV_RESOURCE_DESCRIPTION,
    timeout: int = 120,
) -> str:
    """Create a custom-mode network and wait for the insert op to finish.

    ``auto_create_subnetworks=false`` gives a custom-mode network (the
    suite carves explicit subnetworks). A custom-mode network ships with
    a default route (0.0.0.0/0 via the implicit default-internet-gateway)
    so no route table / IGW resource is created — those have no Compute
    Engine analog. Returns the network short name.
    """
    net = compute_v1.Network()
    net.name = name
    net.auto_create_subnetworks = False
    net.description = description
    routing = compute_v1.NetworkRoutingConfig()
    routing.routing_mode = routing_mode
    net.routing_config = routing

    op = compute_v1.NetworksClient().insert(project=project, network_resource=net)
    op_name = _op_name(op)
    if op_name:
        _wait_or_rollback(
            lambda: wait_for_global_op(project, op_name, timeout=timeout),
            lambda: delete_with_retry(delete_network, project, name, timeout=timeout, resource_desc=f"network {name}"),
            resource_desc=f"network {name}",
        )
    return name


def get_network(project: str, name: str) -> compute_v1.Network:
    """Return the live ``Network`` resource (raises ``NotFound`` if absent)."""
    return compute_v1.NetworksClient().get(project=project, network=name)


def delete_network(project: str, name: str, *, timeout: int = 120) -> None:
    """Delete a network and wait for the op (``NotFound`` is idempotent)."""
    try:
        op = compute_v1.NetworksClient().delete(project=project, network=name)
    except gax.NotFound:
        return
    op_name = _op_name(op)
    if op_name:
        wait_for_global_op(project, op_name, timeout=timeout)


def network_has_isv_ownership(net: compute_v1.Network) -> bool:
    """Return True iff a network carries the ISV ownership marker in its description."""
    return ISV_OWNERSHIP_MARKER in (net.description or "").lower()


# --------------------------------------------------------------------- #
# Subnetwork lifecycle                                                  #
# --------------------------------------------------------------------- #


def insert_subnetwork(
    project: str,
    region: str,
    name: str,
    network_name: str,
    cidr: str,
    *,
    description: str = ISV_RESOURCE_DESCRIPTION,
    enable_flow_logs: bool = False,
    timeout: int = 180,
) -> None:
    """Create a regional subnetwork and wait for the op to finish.

    The subnetwork is the resource that owns ``cidr`` on Compute Engine.
    ``enable_flow_logs`` turns on VPC Flow Logs (a telemetry source for
    the SDN latency/perf step). The proto has no ``labels`` field; only
    the ``description`` marker is used for provenance.
    """
    subnet = compute_v1.Subnetwork()
    subnet.name = name
    subnet.ip_cidr_range = cidr
    subnet.network = network_url(project, network_name)
    subnet.region = region
    subnet.description = description
    if enable_flow_logs:
        log_cfg = compute_v1.SubnetworkLogConfig()
        log_cfg.enable = True
        subnet.log_config = log_cfg

    op = compute_v1.SubnetworksClient().insert(project=project, region=region, subnetwork_resource=subnet)
    op_name = _op_name(op)
    if op_name:
        _wait_or_rollback(
            lambda: wait_for_regional_op(project, region, op_name, timeout=timeout),
            lambda: delete_with_retry(
                delete_subnetwork,
                project,
                region,
                name,
                timeout=timeout,
                resource_desc=f"subnetwork {name}",
            ),
            resource_desc=f"subnetwork {name}",
        )


def get_subnetwork(project: str, region: str, name: str) -> compute_v1.Subnetwork:
    """Return the live ``Subnetwork`` resource (raises ``NotFound`` if absent)."""
    return compute_v1.SubnetworksClient().get(project=project, region=region, subnetwork=name)


def list_subnetworks_for_network(project: str, region: str, network_name: str) -> list[compute_v1.Subnetwork]:
    """Return subnetworks in ``region`` bound to ``network_name`` (exact tail match)."""
    out: list[compute_v1.Subnetwork] = []
    for sub in compute_v1.SubnetworksClient().list(project=project, region=region):
        if short_name(sub.network) == network_name:
            out.append(sub)
    return out


def delete_subnetwork(project: str, region: str, name: str, *, timeout: int = 180) -> None:
    """Delete a subnetwork and wait for the op (``NotFound`` is idempotent)."""
    try:
        op = compute_v1.SubnetworksClient().delete(project=project, region=region, subnetwork=name)
    except gax.NotFound:
        return
    op_name = _op_name(op)
    if op_name:
        wait_for_regional_op(project, region, op_name, timeout=timeout)


def subnet_readiness_state(op_done: bool) -> str:
    """Return the canonical subnet readiness string.

    Documented Compute Engine quirk: ``Subnetwork.state`` is EMPTY for a
    freshly-created custom-mode subnet even after the regional op reports
    DONE. The op-DONE signal IS the canonical readiness gate — callers
    pass ``op_done=True`` after ``wait_for_regional_op`` returns and we
    emit ``"READY"``. Never propagate the empty proto field as
    ``"UNKNOWN"`` (a documented false-negative).
    """
    return "READY" if op_done else "UNKNOWN"


# --------------------------------------------------------------------- #
# DHCP options (synthetic — no Compute Engine resource)                 #
# --------------------------------------------------------------------- #


def dhcp_options_payload(network_name: str) -> dict[str, Any]:
    """Return the suite's ``dhcp_options`` object for a Compute Engine network.

    Compute Engine exposes no DHCP-options resource. Linux guests resolve
    internal DNS through the metadata server at ``169.254.169.254``, which
    we emit in ``domain_name_servers`` so the validator's non-empty-DNS
    check passes from a REAL platform signal.

    ``domain_name`` is OMITTED (not emitted as ``null``): the
    ``vpc_ip_config`` output schema types ``dhcp_options.domain_name`` as
    ``{"type": "string"}``, so a ``None`` value fails schema validation.
    The field is optional (not in ``required``) and validators read it via
    ``.get(...)``, so omitting it is the honest, schema-valid encoding of
    "Compute Engine has no DHCP-options domain resource."
    """
    return {
        "dhcp_options_id": network_name,
        "domain_name_servers": [METADATA_DNS_SERVER],
        "ntp_servers": [],
    }


# --------------------------------------------------------------------- #
# Firewall lifecycle                                                    #
# --------------------------------------------------------------------- #


def make_allowed(protocol: str, ports: list[str] | None = None) -> compute_v1.Allowed:
    """Build an ``Allowed`` entry. ``I_p_protocol`` is ALWAYS set (empty -> 400)."""
    allowed = compute_v1.Allowed()
    allowed.I_p_protocol = protocol
    if ports:
        allowed.ports = ports
    return allowed


def make_denied(protocol: str, ports: list[str] | None = None) -> compute_v1.Denied:
    """Build a ``Denied`` entry for deny-action firewall rules."""
    denied = compute_v1.Denied()
    denied.I_p_protocol = protocol
    if ports:
        denied.ports = ports
    return denied


def build_firewall(
    name: str,
    network_name: str,
    project: str,
    *,
    direction: str = "INGRESS",
    priority: int = 1000,
    allowed: list[compute_v1.Allowed] | None = None,
    denied: list[compute_v1.Denied] | None = None,
    source_ranges: list[str] | None = None,
    destination_ranges: list[str] | None = None,
    target_tags: list[str] | None = None,
    target_service_accounts: list[str] | None = None,
    description: str = ISV_RESOURCE_DESCRIPTION,
    enable_logging: bool = False,
) -> compute_v1.Firewall:
    """Build a ``Firewall`` proto bound to ``network_name``.

    Compute Engine firewalls are unidirectional and project-scoped. An
    allow rule MUST carry at least one ``Allowed`` with ``I_p_protocol``
    set (empty ``allowed[]`` -> HTTP 400) — callers pass ``allowed=`` for
    INGRESS/EGRESS allows or ``denied=`` for explicit-deny rules. Lower
    numeric ``priority`` wins.
    """
    fw = compute_v1.Firewall()
    fw.name = name
    fw.network = network_url(project, network_name)
    fw.direction = direction
    fw.priority = priority
    fw.description = description
    if allowed:
        fw.allowed = allowed
    if denied:
        fw.denied = denied
    if source_ranges is not None:
        fw.source_ranges = source_ranges
    if destination_ranges is not None:
        fw.destination_ranges = destination_ranges
    if target_tags is not None:
        fw.target_tags = target_tags
    if target_service_accounts is not None:
        fw.target_service_accounts = target_service_accounts
    if enable_logging:
        log_cfg = compute_v1.FirewallLogConfig()
        log_cfg.enable = True
        fw.log_config = log_cfg
    return fw


def insert_firewall(project: str, fw: compute_v1.Firewall, *, timeout: int = 120) -> None:
    """Insert a firewall and wait for the global op to finish."""
    op = compute_v1.FirewallsClient().insert(project=project, firewall_resource=fw)
    op_name = _op_name(op)
    if op_name:
        _wait_or_rollback(
            lambda: wait_for_global_op(project, op_name, timeout=timeout),
            lambda: delete_with_retry(
                delete_firewall,
                project,
                fw.name,
                timeout=timeout,
                resource_desc=f"firewall {fw.name}",
            ),
            resource_desc=f"firewall {fw.name}",
        )


def patch_firewall(project: str, name: str, fw: compute_v1.Firewall, *, timeout: int = 120) -> None:
    """Patch an existing firewall (e.g. swap ``allowed`` ports) and wait."""
    op = compute_v1.FirewallsClient().patch(project=project, firewall=name, firewall_resource=fw)
    op_name = _op_name(op)
    if op_name:
        wait_for_global_op(project, op_name, timeout=timeout)


def get_firewall(project: str, name: str) -> compute_v1.Firewall:
    """Return the live ``Firewall`` resource (raises ``NotFound`` if absent)."""
    return compute_v1.FirewallsClient().get(project=project, firewall=name)


def delete_firewall(project: str, name: str, *, timeout: int = 120) -> None:
    """Delete a firewall and wait for the op (``NotFound`` is idempotent)."""
    try:
        op = compute_v1.FirewallsClient().delete(project=project, firewall=name)
    except gax.NotFound:
        return
    op_name = _op_name(op)
    if op_name:
        wait_for_global_op(project, op_name, timeout=timeout)


def list_firewalls_for_network(project: str, network_name: str) -> list[compute_v1.Firewall]:
    """Return all firewall rules bound to ``network_name`` (exact tail match).

    The empty list is the strongest possible default-deny on Compute
    Engine — used by isolation / security-blocking checks.
    """
    out: list[compute_v1.Firewall] = []
    for rule in compute_v1.FirewallsClient().list(project=project):
        if short_name(rule.network) == network_name:
            out.append(rule)
    return out


# --------------------------------------------------------------------- #
# Routes                                                                #
# --------------------------------------------------------------------- #


def list_routes_for_network(project: str, network_name: str) -> list[compute_v1.Route]:
    """Return project routes bound to ``network_name`` (exact tail match)."""
    out: list[compute_v1.Route] = []
    for route in compute_v1.RoutesClient().list(project=project):
        if short_name(route.network) == network_name:
            out.append(route)
    return out


def is_auto_route(route: compute_v1.Route) -> bool:
    """Return True iff ``route`` is a GCE auto-created system route.

    Auto-routes (``default-route-<hex>`` with ``next_hop_network`` set)
    CANNOT be deleted via ``routes.delete`` (API returns 400 "The local
    route cannot be deleted") — they are reaped automatically when their
    parent subnet is deleted. Teardown route-enumeration MUST skip these.
    """
    return bool(route.name.startswith("default-route-") or route.next_hop_network)


def delete_route(project: str, name: str, *, timeout: int = 120) -> None:
    """Delete a (non-auto) route and wait for the op (``NotFound`` is idempotent)."""
    try:
        op = compute_v1.RoutesClient().delete(project=project, route=name)
    except gax.NotFound:
        return
    op_name = _op_name(op)
    if op_name:
        wait_for_global_op(project, op_name, timeout=timeout)


# --------------------------------------------------------------------- #
# VPC peering (bilateral / symmetric — no accept handshake)             #
# --------------------------------------------------------------------- #


def add_peering(
    project: str,
    network_name: str,
    peer_network_name: str,
    peering_name: str,
    *,
    exchange_subnet_routes: bool = True,
    timeout: int = 120,
) -> None:
    """Add a peering from ``network_name`` to ``peer_network_name`` and wait.

    Request-shape rule (raises ``InvalidArgument`` 400 if violated): set
    the peering name ONLY on ``network_peering.name`` — NEVER also on the
    top-level ``NetworksAddPeeringRequest.name``. Peering is symmetric:
    the caller must run this on BOTH networks for the peering to reach
    ``ACTIVE``.
    """
    peering = compute_v1.NetworkPeering()
    peering.name = peering_name
    peering.network = network_url(project, peer_network_name)
    peering.exchange_subnet_routes = exchange_subnet_routes

    request_body = compute_v1.NetworksAddPeeringRequest()
    request_body.network_peering = peering

    op = compute_v1.NetworksClient().add_peering(
        project=project,
        network=network_name,
        networks_add_peering_request_resource=request_body,
    )
    op_name = _op_name(op)
    if op_name:
        wait_for_global_op(project, op_name, timeout=timeout)


def _is_absent_peering(e: Exception) -> bool:
    """Return True iff ``e`` is the GCP 'this peering does not exist' BadRequest.

    Removing an already-gone peering returns HTTP 400 (``gax.BadRequest``)
    whose message names the missing peering, e.g. "There is no peering ..."
    / "peering ... does not exist". That single 400 is genuinely idempotent
    (the desired terminal state is reached). It MUST be distinguished from
    dependency-in-use / other 400s, which are retryable or terminal failures.
    """
    msg = str(e).lower()
    return "peering" in msg and ("does not exist" in msg or "no peering" in msg)


def remove_peering(project: str, network_name: str, peering_name: str, *, timeout: int = 120) -> None:
    """Remove a peering by name and wait.

    Idempotent ONLY on already-absent: ``gax.NotFound`` (network gone) and
    the specific absent-peering ``BadRequest`` (see ``_is_absent_peering``)
    are swallowed because the peering is already gone. Every other
    ``BadRequest`` / ``FailedPrecondition`` — notably dependency-in-use —
    is re-raised so the teardown's ``delete_with_retry`` can retry with
    backoff or record a cleanup failure, instead of this helper reporting a
    non-idempotent 400 as success and leaving the peering behind.
    """
    request_body = compute_v1.NetworksRemovePeeringRequest()
    request_body.name = peering_name
    try:
        op = compute_v1.NetworksClient().remove_peering(
            project=project,
            network=network_name,
            networks_remove_peering_request_resource=request_body,
        )
    except gax.NotFound:
        return
    except gax.BadRequest as e:
        if _is_absent_peering(e):
            return
        raise
    op_name = _op_name(op)
    if op_name:
        wait_for_global_op(project, op_name, timeout=timeout)


def network_peerings(project: str, name: str) -> list[Any]:
    """Return the ``peerings`` list off a live network (empty when none)."""
    net = get_network(project, name)
    return list(net.peerings or [])


def list_peering_routes(
    project: str,
    network_name: str,
    *,
    region: str,
    direction: str = "INCOMING",
    peering_name: str | None = None,
) -> list[Any]:
    """Return exchanged peering routes for ``network_name``.

    Request-shape rule: ``list_peering_routes`` REQUIRES both ``region``
    AND ``direction`` keywords; omitting ``direction`` raises
    ``InvalidArgument`` "Required field 'direction' not specified".
    ``INCOMING`` counts routes received from the peer. Auto-exchanged
    routes lag the ACTIVE peering state, so callers poll this.
    """
    request = compute_v1.ListPeeringRoutesNetworksRequest()
    request.project = project
    request.network = network_name
    request.region = region
    request.direction = direction
    if peering_name:
        request.peering_name = peering_name
    return list(compute_v1.NetworksClient().list_peering_routes(request=request))


# --------------------------------------------------------------------- #
# Addresses (regional static external IP — supports labels)             #
# --------------------------------------------------------------------- #


def insert_address(
    project: str,
    region: str,
    name: str,
    *,
    description: str = ISV_RESOURCE_DESCRIPTION,
    timeout: int = 120,
) -> compute_v1.Address:
    """Reserve a regional static external IP and return the live resource.

    ``compute_v1.Address`` is the ONLY network-domain proto that supports
    a ``labels`` field; we additionally stamp the description marker for
    provenance parity with the label-less types.
    """
    addr = compute_v1.Address()
    addr.name = name
    addr.description = description
    addr.address_type = "EXTERNAL"
    addr.labels = {"createdby": "isvtest"}

    op = compute_v1.AddressesClient().insert(project=project, region=region, address_resource=addr)
    op_name = _op_name(op)
    if op_name:
        _wait_or_rollback(
            lambda: wait_for_regional_op(project, region, op_name, timeout=timeout),
            lambda: delete_with_retry(
                delete_address,
                project,
                region,
                name,
                timeout=timeout,
                resource_desc=f"address {name}",
            ),
            resource_desc=f"address {name}",
        )
    return compute_v1.AddressesClient().get(project=project, region=region, address=name)


def delete_address(project: str, region: str, name: str, *, timeout: int = 120) -> None:
    """Release a reserved static IP and wait (``NotFound`` is idempotent)."""
    try:
        op = compute_v1.AddressesClient().delete(project=project, region=region, address=name)
    except gax.NotFound:
        return
    op_name = _op_name(op)
    if op_name:
        wait_for_regional_op(project, region, op_name, timeout=timeout)


# --------------------------------------------------------------------- #
# Probe instances (SSH / traffic tests)                                 #
# --------------------------------------------------------------------- #


def build_probe_instance(
    *,
    project: str,
    zone: str,
    name: str,
    network_name: str,
    subnet_name: str | None,
    machine_type: str = DEFAULT_PROBE_MACHINE_TYPE,
    source_image: str = DEFAULT_PROBE_IMAGE,
    ssh_user: str | None = None,
    ssh_pubkey: str | None = None,
    external_ip: bool = True,
    network_tags: list[str] | None = None,
    service_accounts: list[str] | None = None,
    description: str = ISV_RESOURCE_DESCRIPTION,
) -> compute_v1.Instance:
    """Build an ephemeral probe ``Instance`` for network validation.

    Service-account semantics (installed-SDK reality): the proto-plus
    ``compute_v1`` REST client serializes a repeated field the same way
    whether it is unset or an explicit empty list — both ``service_accounts``
    unset and ``service_accounts=[]`` emit ``"serviceAccounts": []`` on the
    wire (verified against the installed ``google-cloud-compute``). So an
    empty list is NOT a reliable way to express "this VM has no service
    account" and is indistinguishable from leaving the field unset. To make
    a VM carry a SPECIFIC, distinct service identity (e.g. the two-VM
    observation in sg_service_scoping), pass a NON-EMPTY list of SA emails;
    that list always round-trips. ``None`` / ``[]`` leave the SA selection
    to GCE's default behavior and must not be relied on to assert a no-SA
    state.

    ``external_ip`` controls whether an ``ONE_TO_ONE_NAT`` access config is
    attached (SSH-over-public-IP path). A subnetwork URL is required for a
    custom-mode network NIC; pass ``subnet_name``.
    """
    instance = compute_v1.Instance()
    instance.name = name
    instance.machine_type = f"zones/{zone}/machineTypes/{machine_type}"
    instance.description = description

    boot = compute_v1.AttachedDisk()
    boot.boot = True
    boot.auto_delete = True
    init = compute_v1.AttachedDiskInitializeParams()
    init.source_image = source_image
    init.disk_size_gb = 20
    boot.initialize_params = init
    instance.disks = [boot]

    nic = compute_v1.NetworkInterface()
    nic.network = network_url(project, network_name)
    if subnet_name:
        region = zone_to_region(zone)
        nic.subnetwork = subnetwork_url(project, region, subnet_name)
    if external_ip:
        nat = compute_v1.AccessConfig()
        nat.type_ = "ONE_TO_ONE_NAT"
        nat.name = "External NAT"
        nic.access_configs = [nat]
    instance.network_interfaces = [nic]

    if network_tags:
        instance.tags = compute_v1.Tags(items=list(network_tags))

    # service_accounts semantics (see docstring):
    #   None / [] -> serialize identically as "serviceAccounts": []; SA
    #                selection is left to GCE's default behavior. NOT a
    #                reliable "no service account" signal.
    #   [email..] -> attach the named SAs with cloud-platform scope; a
    #                non-empty list always round-trips, so this is the only
    #                way to pin a VM to a specific, distinct identity.
    if service_accounts:
        sa_objs: list[compute_v1.ServiceAccount] = []
        for email in service_accounts:
            sa = compute_v1.ServiceAccount()
            sa.email = email
            sa.scopes = ["https://www.googleapis.com/auth/cloud-platform"]
            sa_objs.append(sa)
        instance.service_accounts = sa_objs

    if ssh_user and ssh_pubkey:
        ssh_item = compute_v1.Items()
        ssh_item.key = "ssh-keys"
        ssh_item.value = f"{ssh_user}:{ssh_pubkey}"
        instance.metadata = compute_v1.Metadata(items=[ssh_item])

    return instance


def insert_instance(project: str, zone: str, instance: compute_v1.Instance, *, timeout: int = 300) -> None:
    """Insert an instance and wait for the zonal op to finish."""
    op = compute_v1.InstancesClient().insert(project=project, zone=zone, instance_resource=instance)
    op_name = _op_name(op)
    if op_name:
        _wait_or_rollback(
            lambda: wait_for_zonal_op(project, zone, op_name, timeout=timeout),
            lambda: delete_with_retry(
                delete_instance,
                project,
                zone,
                instance.name,
                timeout=timeout,
                resource_desc=f"instance {instance.name}",
            ),
            resource_desc=f"instance {instance.name}",
        )


def delete_instance(project: str, zone: str, name: str, *, timeout: int = 300) -> None:
    """Delete an instance and wait for the zonal op (``NotFound`` idempotent)."""
    try:
        op = compute_v1.InstancesClient().delete(project=project, zone=zone, instance=name)
    except gax.NotFound:
        return
    op_name = _op_name(op)
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=timeout)


def delete_disk(project: str, zone: str, name: str, *, timeout: int = 300) -> None:
    """Delete a Persistent Disk and wait for the zonal op (``NotFound`` idempotent).

    Compute ``disks.delete`` returns a synchronous ack while the disk is removed
    asynchronously, so a caller that only issues the call (without waiting) can
    report cleanup success before the disk is actually gone. Blocking on the
    returned zonal op until DONE makes deletion observable, mirroring
    ``delete_instance`` / ``delete_network``.
    """
    try:
        op = compute_v1.DisksClient().delete(project=project, zone=zone, disk=name)
    except gax.NotFound:
        return
    op_name = _op_name(op)
    if op_name:
        wait_for_zonal_op(project, zone, op_name, timeout=timeout)


def instances_in_network(project: str, network_name: str) -> list[tuple[str, compute_v1.Instance]]:
    """Return ``(zone, Instance)`` pairs for every instance on ``network_name``.

    Compute Engine has no project-wide "list instances in network" call;
    ``aggregated_list`` walks every zone and we filter on
    ``networkInterfaces[*].network`` by exact tail match. Used by teardown
    to find probe VMs left on the shared network.
    """
    out: list[tuple[str, compute_v1.Instance]] = []
    request = compute_v1.AggregatedListInstancesRequest(project=project)
    for zone_scope, scoped in compute_v1.InstancesClient().aggregated_list(request=request):
        for inst in scoped.instances or ():
            for nic in inst.network_interfaces:
                if short_name(nic.network) == network_name:
                    zone = zone_scope.rsplit("/", 1)[-1] if "/" in zone_scope else zone_scope
                    out.append((zone, inst))
                    break
    return out
