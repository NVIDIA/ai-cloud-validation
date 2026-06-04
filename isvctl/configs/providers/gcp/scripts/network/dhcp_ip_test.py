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

"""DHCP/IP management test - launch a probe VM for DhcpIpManagementCheck.

Translates the AWS provider's ``dhcp_ip_test`` workflow to Compute Engine.
This is a REAL stub (NOT the my-isv demo-skip pattern): it launches one
Ubuntu probe VM into the SHARED create_network VPC, leaves it RUNNING +
SSH-reachable, and emits the SSH prerequisites the validator needs.

Documented divergences:

  * No managed key-pair store — generate a local PEM/.pub pair and push
    the public key via instance metadata ``ssh-keys``. ``key_name`` is the
    local stem, ``key_file`` the absolute private-key path, ``key_created``
    the verified-reuse bool forwarded to teardown for cleanup gating.
  * External IPs are attached per-NIC via an ``ONE_TO_ONE_NAT`` access
    config at launch (``external_ip=True``); ``public_ip`` is read from
    ``accessConfigs[0].natIP``, ``private_ip`` from ``networkIP``.

Lifecycle contract (asymmetric with the connectivity stub): the
DhcpIpManagementCheck validator SSHes into the VM AFTER this step returns,
so on SUCCESS the VM stays RUNNING — the shared teardown step (which
receives this step's key_file/key_name/key_created and finds the VM via
aggregated_list) reaps it. We delete the VM + local key in ``finally``
ONLY when ``result["success"]`` is False.

Cleanup discipline: ``instance_id`` (the deterministic instance name) is
stamped as the cleanup tracker IMMEDIATELY after the insert ack, BEFORE the
async wait.
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
    ISV_NETWORK_TAG,
    build_probe_instance,
    delete_instance,
    insert_instance,
)
from common.ssh_utils import wait_for_ssh, wait_for_ssh_stable

_CLEANUP_INSTANCE_WAIT_S = 180


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="DHCP/IP management test (GCP)")
    parser.add_argument("--vpc-id", required=True, help="Shared network short name")
    parser.add_argument("--subnet-id", required=True, help="Subnetwork short name to launch in")
    parser.add_argument("--sg-id", required=True, help="Shared firewall rule name (allows tcp:22)")
    parser.add_argument("--region", required=True, help="GCP region of the subnetwork")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region narrowing)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    key_name = unique_suffix("isv-dhcp-key")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "dhcp_ip",
        "public_ip": None,
        "private_ip": None,
        "key_file": None,
        "key_name": key_name,
        "key_created": False,
        "ssh_user": DEFAULT_SSH_USER,
        "instance_id": None,
    }

    # Cleanup trackers — stamped before the async insert wait.
    instance_name: str | None = None
    key_priv: str | None = None
    key_created = False

    try:
        # 1. Local SSH keypair (verified-reuse; created bool forwarded for
        # teardown gating).
        key_priv, key_created = generate_ssh_keypair(key_name)
        ssh_pubkey = read_ssh_pubkey(key_priv)
        result["key_file"] = key_priv
        result["key_created"] = key_created

        # 2. Launch ONE Ubuntu probe VM into the shared VPC/subnet with an
        # external IP + the probe network tag + ssh metadata.
        candidate_name = unique_suffix("isv-dhcp-vm")
        instance = build_probe_instance(
            project=project,
            zone=zone,
            name=candidate_name,
            network_name=args.vpc_id,
            subnet_name=args.subnet_id,
            ssh_user=DEFAULT_SSH_USER,
            ssh_pubkey=ssh_pubkey,
            external_ip=True,
            network_tags=[ISV_NETWORK_TAG],
        )
        # Stamp the cleanup tracker AND result["instance_id"] IMMEDIATELY
        # after the insert ack returns, BEFORE the wait. The instance name
        # is deterministic, so a wait-side failure still leaves a truthful
        # teardown target.
        instance_name = candidate_name
        result["instance_id"] = candidate_name
        insert_instance(project, zone, instance)

        # 3. Wait for RUNNING + public IP.
        result["state"] = poll_instance_state(project, zone, candidate_name, target_canonical="running", timeout=300)
        inst = get_instance(project, zone, candidate_name)
        public_ip = first_external_ip(inst) or wait_for_public_ip(project, zone, candidate_name, timeout=120)
        result["public_ip"] = public_ip
        result["private_ip"] = first_internal_ip(inst)

        if not public_ip:
            raise RuntimeError("Instance reached RUNNING but has no external IP for SSH")

        # 4. Gate on SSH stability (not first-SSH) so the validator's SSH
        # session does not race the guest-agent's authorized_keys replay.
        if not wait_for_ssh(public_ip, DEFAULT_SSH_USER, key_priv):
            raise RuntimeError(f"SSH not reachable on {public_ip}")
        if not wait_for_ssh_stable(public_ip, DEFAULT_SSH_USER, key_priv):
            raise RuntimeError(f"SSH did not stabilize on {public_ip}")

        # Success: VM is RUNNING + SSH-reachable. Leave it up for the
        # validator — teardown reaps it via the forwarded key + aggregated
        # list.
        result["success"] = True

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # Asymmetric cleanup: on SUCCESS the VM stays up for the validator
        # (teardown reaps it). On FAILURE clean up the VM + local key here.
        if not result["success"]:
            try:
                if instance_name:
                    print(f"Cleanup-on-failure: deleting instance {instance_name}", file=sys.stderr)
                    delete_with_retry(
                        delete_instance,
                        project,
                        zone,
                        instance_name,
                        resource_desc=f"instance {instance_name}",
                        timeout=_CLEANUP_INSTANCE_WAIT_S,
                    )
                if key_created and key_priv:
                    delete_local_keypair(key_priv)
            except Exception as cleanup_exc:
                print(f"Cleanup-on-failure error: {cleanup_exc}", file=sys.stderr)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
