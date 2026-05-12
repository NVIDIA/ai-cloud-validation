#!/usr/bin/env python3
"""nvlink_domain_test — Armada Bridge network suite, test phase.

Validates NVLink domain identification.

NOTE: Bridge API gap — no NVLink domain endpoint available.

NvlinkDomainCheck requires:
  node_id: non-empty string
  tests: {node_resolved, nvlink_support_detected, nvlink_domain_id_present}
  nvlink_supported: True (boolean)
  nvlink_domain_id: non-empty string

Output: {success, platform, node_id, nvlink_supported, nvlink_domain_id, tests}
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

    result: dict[str, Any] = {"success": False, "platform": "network"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "network",
                "node_id": "demo-node-0001",
                "nvlink_supported": True,
                "nvlink_domain_id": "demo-nvlink-domain-0001",
                "tests": {
                    "node_resolved": {"passed": True},
                    "nvlink_support_detected": {"passed": True},
                    "nvlink_domain_id_present": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no NVLink domain endpoint. "
            "See bridge-isv-ncp-status.md Network suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
