#!/usr/bin/env python3
"""describe_tags — Armada Bridge VM suite, test phase.

Retrieves resource tags for a VM instance via:
  GET /tenants/<tenant>/vms/<vm_id>/tags

BLOCKED: Bridge does not expose a tagging API for VMs.
This step is best_effort: true in vm.yaml.

Output: {success, platform, instance_id, tags, tag_count}
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
                "tags": {
                    "Name": "isv-test-gpu",
                    "CreatedBy": "armada-bridge",
                },
                "tag_count": 2,
            }
        )
    else:
        raise NotImplementedError(
            "describe_tags: Bridge has no VM tagging API. "
            "This step is blocked (best_effort: true). "
            "Implement when Bridge exposes GET /tenants/<tenant>/vms/<vm_id>/tags."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
