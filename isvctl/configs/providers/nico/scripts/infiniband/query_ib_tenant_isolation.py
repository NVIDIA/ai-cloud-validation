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

"""Query InfiniBand partitions for tenant-isolation evidence (SDN04-04).

NICo isolates InfiniBand compute with the native P_Key partition mechanism: a
tenant's IB interfaces are bound to a tenant-owned ``IbPartition``, each backed
by a single P_Key that the subnet manager enforces. Ports that do not share a
P_Key cannot exchange InfiniBand traffic, regardless of physical connectivity,
so per-tenant-P_Key partitioning *is* the isolation boundary.

This script lists the site's InfiniBand partitions and reports, per partition,
the P_Key, owning tenant, and status. ``IbTenantIsolationCheck`` then asserts
that every partition is tenant-scoped, no P_Key is shared across tenants, and
no tenant partition rides the all-ports default management partition.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/infiniband-partition?siteId={site_id}

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_SSA_ISSUER,
    NICO_CLIENT_ID, NICO_CLIENT_SECRET, and optional NICO_OIDC_SCOPE.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "partitions_checked": 2,
    "partitions": [
      {
        "name": "turbo-net",
        "partition_key": "0x1",
        "tenant_id": "f97df110-f4de-492e-8849-4a6af68026b0",
        "status": "Ready"
      }
    ]
  }

A site with no InfiniBand partitions emits a structured skip
(``skipped`` / ``skip_reason``) so the validation does not hard-fail a fabric
that has no tenant partitions provisioned yet.

Usage:
    NICO_BEARER_TOKEN=<token> python query_ib_tenant_isolation.py \
        --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    infra-controller docs/manuals/networking/infiniband_partitioning.md
    OpenAPI spec: InfiniBandPartition schema (partitionKey / tenantId / status)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, resolve_auth


def partition_record(partition: dict[str, Any]) -> dict[str, Any]:
    """Reduce a NICo InfiniBand partition to the provider-neutral isolation fields."""
    return {
        "name": partition.get("name") or partition.get("partitionName") or "",
        "partition_key": partition.get("partitionKey"),
        "tenant_id": partition.get("tenantId") or "",
        "status": partition.get("status", "Unknown"),
    }


def main() -> int:
    """Query NICo InfiniBand partitions and print tenant-isolation JSON to stdout."""
    parser = argparse.ArgumentParser(description="Query InfiniBand tenant isolation on a NICo site")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "partitions_checked": 0,
        "partitions": [],
    }

    try:
        auth = resolve_auth()

        partitions = forge_get_all(
            args.org,
            "infiniband-partition",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="infinibandPartitions",
        )

        if not partitions:
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = (
                "No InfiniBand partitions found at site; InfiniBand may not be configured "
                "or no tenant partitions are provisioned"
            )
            print(json.dumps(result, indent=2))
            return 0

        result["partitions"] = [partition_record(p) for p in partitions]
        result["partitions_checked"] = len(result["partitions"])
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
