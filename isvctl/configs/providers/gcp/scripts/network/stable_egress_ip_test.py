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

"""Test egress-IP stability across repeated probes on Compute Engine (step ``stable_egress_ip_test``).

Translates the AWS provider's ``stable_egress_ip_test`` (DMS05-01) workflow to
Compute Engine. NVIDIA cloud services use IP allowlists, so a workload must
present a stable egress IP. This stub is self-contained: it creates its OWN
custom-mode network + subnet + SSH firewall + one external-IP VM, SSHes in, and
curls an external echo endpoint N times, asserting every probe reports the same
egress IP. Everything is torn down in ``finally``.

Documented divergences from the AWS provider:

  * There is NO internet-gateway / route-table resource (the AWS oracle builds
    an IGW + default route). Compute Engine custom-mode networks egress to the
    internet implicitly via ``default-internet-gateway`` (see the create_vpc
    divergence), so no IGW/route-table block is created.
  * The VM's stable egress IP is its external ``accessConfigs[ONE_TO_ONE_NAT]``
    ``natIP`` — ephemeral but stable for the running VM's lifetime, which is the
    same lifetime guarantee the AWS single-run probe relies on. A reserved
    static ``compute_v1.Address`` is the production stable-egress pattern but is
    not required to pass this single-run probe.
  * SSH (tcp/22) ingress is restricted to the operator-trusted source ranges
    (``NETWORK_FIREWALL_TRUST_IP``) — never 0.0.0.0/0. There is no fallback: an
    unset/invalid/broad value fails the test closed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    delete_local_keypair,
    first_external_ip,
    generate_ssh_keypair,
    get_instance,
    narrow_region_to_zone,
    poll_instance_state,
    read_ssh_pubkey,
    resolve_project,
    unique_suffix,
    wait_for_public_ip,
)
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    DEFAULT_SSH_USER,
    ISV_NETWORK_TAG,
    build_firewall,
    build_probe_instance,
    carve_subnet_cidrs,
    delete_firewall,
    delete_instance,
    delete_network,
    delete_subnetwork,
    insert_firewall,
    insert_instance,
    insert_network,
    insert_subnetwork,
    make_allowed,
    resolve_trusted_firewall_sources,
)
from common.ssh_utils import ssh_run, wait_for_ssh, wait_for_ssh_stable

TEST_NAME = "stable_egress_ip"
TEST_NAMES = ("create_instance", "probe_egress_ip", "egress_ip_stable")


def probe_egress_ip(
    public_ip: str,
    key_file: str,
    endpoint: str,
    probes: int,
    interval_seconds: float,
    ssh_user: str,
) -> dict[str, Any]:
    """Probe the egress IP via SSH + curl, ``probes`` times, ``interval_seconds`` apart.

    Returns the observed egress IPs in ``ips`` (kept internal, as the AWS oracle
    does); the caller emits the probe COUNT and asserts every probe matched.
    """
    result: dict[str, Any] = {"passed": False, "endpoint": endpoint, "ips": []}

    if not wait_for_ssh(public_ip, ssh_user, key_file):
        result["error"] = f"SSH not reachable on {public_ip}"
        return result
    if not wait_for_ssh_stable(public_ip, ssh_user, key_file):
        result["error"] = f"SSH did not stabilize on {public_ip}"
        return result

    # endpoint is shlex-quoted so a typo'd / hostile --endpoint cannot smuggle
    # shell metacharacters onto the remote login shell ssh invokes.
    cmd = f"curl -s --max-time 5 {shlex.quote(endpoint)}"
    ips: list[str] = []
    for attempt in range(1, probes + 1):
        if attempt > 1:
            time.sleep(interval_seconds)
        exit_code, stdout, stderr = ssh_run(public_ip, ssh_user, key_file, cmd, timeout=15)
        if exit_code != 0:
            result["error"] = f"probe {attempt}/{probes} failed (exit={exit_code}): {stderr.strip() or stdout.strip()}"
            return result
        ip = stdout.strip()
        if not ip:
            result["error"] = f"probe {attempt}/{probes} returned empty response"
            return result
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            result["error"] = f"probe {attempt}/{probes} returned non-IP value: {ip!r}"
            return result
        ips.append(ip)

    result["passed"] = True
    result["ips"] = ips
    result["message"] = f"Collected {len(ips)} egress IP probes from {endpoint}"
    return result


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test stable egress IP (DMS05-01)")
    parser.add_argument("--region", required=True, help="GCP region (narrowed to <region>-a if no --zone)")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region narrowing)")
    parser.add_argument("--cidr", default="10.100.0.0/16", help="Aggregate CIDR to carve the test subnet from")
    parser.add_argument("--probes", type=int, default=3, help="Number of egress IP probes")
    parser.add_argument("--interval-seconds", type=float, default=2.0, help="Delay between probes")
    parser.add_argument(
        "--endpoint",
        default="https://api.ipify.org",
        help="External IP-discovery endpoint that echoes the caller's egress IP",
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)
    region = zone.rsplit("-", 1)[0]
    ssh_user = DEFAULT_SSH_USER

    network_name = unique_suffix("isv-egress-net")
    subnet_name = unique_suffix("isv-egress-subnet")
    fw_name = unique_suffix("isv-egress-fw")
    instance_name = unique_suffix("isv-egress-vm")
    key_name = unique_suffix("isv-egress-key")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": TEST_NAME,
        "tests": {name: {"passed": False} for name in TEST_NAMES},
    }

    network_created = False
    subnet_created = False
    fw_created = False
    instance_created = False
    key_priv: str | None = None
    key_created = False

    try:
        # SSH ingress source ranges resolve BEFORE any resource is created so an
        # unset/invalid NETWORK_FIREWALL_TRUST_IP fails closed with nothing to
        # clean up (there is no fallback source range for SSH ingress).
        trusted_ssh_sources = resolve_trusted_firewall_sources()

        key_priv, key_created = generate_ssh_keypair(key_name)
        ssh_pubkey = read_ssh_pubkey(key_priv)

        # Setup: custom-mode network + subnet + tag-scoped SSH firewall. Stamp
        # each *_created tracker BEFORE its insert helper (the helper runs
        # _wait_or_rollback and can raise on a partial create; the finally
        # cleanup gates on the tracker). Mirrors stable_ip_test / create_vpc.
        subnet_cidr = carve_subnet_cidrs(args.cidr, 1)[0]
        network_created = True
        insert_network(project, network_name)
        subnet_created = True
        insert_subnetwork(project, region, subnet_name, network_name, subnet_cidr)
        fw = build_firewall(
            fw_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=[make_allowed("tcp", ["22"])],
            source_ranges=trusted_ssh_sources,
            target_tags=[ISV_NETWORK_TAG],
        )
        fw_created = True
        insert_firewall(project, fw)

        # create_instance — launch ONE external-IP VM. Stamp the cleanup tracker
        # BEFORE waiting on the async insert.
        inst_resource = build_probe_instance(
            project=project,
            zone=zone,
            name=instance_name,
            network_name=network_name,
            subnet_name=subnet_name,
            ssh_user=ssh_user,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[ISV_NETWORK_TAG],
        )
        instance_created = True
        insert_instance(project, zone, inst_resource)
        poll_instance_state(project, zone, instance_name, target_canonical="running", timeout=300)
        inst = get_instance(project, zone, instance_name)
        public_ip = first_external_ip(inst) or wait_for_public_ip(project, zone, instance_name, timeout=120)
        if not public_ip:
            raise RuntimeError("instance reached RUNNING but has no external IP for SSH/egress probe")
        result["tests"]["create_instance"] = {"passed": True, "instance_id": instance_name}

        # probe_egress_ip — SSH in and curl the echo endpoint N times.
        probe_result = probe_egress_ip(
            public_ip=public_ip,
            key_file=key_priv,
            endpoint=args.endpoint,
            probes=args.probes,
            interval_seconds=args.interval_seconds,
            ssh_user=ssh_user,
        )
        # Emit ``probes`` as the integer probe COUNT (matches both oracles, which
        # keep the observed IP list internal); the stability subtest below works
        # off the internal ``ips`` list.
        probe_ips = probe_result["ips"]
        result["tests"]["probe_egress_ip"] = {
            "passed": probe_result["passed"],
            "probes": len(probe_ips),
            **({"error": probe_result["error"]} if not probe_result["passed"] and "error" in probe_result else {}),
        }
        if not probe_result["passed"]:
            raise RuntimeError(probe_result.get("error", "egress IP probing failed"))

        # egress_ip_stable — every probe must report the same egress IP.
        distinct = sorted(set(probe_ips))
        result["tests"]["egress_ip_stable"] = {
            "passed": len(distinct) == 1,
            "egress_ip": distinct[0] if len(distinct) == 1 else None,
            **({"error": f"egress IP changed across probes: {', '.join(distinct)}"} if len(distinct) != 1 else {}),
        }

        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Capture every cloud-delete bool so a leaked resource fails the step
        # instead of coexisting with success=True. Each delete is gated
        # independently. Delete in dependency order: instance, firewall, subnet,
        # network.
        cleanup_errors: list[str] = []
        if instance_created and not delete_with_retry(
            delete_instance, project, zone, instance_name, resource_desc=f"instance {instance_name}"
        ):
            cleanup_errors.append(f"instance {instance_name}")
        if fw_created and not delete_with_retry(delete_firewall, project, fw_name, resource_desc=f"firewall {fw_name}"):
            cleanup_errors.append(f"firewall {fw_name}")
        if subnet_created and not delete_with_retry(
            delete_subnetwork, project, region, subnet_name, resource_desc=f"subnetwork {subnet_name}"
        ):
            cleanup_errors.append(f"subnetwork {subnet_name}")
        if network_created and not delete_with_retry(
            delete_network, project, network_name, resource_desc=f"network {network_name}"
        ):
            cleanup_errors.append(f"network {network_name}")
        if cleanup_errors:
            result.setdefault("cleanup_errors", []).extend(cleanup_errors)
            result["success"] = False
        # Local SSH keypair is a workstation file, not a leaked cloud resource;
        # delete best-effort without affecting result["success"].
        if key_created and key_priv:
            try:
                delete_local_keypair(key_priv)
            except Exception as cleanup_exc:
                print(f"Cleanup error (local key): {cleanup_exc}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
