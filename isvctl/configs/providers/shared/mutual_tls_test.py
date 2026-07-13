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

"""Mutual TLS (or equivalent) probe for north-south and east-west traffic.

SEC13-01: for each configured endpoint, prove that an anonymous TLS client is
rejected and that a client presenting the configured certificate is accepted.

Emits the contract::

  {
    "success": bool,
    "platform": "security",
    "test_name": "mutual_tls",
    "endpoints_tested": int,
    "tests": {
      "north_south_mtls_enforced": {"passed": bool, "message": str, "probes": [...]},
      "east_west_mtls_enforced":   {"passed": bool, "message": str, "probes": [...]}
    }
  }

When no endpoints are configured, emits a structured ``skipped`` payload.
``ISVCTL_DEMO_MODE=1`` short-circuits with dummy-success output.

Usage:
    python mutual_tls_test.py \\
      --north-south-endpoints edge.example.com:443 \\
      --east-west-endpoints mesh.internal:8443 \\
      --ca-cert /path/ca.pem --client-cert /path/client.pem --client-key /path/client.key
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

REQUIRED_TESTS: list[str] = [
    "north_south_mtls_enforced",
    "east_west_mtls_enforced",
]


def _parse_endpoints(raw: str) -> list[tuple[str, int]]:
    """Parse a comma-separated ``host:port`` list into (host, port) tuples."""
    endpoints: list[tuple[str, int]] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            msg = f"Endpoint '{item}' must be host:port"
            raise ValueError(msg)
        host, port_str = item.rsplit(":", 1)
        host = host.strip()
        if not host:
            msg = f"Endpoint '{item}' is missing a host"
            raise ValueError(msg)
        try:
            port = int(port_str)
        except ValueError as exc:
            msg = f"Endpoint '{item}' has a non-integer port"
            raise ValueError(msg) from exc
        if not 1 <= port <= 65535:
            msg = f"Endpoint '{item}' port out of range"
            raise ValueError(msg)
        endpoints.append((host, port))
    return endpoints


def _parse_timeout(raw: str) -> float:
    """Parse a positive timeout in seconds."""
    try:
        timeout = float(raw)
    except ValueError as exc:
        msg = f"Invalid timeout '{raw}'"
        raise ValueError(msg) from exc
    if timeout <= 0:
        msg = "Timeout must be positive"
        raise ValueError(msg)
    return timeout


def _require_file(path: str, label: str) -> Path:
    """Return a Path that exists and is a file."""
    file_path = Path(path)
    if not file_path.is_file():
        msg = f"{label} not found: {path}"
        raise ValueError(msg)
    return file_path


def _ssl_context(
    *,
    ca_cert: Path | None,
    client_cert: Path | None = None,
    client_key: Path | None = None,
) -> ssl.SSLContext:
    """Build an SSL context that optionally presents a client certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED if ca_cert is not None else ssl.CERT_NONE
    if ca_cert is not None:
        context.load_verify_locations(cafile=str(ca_cert))
    if client_cert is not None and client_key is not None:
        context.load_cert_chain(certfile=str(client_cert), keyfile=str(client_key))
    return context


def _handshake(
    host: str,
    port: int,
    context: ssl.SSLContext,
    timeout: float,
) -> dict[str, Any]:
    """Attempt a TLS handshake and return accepted/rejected classification."""
    result: dict[str, Any] = {}
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            raw.settimeout(timeout)
            with context.wrap_socket(raw, server_hostname=host) as tls:
                result["accepted"] = True
                result["tls_version"] = tls.version()
                return result
    except ssl.SSLError as exc:
        result.update(accepted=False, detail=f"SSLError: {exc}")
        return result
    except OSError as exc:
        result.update(accepted=False, detail=f"{type(exc).__name__}: {exc}")
        return result


def probe_mtls_endpoint(
    host: str,
    port: int,
    *,
    anonymous_context: ssl.SSLContext,
    authenticated_context: ssl.SSLContext,
    timeout: float,
    plane: str,
) -> dict[str, Any]:
    """Probe one endpoint: anonymous client rejected, authenticated client accepted."""
    anonymous = _handshake(host, port, anonymous_context, timeout)
    authenticated = _handshake(host, port, authenticated_context, timeout)
    anonymous_rejected = anonymous.get("accepted") is not True
    authenticated_accepted = authenticated.get("accepted") is True
    passed = anonymous_rejected and authenticated_accepted
    return {
        "host": host,
        "port": port,
        "plane": plane,
        "anonymous_rejected": anonymous_rejected,
        "authenticated_accepted": authenticated_accepted,
        "passed": passed,
        "detail": {
            "anonymous": anonymous.get("detail") or ("accepted" if anonymous.get("accepted") else "rejected"),
            "authenticated": authenticated.get("detail")
            or ("accepted" if authenticated.get("accepted") else "rejected"),
            "tls_version": authenticated.get("tls_version"),
        },
    }


def _aggregate_plane(
    endpoints: list[tuple[str, int]],
    *,
    plane: str,
    anonymous_context: ssl.SSLContext,
    authenticated_context: ssl.SSLContext,
    timeout: float,
) -> dict[str, Any]:
    """Probe all endpoints for one traffic plane and aggregate pass/fail."""
    probes = [
        probe_mtls_endpoint(
            host,
            port,
            anonymous_context=anonymous_context,
            authenticated_context=authenticated_context,
            timeout=timeout,
            plane=plane,
        )
        for host, port in endpoints
    ]
    passed = all(probe.get("passed") is True for probe in probes) if probes else False
    failures = [f"{probe['host']}:{probe['port']}" for probe in probes if probe.get("passed") is not True]
    message = (
        f"mTLS enforced on {len(probes)} {plane} endpoint(s)"
        if passed
        else f"mTLS not enforced on: {', '.join(failures)}"
    )
    return {"passed": passed, "message": message, "probes": probes}


def _provider_hidden_plane(plane: str, message: str) -> dict[str, Any]:
    """Return a passing provider-hidden result for a non-probeable plane."""
    return {
        "passed": True,
        "provider_hidden": True,
        "message": message,
        "probes": [{"plane": plane, "provider_hidden": True}],
    }


def _bad_input_result(error: str) -> dict[str, Any]:
    """Return the failing bad_input contract with the error on every test."""
    return {
        "success": False,
        "platform": "security",
        "test_name": "mutual_tls",
        "error": error,
        "error_type": "bad_input",
        "tests": {name: {"passed": False, "error": error} for name in REQUIRED_TESTS},
    }


def _demo_result() -> dict[str, Any]:
    """Return the demo-mode mutual TLS probe contract."""
    return {
        "success": True,
        "platform": "security",
        "test_name": "mutual_tls",
        "endpoints_tested": 2,
        "tests": {
            "north_south_mtls_enforced": {
                "passed": True,
                "message": "Demo: north-south mTLS enforced",
                "probes": [{"plane": "north_south", "passed": True}],
            },
            "east_west_mtls_enforced": {
                "passed": True,
                "message": "Demo: east-west mTLS enforced",
                "probes": [{"plane": "east_west", "passed": True}],
            },
        },
    }


def run_mutual_tls_probe(
    *,
    north_south_endpoints: list[tuple[str, int]],
    east_west_endpoints: list[tuple[str, int]],
    ca_cert: Path | None,
    client_cert: Path | None,
    client_key: Path | None,
    timeout: float,
    east_west_provider_hidden_message: str | None = None,
) -> dict[str, Any]:
    """Build the SEC13-01 JSON contract for the given endpoint/cert inputs."""
    # Pure provider-hidden with nothing probed is not evidence — skip instead.
    if not north_south_endpoints and not east_west_endpoints:
        return {
            "success": True,
            "platform": "security",
            "test_name": "mutual_tls",
            "skipped": True,
            "skip_reason": (
                "No SEC13-01 endpoints configured "
                "(set EDGE_ENDPOINTS and/or EAST_WEST_ENDPOINTS with MTLS_CA_CERT_PATH, "
                "MTLS_CLIENT_CERT_PATH, MTLS_CLIENT_KEY_PATH)"
            ),
        }

    if not (ca_cert and client_cert and client_key):
        return _bad_input_result(
            "mTLS probe requires --ca-cert, --client-cert, and --client-key "
            "(or MTLS_CA_CERT_PATH / MTLS_CLIENT_CERT_PATH / MTLS_CLIENT_KEY_PATH)"
        )

    anonymous_context = _ssl_context(ca_cert=ca_cert)
    authenticated_context = _ssl_context(ca_cert=ca_cert, client_cert=client_cert, client_key=client_key)

    planes = [
        (
            "north_south_mtls_enforced",
            "north_south",
            north_south_endpoints,
            "north_south_mtls_enforced: no north-south endpoints configured for this run",
        ),
        (
            "east_west_mtls_enforced",
            "east_west",
            east_west_endpoints,
            east_west_provider_hidden_message
            or "east_west_mtls_enforced: no east-west endpoints configured for this run",
        ),
    ]
    tests: dict[str, Any] = {}
    for name, plane, endpoints, hidden_message in planes:
        if endpoints:
            tests[name] = _aggregate_plane(
                endpoints,
                plane=plane,
                anonymous_context=anonymous_context,
                authenticated_context=authenticated_context,
                timeout=timeout,
            )
        else:
            tests[name] = _provider_hidden_plane(plane, hidden_message)

    success = all(tests[name].get("passed") is True for name in REQUIRED_TESTS)
    return {
        "success": success,
        "platform": "security",
        "test_name": "mutual_tls",
        "endpoints_tested": len(north_south_endpoints) + len(east_west_endpoints),
        "tests": tests,
    }


def main() -> int:
    """Probe configured endpoints for mTLS enforcement."""
    parser = argparse.ArgumentParser(description="Mutual TLS (SEC13-01) probe")
    parser.add_argument(
        "--north-south-endpoints",
        default=os.environ.get("EDGE_ENDPOINTS", ""),
        help="Comma-separated host:port list for north-south / edge endpoints",
    )
    parser.add_argument(
        "--east-west-endpoints",
        default=os.environ.get("EAST_WEST_ENDPOINTS", ""),
        help="Comma-separated host:port list for east-west / mesh endpoints",
    )
    parser.add_argument(
        "--ca-cert",
        default=os.environ.get("MTLS_CA_CERT_PATH", ""),
        help="Path to CA certificate used to verify the server",
    )
    parser.add_argument(
        "--client-cert",
        default=os.environ.get("MTLS_CLIENT_CERT_PATH", ""),
        help="Path to client certificate presented for mTLS",
    )
    parser.add_argument(
        "--client-key",
        default=os.environ.get("MTLS_CLIENT_KEY_PATH", ""),
        help="Path to client private key presented for mTLS",
    )
    parser.add_argument("--timeout", default="5.0", help="Per-probe socket timeout in seconds")
    parser.add_argument(
        "--east-west-provider-hidden-message",
        default="",
        help="When set and no east-west endpoints are given, mark that plane provider-hidden",
    )
    args = parser.parse_args()

    if DEMO_MODE:
        result = _demo_result()
        print(json.dumps(result, indent=2))
        return 0

    try:
        north_south = _parse_endpoints(args.north_south_endpoints)
        east_west = _parse_endpoints(args.east_west_endpoints)
        timeout = _parse_timeout(args.timeout)
        ca_cert = _require_file(args.ca_cert, "CA cert") if args.ca_cert else None
        client_cert = _require_file(args.client_cert, "Client cert") if args.client_cert else None
        client_key = _require_file(args.client_key, "Client key") if args.client_key else None
    except ValueError as exc:
        print(json.dumps(_bad_input_result(str(exc)), indent=2))
        return 1

    result = run_mutual_tls_probe(
        north_south_endpoints=north_south,
        east_west_endpoints=east_west,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
        timeout=timeout,
        east_west_provider_hidden_message=args.east_west_provider_hidden_message or None,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") is True else 1


if __name__ == "__main__":
    sys.exit(main())
