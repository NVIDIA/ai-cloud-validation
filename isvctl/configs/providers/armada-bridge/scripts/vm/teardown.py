#!/usr/bin/env python3
"""teardown — Armada Bridge VM suite, teardown phase.

Terminates a VM instance via:
  DELETE /tenants/<tenant>/vms/<vm_id>

Pass --skip-destroy to skip termination (useful when ARMADA_BRIDGE_SKIP_TEARDOWN=true).

Output: {success, platform} or {success, platform, skipped: true} when skip-destroy
        is set.
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
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--skip-destroy", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "vm"}

    if args.skip_destroy:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "skipped": True,
            }
        )
    elif DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
            }
        )
    else:
        raise NotImplementedError(
            "teardown: uncomment the Bridge implementation block. "
            "DELETE /tenants/<tenant>/vms/<vm_id> with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
