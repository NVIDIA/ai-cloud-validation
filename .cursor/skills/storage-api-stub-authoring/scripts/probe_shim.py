#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Isolated preflight for storage provider shims.

Loads the provider manifest, exercises each Python shim in-process, and
prints pass/fail per subtest. Run from repo root after ``uv sync``::

    uv run python .cursor/skills/storage-api-stub-authoring/scripts/probe_shim.py \\
        --manifest isvctl/configs/providers/my-isv/config/overlays/storage-provider-manifest.yaml

Exit code 0 when all providers pass; 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys

from isvtest.core.storage import ManifestError, load_provider_registry
from isvtest.core.storage_provider import (
    AuthenticationError,
    CreateVolumeRequest,
    GetTenantQuotaRequest,
    ListVolumesRequest,
    NotSupportedError,
)


def _probe_provider(name: str, api) -> bool:
    """Return True when all three subtest areas pass."""
    ok = True

    try:
        api.health_check()
        print(f"  [PASS] api-authentication[{name}] health_check() ok")
    except AuthenticationError as exc:
        print(f"  [FAIL] api-authentication[{name}] AuthenticationError: {exc}")
        print(f"  [SKIP] volume-provisioning[{name}] (auth failed)")
        print(f"  [SKIP] tenant-quota[{name}] (auth failed)")
        return False
    except Exception as exc:
        print(f"  [FAIL] api-authentication[{name}] {type(exc).__name__}: {exc}")
        ok = False

    try:
        api.create_volume(CreateVolumeRequest(size_bytes=1 << 30, volume_type="file", name="probe-shim-test"))
        print(f"  [PASS] volume-provisioning[{name}] create_volume succeeded")
    except NotSupportedError:
        try:
            existing = list(api.list_volumes(ListVolumesRequest()).volumes)
        except Exception as exc:
            print(f"  [FAIL] volume-provisioning[{name}] list_volumes() raised {type(exc).__name__}: {exc}")
            ok = False
        else:
            count = len(existing)
            if count >= 1:
                print(
                    f"  [PASS] volume-provisioning[{name}] CSI fallback: observed {count} volume(s) via list_volumes()"
                )
            else:
                print(
                    f"  [WARN] volume-provisioning[{name}] CSI fallback: "
                    f"observed 0 volumes — create a PVC against the StorageClass, then re-probe"
                )
                ok = False
    except Exception as exc:
        print(f"  [FAIL] volume-provisioning[{name}] create_volume raised {type(exc).__name__}: {exc}")
        ok = False

    try:
        quota = api.get_tenant_quota(GetTenantQuotaRequest())
    except Exception as exc:
        print(f"  [FAIL] tenant-quota[{name}] get_tenant_quota() raised {type(exc).__name__}: {exc}")
        ok = False
    else:
        if quota.hard_limit_bytes <= 0:
            print(f"  [FAIL] tenant-quota[{name}] hard_limit_bytes={quota.hard_limit_bytes} (must be > 0)")
            ok = False
        else:
            print(
                f"  [PASS] tenant-quota[{name}] hard_limit_bytes={quota.hard_limit_bytes} used_bytes={quota.used_bytes}"
            )

    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe storage shims in isolation.")
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to storage-provider-manifest.yaml",
    )
    args = parser.parse_args(argv)

    try:
        providers = load_provider_registry({"manifest_path": args.manifest})
    except ManifestError as exc:
        print(f"[FAIL] manifest load: {exc}", file=sys.stderr)
        return 1

    shim_providers = [p for p in providers if p.has_shim]
    if not shim_providers:
        print("[WARN] no providers with Python shim loaded — nothing to probe")
        return 0

    all_ok = True
    for provider in shim_providers:
        print(f"Provider: {provider.name}")
        assert provider.api is not None
        if not _probe_provider(provider.name, provider.api):
            all_ok = False
        print()

    if all_ok:
        print("All shim probes passed.")
        return 0
    print("One or more shim probes failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
