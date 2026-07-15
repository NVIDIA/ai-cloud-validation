# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared Cloud KMS inventory helpers for GCP validation stubs."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_kms_locations(client: Any, project: str) -> Iterator[Any]:
    """Yield every KMS location, following raw ``next_page_token`` responses.

    ``KeyManagementServiceClient.list_locations`` returns a raw
    ``ListLocationsResponse`` rather than one of the generated lazy pagers used
    by most Google Cloud list methods. Callers must therefore carry the token
    explicitly or they silently inspect only the first page.
    """
    page_token = ""
    seen_tokens: set[str] = set()
    while True:
        request = {"name": f"projects/{project}"}
        if page_token:
            request["page_token"] = page_token
        response = client.list_locations(request=request)
        yield from getattr(response, "locations", None) or []

        next_token = str(getattr(response, "next_page_token", "") or "")
        if not next_token:
            return
        if next_token in seen_tokens:
            raise RuntimeError(f"Cloud KMS location pagination repeated token {next_token!r}")
        seen_tokens.add(next_token)
        page_token = next_token
