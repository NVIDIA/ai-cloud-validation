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

"""Release a Firebird bare-metal server after testing.

BMs are tenant-owned hardware and are never deleted. Teardown only undoes what
launch_instance did, as reported by its output flags:
  --deprovision      launch provisioned (or was told to reuse) the BM: wipe the OS
  --detach-subnet    launch attached the subnet: release the BM's DHCP reservation
                     on it, then detach it
  --detach-project   launch attached the project: return the BM to the tenant pool
A BM that launch selected but rejected (e.g. already RUNNING) is left untouched.
--delete-key-pair is passed only for a key launch generated (``generated_key``):
the key pair and its private run directory are removed; a supplied key is kept.
Idempotent: an already-released BM succeeds.

Provisioning reserves the BM's subnet address and a deprovision keeps that
reservation, and the API refuses a subnet detach (409) while the BM still holds
one. So when this run attached the subnet, teardown deletes the BM's
reservation on it and waits until it is gone before detaching. A BM the tenant
had already attached keeps both its subnet and its reservation.

Usage:
    python teardown.py --instance-id bm.xxx --deprovision [--detach-subnet] [--detach-project] \
        [--delete-key-pair --key-file /tmp/key]
    python teardown.py --instance-id bm.xxx --skip-destroy

Output JSON:
{
    "success": true,
    "platform": "bm",
    "resources_deleted": ["instance:bm.xxx", "dhcp_reservation:dhcpres.xxx", "subnet_attachment:bm.xxx",
                          "key_pair:/tmp/key"],
    "message": "Teardown completed"
}
"""

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdApiError, FirebirdClient, log, remaining
from common.network import locate_subnet, vpcs_path

# Polls for a deleted reservation to leave the listing (and its interval, seconds).
RESERVATION_GONE_POLLS = 24
RESERVATION_POLL_SECONDS = 5
# Subnet-detach attempts while the API still answers 409, and the first backoff (doubles).
DETACH_ATTEMPTS = 4
DETACH_BACKOFF_SECONDS = 10


def _bm_reservations(client: FirebirdClient, path: str, bm_id: str) -> list[dict[str, Any]]:
    """Return the DHCP reservations at ``path`` that belong to ``bm_id``."""
    return [r for r in client.paginate(path, "reservations") if r.get("bmId") == bm_id]


def release_reservation(client: FirebirdClient, bm_id: str, subnet_id: str, deadline: float) -> list[str]:
    """Delete the BM's DHCP reservation on its subnet and wait until it is gone.

    Returns the IDs of the reservations deleted ([] when there was none). A
    subnet the project cannot resolve, or a reservation listing that answers
    404, is skipped: the detach that follows reports its own error.
    """
    try:
        vpc, _ = locate_subnet(client, subnet_id)
    except RuntimeError as e:
        log(f"  skipping DHCP reservation release: {e}")
        return []
    path = vpcs_path(client, vpc.get("id", ""), f"/subnets/{quote(subnet_id)}/dhcp/reservations")
    try:
        held = _bm_reservations(client, path, bm_id)
    except FirebirdApiError as e:
        if e.status != 404:
            raise
        log(f"  no DHCP reservation listing for subnet {subnet_id}; skipping release")
        return []
    if not held:
        log(f"  BM {bm_id} holds no DHCP reservation on subnet {subnet_id}")
        return []

    log(f"  releasing DHCP reservation of {bm_id} on subnet {subnet_id}")
    try:
        response = client.request("DELETE", path, params={"bm_id": bm_id})
        client.wait_operation(response.get("operation") or {}, remaining(deadline))
    except FirebirdApiError as e:
        if e.status != 404:
            raise
    for _ in range(RESERVATION_GONE_POLLS):
        if not _bm_reservations(client, path, bm_id):
            return [r.get("id", "") for r in held]
        time.sleep(RESERVATION_POLL_SECONDS)
    raise FirebirdApiError(
        f"DHCP reservation of {bm_id} on subnet {subnet_id} still listed "
        f"{RESERVATION_GONE_POLLS * RESERVATION_POLL_SECONDS}s after its delete completed"
    )


def detach_subnet(client: FirebirdClient, bm_id: str, deadline: float) -> None:
    """Detach the BM from its subnet, retrying while the API answers 409 (conflict).

    A reservation release can take a moment to reach the check the detach makes,
    so a 409 is retried with backoff; the last 409 is raised with its API message.
    """
    delay = DETACH_BACKOFF_SECONDS
    for attempt in range(1, DETACH_ATTEMPTS + 1):
        try:
            client.bm_action(bm_id, "detach-subnet", timeout=remaining(deadline))
            return
        except FirebirdApiError as e:
            if e.status != 409 or attempt == DETACH_ATTEMPTS:
                raise
            log(f"  detach-subnet refused ({e}); retrying in {delay}s")
        time.sleep(delay)
        delay *= 2


def main() -> int:
    """Deprovision/detach what launch acquired and delete the local SSH key.

    Returns:
        0 on success, 1 on failure
    """
    parser = argparse.ArgumentParser(description="Teardown Firebird BM")
    parser.add_argument("--instance-id", default="", help="BM ID (bm.ULID)")
    parser.add_argument("--deprovision", action="store_true", help="Deprovision (wipe) the BM")
    parser.add_argument("--detach-subnet", action="store_true", help="Detach the BM from its subnet")
    parser.add_argument("--detach-project", action="store_true", help="Detach the BM from the project")
    parser.add_argument(
        "--delete-key-pair", action="store_true", help="Delete the SSH key pair launch generated, and its directory"
    )
    parser.add_argument("--key-file", help="SSH private key path (with --delete-key-pair)")
    parser.add_argument("--skip-destroy", action="store_true", help="Skip teardown")
    parser.add_argument("--timeout", type=int, default=4700, help="Overall timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bm",
        "resources_deleted": [],
    }

    if args.skip_destroy:
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = (
            f"Teardown skipped (BM_SKIP_TEARDOWN=true). BM {args.instance_id} is still provisioned; "
            "rerun with BM_INSTANCE_ID/BM_KEY_FILE and without BM_SKIP_TEARDOWN to release it."
        )
        result["message"] = result["skip_reason"]
        print(json.dumps(result, indent=2))
        return 0

    deadline = time.monotonic() + args.timeout
    try:
        bm: dict[str, Any] = {}
        client = None
        if args.instance_id and (args.deprovision or args.detach_subnet or args.detach_project):
            client = FirebirdClient()
            try:
                bm = client.get_bm(args.instance_id)
            except FirebirdApiError as e:
                if e.status != 404:
                    raise
                print(f"  BM {args.instance_id} not in project (already released)", file=sys.stderr)

        if client and bm and args.deprovision and bm.get("state") not in ("AVAILABLE", "ALLOCATED"):
            print(f"Deprovisioning BM {args.instance_id}...", file=sys.stderr)
            client.bm_action(args.instance_id, "deprovision", {"force": False}, timeout=remaining(deadline))
            bm = client.wait_bm(args.instance_id, ("AVAILABLE",), remaining(deadline))
            result["resources_deleted"].append(f"instance:{args.instance_id}")

        if client and bm and args.detach_subnet and bm.get("subnetId"):
            # Only a BM with no OS loses its address; a running one is left for detach to refuse.
            if bm.get("state") in ("AVAILABLE", "ALLOCATED"):
                for reservation_id in release_reservation(client, args.instance_id, bm["subnetId"], deadline):
                    result["resources_deleted"].append(f"dhcp_reservation:{reservation_id}")
            print("Detaching BM from subnet...", file=sys.stderr)
            detach_subnet(client, args.instance_id, deadline)
            bm = client.wait_bm(args.instance_id, ("AVAILABLE", "ALLOCATED"), remaining(deadline), subnet="")
            result["resources_deleted"].append(f"subnet_attachment:{args.instance_id}")

        if client and bm and args.detach_project:
            print("Detaching BM from project...", file=sys.stderr)
            client.request("POST", client.bm_path(args.instance_id, "/detach"), {})
            result["resources_deleted"].append(f"project_attachment:{args.instance_id}")

        if args.delete_key_pair and args.key_file:
            for path in (Path(args.key_file), Path(f"{args.key_file}.pub")):
                path.unlink(missing_ok=True)
            # The generated key's mkdtemp directory; left alone if anything else is in it.
            with contextlib.suppress(OSError):
                Path(args.key_file).parent.rmdir()
            result["resources_deleted"].append(f"key_pair:{args.key_file}")

        result["message"] = "Teardown completed"
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        print(f"ERROR: {e}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
