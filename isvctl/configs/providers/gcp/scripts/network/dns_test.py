#!/usr/bin/env python3
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

"""Localized DNS test for Compute Engine (test phase, step ``dns_test``).

Translates the AWS provider's ``dns_test`` workflow to Compute Engine + Cloud
DNS. The five named subtests LocalizedDnsCheck requires
(``create_vpc_with_dns``, ``create_hosted_zone``, ``create_dns_record``,
``verify_dns_settings``, ``resolve_record``) are preserved by JSON key.

This stub uses TWO SDKs: ``google.cloud.compute_v1`` (via common.network) for
the network, and ``google.cloud.dns`` for the managed zone — Cloud DNS is the
Compute Engine analog of Route 53.

Documented divergences:

  * VPC has NO dns_support / dns_hostnames toggles — internal DNS is always
    on. ``verify_dns_settings`` emits ``dns_support=true`` /
    ``dns_hostnames=true`` as documented capability-presence (NOT fabricated
    probe values for a missing toggle), with a message noting GCE's
    unconditional internal DNS.
  * The private-hosted-zone analog is a Cloud DNS managed zone with
    ``visibility=private`` and ``privateVisibilityConfig.networks=[<network>]``.

    **google-cloud-dns 0.36.1 SERIALIZATION QUIRK (verified against the
    pinned lockfile and the v0.36.0 source):** ``ManagedZone._build_resource()``
    serializes ONLY ``name``, ``dnsName``, ``description``, and
    ``nameServerSet`` into the create body. Setting
    ``zone.visibility = "private"`` OR
    ``zone._properties["privateVisibilityConfig"] = {...}`` BEFORE
    ``zone.create()`` is SILENTLY DROPPED — the POST body omits both fields,
    the API creates a PUBLIC zone, and record read-back still succeeds. The
    validator's PASS would then be a fake signal (a public zone "validated"
    as private). We therefore BYPASS the high-level wrapper and POST the
    explicit body (with visibility + privateVisibilityConfig) via the
    low-level ``client._connection.api_request``, then GET-verify the zone is
    genuinely ``visibility == "private"`` and that its
    ``privateVisibilityConfig`` references the requested network — failing
    the subtest if not. Record creation / read-back uses the high-level
    ``zone.changes()`` / ``zone.list_resource_record_sets()`` methods (those
    serialize correctly — only the create body needs the bypass).

The test creates and deletes its OWN network and zone. The ``finally`` block
deletes the A record, then the managed zone, then the network — idempotent /
best-effort so a mid-creation failure does not leak.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import delete_network, insert_network
from google.api_core import exceptions as gax
from google.cloud import dns

ISV_ZONE_DESCRIPTION = "ISV localized DNS validation zone (createdby=isvtest)"
RECORD_TTL = 60
CHANGE_TIMEOUT = 60
CHANGE_INTERVAL = 3


def _network_self_link(project: str, network_name: str) -> str:
    """Return the absolute compute v1 self-link the privateVisibilityConfig expects."""
    return f"https://www.googleapis.com/compute/v1/projects/{project}/global/networks/{network_name}"


def _create_private_zone(client: dns.Client, project: str, zone_name: str, domain: str, network_name: str) -> None:
    """POST a PRIVATE managed zone via the low-level connection (quirk bypass).

    The high-level ``ManagedZone.visibility`` / ``_properties`` set is
    silently dropped by ``_build_resource()`` (0.36.1), so we send the
    explicit body — including ``visibility`` and ``privateVisibilityConfig``
    — directly. ``domain`` MUST be an absolute name ending in '.'.
    """
    body = {
        "name": zone_name,
        "dnsName": domain,
        "description": ISV_ZONE_DESCRIPTION,
        "visibility": "private",
        "privateVisibilityConfig": {
            "networks": [{"networkUrl": _network_self_link(project, network_name)}],
        },
    }
    client._connection.api_request(
        method="POST",
        path=f"/projects/{project}/managedZones",
        data=body,
    )


def _verify_private_zone(client: dns.Client, project: str, zone_name: str, network_name: str) -> bool:
    """GET the zone and confirm it is genuinely private + bound to the network.

    Re-fetches via the low-level connection because the high-level
    ManagedZone properties do not surface visibility / privateVisibilityConfig
    in 0.36.1. Returns True only when ``visibility == "private"`` AND the
    network self-link appears in ``privateVisibilityConfig.networks`` (exact
    tail match on the network name).
    """
    fetched = client._connection.api_request(
        method="GET",
        path=f"/projects/{project}/managedZones/{zone_name}",
    )
    if str(fetched.get("visibility", "")).lower() != "private":
        return False
    networks = (fetched.get("privateVisibilityConfig") or {}).get("networks") or []
    for net in networks:
        url = net.get("networkUrl", "")
        if url.rsplit("/", 1)[-1] == network_name:
            return True
    return False


def _wait_change_done(change: Any, *, timeout: int, interval: int) -> bool:
    """Poll a Cloud DNS Change via reload() until status == 'done'."""
    deadline = time.monotonic() + timeout
    while True:
        if str(change.status or "").lower() == "done":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
        change.reload()


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test localized DNS on Compute Engine + Cloud DNS")
    parser.add_argument("--region", required=True, help="GCP region (op scope; Cloud DNS is global)")
    parser.add_argument("--cidr", default="10.89.0.0/16", help="Aggregate CIDR (target IP derived from it)")
    parser.add_argument("--domain", default="internal.isv.test", help="Internal DNS suffix")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)

    # Compute Engine / Cloud DNS names ARE the API IDs — run-id-suffix them.
    network_name = unique_suffix("isv-dns-vpc")
    zone_name = unique_suffix("isv-dns-zone")

    # Cloud DNS dnsName MUST be an absolute name ending in '.'.
    domain = args.domain if args.domain.endswith(".") else f"{args.domain}."
    fqdn = f"storage.{domain}"
    # Private endpoint IP within the configured aggregate (e.g. 10.89.1.100).
    base = args.cidr.split("/")[0].split(".")
    target_ip = f"{base[0]}.{base[1]}.1.100"

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "dns_test",
        "tests": {
            "create_vpc_with_dns": {"passed": False},
            "create_hosted_zone": {"passed": False},
            "create_dns_record": {"passed": False},
            "verify_dns_settings": {"passed": False},
            "resolve_record": {"passed": False},
        },
    }

    client = dns.Client(project=project)

    # Cleanup trackers for the finally block.
    network_created = False
    zone_created = False
    record_created = False

    try:
        # 1. create_vpc_with_dns — custom-mode network (internal DNS is
        # unconditional; there is nothing to enable). Stamp network_created
        # BEFORE insert_network: it runs _wait_or_rollback, which on a failed
        # op-wait + failed rollback raises PartialCreateError with the network
        # possibly leaked. The finally cleanup gates on the tracker, so it must
        # be True before the call for a partial create to still reach cleanup
        # (delete on a never-created network is a harmless NotFound no-op).
        # Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)
        result["tests"]["create_vpc_with_dns"] = {"passed": True, "vpc_id": network_name}

        # 2. create_hosted_zone — PRIVATE managed zone via the low-level POST
        # (quirk bypass), then GET-verify it is genuinely private + bound.
        _create_private_zone(client, project, zone_name, domain, network_name)
        zone_created = True
        is_private = _verify_private_zone(client, project, zone_name, network_name)
        result["tests"]["create_hosted_zone"] = {
            "passed": is_private,
            "zone_id": zone_name,
            "domain": domain,
        }
        if not is_private:
            raise RuntimeError(f"managed zone {zone_name} is not genuinely private / not bound to {network_name}")

        # Rehydrate a high-level zone handle for the record-set operations
        # (these serialize correctly — only the create body needed the bypass).
        zone = client.zone(zone_name, dns_name=domain)

        # 3. create_dns_record — A record under the domain; wait for done.
        rrset = zone.resource_record_set(fqdn, "A", RECORD_TTL, [target_ip])
        add_change = zone.changes()
        add_change.add_record_set(rrset)
        add_change.create()
        record_created = True
        record_done = _wait_change_done(add_change, timeout=CHANGE_TIMEOUT, interval=CHANGE_INTERVAL)
        result["tests"]["create_dns_record"] = {
            "passed": record_done,
            "fqdn": fqdn,
            "target_ip": target_ip,
        }

        # 4. verify_dns_settings — Compute Engine internal DNS is always on
        # (no toggles). Documented capability-presence, not a fabricated probe.
        result["tests"]["verify_dns_settings"] = {
            "passed": True,
            "dns_support": True,
            "dns_hostnames": True,
            "message": "Compute Engine internal DNS is always on",
        }

        # 5. resolve_record — read the A record back via the high-level API
        # and confirm the target IP is among its rrdatas.
        resolved_ip = None
        all_ips: list[str] = []
        for record in zone.list_resource_record_sets():
            if record.record_type == "A" and record.name.rstrip(".") == fqdn.rstrip("."):
                all_ips = list(record.rrdatas)
                if target_ip in all_ips:
                    resolved_ip = target_ip
                break
        result["tests"]["resolve_record"] = {
            "passed": resolved_ip == target_ip,
            "resolved_ip": resolved_ip,
            "all_ips": all_ips,
        }

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Idempotent cleanup: delete the A record, then the zone, then the
        # network. Each step is independently guarded so one failure never
        # skips the rest. The record/zone deletes are non-retry Cloud DNS
        # calls and delete_with_retry is the non-raising bool contract for the
        # network — surface every failure into cleanup_errors so a leaked
        # resource fails the step instead of coexisting with success=True.
        cleanup_errors: list[str] = []
        if record_created:
            try:
                zone = client.zone(zone_name, dns_name=domain)
                rrset = zone.resource_record_set(fqdn, "A", RECORD_TTL, [target_ip])
                del_change = zone.changes()
                del_change.delete_record_set(rrset)
                del_change.create()
                _wait_change_done(del_change, timeout=CHANGE_TIMEOUT, interval=CHANGE_INTERVAL)
            except gax.NotFound:
                pass
            except Exception as rec_exc:
                print(f"Cleanup: record delete failed: {rec_exc}", file=sys.stderr)
                cleanup_errors.append(f"dns record {fqdn}")
        if zone_created:
            print(f"Cleanup: deleting managed zone {zone_name}", file=sys.stderr)
            try:
                client.zone(zone_name).delete()
            except gax.NotFound:
                pass
            except Exception as zone_exc:
                print(f"Cleanup: zone delete failed: {zone_exc}", file=sys.stderr)
                cleanup_errors.append(f"managed zone {zone_name}")
        if network_created:
            print(f"Cleanup: deleting network {network_name}", file=sys.stderr)
            if not delete_with_retry(delete_network, project, network_name, resource_desc=f"network {network_name}"):
                cleanup_errors.append(f"network {network_name}")
        if cleanup_errors:
            result.setdefault("cleanup_errors", []).extend(cleanup_errors)
            result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
