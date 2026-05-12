#!/usr/bin/env python3
"""disable_access_key — Armada Bridge control-plane suite, test phase.

BLOCKED: Bridge has no PATCH /key-manager/api-key endpoint to disable a key.
New Bridge work needed: add disableApiKey() to key-manager.service.ts.
See bridge-isv-ncp-status.md Control Plane suite.

In demo mode returns a synthetic Inactive status so the suite can proceed.

Output: {success, platform, status}
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
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "status": "Inactive",
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: PATCH /key-manager/api-key does not exist. "
            "New Bridge work needed: add disableApiKey() to key-manager.service.ts. "
            "See bridge-isv-ncp-status.md Control Plane suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
