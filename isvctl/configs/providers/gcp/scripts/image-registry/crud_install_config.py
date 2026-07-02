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

"""CRUD an OS install configuration using a Compute Engine instance template.

Self-contained test that models the AWS oracle's EC2 Launch Template lifecycle
on Compute Engine's closest analog, the **instance template**
(``InstanceTemplatesClient``: insert / get / list / delete). Instance templates
are **immutable** — there is no in-place update — so the reference's UPDATE
operation is realized as create-replacement: a new template registered
alongside the original.

  * CREATE — ``InstanceTemplatesClient.insert`` registers the base template.
  * READ   — ``InstanceTemplatesClient.get`` reads it back and verifies shape.
  * UPDATE — register a REPLACEMENT template (immutability adaptation), not an
             in-place mutation.
  * DELETE — delete every template this step created (base + replacement).

The suite ``install_config_crud`` group runs only ``StepSuccessCheck`` +
``FieldExistsCheck`` (config_id, config_name, operations) — there is no
``CrudOperationsCheck`` here, so the operations dict is presence-checked, not
gated on specific operation keys. The four keys mirror the oracle for parity.

Required JSON output:
    {
        "success":     bool,
        "platform":    "image_registry",
        "config_id":   str,            # base instance template name
        "config_name": str,
        "operations": {
            "create": {"passed": bool, ...},
            "read":   {"passed": bool, ...},
            "update": {"passed": bool, ...},
            "delete": {"passed": bool, ...},
        },
    }

Usage:
    python crud_install_config.py --region <region>

AWS reference implementation:
    ../../aws/scripts/image-registry/crud_install_config.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix, wait_for_global_op
from common.errors import handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import compute_v1

# A stable public image for the template's boot disk. The template is never
# instantiated (CRUD only), so a small always-available non-GPU image keeps the
# test fast and quota-free.
_TEMPLATE_SOURCE_IMAGE = "projects/debian-cloud/global/images/family/debian-12"


def _build_template(name: str, machine_type: str) -> compute_v1.InstanceTemplate:
    """Build a minimal valid InstanceTemplate (global resource; bare machine type)."""
    props = compute_v1.InstanceProperties()
    props.machine_type = machine_type

    boot = compute_v1.AttachedDisk()
    boot.boot = True
    boot.auto_delete = True
    init = compute_v1.AttachedDiskInitializeParams()
    init.source_image = _TEMPLATE_SOURCE_IMAGE
    init.disk_size_gb = 20
    boot.initialize_params = init
    props.disks = [boot]

    nic = compute_v1.NetworkInterface()
    nic.network = "global/networks/default"
    props.network_interfaces = [nic]

    template = compute_v1.InstanceTemplate()
    template.name = name
    template.description = "ISV image-registry install-config template (createdby=isvtest)"
    template.properties = props
    return template


def _insert_template(
    client: compute_v1.InstanceTemplatesClient,
    project: str,
    name: str,
    machine_type: str,
    on_accepted: Callable[[], None] | None = None,
) -> None:
    """Insert an instance template and block on the global op until DONE.

    ``on_accepted`` (if given) fires the instant ``insert`` returns an accepted
    operation -- i.e. this step now owns ``name`` -- BEFORE the op-wait, so a
    wait-side timeout still leaves the caller tracking the template for cleanup
    (NotFound is idempotent if the insert never materialized it). A synchronous
    AlreadyExists/Conflict raised by ``insert`` propagates BEFORE ``on_accepted``
    fires, so the caller never claims ownership of a pre-existing template.
    """
    op = client.insert(project=project, instance_template_resource=_build_template(name, machine_type))
    if on_accepted is not None:
        on_accepted()
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=120)


def _delete_template(client: compute_v1.InstanceTemplatesClient, project: str, name: str) -> bool:
    """Delete an instance template; NotFound is idempotent success."""
    try:
        op = client.delete(project=project, instance_template=name)
        op_name = getattr(op, "name", "")
        if op_name:
            wait_for_global_op(project, op_name, timeout=120)
        return True
    except gax.NotFound:
        return True
    except gax.GoogleAPICallError as e:
        print(f"  warn: template delete {name} raised: {e}", file=sys.stderr)
        return False


@handle_gcp_errors
def main() -> int:
    """Run the create/read/update(create-replacement)/delete lifecycle on a template."""
    parser = argparse.ArgumentParser(description="CRUD OS install config (Compute Engine instance template)")
    parser.add_argument("--region", required=True, help="GCP region (templates are global; contract parity)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--machine-type", default="e2-standard-2", help="Template machine type (bare name)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    client = compute_v1.InstanceTemplatesClient()

    base_name = unique_suffix("isv-ir-config")
    replacement_name = unique_suffix("isv-ir-config-v2")

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "config_id": base_name,
        "config_name": base_name,
        "operations": {
            "create": {"passed": False},
            "read": {"passed": False},
            "update": {"passed": False},
            "delete": {"passed": False},
        },
    }

    created: list[str] = []
    try:
        # CREATE — claim ownership of base_name ONLY once the insert ack proves
        # this step created it (on_accepted), never before the call. The name is
        # reserved server-side on the insert ack, so stamping there -- before the
        # op-wait -- still lets cleanup-on-failure delete it on a wait-side
        # failure (DONE-with-errors / timeout); NotFound is idempotent if the
        # insert never materialized it. A synchronous AlreadyExists/Conflict
        # raises before on_accepted, so a name collision with a pre-existing
        # template is reported as a failure and never deleted by this step.
        _insert_template(client, project, base_name, args.machine_type, on_accepted=lambda: created.append(base_name))
        result["operations"]["create"] = {"passed": True, "config_id": base_name, "message": f"Created {base_name}"}

        # READ
        fetched = client.get(project=project, instance_template=base_name)
        read_machine_type = fetched.properties.machine_type
        result["operations"]["read"] = {
            "passed": fetched.name == base_name,
            "config_name": fetched.name,
            "machine_type": read_machine_type,
            "message": f"Read template {fetched.name}: machine_type={read_machine_type}",
        }
        if not result["operations"]["read"]["passed"]:
            raise RuntimeError("read-back name mismatch")

        # UPDATE — instance templates are immutable; register a replacement.
        # Same accept-then-track rule: claim ownership of replacement_name only
        # on its insert ack, before the op-wait.
        _insert_template(
            client, project, replacement_name, "e2-standard-4", on_accepted=lambda: created.append(replacement_name)
        )
        result["operations"]["update"] = {
            "passed": True,
            "replacement_config_id": replacement_name,
            "message": f"Registered replacement template {replacement_name} (immutable create-replacement)",
        }

        # DELETE — remove every template this step created. Attempt EVERY
        # delete before judging success: `all(... for ...)` short-circuits, so
        # a transient failure on the base template would skip the replacement
        # and strand it (shared teardown never receives these per-step template
        # IDs). Collect each result first, then AND the materialized list.
        delete_results = [_delete_template(client, project, name) for name in created]
        delete_ok = all(delete_results)
        result["operations"]["delete"] = {
            "passed": delete_ok,
            "deleted": created,
            "message": f"Deleted templates {created}",
        }
        if not delete_ok:
            raise RuntimeError("one or more template deletes failed")
        created = []

        result["success"] = True
    except (gax.GoogleAPICallError, RuntimeError, TimeoutError) as e:
        result["error"] = str(e)
        # Cleanup on partial failure so the test leaves no template behind.
        for name in created:
            _delete_template(client, project, name)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
