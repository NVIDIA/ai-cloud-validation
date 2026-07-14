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

"""Provision a throwaway specified key synced to a NICo site (AUTH-XX-03 setup).

Creates an ephemeral SSH key and an SSH Key Group that syncs it to the target
site, so query_key_access.py can evidence key-based serial-console (SOL) access
end-to-end without manual setup. The matching private key is generated in a temp
dir and immediately discarded -- only the public key is registered, so the
credential is unusable by anyone and safe to leave until teardown removes it.

teardown_key_access.py deletes everything this script creates (it reads the
created IDs from this step's output), so the IDs are always emitted -- even on a
mid-provision failure -- to keep cleanup reliable.

NICo API endpoints used (``/carbide/`` segment, like the other NICo scripts):
  POST  /{org}/carbide/sshkey
  POST  /{org}/carbide/sshkeygroup
  GET   /{org}/carbide/sshkeygroup/{id}        (poll for sync)
  PATCH /{org}/carbide/site/{site_id}          (best-effort; older API only)

Auth:
  - NICO_BEARER_TOKEN, or OIDC client_credentials
    (NICO_SSA_ISSUER / NICO_CLIENT_ID / NICO_CLIENT_SECRET).

Output fields (consumed by teardown_key_access.py via Jinja step references):
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "sshkey_id": "...",            # "" when not created
    "sshkeygroup_id": "...",       # "" when not created
    "synced": true,
    "restore_ssh_keys_enabled": false | null  # prior site flag to restore, or null
  }

Usage:
    NICO_BEARER_TOKEN=<token> \
        python setup_key_access.py --org <org> --site-id <uuid> --api-base <url>
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import (
    NicoAuthError,
    forge_get,
    forge_patch,
    forge_post,
    resolve_auth,
    sshkeygroup_synced_to_site,
)

SYNC_POLL_TIMEOUT_SECONDS = 180
SYNC_POLL_INTERVAL_SECONDS = 5


def _generate_public_key(comment: str) -> str:
    """Generate an ed25519 keypair and return only the public key.

    The private key lives in a temp dir that is removed on return, so the
    registered public key has no usable counterpart.
    """
    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "id_ed25519")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", key_path, "-q"],
            check=True,
            capture_output=True,
        )
        return Path(f"{key_path}.pub").read_text().strip()


def _wait_for_sync(org: str, group_id: str, site_id: str, token: str, *, base_url: str) -> bool:
    """Poll the key group until it is synced to the site or the timeout elapses."""
    if not group_id:
        # No group id means nothing to poll (the create response had no id).
        return False
    deadline = time.monotonic() + SYNC_POLL_TIMEOUT_SECONDS
    while True:
        group = forge_get(org, f"sshkeygroup/{group_id}", token, base_url=base_url)
        if sshkeygroup_synced_to_site(group, site_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SYNC_POLL_INTERVAL_SECONDS)


def main() -> int:
    """Provision the throwaway specified key and print the JSON contract to stdout."""
    parser = argparse.ArgumentParser(description="Provision a throwaway SSH key synced to a NICo site")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:8]
    name = f"isvtest-auth-xx-03-{suffix}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "sshkey_id": "",
        "sshkeygroup_id": "",
        "synced": False,
        "restore_ssh_keys_enabled": None,
    }

    try:
        auth = resolve_auth()

        public_key = _generate_public_key(comment=name)
        key = forge_post(
            args.org, "sshkey", auth.token, base_url=args.api_base, body={"name": name, "publicKey": public_key}
        )
        result["sshkey_id"] = key.get("id") or ""

        group = forge_post(
            args.org,
            "sshkeygroup",
            auth.token,
            base_url=args.api_base,
            body={"name": name, "sshKeyIds": [result["sshkey_id"]], "siteIds": [args.site_id]},
        )
        result["sshkeygroup_id"] = group.get("id") or ""

        result["synced"] = _wait_for_sync(
            args.org, result["sshkeygroup_id"], args.site_id, auth.token, base_url=args.api_base
        )

        if not result["synced"]:
            result["error"] = "SSH key group did not sync to the site before timeout"
        else:
            # Older clusters gate SSH-key SOL access on a tenant-settable site flag;
            # newer ones derive it from key-group sync (the flag is deprecated and
            # the PATCH may be rejected). Enabling it is therefore best-effort: only
            # record a restore value when we actually flipped it off->on.
            site = forge_get(args.org, f"site/{args.site_id}", auth.token, base_url=args.api_base)
            if not site.get("isSerialConsoleSSHKeysEnabled"):
                try:
                    forge_patch(
                        args.org,
                        f"site/{args.site_id}",
                        auth.token,
                        base_url=args.api_base,
                        body={"isSerialConsoleSSHKeysEnabled": True},
                    )
                    result["restore_ssh_keys_enabled"] = False
                except Exception:
                    # Deprecated/derived on this API version; nothing to restore.
                    pass

            result["success"] = True

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except FileNotFoundError:
        result["error"] = "ssh-keygen not found; it is required to generate the throwaway specified key"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
