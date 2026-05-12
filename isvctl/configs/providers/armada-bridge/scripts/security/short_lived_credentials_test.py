#!/usr/bin/env python3
"""short_lived_credentials_test — Armada Bridge security suite, test phase.

Validates that node and workload credentials have appropriate TTLs.

Output: {success, platform, node_credential_ttl_seconds, workload_credential_ttl_seconds,
         max_ttl_seconds, tests}
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
    parser.add_argument("--kc-token-url", required=True)
    parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "security"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "security",
                "node_credential_ttl_seconds": 3600,
                "workload_credential_ttl_seconds": 3600,
                "max_ttl_seconds": 86400,
                "tests": {
                    "node_credential_has_expiry": {"passed": True},
                    "node_credential_ttl_within_bound": {"passed": True},
                    "workload_credential_has_expiry": {"passed": True},
                    "workload_credential_ttl_within_bound": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "short_lived_credentials_test: implement credential TTL validation with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
