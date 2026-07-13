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

"""Tests for providers/shared/mutual_tls_test.py."""

from __future__ import annotations

import importlib.util
import json
import ssl
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "isvctl" / "configs" / "providers" / "shared" / "mutual_tls_test.py"
)
_spec = importlib.util.spec_from_file_location("mutual_tls_test", _SCRIPT_PATH)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_parse_endpoints_accepts_host_port_list() -> None:
    """Comma-separated host:port strings parse into tuples."""
    assert probe._parse_endpoints("a.example:443,b.example:8443") == [
        ("a.example", 443),
        ("b.example", 8443),
    ]


def test_parse_endpoints_rejects_missing_port() -> None:
    """Endpoints without a port raise ValueError."""
    with pytest.raises(ValueError, match="host:port"):
        probe._parse_endpoints("edge.example")


def test_demo_result_emits_both_planes() -> None:
    """Demo mode contract includes both required planes."""
    result = probe._demo_result()
    assert result["success"] is True
    assert result["endpoints_tested"] == 2
    assert result["tests"]["north_south_mtls_enforced"]["passed"] is True
    assert result["tests"]["east_west_mtls_enforced"]["passed"] is True


def test_run_skips_when_no_endpoints() -> None:
    """Empty endpoint lists produce a structured skip."""
    result = probe.run_mutual_tls_probe(
        north_south_endpoints=[],
        east_west_endpoints=[],
        ca_cert=None,
        client_cert=None,
        client_key=None,
        timeout=1.0,
    )
    assert result["skipped"] is True
    assert "No SEC13-01 endpoints configured" in result["skip_reason"]


def test_run_fails_when_endpoints_without_certs() -> None:
    """Endpoints without cert paths fail with bad_input."""
    result = probe.run_mutual_tls_probe(
        north_south_endpoints=[("edge.example", 443)],
        east_west_endpoints=[],
        ca_cert=None,
        client_cert=None,
        client_key=None,
        timeout=1.0,
        east_west_provider_hidden_message="hidden",
    )
    assert result["success"] is False
    assert result["error_type"] == "bad_input"


def test_run_with_provider_hidden_east_west_and_probed_north_south(tmp_path: Path) -> None:
    """North-south probe + east-west provider-hidden satisfies the contract."""
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "client.pem"
    key = tmp_path / "client.key"
    for path in (ca, cert, key):
        path.write_text("placeholder\n", encoding="utf-8")

    def fake_probe(
        host: str,
        port: int,
        *,
        anonymous_context: ssl.SSLContext,
        authenticated_context: ssl.SSLContext,
        timeout: float,
        plane: str,
    ) -> dict[str, Any]:
        return {
            "host": host,
            "port": port,
            "plane": plane,
            "anonymous_rejected": True,
            "authenticated_accepted": True,
            "passed": True,
            "detail": {},
        }

    with (
        patch.object(probe, "_ssl_context", return_value=MagicMock()),
        patch.object(probe, "probe_mtls_endpoint", side_effect=fake_probe),
    ):
        result = probe.run_mutual_tls_probe(
            north_south_endpoints=[("edge.example", 443)],
            east_west_endpoints=[],
            ca_cert=ca,
            client_cert=cert,
            client_key=key,
            timeout=1.0,
            east_west_provider_hidden_message="AWS east-west is provider-hidden",
        )

    assert result["success"] is True
    assert result["endpoints_tested"] == 1
    assert result["tests"]["north_south_mtls_enforced"]["passed"] is True
    assert result["tests"]["east_west_mtls_enforced"]["provider_hidden"] is True


def test_handshake_marks_ssl_error_as_rejected() -> None:
    """SSL errors during wrap_socket classify as not accepted."""
    context = MagicMock()
    raw = MagicMock()
    raw.__enter__ = MagicMock(return_value=raw)
    raw.__exit__ = MagicMock(return_value=None)
    raw.settimeout = MagicMock()

    def raise_ssl(*_args: object, **_kwargs: object) -> None:
        raise ssl.SSLError("certificate required")

    context.wrap_socket.side_effect = raise_ssl

    with patch.object(probe.socket, "create_connection", return_value=raw):
        result = probe._handshake("edge.example", 443, context, 1.0)

    assert result["accepted"] is False
    assert "SSLError" in result["detail"]


def test_main_demo_mode(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI demo mode prints the demo contract and exits 0."""
    monkeypatch.setattr(probe, "DEMO_MODE", True)
    monkeypatch.setattr(
        "sys.argv",
        ["mutual_tls_test.py"],
    )
    assert probe.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["test_name"] == "mutual_tls"
    assert payload["success"] is True
