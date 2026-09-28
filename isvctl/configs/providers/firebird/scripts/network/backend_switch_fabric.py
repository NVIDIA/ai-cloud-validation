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

"""Backend switch fabric of a BM from the Firebird topology API (NET01-01).

Reads ``GET /topology/nodes/{node_id}`` - a flat route at the API root, not under
``/api/v1``, with the same bearer token and snake_case JSON. Each populated tier
(``tiers.leaf|spine|core``) carries one equivalence-class switch ID. The node is
--node-id (the provisioned BM when configured), otherwise the first BM the
tenant's topology lists (``GET /topology/nodes?node_kind=bm``). A missing tier
fails its subtest.

Usage:
    python backend_switch_fabric.py [--node-id bm.xxx]

Output JSON:
{
    "success": true,
    "platform": "network",
    "test_name": "backend_switch_fabric",
    "node_id": "bm.xxx",
    "fabric": {"leaf_switch_ids": ["sw.a"], "spine_switch_ids": ["sw.b"], "core_switch_ids": ["sw.c"]},
    "tests": {"node_resolved": {"passed": true}, "leaf_switch_ids_present": {"passed": true},
              "spine_switch_ids_present": {"passed": true}, "core_switch_ids_present": {"passed": true}}
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import FirebirdClient, log

TIERS = ("leaf", "spine", "core")


def main() -> int:
    """Resolve the node's topology and emit the NET01-01 contract.

    Returns:
        0 when every tier is reported, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird backend switch fabric")
    parser.add_argument("--node-id", default="", help="BM to inspect (bm.ULID); default: first listed BM")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    fabric: dict[str, list[str]] = {f"{tier}_switch_ids": [] for tier in TIERS}
    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "backend_switch_fabric",
        "node_id": args.node_id,
        "fabric": fabric,
        "tests": tests,
    }
    try:
        client = FirebirdClient()
        node_id = args.node_id
        if not node_id:
            listed = client.request("GET", "/topology/nodes", params={"node_kind": "bm", "page_size": 1}, prefix="")
            node_id = next((n.get("node_id") for n in listed.get("nodes") or []), "")
            if not node_id:
                raise RuntimeError("the topology API lists no BM for this tenant")
        result["node_id"] = node_id
        node = client.request("GET", f"/topology/nodes/{quote(node_id)}", prefix="")
        tests["node_resolved"] = {"passed": node.get("node_id") == node_id, "status": node.get("topology_status")}
        for tier in TIERS:
            switch_id = ((node.get("tiers") or {}).get(tier) or {}).get("id")
            fabric[f"{tier}_switch_ids"] = [switch_id] if switch_id else []
            tests[f"{tier}_switch_ids_present"] = {"passed": bool(switch_id)}
            if not switch_id:
                tests[f"{tier}_switch_ids_present"]["error"] = f"no {tier} tier in the node's topology"
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    for key in ("node_resolved", *(f"{tier}_switch_ids_present" for tier in TIERS)):
        tests.setdefault(key, {"passed": False, "error": result.get("error", "not run")})
    result["success"] = all(t["passed"] for t in tests.values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
