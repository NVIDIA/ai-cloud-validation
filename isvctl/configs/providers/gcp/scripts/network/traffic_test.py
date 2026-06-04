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

"""Test real network traffic flow on Compute Engine (step ``traffic_validation``).

Translates the AWS provider's ``traffic_test`` workflow to Compute Engine.
Self-contained: creates its OWN network + subnet + firewalls + three probe
VMs, drives ping/curl over SSH, and tears everything down in ``finally``.

Documented divergences from the AWS provider:

  * No internet gateway resource — custom-mode networks ship with an
    implicit default route via ``default-internet-gateway``. ``create_igw``
    is an honest no-op success entry with that note.
  * No IAM/SSM role for traffic generation — Compute Engine uses SSH.
    ``create_iam`` is a no-op success; ``ssm_ready`` becomes an SSH-readiness
    gate (the JSON key is preserved with ``message: 'ssh-ready'``).
  * Two security groups (allow/deny ICMP) become two firewalls scoped by
    network tag: ``sg_allow`` (INGRESS allow icmp + tcp:22, targetTags=
    [allow-tag]) and a deny setup where the deny-tagged target gets NO ICMP
    allow under custom-mode default-deny INGRESS.
  * Compute Engine instance status is ``RUNNING`` (not ``running``) — every
    emitted state flows through ``canonical_state``.
  * SSH replaces SSM RunShellScript: ping/curl are executed via ``ssh_run``
    from the source VM. ``traffic_blocked.passed`` is derived from a REAL
    ping timeout/failure to the deny-tagged target.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    delete_local_keypair,
    first_external_ip,
    first_internal_ip,
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
from common.ssh_utils import ssh_run, wait_for_ssh_stable

# Network tags that scope the allow / deny firewalls to specific probe VMs.
ALLOW_TAG = "isv-traffic-allow"
DENY_TAG = "isv-traffic-deny"


def _parse_ping_latency(stdout: str) -> float | None:
    """Parse the average RTT (ms) from ``ping`` summary output, or None."""
    for line in stdout.splitlines():
        if "min/avg/max" in line or "avg" in line:
            # e.g. "rtt min/avg/max/mdev = 0.3/0.5/0.7/0.1 ms"
            try:
                stats = line.split("=", 1)[1].strip().split()[0]
                parts = stats.split("/")
                if len(parts) >= 2:
                    return float(parts[1])
            except (IndexError, ValueError):
                continue
    return None


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Test network traffic flow on Compute Engine")
    parser.add_argument("--region", required=True, help="GCP region (narrowed to <region>-a if no --zone)")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region narrowing)")
    parser.add_argument("--cidr", default="10.93.0.0/16", help="Aggregate CIDR to carve the test subnet from")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)
    region = zone.rsplit("-", 1)[0]
    ssh_user = DEFAULT_SSH_USER

    # Compute Engine names ARE the API IDs — run-id-suffix every created
    # resource so parallel runs don't collide on AlreadyExists.
    network_name = unique_suffix("isv-traffic-net")
    subnet_name = unique_suffix("isv-traffic-subnet")
    fw_allow_name = unique_suffix("isv-traffic-fw-allow")
    fw_allow_icmp_name = unique_suffix("isv-traffic-fw-allow-icmp")
    fw_deny_name = unique_suffix("isv-traffic-fw-deny")
    source_name = unique_suffix("isv-traffic-source")
    target_allow_name = unique_suffix("isv-traffic-target-allow")
    target_deny_name = unique_suffix("isv-traffic-target-deny")
    key_name = unique_suffix("isv-traffic-key")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "traffic_flow",
        "network_id": network_name,
        "tests": {},
    }

    # Per-resource cleanup trackers. Instance names are stamped True BEFORE
    # waiting on the async insert so a wait-side failure still cleans up.
    network_created = False
    subnet_created = False
    fw_allow_created = False
    fw_allow_icmp_created = False
    fw_deny_created = False
    source_created = False
    target_allow_created = False
    target_deny_created = False
    key_priv: str | None = None
    key_created = False

    try:
        # SSH ingress source ranges resolve BEFORE any resource is created so an
        # unset/invalid NETWORK_FIREWALL_TRUST_IP fails closed with nothing to
        # clean up. tcp/22 must never open to 0.0.0.0/0 — the firewall ingress
        # gate forbids it (see common.network.resolve_trusted_firewall_sources);
        # there is no fallback source range. Mirrors create_vpc / floating_ip.
        trusted_ssh_sources = resolve_trusted_firewall_sources()

        # Local SSH key pair (verified-reuse). Public key pushed via metadata.
        key_priv, key_created = generate_ssh_keypair(key_name)
        ssh_pubkey = read_ssh_pubkey(key_priv)

        # 1. create_vpc — custom-mode network + one subnet carved from --cidr.
        # Stamp each *_created tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. The finally
        # cleanup gates on the tracker, so it must be True before the call for a
        # partial create to still reach cleanup (delete on a never-created
        # resource is a harmless NotFound no-op). Mirrors create_vpc/byoip_test.
        subnet_cidr = carve_subnet_cidrs(args.cidr, 1)[0]
        network_created = True
        insert_network(project, network_name)
        subnet_created = True
        insert_subnetwork(project, region, subnet_name, network_name, subnet_cidr)
        result["tests"]["create_vpc"] = {"passed": True}

        # 2. create_igw — implicit on Compute Engine (no IGW resource).
        result["tests"]["create_igw"] = {
            "passed": True,
            "message": "default-internet-gateway implicit on Compute Engine",
        }

        # 3. create_iam — no-op (service-account model, no SSM role).
        result["tests"]["create_iam"] = {
            "passed": True,
            "message": "no-op on Compute Engine — service-account model",
        }

        # 4. create_security_groups — the allow setup is split into TWO rules
        # so the admin-port restriction is not widened to the whole internet:
        #   * fw_allow: tcp:22 (SSH) scoped to the operator-trusted source only
        #     (NEVER 0.0.0.0/0) — the harness SSHes the allow-tagged source VM.
        #   * fw_allow_icmp: a SEPARATE icmp rule scoped to the test subnet CIDR
        #     so the source VM can ping the allow-tagged target over ICMP. ICMP
        #     is not an admin port, so the trust-IP policy does not apply; the
        #     subnet CIDR (RFC1918) is the honest source for instance-to-
        #     instance probes, not 0.0.0.0/0.
        # Each allow rule MUST carry at least one Allowed with I_p_protocol set
        # (empty allowed[] -> HTTP 400). The deny-tagged target gets NO ICMP
        # allow under custom-mode default-deny INGRESS.
        fw_allow = build_firewall(
            fw_allow_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=[make_allowed("tcp", ["22"])],
            source_ranges=trusted_ssh_sources,
            target_tags=[ALLOW_TAG],
        )
        fw_allow_created = True
        insert_firewall(project, fw_allow)
        icmp_sources = sorted({subnet_cidr, *trusted_ssh_sources})
        fw_allow_icmp = build_firewall(
            fw_allow_icmp_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=[make_allowed("icmp")],
            source_ranges=icmp_sources,
            target_tags=[ALLOW_TAG],
        )
        fw_allow_icmp_created = True
        insert_firewall(project, fw_allow_icmp)
        # Deny target needs SSH-less inbound denial of ICMP but the SOURCE
        # still SSHes only to the allow-tagged target; the deny target is a
        # ping destination only. To keep the deny target reachable for
        # nothing (it is only pinged), we add a tcp:22 allow on DENY_TAG so
        # the rule is non-empty AND honestly carries NO icmp allow — ICMP to
        # the deny target is blocked by default-deny INGRESS. The tcp:22 source
        # is the operator-trusted range (NEVER 0.0.0.0/0), same as fw_allow.
        fw_deny = build_firewall(
            fw_deny_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=[make_allowed("tcp", ["22"])],
            source_ranges=trusted_ssh_sources,
            target_tags=[DENY_TAG],
        )
        fw_deny_created = True
        insert_firewall(project, fw_deny)
        result["tests"]["create_security_groups"] = {
            "passed": True,
            "sg_allow": fw_allow_name,
            "sg_deny": fw_deny_name,
        }

        # 5. launch_instances — three VMs: a source (allow-tagged, SSH probe
        # origin), an allow-target (allow-tagged), a deny-target (deny-tagged,
        # no ICMP allow). Stamp the cleanup tracker BEFORE the insert wait.
        source_inst = build_probe_instance(
            project=project,
            zone=zone,
            name=source_name,
            network_name=network_name,
            subnet_name=subnet_name,
            ssh_user=ssh_user,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[ALLOW_TAG],
        )
        source_created = True
        insert_instance(project, zone, source_inst)

        target_allow_inst = build_probe_instance(
            project=project,
            zone=zone,
            name=target_allow_name,
            network_name=network_name,
            subnet_name=subnet_name,
            ssh_user=ssh_user,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[ALLOW_TAG],
        )
        target_allow_created = True
        insert_instance(project, zone, target_allow_inst)

        target_deny_inst = build_probe_instance(
            project=project,
            zone=zone,
            name=target_deny_name,
            network_name=network_name,
            subnet_name=subnet_name,
            ssh_user=ssh_user,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[DENY_TAG],
        )
        target_deny_created = True
        insert_instance(project, zone, target_deny_inst)
        result["tests"]["launch_instances"] = {
            "passed": True,
            "instances": [source_name, target_allow_name, target_deny_name],
        }

        # 6. instances_running — poll each to canonical 'running', read IPs.
        running_map: dict[str, Any] = {}
        for inst_name in (source_name, target_allow_name, target_deny_name):
            state = poll_instance_state(project, zone, inst_name, target_canonical="running", timeout=300)
            inst = get_instance(project, zone, inst_name)
            running_map[inst_name] = {
                "state": state,
                "private_ip": first_internal_ip(inst),
                "public_ip": first_external_ip(inst),
            }
        result["tests"]["instances_running"] = {"passed": True, "instances": running_map}

        source_public = running_map[source_name]["public_ip"] or wait_for_public_ip(
            project, zone, source_name, timeout=120
        )
        target_allow_private = running_map[target_allow_name]["private_ip"]
        target_deny_private = running_map[target_deny_name]["private_ip"]
        if not source_public:
            raise RuntimeError("source VM has no external IP after RUNNING")

        # 7. ssm_ready — SSH readiness gate replaces SSM. Require consecutive
        # successes so the post-cloud-init sshd bounce is washed out.
        ssh_ready = wait_for_ssh_stable(
            host=source_public,
            user=ssh_user,
            key_file=key_priv,
            consecutive=3,
            interval=10,
            max_attempts=36,
        )
        result["tests"]["ssm_ready"] = {
            "passed": ssh_ready,
            "message": "ssh-ready" if ssh_ready else "ssh-not-ready",
        }
        if not ssh_ready:
            raise RuntimeError("source VM SSH did not stabilize")

        # 8. traffic_allowed — ping the allow-target private IP (ICMP allowed).
        rc, out, _err = ssh_run(
            source_public,
            ssh_user,
            key_priv,
            f"ping -c 3 -W 2 {target_allow_private}",
            timeout=30,
        )
        latency = _parse_ping_latency(out)
        result["tests"]["traffic_allowed"] = {"passed": rc == 0, "latency_ms": latency}

        # 9. traffic_blocked — ping the deny-target private IP, expect FAILURE
        # (no ICMP allow on DENY_TAG under default-deny INGRESS). passed=True
        # means traffic was correctly blocked — derived from the real ping
        # timeout/non-zero exit.
        rc_blocked, _out_b, _err_b = ssh_run(
            source_public,
            ssh_user,
            key_priv,
            f"ping -c 3 -W 2 {target_deny_private}",
            timeout=30,
        )
        result["tests"]["traffic_blocked"] = {"passed": rc_blocked != 0}

        # 10. internet_icmp — ping 8.8.8.8 from the source VM.
        rc_inet, out_inet, _err_i = ssh_run(
            source_public,
            ssh_user,
            key_priv,
            "ping -c 3 -W 2 8.8.8.8",
            timeout=30,
        )
        result["tests"]["internet_icmp"] = {
            "passed": rc_inet == 0,
            "latency_ms": _parse_ping_latency(out_inet),
        }

        # 11. internet_http — curl HTTPS from the source VM.
        rc_http, out_http, _err_h = ssh_run(
            source_public,
            ssh_user,
            key_priv,
            "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://www.google.com",
            timeout=30,
        )
        http_ok = rc_http == 0 and out_http.strip().startswith(("2", "3"))
        result["tests"]["internet_http"] = {"passed": http_ok, "public_ip": source_public}

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Tear down everything THIS run created. Order: VMs, firewalls,
        # subnet, network, local key. delete_with_retry never raises and
        # returns False only on exhausted retries — capture every cloud-delete
        # bool so a leaked resource fails the step instead of coexisting with
        # success=True. Each delete is gated independently, so a failed
        # sibling never skips the rest.
        cleanup_errors: list[str] = []
        for created, inst_name in (
            (source_created, source_name),
            (target_allow_created, target_allow_name),
            (target_deny_created, target_deny_name),
        ):
            if created and not delete_with_retry(
                delete_instance, project, zone, inst_name, resource_desc=f"instance {inst_name}"
            ):
                cleanup_errors.append(f"instance {inst_name}")
        if fw_allow_created and not delete_with_retry(
            delete_firewall, project, fw_allow_name, resource_desc=f"firewall {fw_allow_name}"
        ):
            cleanup_errors.append(f"firewall {fw_allow_name}")
        if fw_allow_icmp_created and not delete_with_retry(
            delete_firewall, project, fw_allow_icmp_name, resource_desc=f"firewall {fw_allow_icmp_name}"
        ):
            cleanup_errors.append(f"firewall {fw_allow_icmp_name}")
        if fw_deny_created and not delete_with_retry(
            delete_firewall, project, fw_deny_name, resource_desc=f"firewall {fw_deny_name}"
        ):
            cleanup_errors.append(f"firewall {fw_deny_name}")
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
        # Local SSH keypair is a workstation file, not a leaked cloud
        # resource; delete best-effort without affecting result["success"].
        if key_created and key_priv:
            try:
                delete_local_keypair(key_priv)
            except Exception as cleanup_exc:
                print(f"Cleanup error (local key): {cleanup_exc}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
