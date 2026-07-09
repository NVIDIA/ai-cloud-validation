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

"""Tear down the observability VPC Flow Logs configuration (teardown phase).

Translates the AWS oracle's ``teardown_vpc_flow_logs`` (delete the standalone
Flow Log + CloudWatch group + IAM role/policy) onto Compute Engine, where flow
logging is embedded in ``Subnetwork.log_config`` and has no standalone
log-group / IAM-role resource. When ``--flow-logs-created`` is true, this step
disables logging on ONLY the exact ``--subnet-ids`` allowlist forwarded from
``enable_vpc_flow_logs`` (its ``patched_flow_log_subnets`` — the subnets THIS run
actually patched), NEVER the observed diagnostic list. An operator-owned or
other-run subnet already logging on an adopted VPC is therefore never disabled.
Otherwise this step is a successful no-op.

Each disable is a fingerprint-guarded ``subnetworks.patch`` (waited to DONE);
``NotFound`` is idempotent success (an already-deleted subnet has no logging to
disable). The final ``success`` is the AND of every per-subnet result.
``--skip-destroy`` short-circuits to success BEFORE resolving the project.

Emits:
    {"success": bool, "platform": "observability", "resources_destroyed": bool,
     "resources_deleted": [str, ...], "message": str, "error": str?}

AWS reference implementation:
    ../../aws/scripts/observability/teardown_vpc_flow_logs.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import patch_subnetwork_flow_logs

_FALSY_SENTINELS = {"", "none", "null", "false"}


def _truthy(arg: str | None) -> bool:
    """Per-arg sentinel check: '' / 'none' / 'null' / 'false' are falsy."""
    if arg is None:
        return False
    return arg.strip().lower() not in _FALSY_SENTINELS


def _split_ids(raw: str | None) -> list[str]:
    """Split a comma-separated id arg, dropping falsy sentinels."""
    return [t.strip() for t in (raw or "").split(",") if t.strip() and t.strip().lower() not in _FALSY_SENTINELS]


@handle_gcp_errors
def main() -> int:
    """Disable VPC Flow Logs on the run-patched subnets and emit structured JSON."""
    parser = argparse.ArgumentParser(description="Teardown the GCP observability VPC Flow Logs configuration")
    parser.add_argument("--region", required=True, help="GCP region containing the patched subnetworks")
    parser.add_argument(
        "--subnet-ids",
        default="none",
        help="Comma-separated subnets THIS run patched (enable_vpc_flow_logs.patched_flow_log_subnets)",
    )
    parser.add_argument(
        "--flow-logs-created", default="false", help="Bool sentinel from enable_vpc_flow_logs.flow_logs_created"
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve the flow-log config (short-circuit)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "teardown_flow_logs",
        "resources_destroyed": False,
        "resources_deleted": [],
        "message": "",
    }

    # Preservation-mode short-circuits BEFORE any auth / client construction.
    if args.skip_destroy:
        result["success"] = True
        result["message"] = "Teardown skipped (--skip-destroy / GCP_OBSERVABILITY_SKIP_TEARDOWN=true)."
        print(json.dumps(result, indent=2))
        return 0

    flow_logs_created = _truthy(args.flow_logs_created)
    subnet_ids = _split_ids(args.subnet_ids)

    # Nothing this run enabled -> successful no-op. NEVER disable logging on the
    # observed diagnostic list (which can include operator-owned subnets).
    if not flow_logs_created or not subnet_ids:
        result["success"] = True
        result["message"] = "No run-owned VPC Flow Logs to disable (flow_logs_created=false or no subnets tracked)."
        print(json.dumps(result, indent=2))
        return 0

    project = resolve_project(args.project)

    all_ok = True
    for subnet_name in subnet_ids:
        print(f"Disabling VPC Flow Logs on run-patched subnet {subnet_name}...", file=sys.stderr)
        # delete_with_retry gives idempotent NotFound-as-success + bounded retry;
        # patch_subnetwork_flow_logs(enable=False) is the disable operation.
        ok = delete_with_retry(
            patch_subnetwork_flow_logs,
            project,
            args.region,
            subnet_name,
            enable=False,
            resource_desc=f"flow logs on subnet {subnet_name}",
        )
        if ok:
            result["resources_deleted"].append(f"flow_logs:{subnet_name}")
        else:
            all_ok = False

    result["success"] = all_ok
    result["resources_destroyed"] = all_ok
    if all_ok:
        result["message"] = f"Disabled VPC Flow Logs on {len(result['resources_deleted'])} run-patched subnet(s)"
    else:
        result["message"] = "One or more VPC Flow Log disable operations failed"
        result["error"] = "VPC Flow Log teardown failed"

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
