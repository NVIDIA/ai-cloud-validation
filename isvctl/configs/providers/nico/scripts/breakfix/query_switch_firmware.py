#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Query NVSwitch tray firmware versions from NICo Flow (BFX03-02).

NICo's read-only tray list endpoint returns every tray at a Flow-enabled site.
The provider filters the version-specific tray type values client-side, and
NVSwitch trays expose their installed firmware through ``firmwareVersion``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from breakfix._common import emit, skip_result
from common.nico_client import NicoAuthError, forge_get_all, resolve_auth

_FLOW_DISABLED_MESSAGE = "site does not have nico flow enabled"


def _is_nvswitch(component: dict[str, Any]) -> bool:
    """Return whether a tray is explicitly typed as an NVSwitch."""
    component_type = re.sub(r"[^a-z0-9]", "", str(component.get("type") or "").lower())
    return component_type in {"switch", "nvswitch", "componenttypenvswitch"}


def _tray_id(component: dict[str, Any]) -> str:
    """Choose the first stable, human-useful identifier NICo provides."""
    for field in ("componentId", "id", "serialNumber", "name"):
        value = component.get(field)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _flow_disabled(exc: HTTPError) -> bool:
    """Return whether NICo rejected the query because Flow is disabled."""
    return exc.code == 412 and _FLOW_DISABLED_MESSAGE in str(exc).lower()


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
        components = forge_get_all(
            args.org,
            "tray",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="trays",
        )
    except NicoAuthError as exc:
        result.update(error_type="auth", error=str(exc))
        return emit(result)
    except HTTPError as exc:
        if _flow_disabled(exc):
            skip = skip_result(
                args.site_id,
                "NICo Flow is not enabled for this site; NVSwitch tray firmware is unavailable (BFX03-02 gap)",
                gap="BFX03-02",
            )
            skip["trays"] = []
            return emit(skip)
        result.update(error_type="api", error=f"NICo tray query failed (HTTP {exc.code})")
        return emit(result)
    except (URLError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return emit(result)

    seen: set[str] = set()
    trays: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict) or not _is_nvswitch(component):
            continue
        tray_id = _tray_id(component)
        if tray_id and tray_id in seen:
            continue
        if tray_id:
            seen.add(tray_id)
        trays.append(
            {
                "tray_id": tray_id,
                "firmware_version": str(component.get("firmwareVersion") or ""),
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
