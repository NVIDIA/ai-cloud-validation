#!/usr/bin/env python3
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

"""Insecure-protocols probe (shared across providers).

Sends a raw-socket ClientHello at each legacy version (SSLv3, TLSv1.0,
TLSv1.1) per endpoint and classifies the response via the ServerHello
version field. Separately probes plain HTTP on the configured HTTP port.

The prober deliberately avoids ``ssl.SSLContext`` so disabled-at-library
levels do not mask server-side acceptance. SSLv3 hello is sent without
extensions because strict servers reject SSLv3 + extensions as malformed;
TLSv1.0/1.1 hellos carry SNI so vhosted endpoints (ALB, CloudFront) hit
the right cert, plus ECC extensions so ECDSA-only endpoints can negotiate
legacy ECDSA suites when they are enabled.

Emits the contract::

  {
    "success": bool,
    "platform": "security",
    "test_name": "insecure_protocols",
    "endpoints_tested": int,
    "tests": {
      "sslv3_disabled":      {"passed": bool, "message": str, "probes": [...]},
      "tlsv1_0_disabled":    {"passed": bool, ...},
      "tlsv1_1_disabled":    {"passed": bool, ...},
      "plain_http_disabled": {"passed": bool, ...}
    }
  }

When no endpoints are configured, emits a structured ``skipped`` payload.
``ISVCTL_DEMO_MODE=1`` short-circuits with dummy-success output.

Usage:
    python insecure_protocols_test.py --endpoints host1:443,host2:8443
    python insecure_protocols_test.py --endpoints host:443 --http-port 80
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

# TLS record / handshake constants
_RECORD_HANDSHAKE = 0x16
_RECORD_ALERT = 0x15
_HANDSHAKE_SERVER_HELLO = 0x02

# Cipher suites that span SSLv3/TLSv1.0/1.1 and modern stacks. Without the
# historic suites a server with SSLv3 enabled may still reply
# ``handshake_failure`` because of zero overlap, masking the result.
_LEGACY_CIPHERS_HEX = (
    "000a"  # TLS_RSA_WITH_3DES_EDE_CBC_SHA
    "002f"  # TLS_RSA_WITH_AES_128_CBC_SHA
    "0035"  # TLS_RSA_WITH_AES_256_CBC_SHA
    "0005"  # TLS_RSA_WITH_RC4_128_SHA
    "0004"  # TLS_RSA_WITH_RC4_128_MD5
    "0009"  # TLS_RSA_WITH_DES_CBC_SHA
    "0064"  # TLS_RSA_EXPORT1024_WITH_RC4_56_SHA
    "0062"  # TLS_RSA_EXPORT1024_WITH_DES_CBC_SHA
    "0003"  # TLS_RSA_EXPORT_WITH_RC4_40_MD5
    "0006"  # TLS_RSA_EXPORT_WITH_RC2_CBC_40_MD5
    "0013"  # TLS_DHE_DSS_WITH_3DES_EDE_CBC_SHA
    "0016"  # TLS_DHE_RSA_WITH_3DES_EDE_CBC_SHA
    "0033"  # TLS_DHE_RSA_WITH_AES_128_CBC_SHA
    "0039"  # TLS_DHE_RSA_WITH_AES_256_CBC_SHA
    "c003"  # TLS_ECDH_ECDSA_WITH_3DES_EDE_CBC_SHA
    "c004"  # TLS_ECDH_ECDSA_WITH_AES_128_CBC_SHA
    "c005"  # TLS_ECDH_ECDSA_WITH_AES_256_CBC_SHA
    "c008"  # TLS_ECDHE_ECDSA_WITH_3DES_EDE_CBC_SHA
    "c009"  # TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA
    "c00a"  # TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA
    "c013"  # TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA
    "c014"  # TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA
)

# TLS_FALLBACK_SCSV is intentionally omitted. These probes test whether a
# server accepts each legacy protocol, not whether it rejects fallback handshakes.

VERSIONS = {
    "sslv3": 0x0300,  # https://www.rfc-editor.org/info/rfc6101/#appendix-A.1
    "tlsv1_0": 0x0301,  # https://www.rfc-editor.org/info/rfc2246/#appendix-A.1
    "tlsv1_1": 0x0302,  # https://www.rfc-editor.org/info/rfc4346/#appendix-A.1
}

REQUIRED_TESTS: list[str] = [
    "sslv3_disabled",
    "tlsv1_0_disabled",
    "tlsv1_1_disabled",
    "plain_http_disabled",
]


def _build_sni_extension(host: str) -> bytes:
    """Build a TLS server_name (SNI) extension for the given host.

    Skip for IP literals - SNI is name-only per RFC 6066.
    """
    try:
        socket.inet_aton(host)
        return b""
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return b""
    except OSError:
        pass

    host_bytes = host.encode("idna")
    server_name = b"\x00" + len(host_bytes).to_bytes(2, "big") + host_bytes
    server_name_list = len(server_name).to_bytes(2, "big") + server_name
    return b"\x00\x00" + len(server_name_list).to_bytes(2, "big") + server_name_list


def _extension(ext_type: int, data: bytes) -> bytes:
    """Build a TLS extension block."""
    return ext_type.to_bytes(2, "big") + len(data).to_bytes(2, "big") + data


def _build_ecc_extensions() -> bytes:
    """Build ECC extensions needed to negotiate ECDSA legacy cipher suites."""
    groups = b"".join(
        group.to_bytes(2, "big")
        for group in (
            0x0017,  # secp256r1
            0x0018,  # secp384r1
            0x0019,  # secp521r1
        )
    )
    supported_groups = _extension(0x000A, len(groups).to_bytes(2, "big") + groups)
    ec_point_formats = _extension(0x000B, b"\x01\x00")  # uncompressed
    return supported_groups + ec_point_formats


def _build_client_hello(host: str, version: int) -> bytes:
    """Build a raw ClientHello record for the requested protocol version."""
    ciphers = bytes.fromhex(_LEGACY_CIPHERS_HEX)
    random = os.urandom(32)
    body = (
        version.to_bytes(2, "big")
        + random
        + b"\x00"  # session_id length = 0
        + len(ciphers).to_bytes(2, "big")
        + ciphers
        + b"\x01\x00"  # compression: null
    )

    # SSLv3 strictly predates TLS extensions; some servers reject the
    # ClientHello as malformed when extensions are present at v3 framing.
    if version != 0x0300:
        extensions = _build_sni_extension(host) + _build_ecc_extensions()
        body += len(extensions).to_bytes(2, "big") + extensions

    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16" + version.to_bytes(2, "big") + len(handshake).to_bytes(2, "big") + handshake


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from the socket. Returns short read on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def probe_tls_version(host: str, port: int, version: int, timeout: float = 5.0) -> dict[str, Any]:
    """Send a ClientHello at ``version`` to host:port and classify the response.

    Returns a dict with::
        category: accepted | downgraded:<hex> | refused | closed | timeout | unexpected | error
        host, port, requested_version
        chosen_version (if any)
        detail (human-readable extra context)
    """
    requested_hex = f"0x{version:04x}"
    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "requested_version": requested_hex,
    }
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(_build_client_hello(host, version))
            head = _recv_exact(sock, 5)
            if len(head) < 5:
                result.update(category="closed", detail="server closed before record header")
                return result

            rtype = head[0]
            record_len = int.from_bytes(head[3:5], "big")
            body = _recv_exact(sock, record_len) if record_len else b""

            if rtype == _RECORD_ALERT:
                level = body[0] if len(body) >= 1 else None
                desc = body[1] if len(body) >= 2 else None
                result.update(
                    category="refused",
                    detail=f"alert level={level} desc={desc}",
                )
                return result

            if rtype == _RECORD_HANDSHAKE and len(body) >= 6 and body[0] == _HANDSHAKE_SERVER_HELLO:
                chosen = int.from_bytes(body[4:6], "big")
                result["chosen_version"] = f"0x{chosen:04x}"
                if chosen == version:
                    result["category"] = "accepted"
                else:
                    result["category"] = f"downgraded:0x{chosen:04x}"
                return result

            result.update(category="unexpected", detail=f"record type=0x{rtype:02x}")
            return result
    except (ConnectionResetError, ConnectionRefusedError, BrokenPipeError) as exc:
        result.update(category="closed", detail=type(exc).__name__)
        return result
    except TimeoutError:
        result.update(category="timeout", detail="no response within budget")
        return result
    except (OSError, UnicodeError) as exc:
        result.update(category="error", detail=f"{type(exc).__name__}: {exc}")
        return result


def probe_plain_http(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
    """Connect to host:port and look for an HTTP response banner.

    Disabled = refused / RST / timeout / no HTTP banner. Enabled = any
    response that starts with ``HTTP/``.
    """
    result: dict[str, Any] = {"host": host, "port": port}
    request = f"GET / HTTP/1.0\r\nHost: {host}\r\nUser-Agent: isv-sec13-02/1.0\r\n\r\n".encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            data = sock.recv(64)
    except (ConnectionResetError, ConnectionRefusedError, BrokenPipeError) as exc:
        result.update(category="closed", detail=type(exc).__name__)
        return result
    except TimeoutError:
        result.update(category="timeout", detail="no response within budget")
        return result
    except (OSError, UnicodeError) as exc:
        result.update(category="error", detail=f"{type(exc).__name__}: {exc}")
        return result

    if data.startswith(b"HTTP/"):
        status = data.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        result.update(category="accepted", detail=f"banner={status}")
        return result
    result.update(category="closed", detail="no HTTP response banner")
    return result


def _parse_endpoints(spec: str) -> list[tuple[str, int]]:
    """Parse comma-separated ``host:port`` list. Empty returns []."""
    endpoints: list[tuple[str, int]] = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        host, _, port = item.rpartition(":")
        if not host or not port:
            raise ValueError(f"endpoint {item!r} must be host:port")
        endpoints.append((host, _parse_port(port, "port")))
    return endpoints


def _parse_port(value: str, name: str) -> int:
    """Parse and range-check a TCP port."""
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be 1-65535")
    return port


def _parse_timeout(value: str) -> float:
    """Parse and validate a positive per-probe timeout."""
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ValueError("timeout must be a number") from exc
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    return timeout


def _aggregate(
    endpoints: list[tuple[str, int]],
    http_port: int,
    timeout: float,
) -> dict[str, dict[str, Any]]:
    """Probe each endpoint at each version and aggregate per-test booleans."""
    tests: dict[str, dict[str, Any]] = {name: {"passed": True, "probes": []} for name in REQUIRED_TESTS}

    for host, port in endpoints:
        for label, version in VERSIONS.items():
            probe = probe_tls_version(host, port, version, timeout=timeout)
            tests[f"{label}_disabled"]["probes"].append(probe)
            if not _probe_confirms_disabled(probe):
                tests[f"{label}_disabled"]["passed"] = False

        http_probe = probe_plain_http(host, http_port, timeout=timeout)
        tests["plain_http_disabled"]["probes"].append(http_probe)
        if not _probe_confirms_disabled(http_probe):
            tests["plain_http_disabled"]["passed"] = False

    for name in REQUIRED_TESTS:
        if tests[name]["passed"]:
            tests[name]["message"] = f"{name.replace('_', ' ')} confirmed across {len(endpoints)} endpoint(s)"
        else:
            offenders = [_format_probe_failure(p) for p in tests[name]["probes"] if not _probe_confirms_disabled(p)]
            tests[name]["error"] = f"accepted or inconclusive on {', '.join(offenders)}"
    return tests


def _probe_confirms_disabled(probe: dict[str, Any]) -> bool:
    """Return True only for categories that prove the insecure path was refused."""
    category = str(probe.get("category", ""))
    return category in {"refused", "closed", "timeout"} or category.startswith("downgraded:")


def _format_probe_failure(probe: dict[str, Any]) -> str:
    """Format accepted or inconclusive probe results for the contract error."""
    endpoint = f"{probe.get('host', '?')}:{probe.get('port', '?')}"
    category = probe.get("category", "missing-category")
    detail = probe.get("detail")
    if detail:
        return f"{endpoint} {category} ({detail})"
    return f"{endpoint} {category}"


def _demo_result() -> dict[str, Any]:
    """Return the demo-mode insecure protocol probe contract."""
    return {
        "success": True,
        "platform": "security",
        "test_name": "insecure_protocols",
        "endpoints_tested": 1,
        "tests": {
            "sslv3_disabled": {"passed": True, "message": "Demo: SSLv3 refused"},
            "tlsv1_0_disabled": {"passed": True, "message": "Demo: TLSv1.0 refused"},
            "tlsv1_1_disabled": {"passed": True, "message": "Demo: TLSv1.1 refused"},
            "plain_http_disabled": {"passed": True, "message": "Demo: plain HTTP refused"},
        },
    }


def main() -> int:
    """Probe configured endpoints for insecure protocol acceptance."""
    parser = argparse.ArgumentParser(description="Insecure-protocols probe")
    parser.add_argument(
        "--endpoints",
        default=os.environ.get("EDGE_ENDPOINTS", ""),
        help="Comma-separated host:port list of HTTPS endpoints to probe",
    )
    parser.add_argument(
        "--http-port",
        default=os.environ.get("EDGE_HTTP_PORT", "80"),
        help="Port to probe for plain HTTP (default 80)",
    )
    parser.add_argument("--timeout", default="5.0", help="Per-probe socket timeout in seconds")
    args = parser.parse_args()

    if DEMO_MODE:
        result = _demo_result()
        print(json.dumps(result, indent=2))
        return 0

    try:
        endpoints = _parse_endpoints(args.endpoints)
        http_port = _parse_port(args.http_port, "--http-port")
        timeout = _parse_timeout(args.timeout)
    except ValueError as exc:
        result = {
            "success": False,
            "platform": "security",
            "test_name": "insecure_protocols",
            "error": str(exc),
            "tests": {name: {"passed": False, "error": str(exc)} for name in REQUIRED_TESTS},
        }
        print(json.dumps(result, indent=2))
        return 1

    if not endpoints:
        result = {
            "success": True,
            "platform": "security",
            "test_name": "insecure_protocols",
            "skipped": True,
            "skip_reason": "No edge endpoints configured (set EDGE_ENDPOINTS or pass --endpoints host:port,...)",
        }
        print(json.dumps(result, indent=2))
        return 0

    tests = _aggregate(endpoints, http_port, timeout)
    success = all(tests[name]["passed"] for name in REQUIRED_TESTS)
    result = {
        "success": success,
        "platform": "security",
        "test_name": "insecure_protocols",
        "endpoints_tested": len(endpoints),
        "tests": tests,
    }
    print(json.dumps(result, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
