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

"""Remove the throwaway specified key provisioned for AUTH-XX-03 (teardown).

Deletes the SSH Key Group and SSH Key created by setup_key_access.py and, when
setup flipped it, restores the site's serial-console SSH-key flag. The IDs are
passed in from the setup step output via Jinja; empty IDs mean setup created
nothing, so this is a no-op. DELETE 404 is treated as already removed
(idempotent teardown). Other failures on one resource do not stop the others;
errors are reported in ``cleanup_errors`` and fail the step.

NICo API endpoints used (``/carbide/`` segment, like the other NICo scripts):
  DELETE /{org}/carbide/sshkeygroup/{id}
  DELETE /{org}/carbide/sshkey/{id}
  PATCH  /{org}/carbide/site/{site_id}        (only when restoring the flag)

Usage (wired via the key_access config; IDs come from the setup step):
    python teardown_key_access.py --org <org> --site-id <uuid> --api-base <url> \
        --sshkeygroup-id <id> --sshkey-id <id> --restore-ssh-keys-enabled <bool|"">
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_delete, forge_patch, resolve_auth


def _delete_if_present(org: str, path: str, token: str, *, base_url: str) -> None:
    """DELETE a NICo resource; treat 404 as already removed."""
    try:
        forge_delete(org, path, token, base_url=base_url)
    except HTTPError as e:
        if e.code == 404:
            return
        raise


def _as_bool(value: str) -> bool | None:
    """Parse a Jinja-rendered flag into True/False, or None when unset."""
    normalized = (value or "").strip().lower()
    if normalized in ("", "none", "null"):
        return None
    return {"true": True, "false": False}.get(normalized)


def main() -> int:
    """Tear down the throwaway specified key and print the JSON contract to stdout."""
    parser = argparse.ArgumentParser(description="Remove the throwaway SSH key/group provisioned for AUTH-XX-03")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument("--api-base", required=True, help="NICo API base URL")
    parser.add_argument("--sshkeygroup-id", default="", help="SSH Key Group ID created by setup")
    parser.add_argument("--sshkey-id", default="", help="SSH Key ID created by setup")
    parser.add_argument(
        "--restore-ssh-keys-enabled",
        default="",
        help="Prior site isSerialConsoleSSHKeysEnabled value to restore (blank = leave as-is)",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "cleanup_errors": [],
    }

    group_id = (args.sshkeygroup_id or "").strip()
    key_id = (args.sshkey_id or "").strip()
    restore = _as_bool(args.restore_ssh_keys_enabled)

    try:
        auth = resolve_auth()

        # Delete the group before the key: deleting the group removes the key's
        # group membership, so the key can then be deleted on its own.
        if group_id:
            try:
                _delete_if_present(args.org, f"sshkeygroup/{group_id}", auth.token, base_url=args.api_base)
            except Exception as e:
                result["cleanup_errors"].append(f"sshkeygroup {group_id}: {type(e).__name__}: {e}")

        if key_id:
            try:
                _delete_if_present(args.org, f"sshkey/{key_id}", auth.token, base_url=args.api_base)
            except Exception as e:
                result["cleanup_errors"].append(f"sshkey {key_id}: {type(e).__name__}: {e}")

        if restore is not None:
            try:
                forge_patch(
                    args.org,
                    f"site/{args.site_id}",
                    auth.token,
                    base_url=args.api_base,
                    body={"isSerialConsoleSSHKeysEnabled": restore},
                )
            except Exception as e:
                result["cleanup_errors"].append(f"restore site flag: {type(e).__name__}: {e}")

        # A failure on one resource does not stop the others (the framework
        # already runs teardown steps best-effort), but any failed deletion
        # fails the step so a leaked key/group is visible.
        result["success"] = not result["cleanup_errors"]
        if result["cleanup_errors"]:
            result["error"] = "; ".join(result["cleanup_errors"])

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
