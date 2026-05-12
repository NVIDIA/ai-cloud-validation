#!/usr/bin/env python3
"""centralized_kms_test — Armada Bridge security suite, test phase.

Blocked: Bridge centralized KMS API not yet available.

Output: {success, platform, kms_keys_total, encrypted_resources_inspected, non_kms_resources, tests}
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

    result: dict[str, Any] = {"success": False, "platform": "security"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "security",
                "kms_keys_total": 2,
                "encrypted_resources_inspected": 5,
                "non_kms_resources": 0,
                "tests": {
                    "kms_service_reachable": {"passed": True},
                    "kms_keys_present": {"passed": True},
                    "all_encrypted_resources_use_kms": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no centralized KMS endpoint. See bridge-isv-ncp-status.md Security suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
