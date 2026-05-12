#!/usr/bin/env python3
"""teardown — Armada Bridge bare metal suite, teardown phase.

Deallocates a bare metal node via:
  POST /tenants/<tenant>/metal/<compute_node_id>/deallocate

Note: Bridge uses POST .../metal/:id/deallocate — NOT DELETE — to release
bare metal nodes back to the pool.

Pass --skip-destroy to skip deallocation (useful when ARMADA_BRIDGE_SKIP_TEARDOWN=true).

Output: {success, platform} or {success, platform, skipped: true} when skip-destroy is set.
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
    parser.add_argument("--compute-node-id", required=True)
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if args.skip_destroy:
        result.update(
            {
                "success": True,
                "platform": "bare_metal",
                "skipped": True,
            }
        )
    elif DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "bare_metal",
            }
        )
    else:
        raise NotImplementedError(
            "teardown: uncomment the Bridge implementation block. "
            "POST /tenants/<tenant>/metal/<id>/deallocate with BridgeClient.from_env(). "
            "Note: Bridge uses POST .../deallocate, NOT DELETE."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
