#!/usr/bin/env python3
"""serial_console — Armada Bridge VM suite, test phase.

Checks serial console output availability for a VM via:
  GET /tenants/<tenant>/vms/<vm_id>/console-output

BLOCKED: Bridge does not expose a serial console API for VMs.
This step is best_effort: true in vm.yaml.

Output: {success, platform, instance_id, console_available, serial_access_enabled,
         output_length}
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
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "vm"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "vm",
                "instance_id": "demo-vm-abc123",
                "console_available": False,
                "serial_access_enabled": True,
                "output_length": 0,
            }
        )
    else:
        raise NotImplementedError(
            "serial_console: Bridge has no serial console API. "
            "This step is blocked (best_effort: true). "
            "Implement when Bridge exposes GET /tenants/<tenant>/vms/<vm_id>/console-output."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
