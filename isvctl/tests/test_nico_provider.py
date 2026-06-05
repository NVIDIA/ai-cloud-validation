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

"""Tests for the NICo provider configuration and auth helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs

import pytest

from isvctl.config.merger import merge_yaml_files

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
NICO_COMMON = ISVCTL_ROOT / "configs" / "providers" / "nico" / "scripts" / "common"
NICO_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config"


class _Response:
    """Minimal context-manager response for urllib-based tests."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _load_nico_client() -> ModuleType:
    script_path = NICO_COMMON / "nico_client.py"
    spec = importlib.util.spec_from_file_location("test_nico_client", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nico_auth_prefers_explicit_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locally supplied NICo bearer token should be the simplest auth path."""
    module = _load_nico_client()
    monkeypatch.setenv("NICO_BEARER_TOKEN", "local-token")
    monkeypatch.setenv("NICO_ISSUER_URL", "https://issuer.example")
    monkeypatch.setenv("NICO_CLIENT_ID", "client-id")
    monkeypatch.setenv("NICO_CLIENT_SECRET", "client-secret")

    auth = module.resolve_auth()

    assert auth.token == "local-token"
    assert auth.source == "NICO_BEARER_TOKEN"


def test_nico_auth_uses_oidc_client_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no bearer token is supplied, NICo auth should use client_credentials."""
    module = _load_nico_client()
    monkeypatch.delenv("NICO_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("NICO_ISSUER_URL", "https://issuer.example/")
    monkeypatch.setenv("NICO_CLIENT_ID", "client-id")
    monkeypatch.setenv("NICO_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("NICO_OIDC_SCOPE", "read:nico")
    seen: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout: int = 30):
        seen.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "authorization": request.get_header("Authorization"),
                "content_type": request.get_header("Content-type"),
                "form": parse_qs(request.data.decode()) if request.data else {},
            }
        )
        if request.full_url.endswith("/.well-known/openid-configuration"):
            return _Response({"token_endpoint": "https://issuer.example/oauth/token"})
        return _Response({"access_token": "oidc-token"})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    auth = module.resolve_auth()

    assert auth.token == "oidc-token"
    assert auth.source == "oidc_client_credentials"
    assert seen == [
        {
            "url": "https://issuer.example/.well-known/openid-configuration",
            "timeout": 30,
            "authorization": None,
            "content_type": None,
            "form": {},
        },
        {
            "url": "https://issuer.example/oauth/token",
            "timeout": 30,
            "authorization": "Basic Y2xpZW50LWlkOmNsaWVudC1zZWNyZXQ=",
            "content_type": "application/x-www-form-urlencoded",
            "form": {"grant_type": ["client_credentials"], "scope": ["read:nico"]},
        },
    ]


@pytest.mark.parametrize("config_name,platform,step_name", [
    ("hardware_ingestion.yaml", "hardware_ingestion", "verify_ingestion"),
    ("dpu_health.yaml", "dpu_health", "check_dpu_health"),
])
def test_nico_configs_expose_api_base_setting(config_name: str, platform: str, step_name: str) -> None:
    """Shipped NICo configs should pass a configurable API base to scripts."""
    merged = merge_yaml_files([NICO_CONFIG / config_name])
    step = merged["commands"][platform]["steps"][0]

    assert merged["tests"]["settings"]["nico_api_base"] == (
        "{{env.NICO_API_BASE | default('https://api.ngc.nvidia.com/v2/org')}}"
    )
    assert step["name"] == step_name
    assert "--api-base" in step["args"]
    assert "{{nico_api_base}}" in step["args"]
