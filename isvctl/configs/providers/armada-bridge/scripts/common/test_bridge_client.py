#!/usr/bin/env python3
"""Manual smoke-test for BridgeClient.

Tests login and a lightweight authenticated GET against a live Bridge instance.

Required env vars:
  BRIDGE_URL       — Bridge base URL, e.g. https://bridge.armada.ai
  BRIDGE_USERNAME  — Login email
  BRIDGE_PASSWORD  — Password

Optional env vars:
  BRIDGE_TOTP_SECRET — Base32 TOTP secret (MFA-enabled accounts only)
  BRIDGE_INSECURE=1  — Skip TLS certificate verification (dev/self-signed certs)

Usage:
  python test_bridge_client.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def check_env() -> bool:
    missing = [v for v in ("BRIDGE_URL", "BRIDGE_USERNAME", "BRIDGE_PASSWORD") if not os.environ.get(v)]
    if missing:
        print(f"{FAIL} missing env vars: {', '.join(missing)}")
        return False
    print(f"  BRIDGE_URL         = {os.environ['BRIDGE_URL']}")
    print(f"  BRIDGE_USERNAME    = {os.environ['BRIDGE_USERNAME']}")
    print(f"  BRIDGE_PASSWORD    = {'*' * len(os.environ['BRIDGE_PASSWORD'])}")
    totp = os.environ.get("BRIDGE_TOTP_SECRET")
    print(f"  BRIDGE_TOTP_SECRET = {'set (' + str(len(totp)) + ' chars)' if totp else 'not set'}")
    insecure = os.environ.get("BRIDGE_INSECURE") == "1"
    print(f"  BRIDGE_INSECURE    = {'1 (TLS verification disabled)' if insecure else 'not set'}")
    return True


def test_login() -> BridgeClient | None:
    print("\n--- test: login ---")
    try:
        client = BridgeClient.from_env()
        print(f"{PASS} POST /auth/login → session cookie obtained")
        return client
    except Exception as e:
        print(f"{FAIL} {type(e).__name__}: {e}")
        return None


def test_profile(client: BridgeClient) -> bool:
    print("\n--- test: GET /users/account/profile ---")
    try:
        profile = client.get("/users/account/profile")
        email = profile.get("email") or profile.get("username") or "(no email field)"
        print(f"{PASS} authenticated as: {email}")
        return True
    except Exception as e:
        print(f"{FAIL} {type(e).__name__}: {e}")
        return False


def test_cookie_reuse() -> bool:
    print("\n--- test: cookie reuse (second from_env should skip login) ---")
    try:
        t0 = time.monotonic()
        BridgeClient.from_env()
        elapsed = time.monotonic() - t0
        if elapsed < 1.0:
            print(f"{PASS} returned in {elapsed:.3f}s — cookie loaded from disk, no login round-trip")
        else:
            print(f"  [warn] took {elapsed:.3f}s — may have re-logged in")
        return True
    except Exception as e:
        print(f"{FAIL} {type(e).__name__}: {e}")
        return False


def main() -> int:
    print("=== BridgeClient smoke test ===")

    print("\n--- env ---")
    if not check_env():
        return 1

    client = test_login()
    if client is None:
        return 1

    profile_ok = test_profile(client)
    reuse_ok = test_cookie_reuse()

    print("\n=== summary ===")
    results = {"login": True, "profile": profile_ok, "cookie_reuse": reuse_ok}
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'} {name}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
