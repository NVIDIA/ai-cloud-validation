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

"""AWS SEC13-01 mutual TLS probe wrapper.

Delegates to the shared ``mutual_tls_test`` probe. Operators supply
``EDGE_ENDPOINTS`` / ``EAST_WEST_ENDPOINTS`` plus ``MTLS_*_PATH`` cert
material to exercise concrete endpoints. When east-west endpoints are
omitted, AWS marks that plane provider-hidden (intra-VPC service mTLS is
customer-owned mesh/sidecar). When no north-south endpoints are configured
either, the shared probe emits a structured skip.

Usage:
    python mutual_tls_test.py --region us-west-2
    EDGE_ENDPOINTS=edge.example.com:443 MTLS_CA_CERT_PATH=... \\
      MTLS_CLIENT_CERT_PATH=... MTLS_CLIENT_KEY_PATH=... \\
      python mutual_tls_test.py --region us-west-2
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

EAST_WEST_HIDDEN_MESSAGE = (
    "east_west_mtls_enforced: AWS EC2/EKS tenants do not receive a "
    "customer-provable east-west mTLS surface in region {region}; "
    "intra-VPC service encryption is customer-owned (mesh/sidecar). "
    "Set EAST_WEST_ENDPOINTS to probe platform-specific east-west endpoints when available."
)


def _load_shared_probe() -> Any:
    """Load providers/shared/mutual_tls_test.py as a module."""
    shared_path = Path(__file__).resolve().parents[3] / "shared" / "mutual_tls_test.py"
    spec = importlib.util.spec_from_file_location("shared_mutual_tls_probe", shared_path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load shared probe at {shared_path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    """Run the shared mTLS probe with AWS-specific east-west defaults."""
    parser = argparse.ArgumentParser(description="AWS mutual TLS (SEC13-01) probe")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--north-south-endpoints",
        default=os.environ.get("EDGE_ENDPOINTS", ""),
        help="Comma-separated host:port north-south endpoints (default: EDGE_ENDPOINTS)",
    )
    parser.add_argument(
        "--east-west-endpoints",
        default=os.environ.get("EAST_WEST_ENDPOINTS", ""),
        help="Comma-separated host:port east-west endpoints (default: EAST_WEST_ENDPOINTS)",
    )
    parser.add_argument("--ca-cert", default=os.environ.get("MTLS_CA_CERT_PATH", ""))
    parser.add_argument("--client-cert", default=os.environ.get("MTLS_CLIENT_CERT_PATH", ""))
    parser.add_argument("--client-key", default=os.environ.get("MTLS_CLIENT_KEY_PATH", ""))
    parser.add_argument("--timeout", default="5.0")
    args = parser.parse_args()

    probe = _load_shared_probe()

    if DEMO_MODE:
        result = probe._demo_result()
        print(json.dumps(result, indent=2))
        return 0

    try:
        north_south = probe._parse_endpoints(args.north_south_endpoints)
        east_west = probe._parse_endpoints(args.east_west_endpoints)
        timeout = probe._parse_timeout(args.timeout)
        ca_cert = probe._require_file(args.ca_cert, "CA cert") if args.ca_cert else None
        client_cert = probe._require_file(args.client_cert, "Client cert") if args.client_cert else None
        client_key = probe._require_file(args.client_key, "Client key") if args.client_key else None
    except ValueError as exc:
        result = {
            "success": False,
            "platform": "security",
            "test_name": "mutual_tls",
            "error": str(exc),
            "error_type": "bad_input",
            "tests": {name: {"passed": False, "error": str(exc)} for name in probe.REQUIRED_TESTS},
        }
        print(json.dumps(result, indent=2))
        return 1

    # Prefer operator-supplied east-west endpoints; otherwise mark provider-hidden
    # so a north-south-only run can still satisfy the two-plane contract.
    east_west_hidden = None if east_west else EAST_WEST_HIDDEN_MESSAGE.format(region=args.region)

    result = probe.run_mutual_tls_probe(
        north_south_endpoints=north_south,
        east_west_endpoints=east_west,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
        timeout=timeout,
        east_west_provider_hidden_message=east_west_hidden,
    )
    print(json.dumps(result, indent=2))
    if result.get("skipped") is True:
        return 0
    return 0 if result.get("success") is True else 1


if __name__ == "__main__":
    sys.exit(main())
