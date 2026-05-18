#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Probe Compute Engine serial console output.

Compute Engine has NO account-level toggle equivalent to AWS's
``get_serial_console_access_status`` — access is gated by IAM. Both
``console_available`` and ``serial_access_enabled`` are derived from a
single ``getSerialPortOutput`` permission probe:

  * ``serial_access_enabled``: True iff the call succeeded under the
    active credentials (False on PermissionDenied / Unauthenticated).
  * ``console_available``: True iff returned ``contents`` are non-empty.

Validator-consumed fields must derive from a real signal: no field is
hardcoded; both flip to False on real permission/auth failures so the
validator sees the actual platform state on this run. The AWS oracle requires step success only when at
least one probe yielded a usable result, so this stub matches that
contract: ``success = console_available or serial_access_enabled``.
Permission/auth denials with no readable output exit rc=1 with a
structured error so the orchestrator sees an honest failure rather than
a green step on a denied probe.

This stub uses the explicit ``GetSerialPortOutputInstanceRequest``
Request object rather than the flattened kwarg form, because the SDK
does NOT accept ``port=`` as a flattened kwarg.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    narrow_region_to_zone,
    resolve_project,
)
from common.errors import handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import compute_v1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Compute Engine serial console")
    parser.add_argument("--instance-id", required=True, help="Instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument(
        "--port",
        type=int,
        default=1,
        help="Serial port (1..4); 1 is the default boot console",
    )
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "console_available": False,
        "serial_access_enabled": False,
        "output_length": 0,
        "region": args.region,
        "zone": zone,
        "project": project,
    }

    try:
        client = compute_v1.InstancesClient()
        request = compute_v1.GetSerialPortOutputInstanceRequest(
            project=project,
            zone=zone,
            instance=args.instance_id,
            port=args.port,
        )
        try:
            response = client.get_serial_port_output(request=request)
            result["serial_access_enabled"] = True
            contents = response.contents or ""
            result["output_length"] = len(contents)
            if contents:
                result["console_available"] = True
                result["output_snippet"] = contents[-500:] if len(contents) > 500 else contents
        except (gax.PermissionDenied, gax.Unauthenticated) as e:
            result["serial_access_enabled"] = False
            result["error"] = f"Serial console access denied: {e}"
        except gax.NotFound as e:
            result["error"] = f"Instance not found: {e}"

        # Per the AWS oracle: step succeeds only when at least one probe
        # returned a usable result. A denied probe with no readable output
        # is an honest failure for the validator to see, not a green step.
        result["success"] = bool(result["console_available"] or result["serial_access_enabled"])

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
