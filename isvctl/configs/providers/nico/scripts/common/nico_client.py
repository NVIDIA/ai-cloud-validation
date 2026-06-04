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

"""Shared NICo API client for NICo validation scripts.

Handles authenticated GET requests with pagination and proper URL encoding.
The NICo REST API uses /forge/ in its URL path (legacy name).
"""

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_API_BASE = "https://api.ngc.nvidia.com/v2/org"
DEFAULT_PAGE_SIZE = 100


def forge_get(
    org: str,
    path: str,
    token: str,
    *,
    base_url: str = DEFAULT_API_BASE,
    params: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Make an authenticated GET request to a single Forge API page.

    Args:
        org: NGC org name.
        path: API path relative to /forge/ (e.g., "machine", "expected-machine").
        token: NGC Bearer token.
        base_url: Forge API base URL (default: NGC production).
        params: Query parameters (will be URL-encoded).
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        HTTPError: On non-2xx response.
    """
    url = f"{base_url}/{org}/forge/{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = ""
        if e.fp:
            body = e.fp.read().decode(errors="replace")[:500]
        raise type(e)(e.url, e.code, f"{e.reason}: {body}", e.headers, None) from e


def forge_get_all(
    org: str,
    path: str,
    token: str,
    *,
    base_url: str = DEFAULT_API_BASE,
    params: dict[str, str] | None = None,
    result_key: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Fetch all pages from a paginated Forge API endpoint.

    Args:
        org: NGC org name.
        path: API path relative to /forge/.
        token: NGC Bearer token.
        base_url: Forge API base URL.
        params: Additional query parameters.
        result_key: JSON key containing the results array. If None, the response
            itself is expected to be a list, or auto-detected from common keys.
        page_size: Number of items per page (max 100).
        timeout: Per-request timeout in seconds.

    Returns:
        Combined list of all items across all pages.
    """
    all_items: list[dict[str, Any]] = []
    page_number = 1

    while True:
        page_params = dict(params or {})
        page_params["pageSize"] = str(min(page_size, 100))
        page_params["pageNumber"] = str(page_number)

        resp = forge_get(org, path, token, base_url=base_url, params=page_params, timeout=timeout)

        # Extract items from response
        if isinstance(resp, list):
            items = resp
        elif result_key and result_key in resp:
            items = resp[result_key]
        else:
            # Auto-detect: look for common Forge API result keys
            for key in ("machines", "expectedMachines", "instances", "sites"):
                if key in resp:
                    items = resp[key]
                    break
            else:
                # Response is a single object, not a list
                items = [resp] if resp else []

        all_items.extend(items)

        # Check if there are more pages
        if len(items) < page_size:
            break

        page_number += 1

    return all_items


def classify_health(health: dict[str, Any]) -> str:
    """Classify machine health as 'healthy' or 'unhealthy'."""
    alerts = health.get("alerts", [])
    return "unhealthy" if alerts else "healthy"


def sum_capabilities(capabilities: list[dict[str, Any]], cap_type: str) -> int:
    """Sum the count field for capabilities of a given type.

    Per the OpenAPI spec, MachineCapability.count is the device count
    (e.g., count=2 means 2 DPUs). We sum across all entries of the type.
    """
    return sum(c.get("count", 1) for c in capabilities if c.get("type") == cap_type)
