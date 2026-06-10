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

"""Report InfiniBand security-key configuration for a NICo site (SDN04-05).

SDN04 requires the InfiniBand fabric to be hardened with the partition key plus
the OpenSM / SHARP subnet-manager keys. The evidence is split across two
sources, and this script gathers what each authoritatively exposes:

- **P_Key** -- read from NICo's InfiniBand partition API. Every NICo-managed
  partition is allocated a P_Key from the configured pool, so a partition that
  carries a (non-default) P_Key proves the partition-key mechanism is live.
- **Management Key (M_Key)** -- read from UFM's ``/app/smconf`` endpoint (the
  same source NICo's IbFabricMonitor uses), when UFM access is configured. The
  key is "configured" when ``m_key`` is non-zero and ``m_key_per_port`` is
  enabled. The secret value is never emitted -- only the derived posture.

The remaining keys (Aggregation Management / SHARP, VendorSpecific, Congestion
Control, Node2Node, Manager2Node) are OpenSM / SHARP settings configured on the
UFM host (see the IB runbook) and are not surfaced by the UFM REST API; they are
reported as ``configured: null`` (unverified) with a pointer to the runbook so
the validation neither fabricates a pass nor hard-fails for them.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/infiniband-partition?siteId={site_id}

UFM endpoint used (optional, when UFM_ADDRESS + credentials are set):
  GET /ufmRest(V3)/app/smconf

Auth:
  - NICo: NICO_BEARER_TOKEN, or OIDC client_credentials.
  - UFM (optional): UFM_ADDRESS + UFM_TOKEN, or UFM_USERNAME / UFM_PASSWORD.
    Set UFM_ALLOW_INSECURE=1 for UFM's self-signed certificate.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "partitions_with_pkey": 2,
    "keys": {
      "p_key":            {"configured": true,  "source": "nico", "detail": "..."},
      "management_key":   {"configured": true,  "source": "ufm",  "detail": "..."},
      "aggregation_management_key": {"configured": null, "source": "ufm-host", "detail": "..."},
      ...
    }
  }

Usage:
    NICO_BEARER_TOKEN=<token> UFM_ADDRESS=<url> UFM_TOKEN=<token> \
        python query_ib_keys.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    infra-controller crates/ib-fabric/src/lib.rs (insecure_fabric_configuration)
    infra-controller docs/playbooks/ib_runbook.md (M_Key / CC_Key / N2N_Key / VS_Key)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get_all, resolve_auth
from common.ufm_client import (
    UfmAuthError,
    describe_http_error,
    describe_url_error,
    get_sm_config,
    parse_key_value,
    resolve_ufm_auth,
    ufm_configured,
)

# UFM's default partition spans every port; its P_Key is not a tenant key.
DEFAULT_PARTITION_PKEY: int = 0x7FFF

# A P_Key's low 15 bits are the partition number; the top bit (0x8000) is the
# membership type. Mask it off so the all-ports default is recognized whether it
# appears as 0x7fff (limited) or 0xffff (full member).
PKEY_BASE_MASK: int = 0x7FFF

# Subnet-manager / SHARP keys that are configured on the UFM host (per the IB
# runbook) but are not exposed by the UFM REST API, so the script cannot observe
# them. Reported as unverified rather than guessed.
UFM_HOST_KEYS: dict[str, str] = {
    "aggregation_management_key": "SHARP Aggregation Management Key (AM_Key)",
    "vendor_specific_key": "VendorSpecific Key (vs_key_enable)",
    "congestion_control_key": "Congestion Control Key (cc_key_enable)",
    "node2node_key": "Node2Node Key (n2n_key_enable)",
    "manager2node_key": "Manager2Node Key (SHARP)",
}


def _pkey_is_tenant_key(value: Any) -> bool:
    """Return whether a partition's P_Key is a real, non-default tenant key."""
    pkey = parse_key_value(value)
    if pkey is None:
        return False
    # Compare partition numbers (membership bit masked) so a full-member default
    # P_Key (0xffff) is excluded just like the limited-member default (0x7fff).
    return (pkey & PKEY_BASE_MASK) != (DEFAULT_PARTITION_PKEY & PKEY_BASE_MASK)


def _management_key_entry() -> dict[str, Any]:
    """Resolve the Management Key (M_Key) posture from UFM, when configured."""
    if not ufm_configured():
        return {
            "configured": None,
            "source": "ufm",
            "detail": "UFM access not configured (set UFM_ADDRESS and UFM_TOKEN to verify the Management Key)",
        }

    try:
        auth = resolve_ufm_auth()
        smconf = get_sm_config(auth)
    except UfmAuthError as e:
        return {"configured": None, "source": "ufm", "detail": f"UFM query failed: {e}"}
    except HTTPError as e:
        return {"configured": None, "source": "ufm", "detail": f"UFM query failed: {describe_http_error(e)}"}
    except URLError as e:
        return {"configured": None, "source": "ufm", "detail": f"UFM query failed: {describe_url_error(e)}"}

    m_key = parse_key_value(smconf.get("m_key"))
    per_port = bool(smconf.get("m_key_per_port"))

    # Mirror NICo's own secure-fabric definition: a configured Management Key is
    # a non-zero m_key protected per-port. The secret value is never emitted.
    if m_key in (None, 0):
        return {"configured": False, "source": "ufm", "detail": "m_key is unset (0)"}
    if not per_port:
        return {"configured": False, "source": "ufm", "detail": "m_key set but m_key_per_port protection is disabled"}
    return {"configured": True, "source": "ufm", "detail": "m_key configured with per-port protection"}


def main() -> int:
    """Gather InfiniBand key configuration and print the JSON contract to stdout."""
    parser = argparse.ArgumentParser(description="Report InfiniBand security-key configuration on a NICo site")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "partitions_with_pkey": 0,
        "keys": {},
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
                "No InfiniBand partitions found at site; cannot evidence the P_Key. "
                "InfiniBand may not be configured or no tenant partitions are provisioned"
            )
            print(json.dumps(result, indent=2))
            return 0

        partitions_with_pkey = sum(1 for p in partitions if _pkey_is_tenant_key(p.get("partitionKey")))
        result["partitions_with_pkey"] = partitions_with_pkey

        keys: dict[str, dict[str, Any]] = {
            "p_key": {
                "configured": partitions_with_pkey >= 1,
                "source": "nico",
                "detail": f"{partitions_with_pkey} partition(s) carry a P_Key",
            },
            "management_key": _management_key_entry(),
        }
        for name, label in UFM_HOST_KEYS.items():
            keys[name] = {
                "configured": None,
                "source": "ufm-host",
                "detail": f"{label} is configured on the UFM host per the IB runbook; not exposed by the UFM REST API",
            }

        result["keys"] = keys
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
