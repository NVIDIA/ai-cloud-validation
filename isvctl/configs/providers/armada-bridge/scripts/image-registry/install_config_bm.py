#!/usr/bin/env python3
"""install_config_bm — Armada Bridge image registry suite, test phase.

Blocked: Bridge has no OS image write API.
See bridge-isv-ncp-status.md Image Registry suite.

Output: {success, platform}
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
    parser.add_argument("--tenant", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "image_registry"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "image_registry",
                "instance_id": "demo-bm-config-node01",
                "config_id": "demo-config-0001",
                "instance_state": "running",
                "state": "running",
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no OS image write API. See bridge-isv-ncp-status.md Image Registry suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
