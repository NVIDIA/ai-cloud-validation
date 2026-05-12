#!/usr/bin/env python3
"""customer_managed_key_test — Armada Bridge security suite, test phase.

Blocked: Bridge BYOK customer managed key API not yet available.

Output: {success, platform, tests}
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
                "key_id": "demo-cmk-0001",
                "resource_id": "demo-encrypted-resource-0001",
                "tests": {
                    "customer_managed_key_available": {"passed": True},
                    "key_manager_is_customer": {"passed": True},
                    "encrypt_decrypt_roundtrip": {"passed": True},
                    "resource_encrypted_with_customer_key": {"passed": True},
                    "provider_managed_key_not_used": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "Bridge API gap: no BYOK customer managed key endpoint. See bridge-isv-ncp-status.md Security suite."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
