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

"""Verify CNP10-01 BMC protocol posture for GCP tenant environments.

Managed Compute Engine does not expose customer-accessible IPMI or Redfish BMC
endpoints; the BMC protocol attack surface is owned by the managed
infrastructure plane rather than the customer project or instance network. This
mirrors the AWS reference, which confirms valid credentials (STS
GetCallerIdentity) and then attests that no customer BMC protocol surface
exists.

The check confirms a valid, reachable GCP environment by reading the operator
project (resourcemanager_v3 ProjectsClient.get_project) — the identity probe
that gates the whole result — then emits the six CNP10-01 subtests with
bmc_protocol_surface="none" and bmc_endpoints_tested=0. If the identity probe
fails, every subtest fails, bmc_protocol_surface is "unknown", and the run
exits non-zero.

Usage:
    python3 bmc_protocol_security_test.py --region us-central1 --project=my-project

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "bmc_protocol_security",
    "bmc_endpoints_tested": 0,
    "bmc_protocol_surface": "none",
    "tests": { ...six subtests... }
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.cloud import resourcemanager_v3

# Subtests emitted by this check, in contract order.
_SUBTESTS = (
    "ipmi_disabled",
    "redfish_tls_enabled",
    "redfish_plain_http_disabled",
    "redfish_authentication_required",
    "redfish_authorization_enforced",
    "redfish_accounting_enabled",
)


@handle_gcp_errors
def main() -> int:
    """Emit GCP CNP10-01 BMC protocol posture evidence."""
    parser = argparse.ArgumentParser(description="BMC protocol security test (GCP)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "bmc_protocol_security",
        "bmc_endpoints_tested": 0,
        "bmc_protocol_surface": "unknown",
        "tests": {
            "ipmi_disabled": {"passed": False},
            "redfish_tls_enabled": {"passed": False},
            "redfish_plain_http_disabled": {"passed": False},
            "redfish_authentication_required": {"passed": False},
            "redfish_authorization_enforced": {"passed": False},
            "redfish_accounting_enabled": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)

        # Identity probe: a successful get_project confirms a valid, reachable
        # GCP environment and gates the whole result.
        projects_client = resourcemanager_v3.ProjectsClient()
        projects_client.get_project(name=f"projects/{project}")

        # Managed-GCE default: no customer IPMI/Redfish endpoint exists, so the
        # protocol surface is "none" and no endpoints are probed.
        evidence = (
            "Managed Compute Engine exposes no customer-accessible IPMI or "
            "Redfish BMC endpoint; the BMC protocol surface is provider-owned. "
            f"Confirmed a valid GCP environment for project {project}."
        )
        result["bmc_protocol_surface"] = "none"
        for subtest in _SUBTESTS:
            result["tests"][subtest] = {
                "passed": True,
                "provider_hidden": True,
                "evidence": evidence,
            }

        result["success"] = all(test.get("passed") for test in result["tests"].values())
    except Exception as e:
        result["error"] = str(e)
        result["bmc_protocol_surface"] = "unknown"
        evidence = f"GCP identity probe failed: {e}"
        for subtest in _SUBTESTS:
            result["tests"][subtest] = {"passed": False, "error": evidence}

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
