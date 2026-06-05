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

import base64
import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest

from isvctl.config.merger import merge_yaml_files

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
NICO_COMMON = ISVCTL_ROOT / "configs" / "providers" / "nico" / "scripts" / "common"
NICO_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config"
NICO_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "nico" / "scripts"


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


@contextlib.contextmanager
def _isolated_common_imports():
    """Make a nico script's ``from common...`` resolve to the nico scripts package.

    Other providers (e.g. aws) ship a sibling top-level ``common`` package, and an
    earlier test in the suite may have cached it in ``sys.modules``. Drop any cached
    ``common`` modules for the duration of the load, then restore them.
    """
    saved = {name: mod for name, mod in sys.modules.items() if name == "common" or name.startswith("common.")}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if n == "common" or n.startswith("common.")]:
            del sys.modules[name]
        sys.modules.update(saved)


def _load_dpu_health_script() -> ModuleType:
    script_path = NICO_SCRIPTS / "dpu" / "check_dpu_health.py"
    spec = importlib.util.spec_from_file_location("test_check_dpu_health", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
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
    client_id = "client-id"
    client_secret = "client-secret"
    monkeypatch.setenv("NICO_ISSUER_URL", "https://issuer.example/")
    monkeypatch.setenv("NICO_CLIENT_ID", client_id)
    monkeypatch.setenv("NICO_CLIENT_SECRET", client_secret)
    monkeypatch.setenv("NICO_OIDC_SCOPE", "read:nico")
    # Build the placeholder Basic header instead of hardcoding its Base64 form
    # so secret scanners do not mistake the test fixture for a live credential.
    expected_authorization = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
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
            "authorization": expected_authorization,
            "content_type": "application/x-www-form-urlencoded",
            "form": {"grant_type": ["client_credentials"], "scope": ["read:nico"]},
        },
    ]


@pytest.mark.parametrize("step_name", ["verify_ingestion", "check_dpu_health"])
def test_nico_bare_metal_config_exposes_api_base_setting(step_name: str) -> None:
    """The shipped NICo bare_metal config should pass a configurable API base to scripts."""
    merged = merge_yaml_files([NICO_CONFIG / "bare_metal.yaml"])
    steps = merged["commands"]["bare_metal"]["steps"]
    step = next(s for s in steps if s["name"] == step_name)

    assert merged["tests"]["settings"]["nico_api_base"] == (
        "{{env.NICO_API_BASE | default('https://api.ngc.nvidia.com/v2/org')}}"
    )
    assert "--api-base" in step["args"]
    assert "{{nico_api_base}}" in step["args"]


def test_dpu_health_script_treats_nullable_machine_lists_as_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NICo JSON null list fields should not crash DPU health extraction."""
    module = _load_dpu_health_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))
    monkeypatch.setattr(
        module,
        "forge_get_all",
        lambda *args, **kwargs: [
            {
                "id": "machine-1",
                "status": "Ready",
                "metadata": {"dmiData": {"chassisSerial": "SER-1"}},
                "machineCapabilities": [{"type": "DPU", "name": "BlueField-3", "count": 2}],
                "health": {"alerts": None, "successes": None},
            }
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_dpu_health.py", "--org", "test-org", "--site-id", "site-1"],
    )

    exit_code = module.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0, payload
    assert payload["success"] is True
    assert payload["machines_checked"] == 1
    assert payload["machines"][0]["dpu_count"] == 2
    # chassis_serial is a debug aid sourced from dmiData (never falls back to machine_id)
    assert payload["machines"][0]["chassis_serial"] == "SER-1"
    assert payload["machines"][0]["health_successes"] == []
    assert payload["machines"][0]["health_alerts"] == []
    assert payload["machines"][0]["dpu_agent_heartbeat"] is True


def test_dpu_health_script_skips_machines_without_dpu(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machines without a DPU capability are filtered out client-side."""
    module = _load_dpu_health_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))
    monkeypatch.setattr(
        module,
        "forge_get_all",
        lambda *args, **kwargs: [
            {"id": "gpu-only", "status": "Ready", "machineCapabilities": [{"type": "GPU", "name": "H100", "count": 8}]},
            {"id": "no-caps", "status": "Ready", "machineCapabilities": None},
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_dpu_health.py", "--org", "test-org", "--site-id", "site-1"],
    )

    exit_code = module.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0, payload
    assert payload["success"] is True
    assert payload["machines_checked"] == 0
    assert payload["machines"] == []


def test_dpu_health_script_treats_nullable_alert_fields_as_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NICo health alerts can contain null target fields."""
    module = _load_dpu_health_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))
    monkeypatch.setattr(
        module,
        "forge_get_all",
        lambda *args, **kwargs: [
            {
                "id": "machine-1",
                "status": "Ready",
                "machineCapabilities": [{"type": "DPU", "name": "DPU", "count": 1}],
                "health": {
                    "successes": [{"id": "DpuDiskUtilizationCheck", "target": None}],
                    "alerts": [
                        {
                            "id": "ContainerExists",
                            "target": None,
                            "message": "container inventory unavailable",
                        }
                    ],
                },
            }
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_dpu_health.py", "--org", "test-org", "--site-id", "site-1"],
    )

    exit_code = module.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0, payload
    assert payload["success"] is True
    assert payload["machines"][0]["health_summary"] == "unhealthy"
    assert payload["machines"][0]["health_successes"] == ["DpuDiskUtilizationCheck"]
    assert payload["machines"][0]["health_alerts"] == []
    assert payload["machines"][0]["dpu_agent_heartbeat"] is True
