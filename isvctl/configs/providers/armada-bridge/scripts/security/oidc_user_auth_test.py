#!/usr/bin/env python3
"""oidc_user_auth_test — Armada Bridge security suite, test phase.

Validates OIDC/JWT authentication enforcement.

Output: {success, platform, issuer_url, audience, target_url, endpoints_tested, tests}
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
                "issuer_url": "https://kc.demo.armada.ai/realms/GPUaaS",
                "audience": "GPUaaS",
                "target_url": "http://localhost:3000/users/account/profile",
                "endpoints_tested": 1,
                "tests": {
                    "valid_token_accepted": {"passed": True},
                    "bad_signature_rejected": {"passed": True},
                    "wrong_issuer_rejected": {"passed": True},
                    "wrong_audience_rejected": {"passed": True},
                    "expired_token_rejected": {"passed": True},
                    "missing_required_claim_rejected": {"passed": True},
                    "discovery_and_jwks_reachable": {"passed": True},
                },
            }
        )
    else:
        raise NotImplementedError(
            "oidc_user_auth_test: implement OIDC auth validation with BridgeClient.from_env()."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
