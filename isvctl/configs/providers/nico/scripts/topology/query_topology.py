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

"""Query physical topology / failure-domain observability for a NICo site (STG05-01).

Physical diversity decisions (spreading a tenant's storage or compute across
independent failure domains) require that the platform expose which failure
domain each host belongs to. In NICo a Rack is "a physical enclosure that
contains a number of Machines ... the physical building blocks of a Site", and
each ingested Machine carries its rack identity as a well-known label. That rack
identifier is the provider-neutral failure domain.

This script reads each machine's labels, extracts the rack/failure-domain
identifier, and emits a provider-neutral per-host record.
``FailureDomainObservabilityCheck`` then asserts the topology is observable
(every host maps to a named failure domain) and that the site exposes enough
distinct domains for the configured diversity floor.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/machine?siteId={site_id}

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_SSA_ISSUER,
    NICO_CLIENT_ID, NICO_CLIENT_SECRET, and optional NICO_OIDC_SCOPE.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "hosts_checked": 2,
    "hosts": [
      {"host_id": "...", "failure_domain": "GVX11F01C02"}
    ]
  }

A site with no ingested machines emits a structured skip (``skipped`` /
``skip_reason``) so the validation does not hard-fail a site with no hardware
discovered yet.

Usage:
    NICO_BEARER_TOKEN=<token> python query_topology.py \
        --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    OpenAPI spec: rest-api/openapi/spec.yaml (Machine.labels; Rack tag / well-known
      location.* + rack labels described under Expected Rack)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.inventory import first_string
from common.nico_client import NicoAuthError, forge_get_all, resolve_auth

# Machine label keys that carry the rack (failure-domain) identity, in priority
# order. NICo populates ``RackIdentifier`` on ingested machines; expected-machine
# manifests use the shorter ``rack`` label.
RACK_LABEL_KEYS = ("RackIdentifier", "rack")


def failure_domain(machine: dict[str, Any]) -> str:
    """Return the machine's rack/failure-domain identifier, or an empty string."""
    labels = machine.get("labels") or {}
    if not isinstance(labels, dict):
        return ""
    return first_string(labels, *RACK_LABEL_KEYS)


def host_record(machine: dict[str, Any]) -> dict[str, Any]:
    """Build the provider-neutral topology record for one NICo machine."""
    return {
        "host_id": machine.get("id", ""),
        "failure_domain": failure_domain(machine),
    }


def main() -> int:
    """Query NICo machines and print per-host failure-domain topology JSON."""
    parser = argparse.ArgumentParser(description="Query NICo failure-domain topology")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "hosts_checked": 0,
        "hosts": [],
    }

    try:
        auth = resolve_auth()

        machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="machines",
        )

        if not machines:
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No machines found at site; no hosts to report failure-domain topology for"
            print(json.dumps(result, indent=2))
            return 0

        result["hosts"] = [host_record(machine) for machine in machines]
        result["hosts_checked"] = len(result["hosts"])
        result["success"] = True

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
