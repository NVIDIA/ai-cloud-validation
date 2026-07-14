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

"""Report specified-key access to out-of-band components for a NICo site (AUTH-XX-03).

AUTH-XX-03 verifies that a tenant-specified key (an SSH key) can be used to
access other components "as possible", with the serial console (SOL) and
network devices called out as examples.

NICo models a tenant-specified key as an SSH Key, grouped into an SSH Key Group
that is synced down to one or more Sites. A Site exposes a serial console (SOL)
whose access the provider enables (``isSerialConsoleEnabled``) and whose
SSH-key authentication the tenant enables (``isSerialConsoleSSHKeysEnabled``).
When a key group with at least one key is synced to a site whose serial console
is enabled with SSH-key auth, that key grants SOL access to the site's machines.

This script gathers the provider-neutral evidence for that access path:

- **specified keys** -- the SSH keys in the key groups synced to the site. Only
  the count and fingerprint-derived posture are surfaced; key material is never
  emitted.
- **serial console (SOL)** -- per-site serial-console configuration and whether
  the synced key can reach it.
- **network devices** -- reported as ``key_access_enabled: null`` (unverified):
  tenant key access to switches is provider-managed and not exposed by the NICo
  tenant REST API, so it is neither asserted nor falsely passed.

NICo API endpoints used (the ``/carbide/`` segment is the current deployed name
for what newer docs call ``/nico/``; the other NICo scripts use it too):
  GET /{org}/carbide/site/{site_id}
  GET /{org}/carbide/sshkeygroup?siteId={site_id}

Auth:
  - NICO_BEARER_TOKEN, or OIDC client_credentials
    (NICO_SSA_ISSUER / NICO_CLIENT_ID / NICO_CLIENT_SECRET).

When no SSH key group with a key is synced to the site there is nothing to
evidence access with, so the script emits a structured skip (``skipped: true``
+ ``skip_reason``) carrying an ``org_key_groups`` count, distinguishing "no key
groups exist at all" from "key groups exist but none are synced to this site".

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "specified_keys": 1,
    "access_targets": [
      {
        "type": "serial_console",
        "name": "<site> serial console (SOL)",
        "key_access_enabled": true,   // tri-state: true | false | null (unverified)
        "reachable": true,            // endpoint present AND key synced to site
        "detail": "..."
      },
      {
        "type": "network_device",
        "name": "...",
        "key_access_enabled": null,
        "reachable": false,
        "detail": "..."
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> \
        python query_key_access.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    infra-controller rest-api/openapi/spec.yaml (Site, SshKeyGroup schemas)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import (
    NicoAuthError,
    forge_get,
    forge_get_all,
    resolve_auth,
    sshkeygroup_synced_to_site,
)


def _count_specified_keys(groups: list[dict[str, Any]]) -> int:
    """Count distinct SSH keys across the key groups synced to the site."""
    key_ids: set[str] = set()
    keys_without_id = 0
    for group in groups:
        for key in group.get("sshKeys") or []:
            key_id = key.get("id") if isinstance(key, dict) else None
            if isinstance(key_id, str) and key_id:
                key_ids.add(key_id)
            else:
                keys_without_id += 1
    return len(key_ids) + keys_without_id


def _keys_synced_to_site(groups: list[dict[str, Any]], site_id: str) -> bool:
    """Return whether a key group with at least one key is synced to the site."""
    return any((group.get("sshKeys") or []) and sshkeygroup_synced_to_site(group, site_id) for group in groups)


def _serial_console_target(site: dict[str, Any]) -> dict[str, Any]:
    """Build the serial-console (SOL) access target from the site config.

    Only called once a specified key is synced to the site, so reachability
    reduces to the console endpoint being present.
    """
    name = f"{site.get('name') or site.get('id') or 'site'} serial console (SOL)"

    if not site.get("isSerialConsoleEnabled"):
        return {
            "type": "serial_console",
            "name": name,
            "key_access_enabled": None,
            "reachable": False,
            "detail": "Provider has not enabled the serial console (SOL) for this site",
        }

    key_access_enabled = bool(site.get("isSerialConsoleSSHKeysEnabled"))
    hostname = (site.get("serialConsoleHostname") or "").strip()

    if not key_access_enabled:
        detail = "Serial console is enabled but SSH-key access is disabled (isSerialConsoleSSHKeysEnabled is false)"
    elif not hostname:
        detail = "Serial console SSH-key access is enabled but no serial console hostname is configured"
    else:
        detail = "Specified key can access the serial console (SOL): provider-enabled, SSH-key auth on, key synced"

    return {
        "type": "serial_console",
        "name": name,
        "key_access_enabled": key_access_enabled,
        "reachable": bool(hostname),
        "detail": detail,
    }


def _network_device_target() -> dict[str, Any]:
    """Build the (unverified) network-device access target.

    Tenant key access to network switches is provider-managed and is not
    exposed by the NICo tenant REST API, so it is reported as unverified rather
    than asserted or falsely passed.
    """
    return {
        "type": "network_device",
        "name": "Network devices",
        "key_access_enabled": None,
        "reachable": False,
        "detail": (
            "Tenant key access to network devices is provider-managed and not exposed by the "
            "NICo tenant REST API; not verifiable from the tenant API"
        ),
    }


def main() -> int:
    """Gather specified-key access evidence and print the JSON contract to stdout."""
    parser = argparse.ArgumentParser(
        description="Report specified-key (SSH) access to out-of-band components on a NICo site"
    )
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "specified_keys": 0,
        "access_targets": [],
    }

    try:
        auth = resolve_auth()

        groups = forge_get_all(
            args.org,
            "sshkeygroup",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="sshKeyGroups",
        )

        result["specified_keys"] = _count_specified_keys(groups)

        if not _keys_synced_to_site(groups, args.site_id):
            # Nothing to evidence access with on this site. Query the org-wide
            # key groups (no siteId filter) so the skip reason can distinguish
            # "no key groups exist at all" from "key groups exist but none are
            # synced to this site" -- the operator needs to know which.
            org_groups = forge_get_all(
                args.org,
                "sshkeygroup",
                auth.token,
                base_url=args.api_base,
                result_key="sshKeyGroups",
            )
            result["org_key_groups"] = len(org_groups)
            result["skipped"] = True
            if org_groups:
                result["skip_reason"] = (
                    f"{len(org_groups)} SSH key group(s) exist for the org but none are synced to this site; "
                    "sync a key group containing an SSH key to the site to enable key-based SOL access"
                )
            else:
                result["skip_reason"] = (
                    "No SSH key groups exist for the org; create one with an SSH key and sync it to the site "
                    "to enable key-based SOL access"
                )
        else:
            site = forge_get(args.org, f"site/{args.site_id}", auth.token, base_url=args.api_base)
            result["access_targets"] = [
                _serial_console_target(site),
                _network_device_target(),
            ]

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
