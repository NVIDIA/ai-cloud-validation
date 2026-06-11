#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OIDC user authentication test - TEMPLATE (replace with your platform implementation).

Verifies that an OIDC-compliant verifier in your platform accepts properly
issued tokens and rejects tokens with bad signature, wrong issuer, wrong
audience, expired exp, or missing required claims, and that the discovery +
JWKS endpoints serve the expected metadata.  Covers SEC01-01.

Required JSON output fields:
  {
    "success": true,
    "platform": "security",
    "test_name": "oidc_user_auth_test",
    "issuer_url": "https://...",
    "audience": "...",
    "target_url": "https://...",
    "endpoints_tested": 1,
    "tests": {
      "valid_token_accepted":            {"passed": true},
      "bad_signature_rejected":          {"passed": true},
      "wrong_issuer_rejected":           {"passed": true},
      "wrong_audience_rejected":         {"passed": true},
      "expired_token_rejected":          {"passed": true},
      "missing_required_claim_rejected": {"passed": true},
      "discovery_and_jwks_reachable":    {"passed": true}
    }
  }

Usage:
    python oidc_user_auth_test.py --region <region>
"""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """OIDC user auth test (template) and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="OIDC user auth test (template)")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--issuer-url", default="", help="OIDC issuer URL (optional)")
    parser.add_argument("--audience", default="", help="Expected audience claim (optional)")
    parser.add_argument("--target-url", default="", help="Platform endpoint to probe with tokens (optional)")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "oidc_user_auth_test",
        "issuer_url": "",
        "audience": "",
        "target_url": "",
        "endpoints_tested": 0,
        "tests": {
            "valid_token_accepted": {"passed": False},
            "bad_signature_rejected": {"passed": False},
            "wrong_issuer_rejected": {"passed": False},
            "wrong_audience_rejected": {"passed": False},
            "expired_token_rejected": {"passed": False},
            "missing_required_claim_rejected": {"passed": False},
            "discovery_and_jwks_reachable": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's OIDC user auth    ║
    # ║  test.                                                           ║
    # ║                                                                  ║
    # ║  Example (pseudocode):                                           ║
    # ║    discovery = fetch(f"{issuer_url}/.well-known/openid-config")  ║
    # ║    jwks = fetch(discovery["jwks_uri"])                           ║
    # ║    valid = mint_token(audience=audience)                         ║
    # ║    assert target_accepts(target_url, valid)                      ║
    # ║    bad_sig = mutate_signature(valid)                             ║
    # ║    assert target_rejects(target_url, bad_sig)                    ║
    # ║    assert target_rejects(target_url, token_with(iss="x"))        ║
    # ║    assert target_rejects(target_url, token_with(aud="x"))        ║
    # ║    assert target_rejects(target_url, token_with_exp_in_past())   ║
    # ║    assert target_rejects(target_url, token_without("sub"))       ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["issuer_url"] = "https://oidc.my-isv.example/realms/test"
        result["audience"] = "isv-validation"
        result["target_url"] = "https://api.my-isv.example/protected"
        result["endpoints_tested"] = 1
        result["tests"] = {
            "valid_token_accepted": {"passed": True},
            "bad_signature_rejected": {"passed": True},
            "wrong_issuer_rejected": {"passed": True},
            "wrong_audience_rejected": {"passed": True},
            "expired_token_rejected": {"passed": True},
            "missing_required_claim_rejected": {"passed": True},
            "discovery_and_jwks_reachable": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's OIDC user auth test"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
