#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Query NVSwitch tray firmware versions from NICo racks (BFX03-02).

NICo's read-only rack list endpoint returns rack components when
``includeComponents`` is enabled.  NVSwitch components expose their installed
firmware through ``firmwareVersion``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from breakfix._common import emit
from common.nico_client import NicoAuthError, forge_get_all, resolve_auth


def _is_nvswitch(component: dict[str, Any]) -> bool:
    """Return whether a rack component is explicitly typed as an NVSwitch."""
    component_type = re.sub(r"[^a-z0-9]", "", str(component.get("type") or "").lower())
    return component_type in {"nvswitch", "componenttypenvswitch"}


def _tray_id(component: dict[str, Any]) -> str:
    """Choose the first stable, human-useful identifier NICo provides."""
    for field in ("componentId", "id", "serialNumber", "name"):
        value = component.get(field)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def main() -> int:
    """List NVSwitch tray firmware versions as provider-neutral JSON."""
    parser = argparse.ArgumentParser(description="Query NICo NVSwitch tray firmware versions")
    parser.add_argument("--org", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--api-base", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "trays": [],
    }
    try:
        auth = resolve_auth()
        racks = forge_get_all(
            args.org,
            "rack",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id, "includeComponents": "true"},
            result_key="racks",
        )
    except NicoAuthError as exc:
        result.update(error_type="auth", error=str(exc))
        return emit(result)
    except (URLError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return emit(result)

    seen: set[str] = set()
    trays: list[dict[str, Any]] = []
    for rack in racks:
        rack_id = str(rack.get("id") or "")
        components = rack.get("components") or []
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, dict) or not _is_nvswitch(component):
                continue
            tray_id = _tray_id(component)
            dedupe_key = tray_id or f"{rack_id}:{component.get('slotId')}:{component.get('trayIdx')}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            trays.append(
                {
                    "tray_id": tray_id,
                    "firmware_version": str(component.get("firmwareVersion") or ""),
                    "rack_id": rack_id,
                    "slot_id": component.get("slotId"),
                    "tray_index": component.get("trayIdx"),
                }
            )

    if not trays:
        result.update(
            success=True,
            skipped=True,
            skip_reason="No NVSwitch tray components were returned for this NICo site",
        )
        return emit(result)

    result.update(success=True, trays=trays)
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
