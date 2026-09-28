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

"""Select a tenant BM, attach it, and provision an OS image on it.

Shared by bare_metal ``launch_instance`` and image-registry ``install_image_bm``.
Firebird BMs are pre-allocated to the tenant, so "launch" means: pick a BM,
attach it to the project and subnet (if not already), then provision an image
with cloud-init user-data that authorizes an SSH key generated for this run (or
an existing key supplied explicitly).
"""

import sys
from pathlib import Path
from typing import Any

from common.firebird_client import FirebirdApiError, FirebirdClient, remaining, to_state
from common.ssh_utils import generate_key_pair, public_key, user_data_b64, wait_for_cloud_init, wait_for_ssh


def select_bm(client: FirebirdClient, bm_id: str, machine_type: str, subnet_id: str) -> dict[str, Any]:
    """Return ``bm_id`` from the tenant pool, or the first AVAILABLE BM there.

    The tenant-pool listing (not the project-scoped GET) is used so a BM that is
    not yet attached to the project can be selected. Automatic selection only
    considers BMs that are unattached or already on ``subnet_id`` (and in no
    other project), preferring BMs already in the project.

    Args:
        client: Firebird API client
        bm_id: Explicit BM ID, or "" to pick one
        machine_type: Machine-type ID to match, or "" for any
        subnet_id: Subnet the BM will be attached to

    Returns:
        The selected BM resource
    """
    pool = client.paginate("/compute/bms", "items")
    if bm_id:
        match = next((bm for bm in pool if bm.get("id") == bm_id), None)
        if match is None:
            raise RuntimeError(f"BM {bm_id} is not in the tenant pool")
        return match
    candidates = [
        bm
        for bm in pool
        if bm.get("state") == "AVAILABLE"
        and bm.get("projectId") in ("", None, client.project_id)
        and bm.get("subnetId") in ("", None, subnet_id)
        and (not machine_type or bm.get("machineTypeId") == machine_type)
    ]
    if not candidates:
        raise RuntimeError(f"No AVAILABLE BM in the tenant pool (machine type: {machine_type or 'any'})")
    candidates.sort(key=lambda bm: bm.get("projectId") != client.project_id)
    return candidates[0]


def provision_bm(
    client: FirebirdClient,
    result: dict[str, Any],
    *,
    name: str,
    bm_id: str,
    machine_type: str,
    subnet_id: str,
    image_id: str,
    ssh_user: str,
    key_file: str,
    deadline: float,
) -> dict[str, Any]:
    """Provision ``image_id`` on a selected BM, wait for SSH and cloud-init; return the RUNNING BM.

    ``key_file`` is an existing private key to reuse, or "" to generate one for
    this run; a generated key is recorded in ``result`` (``key_file``, ``key_name``,
    ``generated_key: true``) so teardown deletes it and only it.

    ``result`` gets ``instance_id``, ``instance_type``, ``state``, ``public_ip`` /
    ``private_ip``, ``cloud_init_status``, and the ``owned`` / ``attached_project`` / ``attached_subnet``
    flags. The attach flags are set before their request, so teardown undoes
    exactly what this run started, even if the request's outcome is lost.
    ``owned`` is set unless the API refused the provision request (4xx): then
    the BM was never this run's to deprovision (for example, another tenant or
    run took it first).
    """
    bm = select_bm(client, bm_id, machine_type, subnet_id)
    bm_id = bm["id"]
    result["instance_id"] = bm_id
    result["instance_type"] = bm.get("machineTypeId") or machine_type
    print(f"Using BM {bm_id} ({bm.get('name')})", file=sys.stderr)

    if bm.get("state") != "AVAILABLE":
        raise RuntimeError(f"BM {bm_id} is {bm.get('state')}, expected AVAILABLE (already provisioned?)")

    if bm.get("subnetId") not in ("", None, subnet_id):
        raise RuntimeError(f"BM {bm_id} is attached to subnet {bm['subnetId']}, not {subnet_id}")
    if not key_file:
        key_file = generate_key_pair(name)
        result.update(key_file=key_file, key_name=Path(key_file).name, generated_key=True)
        print(f"Generated SSH key {key_file}", file=sys.stderr)
    user_data = user_data_b64(public_key(key_file))

    # Attach to project and subnet only when needed; teardown undoes exactly what we did.
    if bm.get("projectId") != client.project_id:
        print("Attaching BM to project...", file=sys.stderr)
        result["attached_project"] = True
        client.request("POST", client.bm_path(bm_id, "/attach"), {})
    if bm.get("subnetId") != subnet_id:
        print("Attaching BM to subnet...", file=sys.stderr)
        result["attached_subnet"] = True
        client.bm_action(bm_id, "attach-subnet", {"subnetId": subnet_id}, timeout=remaining(deadline))
        client.wait_bm(bm_id, ("AVAILABLE",), remaining(deadline), subnet=subnet_id)

    print(f"Provisioning image {image_id} (bare metal takes a while)...", file=sys.stderr)
    body = {
        "imageId": image_id,
        "userDataB64": user_data,
        # Provision replaces the BM's tags; keep the operator's and add ours.
        "tags": {**(bm.get("tags") or {}), "Name": name, "CreatedBy": "isvctl"},
    }
    try:
        response = client.request("POST", client.bm_path(bm_id, "/provision"), body)
    except Exception as e:
        # A 4xx is a refusal: nothing started, and the BM may now be someone else's.
        # Any other failure (5xx, transport, timeout) leaves the outcome unknown.
        if not (isinstance(e, FirebirdApiError) and 400 <= e.status < 500):
            result["owned"] = True
        raise
    result["owned"] = True
    client.wait_operation(response.get("operation") or {}, remaining(deadline))
    bm = client.wait_bm(bm_id, ("RUNNING",), remaining(deadline), power="ON", need_ip=True)
    result["state"] = to_state(bm)
    result["public_ip"] = bm["ipAddress"]
    result["private_ip"] = bm["ipAddress"]

    print("Waiting for SSH (cloud-init)...", file=sys.stderr)
    if not wait_for_ssh(bm["ipAddress"], ssh_user, key_file, deadline):
        raise RuntimeError(f"SSH to {bm['ipAddress']} not ready after provisioning")
    # sshd starts before cloud-init's final modules finish: report the host ready
    # only once cloud-init is done (its outcome is the cloud-init check's to judge).
    print("Waiting for cloud-init to finish...", file=sys.stderr)
    result["cloud_init_status"] = wait_for_cloud_init(bm["ipAddress"], ssh_user, key_file, deadline)
    return bm
