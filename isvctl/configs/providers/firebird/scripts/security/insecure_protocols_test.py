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

"""Insecure protocols on the Firebird API edge (SEC13-02).

Probes the API host (from ``FIREBIRD_API_BASE``, or the ``--endpoints`` /
``EDGE_ENDPOINTS`` override) with the shared raw-socket prober
(``providers/shared/insecure_protocols_test.py``): one ClientHello per legacy
version (SSLv3, TLSv1.0, TLSv1.1) and a plain-HTTP request on port 80. This is
not an API call.

The prober counts a closed connection or a timeout as "refused", which an
unreachable host would also produce. So each endpoint first gets a TLSv1.2
ClientHello: it must answer (ServerHello or alert), or the step fails as
inconclusive instead of passing on silence (``endpoint_reachable``).

Usage:
    python insecure_protocols_test.py [--endpoints host:443] [--http-port 80]

Output JSON: the shared SEC13-02 contract plus ``tests.endpoint_reachable``.
"""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import DEFAULT_API_BASE, log

SHARED_PROBE = Path(__file__).resolve().parents[3] / "shared" / "insecure_protocols_test.py"
TLS_1_2 = 0x0303
# Any TLS answer shows the endpoint is live, so its refusals of legacy versions mean something.
LIVE_CATEGORIES = ("accepted", "refused")


def load_probe() -> ModuleType:
    """Load the shared insecure-protocols prober as a module."""
    spec = importlib.util.spec_from_file_location("shared_insecure_protocols_probe", SHARED_PROBE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the shared probe at {SHARED_PROBE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def api_endpoint(api_base: str) -> tuple[str, int]:
    """Return the (host, port) of an API base URL; HTTPS defaults to 443."""
    parts = urlsplit(api_base if "://" in api_base else f"https://{api_base}")
    if not parts.hostname:
        raise ValueError(f"cannot parse a host from API base {api_base!r}")
    return parts.hostname, parts.port or 443


def reachability(probe: ModuleType, endpoints: list[tuple[str, int]], timeout: float) -> dict[str, Any]:
    """Return a subtest that passes only when every endpoint answers a TLSv1.2 ClientHello."""
    probes = [probe.probe_tls_version(host, port, TLS_1_2, timeout=timeout) for host, port in endpoints]
    silent = [p for p in probes if not str(p.get("category", "")).startswith(LIVE_CATEGORIES + ("downgraded:",))]
    if silent:
        names = ", ".join(f"{p['host']}:{p['port']} {p.get('category')}" for p in silent)
        return {"passed": False, "probes": probes, "error": f"no TLS answer from {names}; results inconclusive"}
    return {"passed": True, "probes": probes, "message": f"{len(endpoints)} endpoint(s) answer TLSv1.2"}


def main() -> int:
    """Probe the API edge for legacy TLS and plain HTTP and emit the SEC13-02 contract.

    Returns:
        0 when every protocol is refused on a reachable endpoint, 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird insecure-protocols test")
    parser.add_argument("--endpoints", default=os.environ.get("EDGE_ENDPOINTS", ""), help="host:port list override")
    parser.add_argument("--http-port", type=int, default=80, help="Port probed for plain HTTP")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-probe socket timeout in seconds")
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "security", "test_name": "insecure_protocols"}
    try:
        probe = load_probe()
        endpoints = probe._parse_endpoints(args.endpoints) if args.endpoints.strip() else []
        if not endpoints:
            endpoints = [api_endpoint(os.environ.get("FIREBIRD_API_BASE", "").strip() or DEFAULT_API_BASE)]
        tests = probe._aggregate(endpoints, http_port=args.http_port, timeout=args.timeout)
        tests["endpoint_reachable"] = reachability(probe, endpoints, args.timeout)
        result.update(endpoints_tested=len(endpoints), tests=tests)
        result["success"] = all(t["passed"] for t in tests.values())
        if not tests["endpoint_reachable"]["passed"]:
            result["error"] = tests["endpoint_reachable"]["error"]
    except Exception as e:
        result["error"] = str(e)
        log(f"ERROR: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
