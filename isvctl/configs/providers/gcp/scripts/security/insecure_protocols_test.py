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

"""SEC13-02 insecure-protocols check — reuses the provider-neutral raw-socket prober.

Verifying that an edge endpoint refuses SSLv3 / TLS1.0 / TLS1.1 / plain HTTP is a
pure wire-protocol probe with no cloud API surface. Google Cloud therefore reuses
the shared prober at ``providers/shared/insecure_protocols_test.py`` (stdlib
``socket`` / raw ClientHello only) rather than reimplementing the probe against any
GCP service: this module loads that prober and runs its probe + aggregation logic
unchanged, re-emitting the result under the domain's contract. Edge minimum-TLS
posture is configured in Cloud Load Balancer SSL policies (operator-owned), which
the prober observes from the outside.

The operator supplies the HTTPS endpoints via ``--endpoints`` (sourced from
``EDGE_ENDPOINTS`` through the provider config); with no endpoints configured the
check emits a structured skip.

Usage:
    python3 insecure_protocols_test.py --endpoints host1:443,host2:8443
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from typing import Any

# providers/gcp/scripts/security/ -> providers/shared/insecure_protocols_test.py
_SHARED_PATH = Path(__file__).resolve().parents[3] / "shared" / "insecure_protocols_test.py"
_spec = importlib.util.spec_from_file_location("_shared_insecure_protocols", _SHARED_PATH)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"shared insecure-protocols prober not found at {_SHARED_PATH}")
_prober = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prober)

# Reused, verbatim, from the shared provider-neutral prober (no cloud dependency):
# the endpoint parser, per-version probe aggregation, the required test-key set,
# and the demo-mode contract.
_parse_endpoints = getattr(_prober, "_parse_endpoints")
_parse_port = getattr(_prober, "_parse_port")
_parse_timeout = getattr(_prober, "_parse_timeout")
_aggregate = getattr(_prober, "_aggregate")
_demo_result = getattr(_prober, "_demo_result")
_required_tests: list[str] = list(getattr(_prober, "REQUIRED_TESTS"))
_demo_mode: bool = bool(getattr(_prober, "DEMO_MODE", False))


def _probe_modern_tls_reachability(
    endpoints: list[tuple[str, int]],
    timeout: float,
) -> dict[str, Any]:
    """Require every target to complete a modern TLS handshake.

    A closed, black-holed, or mistyped endpoint rejects every legacy probe too,
    so legacy refusal alone is not evidence of secure protocol posture. This
    positive control verifies the same HTTPS socket is reachable with TLS 1.2+
    without using certificate trust as a second, unrelated policy check.
    """
    probes: list[dict[str, Any]] = []
    for host, port in endpoints:
        probe: dict[str, Any] = {"host": host, "port": port}
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            with socket.create_connection((host, port), timeout=timeout) as raw:
                raw.settimeout(timeout)
                with context.wrap_socket(raw, server_hostname=host) as tls_socket:
                    negotiated = tls_socket.version()
            if negotiated:
                probe.update(category="accepted", negotiated_version=negotiated)
            else:
                probe.update(category="error", detail="TLS handshake returned no negotiated version")
        except Exception as exc:
            probe.update(category="unreachable", detail=f"{type(exc).__name__}: {exc}")
        probes.append(probe)

    passed = all(probe.get("category") == "accepted" for probe in probes)
    result: dict[str, Any] = {"passed": passed, "probes": probes}
    if passed:
        result["message"] = f"modern TLS reachable on {len(endpoints)} endpoint(s)"
    else:
        failed = [
            f"{probe['host']}:{probe['port']} {probe.get('detail', probe.get('category'))}"
            for probe in probes
            if probe.get("category") != "accepted"
        ]
        result["error"] = f"modern TLS reachability control failed: {', '.join(failed)}"
    return result


def main() -> int:
    """Probe configured edge endpoints for insecure-protocol acceptance."""
    parser = argparse.ArgumentParser(description="Insecure-protocols probe (GCP edge endpoints)")
    parser.add_argument(
        "--endpoints",
        default=os.environ.get("EDGE_ENDPOINTS", ""),
        help="Comma-separated host:port list of HTTPS endpoints to probe",
    )
    parser.add_argument("--http-port", default=os.environ.get("EDGE_HTTP_PORT", "80"), help="Plain-HTTP probe port")
    parser.add_argument("--timeout", default="5.0", help="Per-probe socket timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "insecure_protocols",
        "endpoints_tested": 0,
        "tests": {
            "modern_tls_reachable": {"passed": False},
            "sslv3_disabled": {"passed": False},
            "tlsv1_0_disabled": {"passed": False},
            "tlsv1_1_disabled": {"passed": False},
            "plain_http_disabled": {"passed": False},
        },
    }

    if _demo_mode:
        result.update(_demo_result())
        print(json.dumps(result, indent=2))
        return 0

    try:
        endpoints = _parse_endpoints(args.endpoints)
        http_port = _parse_port(args.http_port, "--http-port")
        timeout = _parse_timeout(args.timeout)
    except ValueError as exc:
        result["error"] = str(exc)
        for name in result["tests"]:
            result["tests"][name] = {"passed": False, "error": str(exc)}
        print(json.dumps(result, indent=2))
        return 1

    if not endpoints:
        # No endpoints configured -> structured skip (the validator honors skipped:true).
        result["success"] = True
        result["skipped"] = True
        result["skip_reason"] = "No edge endpoints configured (set EDGE_ENDPOINTS or pass --endpoints host:port,...)"
        print(json.dumps(result, indent=2))
        return 0

    result["tests"] = _aggregate(endpoints, http_port, timeout)
    result["tests"]["modern_tls_reachable"] = _probe_modern_tls_reachability(endpoints, timeout)
    result["endpoints_tested"] = len(endpoints)
    result["success"] = result["tests"]["modern_tls_reachable"]["passed"] and all(
        result["tests"][name]["passed"] for name in _required_tests
    )
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
