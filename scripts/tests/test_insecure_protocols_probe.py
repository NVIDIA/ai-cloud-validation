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

"""Tests for providers/shared/insecure_protocols_test.py.

Cover the protocol-framing primitive and the response-classification table
without opening real sockets - the wire format is RFC-frozen so we can
exercise the classifier against canned ServerHello / Alert / RST replies.
"""

from __future__ import annotations

import importlib.util
import io
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "isvctl" / "configs" / "providers" / "shared" / "insecure_protocols_test.py"
)
_spec = importlib.util.spec_from_file_location("insecure_protocols_test", _SCRIPT_PATH)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class _FakeSocket:
    """In-memory socket double for probe_tls_version / probe_plain_http."""

    def __init__(self, reply: bytes = b"", raise_on_connect: type[BaseException] | None = None) -> None:
        self._buf = io.BytesIO(reply)
        self.sent: bytes = b""
        self._raise_on_connect = raise_on_connect

    def __enter__(self) -> _FakeSocket:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def settimeout(self, _t: float) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        if self._raise_on_connect is not None:
            raise self._raise_on_connect()
        self.sent += data

    def recv(self, n: int) -> bytes:
        return self._buf.read(n)


def _wrap_with_create_connection(reply: bytes, raise_cls: type[BaseException] | None = None):
    """Patch socket.create_connection to return a _FakeSocket with reply."""
    fake = _FakeSocket(reply=reply, raise_on_connect=raise_cls)

    def _create_connection(_addr: tuple[str, int], timeout: float | None = None) -> _FakeSocket:
        return fake

    return patch.object(probe.socket, "create_connection", side_effect=_create_connection)


def _wrap_with_create_connection_error(exc: BaseException):
    """Patch socket.create_connection to raise a pre-connect exception."""

    def _create_connection(_addr: tuple[str, int], timeout: float | None = None) -> _FakeSocket:
        raise exc

    return patch.object(probe.socket, "create_connection", side_effect=_create_connection)


def _server_hello_record(server_version: int) -> bytes:
    """Build a minimal TLS handshake record carrying a ServerHello of server_version.

    Layout (per RFC 5246 §7.4.1.3): handshake type (0x02), 3-byte length,
    2-byte ServerHello.server_version, 32-byte random.
    """
    body = server_version.to_bytes(2, "big") + b"\x00" * 32
    handshake = b"\x02" + len(body).to_bytes(3, "big") + body
    # Outer record type 0x16 (handshake); the record-layer version isn't
    # what classification uses, so we set it to the same as ServerHello.
    return b"\x16" + server_version.to_bytes(2, "big") + len(handshake).to_bytes(2, "big") + handshake


def _alert_record(level: int = 2, desc: int = 40) -> bytes:
    """Build a minimal TLS Alert record (handshake_failure by default)."""
    return b"\x15\x03\x01\x00\x02" + bytes([level, desc])


def _client_hello_parts(wire: bytes) -> tuple[set[int], dict[int, bytes]]:
    """Return offered cipher suites and extensions from a ClientHello record."""
    assert wire[0] == 0x16
    assert wire[5] == 0x01

    offset = 9  # record header + handshake header
    offset += 2 + 32  # client_version + random
    session_id_len = wire[offset]
    offset += 1 + session_id_len

    cipher_len = int.from_bytes(wire[offset : offset + 2], "big")
    offset += 2
    cipher_bytes = wire[offset : offset + cipher_len]
    offset += cipher_len
    ciphers = {int.from_bytes(cipher_bytes[i : i + 2], "big") for i in range(0, len(cipher_bytes), 2)}

    compression_len = wire[offset]
    offset += 1 + compression_len
    if offset == len(wire):
        return ciphers, {}

    extensions_len = int.from_bytes(wire[offset : offset + 2], "big")
    offset += 2
    extensions_end = offset + extensions_len
    extensions: dict[int, bytes] = {}
    while offset < extensions_end:
        ext_type = int.from_bytes(wire[offset : offset + 2], "big")
        ext_len = int.from_bytes(wire[offset + 2 : offset + 4], "big")
        offset += 4
        extensions[ext_type] = wire[offset : offset + ext_len]
        offset += ext_len
    return ciphers, extensions


class TestProbeTlsVersion:
    """Cover the trap-cases the user spec called out."""

    def test_server_accepts_requested_version(self) -> None:
        """ServerHello.server_version == requested -> accepted."""
        with _wrap_with_create_connection(_server_hello_record(0x0301)):
            result = probe.probe_tls_version("example.com", 443, 0x0301)
        assert result["category"] == "accepted"
        assert result["requested_version"] == "0x0301"
        assert result["chosen_version"] == "0x0301"

    def test_server_negotiates_down_to_request_is_not_acceptance(self) -> None:
        """The single biggest trap: server replies with a LOWER ServerHello version.

        A server can answer a TLSv1.2 request with ServerHello.version=0x0301
        when it does not support 1.2 - that is NOT acceptance of the
        requested version. The probe must classify it as 'downgraded:<hex>'
        and treat the requested version as refused.
        """
        with _wrap_with_create_connection(_server_hello_record(0x0301)):
            result = probe.probe_tls_version("example.com", 443, 0x0303)
        assert result["category"] == "downgraded:0x0301"
        assert result["chosen_version"] == "0x0301"

    def test_alert_record_is_refused(self) -> None:
        """A TLS Alert (e.g. handshake_failure) classifies as refused."""
        with _wrap_with_create_connection(_alert_record()):
            result = probe.probe_tls_version("example.com", 443, 0x0300)
        assert result["category"] == "refused"
        assert "alert" in result["detail"]

    def test_tcp_reset_is_closed(self) -> None:
        """RST during sendall classifies as closed, not raised."""
        with _wrap_with_create_connection(b"", raise_cls=ConnectionResetError):
            result = probe.probe_tls_version("example.com", 443, 0x0300)
        assert result["category"] == "closed"
        assert result["detail"] == "ConnectionResetError"

    def test_timeout_is_timeout(self) -> None:
        """socket.timeout during sendall classifies as timeout."""
        with _wrap_with_create_connection(b"", raise_cls=socket.timeout):
            result = probe.probe_tls_version("example.com", 443, 0x0300)
        assert result["category"] == "timeout"

    def test_zero_length_read_is_closed(self) -> None:
        """Server closes the TCP connection without sending a record."""
        with _wrap_with_create_connection(b""):
            result = probe.probe_tls_version("example.com", 443, 0x0300)
        assert result["category"] == "closed"
        assert "before record header" in result["detail"]

    def test_resolution_error_is_probe_error(self) -> None:
        """DNS failures are inconclusive probe errors, not protocol refusal."""
        err = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        with _wrap_with_create_connection_error(err):
            result = probe.probe_tls_version("typo.invalid", 443, 0x0301)
        assert result["category"] == "error"
        assert "gaierror" in result["detail"]


class TestBuildClientHello:
    """Pin two framing invariants: cipher list and SSLv3 extension asymmetry."""

    def test_hello_includes_legacy_ciphers_without_fallback_scsv(self) -> None:
        """The historic suites must be present, otherwise SSLv3-enabled servers
        will return ``handshake_failure`` for lack of cipher overlap and we
        will misclassify the endpoint as disabled. TLS_FALLBACK_SCSV is omitted
        because these are protocol-acceptance probes, not fallback probes.
        """
        wire = probe._build_client_hello("example.com", 0x0300)
        ciphers, _extensions = _client_hello_parts(wire)

        # 0x000a (3DES) and 0x0004 (RC4_MD5) are markers for "historic suites included".
        assert 0x000A in ciphers
        assert 0x0004 in ciphers
        assert 0x00FF not in ciphers

        for version in probe.VERSIONS.values():
            ciphers, _extensions = _client_hello_parts(probe._build_client_hello("example.com", version))
            assert 0x00FF not in ciphers

    def test_hello_includes_ecdsa_legacy_ciphers(self) -> None:
        """ECDSA-only TLSv1.0/1.1 endpoints need ECDSA CBC suites offered."""
        wire = probe._build_client_hello("example.com", 0x0301)
        ciphers, _extensions = _client_hello_parts(wire)

        assert 0xC003 in ciphers  # TLS_ECDH_ECDSA_WITH_3DES_EDE_CBC_SHA
        assert 0xC004 in ciphers  # TLS_ECDH_ECDSA_WITH_AES_128_CBC_SHA
        assert 0xC005 in ciphers  # TLS_ECDH_ECDSA_WITH_AES_256_CBC_SHA
        assert 0xC008 in ciphers  # TLS_ECDHE_ECDSA_WITH_3DES_EDE_CBC_SHA
        assert 0xC009 in ciphers  # TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA
        assert 0xC00A in ciphers  # TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA

    def test_tls_hello_includes_ecc_extensions(self) -> None:
        """ECDHE suites require supported-groups and point-format extensions."""
        wire = probe._build_client_hello("example.com", 0x0301)
        _ciphers, extensions = _client_hello_parts(wire)

        assert 0x000A in extensions  # supported_groups / elliptic_curves
        assert b"\x00\x17" in extensions[0x000A]  # secp256r1
        assert extensions[0x000B] == b"\x01\x00"  # ec_point_formats: uncompressed

    def test_sslv3_hello_omits_sni_extension(self) -> None:
        """SSLv3 strictly predates TLS extensions: strict servers reject
        ``ClientHello`` with extensions at v3 framing as malformed. We must
        send a bare hello at SSLv3."""
        sslv3 = probe._build_client_hello("example.com", 0x0300)
        tls10 = probe._build_client_hello("example.com", 0x0301)
        # Host is only encoded in the SNI extension; SSLv3 hello must not carry it.
        assert b"example.com" not in sslv3
        assert b"example.com" in tls10

    def test_sni_skipped_for_ipv4_literal(self) -> None:
        """RFC 6066: SNI is name-only. IP literals must not be encoded."""
        wire = probe._build_client_hello("203.0.113.5", 0x0301)
        _ciphers, extensions = _client_hello_parts(wire)
        assert b"203.0.113.5" not in wire
        assert 0x000A in extensions
        assert 0x000B in extensions


class TestAggregate:
    """Aggregate flips a contract key to passed=False as soon as one endpoint accepts."""

    def test_all_refused_aggregates_to_pass(self) -> None:
        def fake_tls(host: str, port: int, version: int, timeout: float = 5.0) -> dict[str, Any]:
            return {"host": host, "port": port, "requested_version": f"0x{version:04x}", "category": "refused"}

        def fake_http(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
            return {"host": host, "port": port, "category": "closed"}

        with (
            patch.object(probe, "probe_tls_version", side_effect=fake_tls),
            patch.object(probe, "probe_plain_http", side_effect=fake_http),
        ):
            tests = probe._aggregate([("a", 443), ("b", 443)], http_port=80, timeout=1.0)

        for name in probe.REQUIRED_TESTS:
            assert tests[name]["passed"] is True
            assert "confirmed across 2 endpoint(s)" in tests[name]["message"]

    def test_one_endpoint_accepts_flips_specific_key_to_fail(self) -> None:
        def fake_tls(host: str, port: int, version: int, timeout: float = 5.0) -> dict[str, Any]:
            cat = "accepted" if host == "weak" and version == 0x0301 else "refused"
            return {"host": host, "port": port, "requested_version": f"0x{version:04x}", "category": cat}

        def fake_http(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
            return {"host": host, "port": port, "category": "closed"}

        with (
            patch.object(probe, "probe_tls_version", side_effect=fake_tls),
            patch.object(probe, "probe_plain_http", side_effect=fake_http),
        ):
            tests = probe._aggregate([("ok", 443), ("weak", 443)], http_port=80, timeout=1.0)

        assert tests["tlsv1_0_disabled"]["passed"] is False
        assert "weak:443" in tests["tlsv1_0_disabled"]["error"]
        # Other contract keys remain passing.
        for name in ("sslv3_disabled", "tlsv1_1_disabled", "plain_http_disabled"):
            assert tests[name]["passed"] is True

    def test_endpoint_errors_fail_instead_of_confirming_compliance(self) -> None:
        def fake_tls(host: str, port: int, version: int, timeout: float = 5.0) -> dict[str, Any]:
            return {
                "host": host,
                "port": port,
                "requested_version": f"0x{version:04x}",
                "category": "error",
                "detail": "gaierror: Name or service not known",
            }

        def fake_http(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
            return {
                "host": host,
                "port": port,
                "category": "error",
                "detail": "gaierror: Name or service not known",
            }

        with (
            patch.object(probe, "probe_tls_version", side_effect=fake_tls),
            patch.object(probe, "probe_plain_http", side_effect=fake_http),
        ):
            tests = probe._aggregate([("typo.invalid", 443)], http_port=80, timeout=1.0)

        for name in probe.REQUIRED_TESTS:
            assert tests[name]["passed"] is False
            assert "typo.invalid" in tests[name]["error"]
            assert "gaierror" in tests[name]["error"]


class TestProbePlainHttp:
    """HTTP-on-80 banner classification."""

    def test_http_banner_classifies_as_accepted(self) -> None:
        with _wrap_with_create_connection(b"HTTP/1.1 200 OK\r\n\r\n"):
            result = probe.probe_plain_http("example.com", 80)
        assert result["category"] == "accepted"
        assert "200 OK" in result["detail"]

    def test_no_banner_classifies_as_closed(self) -> None:
        with _wrap_with_create_connection(b""):
            result = probe.probe_plain_http("example.com", 80)
        assert result["category"] == "closed"

    def test_refused_classifies_as_closed(self) -> None:
        with _wrap_with_create_connection(b"", raise_cls=ConnectionRefusedError):
            result = probe.probe_plain_http("example.com", 80)
        assert result["category"] == "closed"

    def test_resolution_error_is_probe_error(self) -> None:
        err = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        with _wrap_with_create_connection_error(err):
            result = probe.probe_plain_http("typo.invalid", 80)
        assert result["category"] == "error"
        assert "gaierror" in result["detail"]


class TestMain:
    """End-to-end CLI behavior - demo mode + structured skip."""

    def test_demo_mode_short_circuits_with_pass(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(probe, "DEMO_MODE", True)
        monkeypatch.setattr("sys.argv", ["insecure_protocols_test.py"])
        rc = probe.main()
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is True
        assert all(out["tests"][name]["passed"] for name in probe.REQUIRED_TESTS)

    def test_empty_endpoints_emits_structured_skip(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(probe, "DEMO_MODE", False)
        monkeypatch.setattr("sys.argv", ["insecure_protocols_test.py"])
        monkeypatch.delenv("EDGE_ENDPOINTS", raising=False)
        rc = probe.main()
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["skipped"] is True
        assert "EDGE_ENDPOINTS" in out["skip_reason"]

    def test_malformed_endpoint_emits_failure(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(probe, "DEMO_MODE", False)
        monkeypatch.setattr("sys.argv", ["insecure_protocols_test.py", "--endpoints", "noportprovided"])
        rc = probe.main()
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False
        assert "host:port" in out["error"]

    def test_endpoint_port_out_of_range_emits_failure(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(probe, "DEMO_MODE", False)
        monkeypatch.setattr("sys.argv", ["insecure_protocols_test.py", "--endpoints", "edge.example.com:70000"])
        rc = probe.main()
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False
        assert "port must be 1-65535" in out["error"]

    def test_invalid_timeout_emits_failure_before_probe(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(probe, "DEMO_MODE", False)
        monkeypatch.setattr(
            "sys.argv",
            ["insecure_protocols_test.py", "--endpoints", "edge.example.com:443", "--timeout", "0"],
        )

        def _fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, dict[str, Any]]:
            msg = "_aggregate must not run for invalid timeout input"
            raise AssertionError(msg)

        monkeypatch.setattr(probe, "_aggregate", _fail_if_called)
        rc = probe.main()
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False
        assert "timeout must be greater than 0" in out["error"]

    def test_invalid_http_port_emits_failure_before_probe(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(probe, "DEMO_MODE", False)
        monkeypatch.setattr(
            "sys.argv",
            ["insecure_protocols_test.py", "--endpoints", "edge.example.com:443", "--http-port", "-1"],
        )

        def _fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, dict[str, Any]]:
            msg = "_aggregate must not run for invalid HTTP port input"
            raise AssertionError(msg)

        monkeypatch.setattr(probe, "_aggregate", _fail_if_called)
        rc = probe.main()
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False
        assert "--http-port must be 1-65535" in out["error"]
