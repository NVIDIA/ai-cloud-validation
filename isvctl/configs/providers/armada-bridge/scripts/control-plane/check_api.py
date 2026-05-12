#!/usr/bin/env python3
"""check_api — Armada Bridge control-plane suite, setup phase.

Probes two Bridge health endpoints:
  GET /health             → auth-gateway liveness
  GET /orchestrator/health-check → orchestrator liveness

Output: {success, platform, account_id, tests: {auth_gateway_health, orchestrator_health}}
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
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "control_plane"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "control_plane",
                "account_id": "armada-bridge-demo",
                "tests": {
                    "auth_gateway_health": {
                        "passed": True,
                        "message": "GET /health returned OK",
                    },
                    "orchestrator_health": {
                        "passed": True,
                        "message": "GET /orchestrator/health-check returned healthy",
                    },
                },
            }
        )
    else:
        raise NotImplementedError(
            "check_api: uncomment the Bridge implementation block. "
            "Probe GET /health and GET /orchestrator/health-check with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
