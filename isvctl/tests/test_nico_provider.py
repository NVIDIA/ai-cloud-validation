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
from collections.abc import Callable, Iterator
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
    """Load the shared NICo client module directly from the provider scripts."""
    script_path = NICO_COMMON / "nico_client.py"
    spec = importlib.util.spec_from_file_location("test_nico_client", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _isolated_common_imports() -> Iterator[None]:
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
    """Load the check_dpu_health script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "dpu" / "check_dpu_health.py"
    spec = importlib.util.spec_from_file_location("test_check_dpu_health", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
        spec.loader.exec_module(module)
    return module


def _load_ingestion_script() -> ModuleType:
    """Load the verify_ingestion script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "hardware_ingestion" / "verify_ingestion.py"
    spec = importlib.util.spec_from_file_location("test_verify_ingestion", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
        spec.loader.exec_module(module)
    return module


def test_nico_auth_prefers_explicit_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locally supplied NICo bearer token should be the simplest auth path."""
    module = _load_nico_client()
    monkeypatch.setenv("NICO_BEARER_TOKEN", "local-token")
    monkeypatch.setenv("NICO_SSA_ISSUER", "https://issuer.example")
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
    monkeypatch.setenv("NICO_SSA_ISSUER", "https://issuer.example/")
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


def test_forge_get_all_handles_bare_list_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some NICo endpoints return a top-level JSON list rather than a wrapped object."""
    module = _load_nico_client()

    def fake_forge_get(org, path, token, *, base_url, params=None, timeout=30):
        # First page is full (== effective page size) so pagination continues;
        # the short second page ends it.
        if int(params["pageNumber"]) == 1:
            return [{"id": f"m-{i}"} for i in range(100)]
        return [{"id": "m-100"}]

    monkeypatch.setattr(module, "forge_get", fake_forge_get)

    items = module.forge_get_all("org", "machine", "tok", base_url="http://x", result_key="machines")

    assert len(items) == 101
    assert items[0] == {"id": "m-0"}
    assert items[-1] == {"id": "m-100"}


def test_forge_get_all_extracts_result_key_from_wrapped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other NICo endpoints wrap the results array under result_key."""
    module = _load_nico_client()

    def fake_forge_get(org, path, token, *, base_url, params=None, timeout=30):
        return {"machines": [{"id": "m-1"}], "pageNumber": 1}

    monkeypatch.setattr(module, "forge_get", fake_forge_get)

    items = module.forge_get_all("org", "machine", "tok", base_url="http://x", result_key="machines")

    assert items == [{"id": "m-1"}]


@pytest.mark.parametrize("step_name", ["verify_ingestion", "check_dpu_health"])
def test_nico_bare_metal_config_exposes_api_base_setting(step_name: str) -> None:
    """The shipped NICo bare_metal config should pass a configurable API base to scripts."""
    merged = merge_yaml_files([NICO_CONFIG / "bare_metal.yaml"])
    steps = merged["commands"]["bare_metal"]["steps"]
    step = next(s for s in steps if s["name"] == step_name)

    assert merged["tests"]["settings"]["org"] == "{{env.NICO_ORGANIZATION}}"
    assert merged["tests"]["settings"]["nico_api_base"] == "{{env.NICO_API_BASE}}"
    assert "--api-base" in step["args"]
    assert "{{nico_api_base}}" in step["args"]


@pytest.mark.parametrize(
    ("script_name", "load_script"),
    [
        ("verify_ingestion.py", _load_ingestion_script),
        ("check_dpu_health.py", _load_dpu_health_script),
    ],
)
def test_nico_scripts_require_api_base(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script_name: str,
    load_script: Callable[[], ModuleType],
) -> None:
    """NICo scripts should not fall back to a built-in API base."""
    module = load_script()
    monkeypatch.setattr(sys, "argv", [script_name, "--org", "test-org", "--site-id", "site-1"])
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))
    monkeypatch.setattr(module, "forge_get_all", lambda *args, **kwargs: [])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "--api-base" in captured.err


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
        [
            "check_dpu_health.py",
            "--org",
            "test-org",
            "--site-id",
            "site-1",
            "--api-base",
            "http://127.0.0.1:8080/v2/org",
        ],
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
        [
            "check_dpu_health.py",
            "--org",
            "test-org",
            "--site-id",
            "site-1",
            "--api-base",
            "http://127.0.0.1:8080/v2/org",
        ],
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
        [
            "check_dpu_health.py",
            "--org",
            "test-org",
            "--site-id",
            "site-1",
            "--api-base",
            "http://127.0.0.1:8080/v2/org",
        ],
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
