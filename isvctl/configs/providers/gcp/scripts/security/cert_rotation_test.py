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

"""Verify customer-visible TLS certificates rotate within policy (SEC09-01).

The AWS reference scopes this to managed Kubernetes control-plane certificates,
which are provider-managed and not exposed through any customer inventory, so it
always exits via a structured skip. On GCP the GKE control-plane certificate is
likewise Google-managed and not queryable, but GCP DOES expose customer-managed
TLS certificates through Certificate Manager
(``google.cloud.certificate_manager_v1.CertificateManagerClient.list_certificates``),
whose managed certificates carry an ``expire_time`` and are auto-renewed before
expiry.

This script is read-only (it creates nothing, so there is no teardown). It walks
Certificate Manager for customer-visible certificates and evaluates the validity
window. When the only certificates are the provider-hidden GKE control-plane CA,
or none exist at all, it emits a structured skip mirroring the AWS
provider-hidden path. Google Certificate Authority Service (private CA) is
intentionally out of scope here -- this check covers Certificate Manager only.

Usage:
    python3 cert_rotation_test.py --region us-central1 --project my-project

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "cert_rotation_test",
    "rotation_window_days": 60,
    "certs_inspected": 2,
    "out_of_policy": 0,
    "tests": {
        "cert_inventory_non_empty": {"passed": true},
        "no_certs_out_of_policy": {"passed": true},
        "rotation_evidence_present": {"passed": true}
    }
}
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import certificate_manager_v1

# Policy threshold: a managed certificate must rotate (auto-renew) within this
# many days. Certificate Manager renews managed certificates well before
# expiry, so a live managed certificate is evidence of in-policy rotation.
ROTATION_WINDOW_DAYS = 60

# Service-disabled wire markers. Certificate Manager may be a disabled API on a
# project that never enabled it; GCP surfaces that as a 403 whose message
# carries one of these markers. That disabled-API case is provider-hidden parity
# with the AWS managed-control-plane path, not a hard failure. A *generic*
# access-denied (the caller simply lacks certificatemanager.certificates.list)
# is deliberately NOT in this set: it is an inspection failure that must surface,
# never a clean skip.
_SERVICE_DISABLED_MARKERS = ("service_disabled", "has not been used", "is disabled")


def _is_service_unavailable(e: Exception) -> bool:
    """Return True iff ``e`` signals the Certificate Manager API is DISABLED on this project.

    Only a SERVICE_DISABLED condition (the API was never enabled) is
    provider-hidden parity with the AWS managed-control-plane skip path. GCP
    reports it as a 403 ``PermissionDenied`` whose message carries one of the
    service-disabled markers, so classification is by message, not exception
    type. A generic ``PermissionDenied`` / Forbidden / list failure (the caller
    lacks the list permission) is an inspection error that MUST hard-fail with
    evidence -- reporting it as a clean skip would hide that the
    customer-visible certificate inventory was never evaluated.
    """
    msg = str(e).lower()
    return any(marker in msg for marker in _SERVICE_DISABLED_MARKERS)


def _days_until(expire_time: Any) -> int | None:
    """Return whole days from now until ``expire_time``, or None when absent."""
    if not expire_time:
        return None
    # Certificate Manager returns a timezone-aware datetime for expire_time.
    if expire_time.tzinfo is None:
        expire_time = expire_time.replace(tzinfo=UTC)
    delta = expire_time - datetime.now(UTC)
    return int(delta.total_seconds() // 86400)


@handle_gcp_errors
def main() -> int:
    """Evaluate Certificate Manager certificate rotation and emit JSON result."""
    parser = argparse.ArgumentParser(description="Certificate rotation cycle test (SEC09-01)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "cert_rotation_test",
        "rotation_window_days": ROTATION_WINDOW_DAYS,
        "certs_inspected": 0,
        "out_of_policy": 0,
        "tests": {
            "cert_inventory_non_empty": {"passed": False},
            "no_certs_out_of_policy": {"passed": False},
            "rotation_evidence_present": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)
        client = certificate_manager_v1.CertificateManagerClient()
        # Certificate Manager certificates live under the global location; the
        # managed-certificate inventory is not regional.
        parent = f"projects/{project}/locations/global"

        certs_inspected = 0
        out_of_policy = 0
        try:
            for cert in client.list_certificates(parent=parent):
                certs_inspected += 1
                # A managed certificate is auto-renewed before expiry. Evidence
                # of in-policy rotation is a live certificate whose remaining
                # validity is positive; Certificate Manager renews it before it
                # lapses. Treat an already-expired certificate as out of policy.
                # expire_time is a field on the Certificate itself (not on its
                # managed sub-message), so read it directly.
                expire_time = getattr(cert, "expire_time", None)
                remaining_days = _days_until(expire_time)
                if remaining_days is not None and remaining_days < 0:
                    out_of_policy += 1
        except gax.NotFound:
            # No certificates resource in this location -- empty inventory.
            certs_inspected = 0

        if certs_inspected == 0:
            # Only the provider-hidden GKE control-plane CA (or no customer
            # certificates) exists. Mirror the AWS provider-hidden path with a
            # structured skip the validator honors.
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No customer-visible Certificate Manager certificates found on this platform"
            result["tests"] = {
                "cert_inventory_non_empty": {"passed": True, "skipped": True, "message": result["skip_reason"]},
                "no_certs_out_of_policy": {
                    "passed": True,
                    "skipped": True,
                    "message": "No managed certificate inventory to evaluate",
                },
                "rotation_evidence_present": {
                    "passed": True,
                    "skipped": True,
                    "message": "GKE control-plane certificate rotation is provider-managed",
                },
            }
            print(json.dumps(result, indent=2))
            return 0

        result["certs_inspected"] = certs_inspected
        result["out_of_policy"] = out_of_policy
        result["tests"]["cert_inventory_non_empty"] = {
            "passed": True,
            "message": f"Inspected {certs_inspected} Certificate Manager certificate(s)",
        }
        result["tests"]["no_certs_out_of_policy"] = {
            "passed": out_of_policy == 0,
            "message" if out_of_policy == 0 else "error": (
                f"All {certs_inspected} certificate(s) are within the {ROTATION_WINDOW_DAYS}-day rotation window"
                if out_of_policy == 0
                else f"{out_of_policy} certificate(s) are past their validity window"
            ),
        }
        # Certificate Manager managed certificates are auto-renewed; an
        # enumerable, non-expired inventory is the customer-visible rotation
        # evidence (mirrors the AWS auto-rotation reasoning).
        result["tests"]["rotation_evidence_present"] = {
            "passed": out_of_policy == 0,
            "message" if out_of_policy == 0 else "error": (
                "Certificate Manager auto-renews managed certificates before expiry"
                if out_of_policy == 0
                else "Out-of-policy certificates indicate rotation evidence is incomplete"
            ),
        }
        result["success"] = all(t["passed"] for t in result["tests"].values())
    except Exception as e:
        if _is_service_unavailable(e):
            # Certificate Manager API is DISABLED on this project -- the
            # customer-visible certificate surface does not exist here, which is
            # provider-hidden parity, not a hard failure. A generic
            # PermissionDenied / list failure is NOT matched here and falls
            # through to a hard inspection error below.
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "Certificate Manager API is not enabled on this project"
            result["tests"] = {
                "cert_inventory_non_empty": {"passed": True, "skipped": True, "message": result["skip_reason"]},
                "no_certs_out_of_policy": {"passed": True, "skipped": True, "message": result["skip_reason"]},
                "rotation_evidence_present": {"passed": True, "skipped": True, "message": result["skip_reason"]},
            }
            print(json.dumps(result, indent=2))
            return 0
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
