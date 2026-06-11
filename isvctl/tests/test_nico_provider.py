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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest
from isvtest.validations.governance import GovernanceMetricsCheck
from isvtest.validations.health import HealthAggregationCheck, HostHealthCheck
from isvtest.validations.infiniband import IbKeysConfiguredCheck, IbTenantIsolationCheck
from isvtest.validations.sanitization import (
    GpuMemorySanitizationCheck,
    MemorySanitizationCheck,
)

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


def _load_governance_metrics_script() -> ModuleType:
    """Load the query_metrics (governance) script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "governance" / "query_metrics.py"
    spec = importlib.util.spec_from_file_location("test_governance_query_metrics", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
        spec.loader.exec_module(module)
    return module


def _load_host_health_script() -> ModuleType:
    """Load the query_host_health script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "health" / "query_host_health.py"
    spec = importlib.util.spec_from_file_location("test_query_host_health", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
        spec.loader.exec_module(module)
    return module


def _load_health_aggregation_script() -> ModuleType:
    """Load the query_health_aggregation script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "health" / "query_health_aggregation.py"
    spec = importlib.util.spec_from_file_location("test_query_health_aggregation", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
        spec.loader.exec_module(module)
    return module


def _load_ufm_client() -> ModuleType:
    """Load the shared UFM client module directly from the provider scripts."""
    script_path = NICO_COMMON / "ufm_client.py"
    spec = importlib.util.spec_from_file_location("test_ufm_client", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ib_tenant_isolation_script() -> ModuleType:
    """Load the query_ib_tenant_isolation script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "infiniband" / "query_ib_tenant_isolation.py"
    spec = importlib.util.spec_from_file_location("test_query_ib_tenant_isolation", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
        spec.loader.exec_module(module)
    return module


def _load_ib_keys_script() -> ModuleType:
    """Load the query_ib_keys script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "infiniband" / "query_ib_keys.py"
    spec = importlib.util.spec_from_file_location("test_query_ib_keys", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _isolated_common_imports():
        spec.loader.exec_module(module)
    return module


def _load_sanitization_script() -> ModuleType:
    """Load the query_sanitization script as a module for direct unit testing."""
    script_path = NICO_SCRIPTS / "sanitization" / "query_sanitization.py"
    spec = importlib.util.spec_from_file_location("test_query_sanitization", script_path)
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


@pytest.mark.parametrize(
    "step_name",
    [
        "verify_ingestion",
        "check_dpu_health",
        "query_governance_metrics",
        "query_host_health",
        "query_health_aggregation",
        "query_ib_tenant_isolation",
        "query_ib_keys",
        "query_sanitization",
    ],
)
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
        ("query_metrics.py", _load_governance_metrics_script),
        ("query_host_health.py", _load_host_health_script),
        ("query_health_aggregation.py", _load_health_aggregation_script),
        ("query_ib_tenant_isolation.py", _load_ib_tenant_isolation_script),
        ("query_ib_keys.py", _load_ib_keys_script),
        ("query_sanitization.py", _load_sanitization_script),
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


# ---------------------------------------------------------------------------
# query_metrics (governance) script
# ---------------------------------------------------------------------------


def _governance_machine(
    *,
    status: str,
    gpus: int = 8,
    alerts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal NICo machine payload used to drive the governance script."""
    return {
        "id": f"m-{status.lower()}-{gpus}",
        "status": status,
        "machineCapabilities": [{"type": "GPU", "name": "H100", "count": gpus}],
        "health": {"alerts": alerts or []},
    }


def _run_governance_script(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    machines: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drive the governance script with mocked auth/API and return its JSON output."""
    module = _load_governance_metrics_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))
    monkeypatch.setattr(module, "forge_get_all", lambda *args, **kwargs: machines)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "query_metrics.py",
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
    return payload


# ---------------------------------------------------------------------------
# query_host_health (CAP05-01) script
# ---------------------------------------------------------------------------


def _run_script(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    script_name: str,
    machines: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drive a NICo health script with mocked auth/API and return its JSON output."""
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))
    monkeypatch.setattr(module, "forge_get_all", lambda *args, **kwargs: machines)
    monkeypatch.setattr(
        sys,
        "argv",
        [script_name, "--org", "test-org", "--site-id", "site-1", "--api-base", "http://127.0.0.1:8080/v2/org"],
    )

    exit_code = module.main()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0, payload
    return payload


def test_governance_script_classifies_each_status_bucket(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each MachineStatus value should land in the correct governance bucket."""
    machines = [
        _governance_machine(status="Ready", gpus=8),
        _governance_machine(status="Ready", gpus=8, alerts=[{"id": "FanSpeed"}]),
        _governance_machine(status="Maintenance", gpus=8),
        _governance_machine(status="InUse", gpus=8),
        _governance_machine(status="InUse", gpus=4),
        _governance_machine(status="Error", gpus=8),
        # The next two must be ignored entirely so they cannot leak into
        # Reserved/Active via permissive status matching.
        _governance_machine(status="Decommissioned", gpus=8),
        _governance_machine(status="Unknown", gpus=8),
    ]

    payload = _run_governance_script(monkeypatch, capsys, machines)

    assert payload["success"] is True
    assert payload["platform"] == "nico"
    assert payload["site_id"] == "site-1"
    assert payload["machine_count"] == len(machines)

    metrics = payload["metrics"]
    # Delivered excludes the Decommissioned + Unknown machines.
    assert metrics["delivered"] == {"nodes": 6, "gpus": 44}
    # Healthy excludes the machine with the FanSpeed alert.
    assert metrics["healthy"] == {"nodes": 5, "gpus": 36}
    # Reserved = InUse + Maintenance.
    assert metrics["reserved"] == {"nodes": 3, "gpus": 20}
    # Active = InUse only.
    assert metrics["active"] == {"nodes": 2, "gpus": 12}


def test_governance_script_empty_site_returns_zero_buckets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A site with no machines should still emit all four buckets zeroed out."""
    payload = _run_governance_script(monkeypatch, capsys, machines=[])

    assert payload["machine_count"] == 0
    assert payload["metrics"] == {
        "delivered": {"nodes": 0, "gpus": 0},
        "healthy": {"nodes": 0, "gpus": 0},
        "reserved": {"nodes": 0, "gpus": 0},
        "active": {"nodes": 0, "gpus": 0},
    }


def test_governance_script_tolerates_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nullable capability and health fields must not crash aggregation."""
    machines = [
        # No capabilities and no health at all -- counted as delivered + healthy
        # (no alerts means healthy) but contributes zero GPUs.
        {"id": "no-caps", "status": "Ready"},
        # Null inner fields, common in real responses.
        {"id": "null-fields", "status": "Ready", "machineCapabilities": None, "health": None},
    ]

    payload = _run_governance_script(monkeypatch, capsys, machines)

    assert payload["metrics"]["delivered"] == {"nodes": 2, "gpus": 0}
    assert payload["metrics"]["healthy"] == {"nodes": 2, "gpus": 0}
    assert payload["metrics"]["reserved"] == {"nodes": 0, "gpus": 0}
    assert payload["metrics"]["active"] == {"nodes": 0, "gpus": 0}


def test_governance_script_output_satisfies_validation_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: NICo governance JSON should pass GovernanceMetricsCheck."""
    payload = _run_governance_script(
        monkeypatch,
        capsys,
        machines=[
            _governance_machine(status="Ready", gpus=8),
            _governance_machine(status="InUse", gpus=8),
        ],
    )

    check = GovernanceMetricsCheck(config={"step_output": payload})
    check.run()
    assert check._passed is True, check._error


def test_host_health_script_reports_probes_and_components(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script reports probe IDs and an informational component breakdown."""
    module = _load_host_health_script()
    machines = [
        {
            "id": "m-1",
            "status": "Ready",
            "metadata": {"dmiData": {"chassisSerial": "SER-1"}},
            "health": {
                "observedAt": None,
                "successes": [
                    {"id": "BmcSensor", "target": "GPU0_Temp", "message": "temperature 'GPU0_Temp': OK"},
                    {"id": "BmcSensor", "target": "DIMM_A1", "message": "temperature 'DIMM_A1': OK"},
                    {"id": "BgpDaemonEnabled", "target": None},
                ],
                "alerts": [],
            },
        }
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_host_health.py", machines=machines)

    assert payload["success"] is True
    assert payload["hosts_checked"] == 1
    host = payload["hosts"][0]
    assert host["host_id"] == "m-1"
    assert host["chassis_serial"] == "SER-1"
    assert host["health_present"] is True
    assert host["healthy"] is True
    assert host["probe_ids"] == ["BgpDaemonEnabled", "BmcSensor"]
    assert host["alerts"] == []
    # Informational component breakdown: the GPU/DIMM temp sensors map to those buckets.
    comps = host["components"]
    assert comps["gpu"]["present"] is True and comps["gpu"]["probes"] == ["BmcSensor"]
    assert comps["thermal"]["present"] is True
    assert comps["memory"]["present"] is True


def test_host_health_script_surfaces_alert_classifications(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Alerts (incl. leak detection) are surfaced with their classifications."""
    module = _load_host_health_script()
    machines = [
        {
            "id": "m-2",
            "status": "Error",
            "health": {
                "successes": None,
                "alerts": [
                    {
                        "id": "BmcLeakDetection",
                        "target": "RackLeakDetector_1",
                        "message": "Leak detector reports leak",
                        "classifications": ["Leak", "LeakDetector"],
                    }
                ],
            },
        }
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_host_health.py", machines=machines)

    host = payload["hosts"][0]
    assert host["healthy"] is False
    assert host["alerts"][0]["id"] == "BmcLeakDetection"
    assert host["alerts"][0]["classifications"] == ["Leak", "LeakDetector"]
    assert host["components"]["cooling"]["present"] is True
    assert host["components"]["cooling"]["alerting"] is True


def test_host_health_script_computes_observation_age(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid observedAt timestamp yields a non-negative age in seconds."""
    module = _load_host_health_script()
    observed = (datetime.now(UTC) - timedelta(seconds=42)).strftime("%Y-%m-%dT%H:%M:%SZ")
    machines = [{"id": "m-3", "status": "Ready", "health": {"observedAt": observed, "successes": [], "alerts": []}}]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_host_health.py", machines=machines)

    age = payload["hosts"][0]["observed_age_seconds"]
    assert isinstance(age, int)
    assert 40 <= age <= 120


def test_governance_script_surfaces_api_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exceptions from the NICo client should be reported, not raised."""
    module = _load_governance_metrics_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(module, "forge_get_all", _boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "query_metrics.py",
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
    assert exit_code == 1
    assert payload["success"] is False
    assert "simulated outage" in payload["error"]


def test_host_health_real_world_bmc_sensors_pass_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A healthy NICo host (BmcSensor probes, no alerts) passes by default.

    Mirrors a live NICo site where machine health surfaces BMC sensors and no
    alerts. HostHealthCheck should pass: a report is returned and there are no
    alerts -- no dedicated memory probe is required.
    """
    module = _load_host_health_script()
    machines = [
        {
            "id": "m-1",
            "status": "Ready",
            "health": {
                "observedAt": None,
                "successes": [
                    {"id": "BmcSensor", "target": "GPU0_Temp", "message": "temperature 'GPU0_Temp': OK"},
                    {"id": "BmcSensor", "target": "FAN1", "message": "fan 'FAN1': OK"},
                    {"id": "BgpDaemonEnabled", "target": None},
                ],
                "alerts": [],
            },
        }
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_host_health.py", machines=machines)

    host = payload["hosts"][0]
    assert host["health_present"] is True
    assert host["healthy"] is True
    assert host["components"]["memory"]["present"] is False

    check = HostHealthCheck(config={"step_output": payload})
    check.run()
    assert check._passed is True, check._error


def test_host_health_leak_alert_fails_validation_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: a leak-detection alert flows through to a HostHealthCheck failure."""
    module = _load_host_health_script()
    machines = [
        {
            "id": "m-1",
            "status": "Error",
            "health": {
                "successes": [{"id": "BmcSensor", "target": "GPU0_Temp", "message": "temperature 'GPU0_Temp': OK"}],
                "alerts": [
                    {
                        "id": "BmcLeakDetection",
                        "target": "TrayLeakDetector_3",
                        "message": "2 leaking trays",
                        "classifications": ["Leak"],
                    }
                ],
            },
        }
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_host_health.py", machines=machines)

    check = HostHealthCheck(config={"step_output": payload})
    check.run()
    assert check._passed is False
    assert "BmcLeakDetection" in check._error or "1 alert(s)" in check._error


# ---------------------------------------------------------------------------
# query_health_aggregation (CAP05-02) script
# ---------------------------------------------------------------------------


def test_health_aggregation_script_groups_by_instance_type(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Machines should aggregate per instanceTypeId with consistent counts."""
    module = _load_health_aggregation_script()
    machines = [
        {"id": "m-1", "status": "Ready", "instanceTypeId": "it-a", "health": {"alerts": []}},
        {"id": "m-2", "status": "InUse", "instanceTypeId": "it-a", "health": {"alerts": []}},
        {"id": "m-3", "status": "Error", "instanceTypeId": "it-a", "health": {"alerts": []}},
        {"id": "m-4", "status": "Ready", "instanceTypeId": "it-b", "health": {"alerts": [{"id": "FanSpeed"}]}},
        {"id": "m-5", "status": "Ready", "instanceTypeId": None, "health": {"alerts": []}},
        # Decommissioned machines are excluded from the live fleet entirely.
        {"id": "m-6", "status": "Decommissioned", "instanceTypeId": "it-a", "health": {"alerts": []}},
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_health_aggregation.py", machines=machines)

    assert payload["aggregation_level"] == "nodegroup"
    groups = {g["group_id"]: g for g in payload["groups"]}
    assert groups["it-a"]["total"] == 3
    assert groups["it-a"]["healthy"] == 2
    assert groups["it-a"]["unhealthy"] == 1
    assert groups["it-a"]["status"] == "Degraded"
    assert groups["it-a"]["unhealthy_hosts"] == ["m-3"]
    assert groups["it-b"]["status"] == "Degraded"  # alert -> unhealthy
    assert groups["unassigned"]["total"] == 1 and groups["unassigned"]["status"] == "Healthy"


def test_health_aggregation_script_output_satisfies_validation_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: NICo aggregation JSON should pass HealthAggregationCheck."""
    module = _load_health_aggregation_script()
    machines = [
        {"id": "m-1", "status": "Ready", "instanceTypeId": "it-a", "health": {"alerts": []}},
        {"id": "m-2", "status": "Error", "instanceTypeId": "it-a", "health": {"alerts": []}},
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_health_aggregation.py", machines=machines)

    check = HealthAggregationCheck(config={"step_output": payload})
    check.run()
    assert check._passed is True, check._error


# ---------------------------------------------------------------------------
# ufm_client (UFM REST helper)
# ---------------------------------------------------------------------------


def test_ufm_resolve_auth_prefers_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A UFM token uses the /ufmRestV3 base path with a Basic auth header."""
    module = _load_ufm_client()
    monkeypatch.setenv("UFM_ADDRESS", "https://ufm.example:443")
    monkeypatch.setenv("UFM_TOKEN", "ufm-token")
    monkeypatch.delenv("UFM_USERNAME", raising=False)
    monkeypatch.delenv("UFM_PASSWORD", raising=False)

    auth = module.resolve_ufm_auth()

    assert auth.base_url == "https://ufm.example:443/ufmRestV3"
    assert auth.auth_header == "Basic ufm-token"
    assert auth.source == "UFM_TOKEN"
    assert auth.insecure is False


def test_ufm_resolve_auth_basic_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Username/password uses the /ufmRest base path with a base64 Basic header."""
    module = _load_ufm_client()
    monkeypatch.setenv("UFM_ADDRESS", "ufm.example")
    monkeypatch.delenv("UFM_TOKEN", raising=False)
    monkeypatch.setenv("UFM_USERNAME", "admin")
    monkeypatch.setenv("UFM_PASSWORD", "secret")
    monkeypatch.setenv("UFM_ALLOW_INSECURE", "1")
    expected_header = "Basic " + base64.b64encode(b"admin:secret").decode()

    auth = module.resolve_ufm_auth()

    assert auth.base_url == "https://ufm.example/ufmRest"
    assert auth.auth_header == expected_header
    assert auth.source == "UFM_USERNAME"
    assert auth.insecure is True


def test_ufm_resolve_auth_missing_address_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without UFM_ADDRESS, auth resolution raises."""
    module = _load_ufm_client()
    monkeypatch.delenv("UFM_ADDRESS", raising=False)
    monkeypatch.setenv("UFM_TOKEN", "ufm-token")

    with pytest.raises(module.UfmAuthError, match="UFM_ADDRESS"):
        module.resolve_ufm_auth()


def test_ufm_configured_requires_address_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """ufm_configured is True only with an address and a credential."""
    module = _load_ufm_client()
    monkeypatch.delenv("UFM_TOKEN", raising=False)
    monkeypatch.delenv("UFM_USERNAME", raising=False)
    monkeypatch.delenv("UFM_PASSWORD", raising=False)

    monkeypatch.delenv("UFM_ADDRESS", raising=False)
    assert module.ufm_configured() is False

    monkeypatch.setenv("UFM_ADDRESS", "https://ufm.example")
    assert module.ufm_configured() is False

    monkeypatch.setenv("UFM_TOKEN", "tok")
    assert module.ufm_configured() is True


def test_ufm_parse_key_value() -> None:
    """Key values parse from hex/decimal; junk and bools yield None."""
    module = _load_ufm_client()
    assert module.parse_key_value("0x10") == 16
    assert module.parse_key_value("16") == 16
    assert module.parse_key_value("0x0") == 0
    assert module.parse_key_value(8) == 8
    assert module.parse_key_value("") is None
    assert module.parse_key_value(None) is None
    assert module.parse_key_value(True) is None
    assert module.parse_key_value("nothex") is None


def test_ufm_get_sm_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sm_config fetches /app/smconf and returns the parsed object."""
    module = _load_ufm_client()
    smconf = {"subnet_prefix": "0xfe80", "m_key": "0x10", "sm_key": "0x20", "sa_key": "0x30", "m_key_per_port": True}
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout: int = 30, context: Any = None):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        return _Response(smconf)

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    auth = module.UfmAuth(
        base_url="https://ufm.example:443/ufmRestV3",
        auth_header="Basic ufm-token",
        insecure=False,
        source="UFM_TOKEN",
    )

    config = module.get_sm_config(auth)

    assert config == smconf
    assert seen["url"] == "https://ufm.example:443/ufmRestV3/app/smconf"
    assert seen["authorization"] == "Basic ufm-token"


# ---------------------------------------------------------------------------
# query_ib_tenant_isolation (SDN04-04) script
# ---------------------------------------------------------------------------


def _ib_partition(
    *,
    name: str,
    partition_key: str | None,
    tenant_id: str,
    status: str = "Ready",
) -> dict[str, Any]:
    """Build a minimal NICo InfiniBand partition payload."""
    return {"name": name, "partitionKey": partition_key, "tenantId": tenant_id, "status": status}


def test_ib_isolation_script_maps_partition_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script reduces NICo partitions to the neutral isolation fields."""
    module = _load_ib_tenant_isolation_script()
    partitions = [
        _ib_partition(name="turbo-net", partition_key="0x1", tenant_id="tenant-a"),
        _ib_partition(name="storage-net", partition_key="0x2", tenant_id="tenant-b"),
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_ib_tenant_isolation.py", machines=partitions)

    assert payload["success"] is True
    assert payload["platform"] == "nico"
    assert payload["partitions_checked"] == 2
    assert payload["partitions"][0] == {
        "name": "turbo-net",
        "partition_key": "0x1",
        "tenant_id": "tenant-a",
        "status": "Ready",
    }


def test_ib_isolation_script_skips_when_no_partitions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty partition list yields a structured skip, not a failure."""
    module = _load_ib_tenant_isolation_script()

    payload = _run_script(module, monkeypatch, capsys, script_name="query_ib_tenant_isolation.py", machines=[])

    assert payload["success"] is True
    assert payload["skipped"] is True
    assert "No InfiniBand partitions" in payload["skip_reason"]


def test_ib_isolation_script_surfaces_api_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exceptions from the NICo client are reported, not raised."""
    module = _load_ib_tenant_isolation_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(module, "forge_get_all", _boom)
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_ib_tenant_isolation.py", "--org", "o", "--site-id", "s", "--api-base", "http://127.0.0.1/v2/org"],
    )

    exit_code = module.main()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["success"] is False
    assert "simulated outage" in payload["error"]


def test_ib_isolation_script_output_satisfies_validation_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: NICo isolation JSON passes IbTenantIsolationCheck."""
    module = _load_ib_tenant_isolation_script()
    partitions = [
        _ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a"),
        _ib_partition(name="b", partition_key="0x2", tenant_id="tenant-b"),
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_ib_tenant_isolation.py", machines=partitions)

    check = IbTenantIsolationCheck(config={"step_output": payload})
    check.run()
    assert check._passed is True, check._error


def test_ib_isolation_shared_pkey_fails_validation_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: a P_Key shared by two tenants flows through to a failure."""
    module = _load_ib_tenant_isolation_script()
    partitions = [
        _ib_partition(name="a", partition_key="0x5", tenant_id="tenant-a"),
        _ib_partition(name="b", partition_key="0x5", tenant_id="tenant-b"),
    ]

    payload = _run_script(module, monkeypatch, capsys, script_name="query_ib_tenant_isolation.py", machines=partitions)

    check = IbTenantIsolationCheck(config={"step_output": payload})
    check.run()
    assert check._passed is False
    assert "shared across tenants" in check._error


# ---------------------------------------------------------------------------
# query_ib_keys (SDN04-05) script
# ---------------------------------------------------------------------------


def _run_ib_keys_script(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    partitions: list[dict[str, Any]],
    smconf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive the IB-keys script with mocked NICo partitions and optional UFM smconf.

    When ``smconf`` is None, UFM is treated as not configured.
    """
    module = _load_ib_keys_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))
    monkeypatch.setattr(module, "forge_get_all", lambda *args, **kwargs: partitions)

    if smconf is None:
        monkeypatch.setattr(module, "ufm_configured", lambda: False)
    else:
        monkeypatch.setattr(module, "ufm_configured", lambda: True)
        monkeypatch.setattr(
            module,
            "resolve_ufm_auth",
            lambda: SimpleNamespace(base_url="https://ufm/ufmRestV3", auth_header="Basic x", insecure=False),
        )
        monkeypatch.setattr(module, "get_sm_config", lambda auth: smconf)

    monkeypatch.setattr(
        sys,
        "argv",
        ["query_ib_keys.py", "--org", "o", "--site-id", "s", "--api-base", "http://127.0.0.1/v2/org"],
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    return payload


def test_ib_keys_script_pkey_from_partitions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P_Key evidence is derived from non-default partition keys."""
    partitions = [
        _ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a"),
        _ib_partition(name="b", partition_key="0x2", tenant_id="tenant-b"),
        # The default all-ports partition does not count as a tenant P_Key.
        _ib_partition(name="management", partition_key="0x7fff", tenant_id=""),
    ]

    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=partitions)

    assert payload["success"] is True
    assert payload["partitions_with_pkey"] == 2
    assert payload["keys"]["p_key"]["configured"] is True
    assert payload["keys"]["p_key"]["source"] == "nico"


def test_ib_keys_script_full_member_default_excluded_from_pkey_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A full-member default P_Key (0xffff) does not count as a tenant P_Key."""
    partitions = [
        _ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a"),
        # Full-member default partition: same partition number as 0x7fff.
        _ib_partition(name="management", partition_key="0xffff", tenant_id=""),
    ]

    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=partitions)

    assert payload["partitions_with_pkey"] == 1
    assert payload["keys"]["p_key"]["configured"] is True


def test_ib_keys_script_management_key_unverified_without_ufm(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without UFM access the Management Key is reported as unverified (null)."""
    partitions = [_ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a")]

    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=partitions)

    mgmt = payload["keys"]["management_key"]
    assert mgmt["configured"] is None
    assert "UFM access not configured" in mgmt["detail"]
    # The OpenSM/SHARP host keys are always reported as unverified.
    assert payload["keys"]["congestion_control_key"]["configured"] is None
    assert set(payload["keys"]) >= {
        "p_key",
        "management_key",
        "aggregation_management_key",
        "vendor_specific_key",
        "congestion_control_key",
        "node2node_key",
        "manager2node_key",
    }


def test_ib_keys_script_management_key_configured_from_ufm(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-zero m_key with per-port protection marks the Management Key configured."""
    partitions = [_ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a")]
    smconf = {"m_key": "0x771d2fe77f553d47", "sm_key": "0x20", "sa_key": "0x30", "m_key_per_port": True}

    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=partitions, smconf=smconf)

    mgmt = payload["keys"]["management_key"]
    assert mgmt["configured"] is True
    assert mgmt["source"] == "ufm"
    # The raw key value must never be emitted.
    assert "0x771d2fe77f553d47" not in json.dumps(payload)


def test_ib_keys_script_management_key_insecure_when_mkey_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An m_key of 0 marks the Management Key explicitly NOT configured."""
    partitions = [_ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a")]
    smconf = {"m_key": "0x0", "sm_key": "0x20", "sa_key": "0x30", "m_key_per_port": True}

    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=partitions, smconf=smconf)

    assert payload["keys"]["management_key"]["configured"] is False


def test_ib_keys_script_management_key_insecure_without_per_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A set m_key without per-port protection is not a configured Management Key."""
    partitions = [_ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a")]
    smconf = {"m_key": "0x10", "sm_key": "0x20", "sa_key": "0x30", "m_key_per_port": False}

    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=partitions, smconf=smconf)

    assert payload["keys"]["management_key"]["configured"] is False
    assert "m_key_per_port" in payload["keys"]["management_key"]["detail"]


def test_ib_keys_script_skips_when_no_partitions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty partition list yields a structured skip."""
    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=[])

    assert payload["skipped"] is True
    assert "cannot evidence the P_Key" in payload["skip_reason"]


def test_ib_keys_script_output_satisfies_validation_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: NICo IB-keys JSON (with UFM) passes IbKeysConfiguredCheck."""
    partitions = [_ib_partition(name="a", partition_key="0x1", tenant_id="tenant-a")]
    smconf = {"m_key": "0x10", "sm_key": "0x20", "sa_key": "0x30", "m_key_per_port": True}

    payload = _run_ib_keys_script(monkeypatch, capsys, partitions=partitions, smconf=smconf)

    check = IbKeysConfiguredCheck(config={"step_output": payload, "required_keys": ["p_key", "management_key"]})
    check.run()
    assert check._passed is True, check._error


def test_ib_keys_script_surfaces_api_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exceptions from the NICo client are reported, not raised."""
    module = _load_ib_keys_script()
    monkeypatch.setattr(module, "resolve_auth", lambda: SimpleNamespace(token="test-token"))

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(module, "forge_get_all", _boom)
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_ib_keys.py", "--org", "o", "--site-id", "s", "--api-base", "http://127.0.0.1/v2/org"],
    )

    exit_code = module.main()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["success"] is False
    assert "simulated outage" in payload["error"]


# ---------------------------------------------------------------------------
# query_sanitization (SEC21-04/05/06) script
# ---------------------------------------------------------------------------


def _sanitization_machine(
    *,
    machine_id: str = "m-1",
    status: str = "Ready",
    history_statuses: list[str] | None = None,
    is_usable: bool = True,
    instance_id: str | None = None,
    tenant_id: str | None = None,
    gpus: int = 8,
    bios_version: str = "U8E122J-1.51",
) -> dict[str, Any]:
    """Build a NICo machine payload to drive the sanitization script.

    ``history_statuses`` is given oldest-first; each is converted into a
    statusHistory entry with an increasing ``created`` timestamp.
    """
    if history_statuses is None:
        history_statuses = ["InUse", "Reset", "Ready"]
    status_history = [
        {"status": s, "message": "", "created": f"2026-01-01T00:0{i}:00Z"} for i, s in enumerate(history_statuses)
    ]
    capabilities = [{"type": "GPU", "name": "H100", "count": gpus}] if gpus else []
    return {
        "id": machine_id,
        "status": status,
        "isUsableByTenant": is_usable,
        "instanceId": instance_id,
        "tenantId": tenant_id,
        "vendor": "Lenovo",
        "productName": "ThinkSystem SR670 V2",
        "machineCapabilities": capabilities,
        "statusHistory": status_history,
        "metadata": {"dmiData": {"biosVersion": bios_version}},
    }


def test_sanitization_status_token_mapping() -> None:
    """NICo statuses map to the provider-neutral lifecycle tokens."""
    module = _load_sanitization_script()
    assert module.status_token("InUse") == "in_use"
    assert module.status_token("Reset") == "sanitizing"
    assert module.status_token("Ready") == "available"
    assert module.status_token("Maintenance") == "maintenance"
    assert module.status_token(None) == "unknown"


def test_sanitization_ordered_history_appends_current() -> None:
    """History is sorted by created time and the live status is appended once."""
    module = _load_sanitization_script()
    machine = {
        "status": "Ready",
        "statusHistory": [
            {"status": "Reset", "created": "2026-01-01T00:01:00Z"},
            {"status": "InUse", "created": "2026-01-01T00:00:00Z"},
        ],
    }
    assert module.ordered_history_statuses(machine) == ["InUse", "Reset", "Ready"]


def test_sanitization_evaluate_transitions_logic() -> None:
    """The gate flags in_use -> available without an intervening sanitizing stage."""
    module = _load_sanitization_script()
    assert module.evaluate_transitions(["in_use", "sanitizing", "available"]) == (True, True)
    assert module.evaluate_transitions(["in_use", "available"]) == (True, False)
    # maintenance between in_use and available does not satisfy the gate.
    assert module.evaluate_transitions(["in_use", "maintenance", "available"]) == (True, False)
    # never served a tenant -> nothing to sanitize.
    assert module.evaluate_transitions(["initializing", "available"]) == (False, True)


def _run_sanitization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    machines: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drive the sanitization script with mocked auth/API and return its JSON output."""
    module = _load_sanitization_script()
    return _run_script(module, monkeypatch, capsys, script_name="query_sanitization.py", machines=machines)


def test_sanitization_script_builds_clean_record(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A host that went InUse -> Reset -> Ready is sanitized and available."""
    payload = _run_sanitization(monkeypatch, capsys, [_sanitization_machine()])

    assert payload["success"] is True
    assert payload["machines_checked"] == 1
    record = payload["machines"][0]
    assert record["served_tenant"] is True
    assert record["sanitized"] is True
    assert record["available"] is True
    assert record["in_use"] is False
    assert record["has_gpu"] is True
    assert record["stale_tenant_binding"] is False
    assert record["bios_version"] == "U8E122J-1.51"
    assert record["transitions"] == ["in_use", "sanitizing", "available"]


def test_sanitization_script_flags_skipped_sanitization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A host that went InUse -> Ready (no Reset) is flagged unsanitized."""
    machine = _sanitization_machine(history_statuses=["InUse", "Ready"])
    payload = _run_sanitization(monkeypatch, capsys, [machine])

    record = payload["machines"][0]
    assert record["served_tenant"] is True
    assert record["sanitized"] is False


def test_sanitization_script_flags_stale_tenant_binding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Ready+usable host still bound to an instance is a stale binding."""
    machine = _sanitization_machine(
        history_statuses=["InUse", "Reset", "Ready"],
        instance_id="59bdaaff-3998-4fd9-a140-8749beeb605e",
    )
    payload = _run_sanitization(monkeypatch, capsys, [machine])

    record = payload["machines"][0]
    assert record["available"] is False
    assert record["stale_tenant_binding"] is True


def test_sanitization_script_marks_in_use_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A host currently InUse is not yet returned to the pool (still sanitized=true)."""
    machine = _sanitization_machine(status="InUse", history_statuses=["Ready", "InUse"], is_usable=False)
    payload = _run_sanitization(monkeypatch, capsys, [machine])

    record = payload["machines"][0]
    assert record["in_use"] is True
    assert record["available"] is False
    assert record["served_tenant"] is True
    assert record["sanitized"] is True


def test_sanitization_script_output_satisfies_memory_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: clean NICo JSON passes the memory check; a skipped reset fails."""
    clean = _run_sanitization(monkeypatch, capsys, [_sanitization_machine()])
    check = MemorySanitizationCheck(config={"step_output": clean})
    check.run()
    assert check._passed is True, check._error

    dirty = _run_sanitization(monkeypatch, capsys, [_sanitization_machine(history_statuses=["InUse", "Ready"])])
    bad = MemorySanitizationCheck(config={"step_output": dirty})
    bad.run()
    assert bad._passed is False
    assert "1/1 machine(s)" in bad._error
    sub = next(r for r in bad._subtest_results if r["name"].startswith("memory_"))
    assert "without sanitization" in sub["message"]


def test_sanitization_script_output_satisfies_gpu_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: a sanitized GPU host passes the GPU-memory check."""
    payload = _run_sanitization(monkeypatch, capsys, [_sanitization_machine(gpus=8)])
    check = GpuMemorySanitizationCheck(config={"step_output": payload})
    check.run()
    assert check._passed is True, check._error
