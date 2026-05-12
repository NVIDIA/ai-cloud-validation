#!/usr/bin/env python3
"""verify_key_rejected — Armada Bridge control-plane suite, test phase.

BLOCKED: Depends on disable_access_key which requires Bridge PATCH /key-manager/api-key.
Once that endpoint exists, call a guarded endpoint with the disabled key and expect 401.

In demo mode returns a synthetic rejection result so the suite can proceed.

Output: {success, platform, rejected, error_code}
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient  # noqa: F401 — used in the live impl block
from common.errors import handle_bridge_errors

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--secret-access-key", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "rejected": True,
                "error_code": "InvalidClientTokenId",
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: blocked by missing disable_access_key endpoint. "
            "Once PATCH /key-manager/api-key is implemented, call a guarded endpoint "
            "with the disabled key and expect 401. See bridge-isv-ncp-status.md."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
